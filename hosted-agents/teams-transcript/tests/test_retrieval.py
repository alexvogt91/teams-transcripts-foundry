import copy
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from azure.cosmos.exceptions import CosmosResourceNotFoundError

from agent.retrieval import TranscriptReader, TranscriptScope, Unavailable

MEETING, TRANSCRIPT, GENERATION = "a" * 64, "b" * 64, "c" * 64
LOCATOR = MEETING + "." + TRANSCRIPT
NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)


def reader():
    source = object.__new__(TranscriptReader)
    source._scope = TranscriptScope("tenant", "organizer_a")
    source.now = lambda: NOW
    source.container = Mock()
    source._embed = Mock(return_value=[1.0] * 505)
    expiry = (NOW + timedelta(days=1)).isoformat()
    manifest = {
        "id": "manifest:" + TRANSCRIPT,
        "meetingKey": MEETING,
        "transcriptKey": TRANSCRIPT,
        "activeGeneration": GENERATION,
        "profile": "test",
        "expiresAt": expiry,
        "pageCount": 2,
        "acceptedAt": NOW.isoformat(),
        "utteranceCount": 2,
    }
    utterances = [
        {
            "text": "Organizer A will review on Friday.",
            "speaker": "Organizer A",
            "index": 0,
            "start": "-00:00:01.000",
            "end": "00:00:04.000",
            "part": 0,
        }
    ]
    pages = {
        f"utterances:{TRANSCRIPT}:{GENERATION}:{page:06d}": {
            "generation": GENERATION,
            "expiresAt": expiry,
            "utterances": copy.deepcopy(utterances),
        }
        for page in range(2)
    }

    def read(item_id, partition_key):
        assert partition_key == ["tenant", "organizer_a", MEETING]
        if item_id == manifest["id"]:
            return copy.deepcopy(manifest)
        if item_id in pages:
            return pages[item_id]
        raise CosmosResourceNotFoundError(status_code=404)

    source.container.read_item.side_effect = read
    return source, manifest, pages


def test_pagination_pins_generation_and_original_citations():
    source, manifest, pages = reader()
    first = source.get_transcript(LOCATOR)
    assert first["coverage"] == "partial_page" and first["nextPage"] == 1
    last = source.get_transcript(
        LOCATOR, page=first["nextPage"], generation=first["generation"]
    )
    assert last["nextPage"] is None and last["pageCount"] == 2
    assert first["utterances"][0]["start"] == "-00:00:01.000"
    with pytest.raises(Unavailable, match="generation changed"):
        source.get_transcript(LOCATOR, generation="old")
    with pytest.raises(ValueError):
        source.get_transcript(LOCATOR, page=2)


def test_missing_page_and_expired_manifest_cannot_appear_complete():
    source, manifest, pages = reader()
    pages.clear()
    with pytest.raises(Unavailable, match="partial"):
        source.get_transcript(LOCATOR)
    manifest["expiresAt"] = NOW.isoformat()
    with pytest.raises(Unavailable, match="expired"):
        source.get_transcript(LOCATOR)


def test_hybrid_retrieval_filters_unpublished_generations_and_expired_results():
    source, manifest, pages = reader()
    row = {
        "id": "chunk",
        "meetingKey": MEETING,
        "transcriptKey": TRANSCRIPT,
        "generation": GENERATION,
        "profile": "test",
        "expiresAt": manifest["expiresAt"],
        "text": "Organizer A will review on Friday.",
        "locators": [{"index": 0, "start": "-00:00:01.000", "end": "00:00:04.000"}],
        "similarity": 0.60,
    }
    source.container.query_items.return_value = [
        dict(row, generation="unpublished"),
        dict(row, expiresAt=NOW.isoformat()),
        dict(row, similarity=0.01),
        dict(row, similarity=float("nan")),
        row,
    ]
    result = source.search_transcripts("review Friday")
    assert len(result["matches"]) == 1 and result["coverage"] == "excerpts_only"
    citation = result["matches"][0]["citations"][0]
    assert citation["transcript"] == LOCATOR and citation["start"] == "-00:00:01.000"
    sql = source.container.query_items.call_args.args[0]
    assert (
        "RANK RRF" in sql and "FullTextScore(c.text" in sql and "c.type='chunk'" in sql
    )
    parameters = {
        p["name"]: p["value"]
        for p in source.container.query_items.call_args.kwargs["parameters"]
    }
    assert (
        parameters["@tenant"] == "tenant" and parameters["@organizer"] == "organizer_a"
    )
    assert source.container.query_items.call_args.kwargs["partition_key"] == [
        "tenant",
        "organizer_a",
    ]


def test_empty_and_unavailable_are_distinct_and_parameterized():
    source, manifest, pages = reader()
    source.container.query_items.return_value = []
    result = source.search_transcripts("'; DROP DATABASE teams; --")
    assert result["matches"] == [] and "No matching" in result["message"]
    assert "DROP" not in source.container.query_items.call_args.args[0]
    source.container.query_items.side_effect = OSError("private server credentials")
    with pytest.raises(Unavailable) as exc:
        source.search_transcripts("review")
    assert "private" not in str(exc.value)


@pytest.mark.parametrize(
    "locator", ["../otherdb", "SELECT * FROM c", "a.b", MEETING + ".neighbor"]
)
def test_fixed_database_destination_and_locator_boundary(locator):
    source, *_ = reader()
    with pytest.raises(ValueError):
        source.get_transcript(locator)
    source.container.read_item.assert_not_called()
