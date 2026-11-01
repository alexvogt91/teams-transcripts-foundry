import copy
import json
from contextlib import nullcontext
from datetime import timedelta
from functools import partial
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import azure.functions as func
import pytest
from azure.core.pipeline.transport import HttpResponse, HttpTransport
from azure.cosmos import CosmosClient

import function_app
import ingestion
from corpus import (
    CorpusError,
    ENCODING,
    MAX_PAGE_BYTES,
    chunks,
    embedding_vector,
    pages,
    parse_vtt,
)
from ingestion import Embedder, Ingester, handover

VTT = b"WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n<v Organizer A>Deploy on Monday.</v>\n\n00:00:04.000 --> 00:00:07.000\n<v Pat>I will review it by Friday.</v>\n"


@pytest.mark.parametrize("identifier", [b"", b"cue-1\n"])
def test_graph_vtt_header_without_blank_line_preserves_first_cue(identifier):
    utterances = parse_vtt(
        b"WEBVTT\n"
        + identifier
        + b"-00:00:01.000 --> 00:00:02.000\n<v Organizer A>Review Friday.</v>\n"
    )
    assert len(utterances) == 1
    assert utterances[0]["text"] == "Review Friday."
    assert utterances[0]["start"] == "-00:00:01.000"


class Writer:
    def __init__(self):
        self.items, self.revision = {}, 0
        self.fail_kind = None
        self.contend = False

    def read(self, item_id, partition):
        return copy.deepcopy(self.items.get(item_id))

    def create(self, item):
        if item["type"] == self.fail_kind:
            raise CorpusError("transcript_storage_unavailable")
        if item["id"] in self.items:
            return None
        self.revision += 1
        item = copy.deepcopy(item) | {"_etag": str(self.revision)}
        self.items[item["id"]] = item
        return copy.deepcopy(item)

    def replace(self, item, etag):
        if self.contend or self.items[item["id"]]["_etag"] != etag:
            raise CorpusError("ingestion_lease_lost")
        self.revision += 1
        item = copy.deepcopy(item) | {"_etag": str(self.revision)}
        self.items[item["id"]] = item
        return copy.deepcopy(item)


def components(setup):
    now, config, *_ = setup
    work = handover(
        {
            "receivedAt": now.isoformat(),
            "notification": {
                "tenantId": config.tenant_id,
                "organizerId": config.organizer_id,
                "meetingId": "meeting",
                "transcriptId": "transcript",
                "changeType": "created",
            },
        }
    )
    clock = [now]
    writer = Writer()
    graph = Mock()
    graph.transcript_metadata.return_value = {"createdDateTime": now.isoformat()}
    graph.transcript_content.return_value = VTT
    embedder = Mock()
    embedder.embed.side_effect = lambda texts: [[1.0] * 505 for _ in texts]
    ingester = Ingester(
        writer,
        graph,
        embedder,
        config.tenant_id,
        config.organizer_id,
        now=lambda: clock[0],
    )
    return work, clock, writer, graph, embedder, ingester


@pytest.fixture
def queue_runtime(setup, monkeypatch):
    work, clock, writer, graph, embedder, _ = components(setup)
    # Exercise the pinned SDK's real lifecycle with an in-memory HTTP transport.
    transport = MagicMock(spec=HttpTransport)
    response = Mock(spec=HttpResponse, status_code=200, headers={})
    response.body.return_value = b"{}"
    response.text.return_value = "{}"
    transport.send.return_value = response
    writer.client = CosmosClient(
        "https://test.documents.azure.com",
        credential="c3ludGhldGlj",
        transport=transport,
        enable_endpoint_discovery=False,
    )
    reconciler = SimpleNamespace(config=setup[1], graph=graph)
    monkeypatch.setattr(
        function_app, "components", lambda oid=None: nullcontext((reconciler, None))
    )
    monkeypatch.setattr(
        function_app,
        "enrollment",
        lambda: (setup[1].tenant_id, (setup[1].organizer_id,)),
    )
    monkeypatch.setattr(ingestion, "CosmosWriter", Mock(return_value=writer))
    monkeypatch.setattr(ingestion, "Embedder", Mock(return_value=embedder))
    monkeypatch.setattr(ingestion, "Ingester", partial(Ingester, now=lambda: clock[0]))
    for name, value in {
        "TEAMS_INGESTION_ENABLED": "true",
        "TEAMS_COSMOS_ENDPOINT": "https://test.documents.azure.com",
        "TEAMS_DATABASE": "teams-transcripts",
        "TEAMS_CONTAINER": "transcripts",
        "TEAMS_EMBEDDING_ENDPOINT": "https://test.services.ai.azure.com",
        "TEAMS_EMBEDDING_KEY": "synthetic-key",
    }.items():
        monkeypatch.setenv(name, value)
    signals = Mock()
    monkeypatch.setattr(function_app, "emit", signals)
    message = func.QueueMessage(body=json.dumps(work).encode())
    trigger = function_app.transcript_ingest.build().get_user_function()
    yield SimpleNamespace(
        trigger=trigger,
        message=message,
        writer=writer,
        graph=graph,
        embedder=embedder,
        transport=transport,
        signals=signals,
    )
    writer.client.__exit__(None, None, None)


def test_queue_publication_and_replay_close_pinned_sdk_without_retry(queue_runtime):
    runtime = queue_runtime
    runtime.trigger(runtime.message)
    runtime.signals.assert_called_once_with(
        "ingestion_complete", organizerId="organizer-1", status="published"
    )
    original = copy.deepcopy(runtime.writer.items)
    manifest = next(i for i in original.values() if i["type"] == "manifest")
    assert manifest["status"] == "published" and manifest["activeGeneration"]
    runtime.transport.__exit__.assert_called_once()
    runtime.embedder.session.close.assert_called_once()

    runtime.signals.reset_mock()
    runtime.trigger(runtime.message)
    runtime.signals.assert_called_once_with(
        "ingestion_complete", organizerId="organizer-1", status="already_published"
    )
    assert runtime.writer.items == original
    runtime.graph.transcript_content.assert_called_once()
    assert runtime.transport.__exit__.call_count == 2
    assert runtime.embedder.session.close.call_count == 2


@pytest.mark.parametrize("stage", ["embedding_initialization", "storage"])
def test_queue_failure_preserves_classification_and_closes_clients(
    queue_runtime, stage
):
    runtime = queue_runtime
    if stage == "embedding_initialization":
        reason = "embedding_credential_unavailable"
        ingestion.Embedder.side_effect = CorpusError(reason)
    else:
        reason = "transcript_storage_unavailable"
        runtime.writer.fail_kind = "chunk"
    with pytest.raises(RuntimeError, match=reason):
        runtime.trigger(runtime.message)
    runtime.signals.assert_called_once_with(
        "ingestion_failure", organizerId="organizer-1", reason=reason
    )
    runtime.transport.__exit__.assert_called_once()
    if stage == "storage":
        runtime.embedder.session.close.assert_called_once()


def test_queue_cleanup_failure_does_not_emit_success(queue_runtime):
    runtime = queue_runtime
    runtime.transport.__exit__.side_effect = OSError("private transport detail")
    try:
        with pytest.raises(RuntimeError, match="ingestion_unavailable"):
            runtime.trigger(runtime.message)
    finally:
        runtime.transport.__exit__.side_effect = None
    runtime.signals.assert_called_once_with(
        "ingestion_failure", organizerId="organizer-1", reason="ingestion_unavailable"
    )
    runtime.embedder.session.close.assert_called_once()


def test_publish_then_duplicate_never_refreshes_expiry(setup):
    work, clock, writer, graph, embedder, ingester = components(setup)
    assert ingester.ingest(work) == "published"
    original = copy.deepcopy(writer.items)
    clock[0] += timedelta(days=1)
    assert ingester.ingest(work) == "already_published"
    assert writer.items == original
    assert graph.transcript_content.call_count == 1
    manifest = next(i for i in writer.items.values() if i["type"] == "manifest")
    assert all(
        i["generation"] == manifest["activeGeneration"]
        for i in writer.items.values()
        if i["type"] != "manifest"
    )


@pytest.mark.parametrize("fail_kind", ["utterances", "chunk"])
def test_interrupted_generation_is_invisible_until_retry_finishes(setup, fail_kind):
    work, clock, writer, graph, embedder, ingester = components(setup)
    writer.fail_kind = fail_kind
    with pytest.raises(CorpusError):
        ingester.ingest(work)
    manifest = next(i for i in writer.items.values() if i["type"] == "manifest")
    assert not manifest.get("activeGeneration")
    writer.fail_kind = None
    clock[0] += timedelta(minutes=3)
    assert ingester.ingest(work) == "published"
    assert all(i["expiresAt"] == work["expiresAt"] for i in writer.items.values())
    assert manifest["ttl"] > min(i["ttl"] for i in writer.items.values())


def test_active_lease_and_etag_contention_never_publish(setup):
    work, clock, writer, graph, embedder, ingester = components(setup)
    writer.contend = True
    with pytest.raises(CorpusError, match="lease_lost"):
        ingester.ingest(work)
    assert not next(iter(writer.items.values())).get("activeGeneration")
    writer.contend = False
    writer.items[next(iter(writer.items))]["leaseUntil"] = clock[0].timestamp() + 120
    with pytest.raises(CorpusError, match="lease_busy"):
        ingester.ingest(work)
    graph.transcript_content.assert_not_called()


def test_expired_work_cannot_download_or_refresh_retention(setup):
    work, clock, writer, graph, embedder, ingester = components(setup)
    clock[0] += timedelta(days=7)
    with pytest.raises(CorpusError, match="expired"):
        ingester.ingest(work)
    graph.transcript_content.assert_not_called()
    assert not writer.items


def test_expiry_during_embedding_never_publishes(setup):
    work, clock, writer, graph, embedder, ingester = components(setup)

    def expire(texts):
        clock[0] += timedelta(days=7)
        return [[1.0] * 505 for _ in texts]

    embedder.embed.side_effect = expire
    with pytest.raises(CorpusError, match="expired"):
        ingester.ingest(work)
    assert not next(i for i in writer.items.values() if i["type"] == "manifest").get(
        "activeGeneration"
    )


def test_notification_archive_and_enqueue_are_one_checkpoint_boundary(
    setup, notification
):
    now, config, store, graph, reconciler, processor, _ = setup
    queue = Mock(side_effect=RuntimeError("queue_unavailable"))
    processor.enqueue = queue
    with pytest.raises(RuntimeError):
        processor.process(json.dumps({"value": [notification]}).encode(), {}, now)
    assert len(store.blobs) == 1
    queue.side_effect = None
    processor.process(
        json.dumps({"value": [notification]}).encode(), {}, now + timedelta(hours=1)
    )
    first, replay = queue.call_args_list
    assert first.args[0]["expiresAt"] == replay.args[0]["expiresAt"]
    assert first.args[0]["acceptedAt"] == replay.args[0]["acceptedAt"]


def test_unicode_speakers_and_signed_offsets_are_preserved():
    u = parse_vtt(
        "WEBVTT\n\n-00:00:01.000 --> +00:00:02.000\n<v Álex>你好 😀 &amp; café</v><v 李>sí</v>\n".encode()
    )
    assert [x["speaker"] for x in u] == ["Álex", "李"]
    assert u[0]["text"] == "你好 😀 & café"
    assert u[0]["startMs"] == -1000 and u[0]["end"] == "+00:00:02.000"
    plain = parse_vtt(
        b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nNo owner is recorded.\n"
    )
    assert plain[0]["speaker"] is None


def test_long_unicode_cues_have_complete_chunk_coverage_and_bounded_pages():
    text = "你好 😀 café decision. " * 12000
    u = parse_vtt(
        (
            "WEBVTT\n\n00:00:01.000 --> 00:30:00.000\n<v Organizer A>" + text + "</v>\n"
        ).encode()
    )
    canonical_pages = list(pages(u))
    assert all(
        len(json.dumps(p, ensure_ascii=False).encode()) < MAX_PAGE_BYTES
        for p in canonical_pages
    )
    assert "".join(x["text"] for p in canonical_pages for x in p) == text.strip()
    coverage = set()
    previous = None
    for c in chunks(u):
        assert len(ENCODING.encode(c["text"])) <= 800
        for loc in c["locators"]:
            coverage.update(range(loc["charStart"], loc["charEnd"]))
        if previous:
            assert c["locators"][0]["charStart"] < previous["locators"][-1]["charEnd"]
        previous = c
    assert len(coverage) == len(text.strip())


@pytest.mark.parametrize(
    "vector",
    [
        [1.0] * 504,
        [0.0] * 505,
        [float("nan")] * 505,
        [float("inf")] * 505,
        [True] * 505,
        ["private"] * 505,
    ],
)
def test_bad_embeddings_are_rejected(vector):
    with pytest.raises(CorpusError):
        embedding_vector(vector)


def test_embedding_response_indexes_and_float32_contract():
    session = Mock()
    session.post.return_value.status_code = 200
    session.post.return_value.json.return_value = {
        "data": [{"index": 1, "embedding": [1.0] * 505}]
    }
    with pytest.raises(CorpusError, match="response"):
        Embedder("https://account.services.ai.azure.com", "secret", session).embed(
            ["text"]
        )
    session.post.return_value.json.return_value["data"][0]["index"] = 0
    assert (
        len(
            Embedder("https://account.services.ai.azure.com", "secret", session).embed(
                ["text"]
            )[0]
        )
        == 505
    )
