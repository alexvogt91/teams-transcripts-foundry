"""Queue-driven ingestion with immutable expiry and ETag-fenced publication."""

import uuid
from datetime import datetime, timedelta, timezone

import requests
from azure.core import MatchConditions
from azure.cosmos import CosmosClient
from azure.cosmos.exceptions import CosmosHttpResponseError

from corpus import (
    CorpusError,
    PROFILE,
    chunks,
    embedding_vector,
    hash_id,
    pages,
    parse_vtt,
    remaining,
    unix,
    utc,
)


def handover(archive):
    n = archive["notification"]
    if n.get("changeType") != "created" or "transcriptId" not in n:
        return None
    return {
        k: n[k] for k in ("tenantId", "organizerId", "meetingId", "transcriptId")
    } | {
        "schemaVersion": 1,
        "acceptedAt": archive["receivedAt"],
        "expiresAt": utc(unix(archive["receivedAt"]) + 7 * 86400),
    }


class Embedder:
    def __init__(self, endpoint, key, session=None):
        if not key or key.strip() != key:
            raise CorpusError("embedding_credential_unavailable")
        if not endpoint.startswith("https://"):
            raise CorpusError("embedding_endpoint_unavailable")
        self.url = (
            endpoint.rstrip("/")
            + "/openai/deployments/text-embedding-3-large/embeddings?api-version=2024-10-21"
        )
        self.key, self.session = key, session or requests.Session()

    def embed(self, texts):
        try:
            response = self.session.post(
                self.url,
                headers={"api-key": self.key},
                json={"input": texts, "dimensions": 505},
                timeout=(5, 60),
                allow_redirects=False,
            )
            if response.status_code != 200:
                raise CorpusError("embedding_unavailable")
            data = sorted(response.json()["data"], key=lambda x: x["index"])
            if [d["index"] for d in data] != list(range(len(texts))):
                raise CorpusError("invalid_embedding_response")
            return [embedding_vector(d["embedding"]) for d in data]
        except CorpusError:
            raise
        except Exception:
            raise CorpusError("embedding_unavailable") from None


class CosmosWriter:
    def __init__(self, endpoint, database, container, credential):
        self.client = CosmosClient(
            endpoint, credential=credential, logging_enable=False
        )
        self.container = self.client.get_database_client(database).get_container_client(
            container
        )

    def read(self, item_id, partition):
        try:
            return self.container.read_item(item_id, partition_key=partition)
        except CosmosHttpResponseError as exc:
            if exc.status_code == 404:
                return None
            raise CorpusError("transcript_storage_unavailable") from None

    def create(self, item):
        try:
            return self.container.create_item(item)
        except CosmosHttpResponseError as exc:
            if exc.status_code in (409, 412):
                return None
            raise CorpusError("transcript_storage_unavailable") from None

    def replace(self, item, etag):
        try:
            return self.container.replace_item(
                item["id"],
                item,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except CosmosHttpResponseError as exc:
            if exc.status_code in (404, 409, 412):
                raise CorpusError("ingestion_lease_lost") from None
            raise CorpusError("transcript_storage_unavailable") from None


class Ingester:
    def __init__(
        self,
        writer,
        graph,
        embedder,
        tenant,
        organizer,
        now=lambda: datetime.now(timezone.utc),
    ):
        self.writer, self.graph, self.embedder = writer, graph, embedder
        self.tenant, self.organizer, self.now = tenant, organizer, now

    def ingest(self, work):
        if (
            work.get("schemaVersion") != 1
            or work.get("tenantId") != self.tenant
            or work.get("organizerId") != self.organizer
        ):
            raise CorpusError("invalid_ingestion_scope")
        if any(
            not isinstance(work.get(k), str) or not work[k] or len(work[k]) > 2048
            for k in ("meetingId", "transcriptId", "acceptedAt", "expiresAt")
        ):
            raise CorpusError("invalid_ingestion_message")
        # The queue may be replayed, but it cannot move the notification clock.
        if unix(work["expiresAt"]) != unix(work["acceptedAt"]) + 7 * 86400:
            raise CorpusError("invalid_ingestion_expiry")
        remaining(work["expiresAt"], self.now())
        partition = [self.tenant, self.organizer, hash_id(work["meetingId"])]
        base = dict(
            tenantId=self.tenant,
            organizerId=self.organizer,
            meetingKey=partition[2],
            transcriptKey=hash_id(work["transcriptId"]),
            expiresAt=work["expiresAt"],
        )
        manifest_id = "manifest:" + base["transcriptKey"]
        manifest = self.writer.read(manifest_id, partition)
        if manifest:
            base["expiresAt"] = manifest["expiresAt"]
            remaining(base["expiresAt"], self.now())
            if manifest.get("activeGeneration") and manifest.get("profile") == PROFILE:
                return "already_published"
            if manifest.get("leaseUntil", 0) > self.now().timestamp():
                raise CorpusError("ingestion_lease_busy")
        else:
            manifest = self.writer.create(
                dict(
                    base,
                    id=manifest_id,
                    type="manifest",
                    acceptedAt=work["acceptedAt"],
                    meetingId=work["meetingId"],
                    transcriptId=work["transcriptId"],
                    status="pending",
                    ttl=remaining(base["expiresAt"], self.now()),
                )
            )
            if manifest is None:
                raise CorpusError("ingestion_lease_busy")
        owner = uuid.uuid4().hex

        def fence(**changes):
            nonlocal manifest
            manifest = self.writer.replace(
                dict(
                    manifest,
                    **changes,
                    leaseOwner=owner,
                    leaseUntil=int((self.now() + timedelta(minutes=2)).timestamp()),
                    ttl=remaining(base["expiresAt"], self.now()),
                ),
                manifest["_etag"],
            )

        fence(status="ingesting")
        metadata = self.graph.transcript_metadata(
            work["meetingId"], work["transcriptId"]
        )
        fence()
        raw = self.graph.transcript_content(work["meetingId"], work["transcriptId"])
        utterances = parse_vtt(raw)
        content_hash = hash_id(raw.decode("utf-8-sig"))
        generation = hash_id(content_hash + PROFILE)
        prefix = base["transcriptKey"] + ":" + generation
        utterance_pages = list(pages(utterances))
        retrieval_chunks = list(chunks(utterances))

        def write(kind, index, payload):
            fence()
            item = dict(
                base,
                id=f"{kind}:{prefix}:{index:06d}",
                type=kind,
                generation=generation,
                profile=PROFILE,
                index=index,
                ttl=remaining(base["expiresAt"], self.now()),
                **payload,
            )
            self.writer.create(item)

        for index, page in enumerate(utterance_pages):
            write("utterances", index, {"utterances": page})
        for start in range(0, len(retrieval_chunks), 16):
            fence()
            batch = retrieval_chunks[start : start + 16]
            vectors = self.embedder.embed([c["text"] for c in batch])
            for index, (chunk, vector) in enumerate(
                zip(batch, vectors, strict=True), start
            ):
                write("chunk", index, dict(chunk, embedding=vector))
        # Only an owner with the latest ETag can make a generation visible.
        fence(
            activeGeneration=generation,
            contentHash=content_hash,
            profile=PROFILE,
            pageCount=len(utterance_pages),
            chunkCount=len(retrieval_chunks),
            utteranceCount=len(utterances),
            status="published",
            createdAt=metadata.get("createdDateTime"),
            publishedAt=utc(int(self.now().timestamp())),
        )
        return "published"
