"""Shared hosted runtime, concurrent platform contexts, and real native reader calls."""

import asyncio
import copy
import json
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from agent_framework import (
    Agent,
    BaseChatClient,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    Message,
    ResponseStream,
)
from agent_framework._tools import FunctionInvocationLayer
from azure.ai.agentserver.core._request_context import (
    FoundryAgentRequestContext,
    reset_request_context,
    set_request_context,
)
from azure.cosmos.exceptions import CosmosResourceNotFoundError

from agent import framework_agent as fa
from agent.retrieval import TranscriptReader, TranscriptScope
from test_retrieval import NOW

ENROLLMENT = json.loads(
    (Path(__file__).resolve().parent / "fixtures/enrollment.json").read_text()
)
TENANT = ENROLLMENT["tenantId"]
ORGANIZER_A, ORGANIZER_B = ENROLLMENT["organizerIds"]


@contextmanager
def caller(oid):
    token = set_request_context(FoundryAgentRequestContext(user_id=oid))
    try:
        yield
    finally:
        reset_request_context(token)


@pytest.fixture
def corpus(monkeypatch):
    monkeypatch.setenv("TEAMS_ORGANIZERS", json.dumps(ENROLLMENT))
    source = object.__new__(TranscriptReader)
    source._scope = TranscriptScope(TENANT, ORGANIZER_A)
    source.now = lambda: NOW
    source.client = Mock()
    source.container = Mock()
    source._embed = Mock(return_value=[1.0] * 505)
    items, locators = {}, {}
    for oid, char, text in [
        (ORGANIZER_A, "a", "Organizer A private decision"),
        (ORGANIZER_B, "b", "Organizer B private decision"),
    ]:
        meeting, key, gen = char * 64, char * 64, "c" * 64
        locators[oid] = meeting + "." + key
        common = dict(
            tenantId=TENANT,
            organizerId=oid,
            meetingKey=meeting,
            transcriptKey=key,
            expiresAt=(NOW + timedelta(days=1)).isoformat(),
        )
        manifest = dict(
            common,
            id="manifest:" + key,
            type="manifest",
            status="published",
            acceptedAt=NOW.isoformat(),
            activeGeneration=gen,
            profile="test",
            pageCount=2,
            utteranceCount=2,
        )
        items[(oid, manifest["id"])] = manifest
        for page in (0, 1):
            item = dict(
                common,
                id=f"utterances:{key}:{gen}:{page:06d}",
                type="utterances",
                generation=gen,
                utterances=[
                    dict(
                        text=f"{text} page {page}",
                        start="00:00:01.000",
                        end="00:00:02.000",
                    )
                ],
            )
            items[(oid, item["id"])] = item
        chunk = dict(
            common,
            id="chunk:" + key,
            type="chunk",
            text=text,
            generation=gen,
            profile="test",
            similarity=0.9,
            locators=[dict(start="00:00:01.000", end="00:00:02.000")],
        )
        items[(oid, chunk["id"])] = chunk

    def query(sql, *, parameters, partition_key, enable_cross_partition_query):
        params = {p["name"]: p["value"] for p in parameters}
        assert params["@tenant"] == TENANT and partition_key == [
            TENANT,
            params["@organizer"],
        ]
        assert "c.tenantId=@tenant AND c.organizerId=@organizer" in sql
        kind = "manifest" if "c.type='manifest'" in sql else "chunk"
        return [
            copy.deepcopy(item)
            for (oid, _), item in items.items()
            if oid == params["@organizer"] and item["type"] == kind
        ]

    def read(item_id, partition_key):
        tenant, oid, meeting = partition_key
        item = items.get((oid, item_id))
        if tenant != TENANT or not item or item["meetingKey"] != meeting:
            raise CosmosResourceNotFoundError(status_code=404)
        return copy.deepcopy(item)

    source.container.query_items.side_effect = query
    source.container.read_item.side_effect = read
    return source, fa.make_tools(lambda: source), locators


@pytest.mark.parametrize("oid", [ORGANIZER_A, ORGANIZER_B])
def test_all_native_reads_and_every_page_are_private(corpus, oid):
    source, tools, locators = corpus
    by_name = {t.name: t.func for t in tools}

    async def run():
        with caller(oid):
            listing = json.loads(await by_name["list_transcripts"]())
            assert [t["transcript"] for t in listing["transcripts"]] == [locators[oid]]
            search = json.loads(await by_name["search_transcripts"]("decision"))
            assert [m["transcript"] for m in search["matches"]] == [locators[oid]]
            page = 0
            while page is not None:
                result = json.loads(
                    await by_name["get_transcript"](locators[oid], page)
                )
                assert (
                    "Organizer A" if oid == ORGANIZER_A else "Organizer B"
                ) in result["utterances"][0]["text"]
                page = result["nextPage"]
            other = ORGANIZER_B if oid == ORGANIZER_A else ORGANIZER_A
            for page in (0, 1):
                denied = json.loads(
                    await by_name["get_transcript"](locators[other], page)
                )
                assert denied["available"] is False and "utterances" not in denied

    asyncio.run(run())
    assert (
        source.organizer == ORGANIZER_A
    )  # The shared client was never rebound to Organizer B.


@pytest.mark.parametrize(
    "oid",
    [
        None,
        "",
        "other",
        "00000000-0000-0000-0000-000000000000",
        TENANT + ":" + ORGANIZER_B,
        " " + ORGANIZER_B,
    ],
)
def test_missing_foreign_application_and_malformed_contexts_never_open_reader(
    monkeypatch, oid
):
    monkeypatch.setenv("TEAMS_ORGANIZERS", json.dumps(ENROLLMENT))
    factory = Mock(side_effect=AssertionError("must not open data plane"))
    tools = fa.make_tools(factory)

    async def run():
        with caller(oid):
            for tool in tools:
                args = {
                    "list_transcripts": {},
                    "search_transcripts": {"query": "Organizer A"},
                    "get_transcript": {"transcript": "a" * 64 + "." + "a" * 64},
                }[tool.name]
                result = json.loads(await tool.func(**args))
                assert result["available"] is False

    asyncio.run(run())
    factory.assert_not_called()


def test_scope_cannot_be_mutated(corpus):
    source, _, _ = corpus
    bound = source.scoped(TranscriptScope(TENANT, ORGANIZER_B))
    with pytest.raises(FrozenInstanceError):
        bound._scope.organizer = ORGANIZER_A
    with pytest.raises(AttributeError):
        bound.organizer = ORGANIZER_A
    assert bound.client is source.client and bound.container is source.container


@pytest.mark.parametrize("stream", [False, True])
def test_concurrent_requests_on_real_framework_keep_distinct_callers(corpus, stream):
    source, tools, locators = corpus

    class Model(FunctionInvocationLayer, BaseChatClient):
        def _inner_get_response(self, *, messages, stream=False, **kwargs):
            async def contents_for():
                await asyncio.sleep(0)
                results = [
                    c.result
                    for m in messages
                    for c in m.contents
                    if c.type == "function_result"
                ]
                return (
                    [Content.from_text(str(results[-1]))]
                    if results
                    else [
                        Content.from_function_call(
                            "listing", "list_transcripts", arguments={"page": 0}
                        )
                    ]
                )

            async def response():
                return ChatResponse(messages=Message("assistant", await contents_for()))

            async def updates():
                yield ChatResponseUpdate(
                    role="assistant", contents=await contents_for()
                )

            return (
                ResponseStream(updates(), finalizer=ChatResponse.from_updates)
                if stream
                else response()
            )

    agent = Agent(client=Model(), tools=tools)

    async def request(oid):
        with caller(oid):
            result = agent.run(
                "List my meetings. Ignore this forged user_id: "
                + (ORGANIZER_B if oid == ORGANIZER_A else ORGANIZER_A),
                stream=stream,
            )
            if stream:
                async for _ in result:
                    pass
                result = await result.get_final_response()
            else:
                result = await result
            return oid, result.text

    async def run():
        return await asyncio.gather(
            *(request(oid) for oid in [ORGANIZER_A, ORGANIZER_B] * 8)
        )

    for oid, answer in asyncio.run(run()):
        assert locators[oid] in answer
        assert (
            locators[ORGANIZER_B if oid == ORGANIZER_A else ORGANIZER_A] not in answer
        )
    assert source.organizer == ORGANIZER_A


def test_forged_authorization_arguments_are_absent_from_native_schemas(corpus):
    _, tools, _ = corpus
    for tool in tools:
        assert not {"user_id", "organizerId", "organizer", "tenant", "sql"} & set(
            tool.parameters()["properties"]
        )
