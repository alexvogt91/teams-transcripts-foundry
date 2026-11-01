"""Fixed scoped native reads. Callers cannot provide SQL or database destinations."""

import copy
import math
import re
import struct
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
from azure.cosmos import CosmosClient
from azure.cosmos.exceptions import CosmosHttpResponseError

MIN_SIMILARITY = 0.30  # The proven flat-cosine store returns similarity, not distance.


class Unavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class TranscriptScope:
    tenant: str
    organizer: str


class TranscriptReader:
    @property
    def tenant(self):
        return self._scope.tenant

    @property
    def organizer(self):
        return self._scope.organizer

    def scoped(self, scope):
        # Each invocation owns an immutable scope; the process-wide client is never rebound.
        bound = copy.copy(self)
        bound._scope = scope
        return bound

    def __init__(
        self,
        endpoint,
        database,
        container,
        tenant,
        organizer,
        credential,
        embedding_endpoint,
        embedding_key,
        now=lambda: datetime.now(timezone.utc),
    ):
        self.client = CosmosClient(
            endpoint, credential=credential, logging_enable=False
        )
        self.container = self.client.get_database_client(database).get_container_client(
            container
        )
        self._scope, self.now = TranscriptScope(tenant, organizer), now
        self.session = requests.Session()
        self.embedding_url = (
            embedding_endpoint.rstrip("/")
            + "/openai/deployments/text-embedding-3-large/embeddings?api-version=2024-10-21"
        )
        if not embedding_key or embedding_key.strip() != embedding_key:
            raise Unavailable(
                "Transcript search is unavailable: embedding configuration is missing."
            )
        self.embedding_key = embedding_key

    def _query(self, query, parameters):
        try:
            return list(
                self.container.query_items(
                    query,
                    parameters=parameters,
                    partition_key=[self.tenant, self.organizer],
                    enable_cross_partition_query=True,
                )
            )
        except Exception:
            raise Unavailable(
                "Transcript data is temporarily unavailable. Try again later."
            ) from None

    def _parameters(self):
        return [
            {"name": "@tenant", "value": self.tenant},
            {"name": "@organizer", "value": self.organizer},
            {"name": "@now", "value": self.now().isoformat().replace("+00:00", "Z")},
        ]

    def _live(self, item):
        try:
            return (
                datetime.fromisoformat(item["expiresAt"].replace("Z", "+00:00"))
                > self.now()
            )
        except (KeyError, ValueError, TypeError):
            return False

    def _read(self, item_id, meeting_key):
        try:
            item = self.container.read_item(
                item_id, partition_key=[self.tenant, self.organizer, meeting_key]
            )
            return item if self._live(item) else None
        except CosmosHttpResponseError as exc:
            if exc.status_code == 404:
                return None
            raise Unavailable(
                "Transcript data is temporarily unavailable. Try again later."
            ) from None
        except Exception:
            raise Unavailable(
                "Transcript data is temporarily unavailable. Try again later."
            ) from None

    @staticmethod
    def _locator(manifest):
        return manifest["meetingKey"] + "." + manifest["transcriptKey"]

    def list_transcripts(self, page=0):
        if isinstance(page, bool) or not isinstance(page, int) or not 0 <= page <= 200:
            raise ValueError("page must be between 0 and 200")
        rows = self._query(
            f"SELECT c.id, c.meetingKey, c.transcriptKey, c.createdAt, c.acceptedAt, c.expiresAt, c.activeGeneration, c.pageCount, c.utteranceCount FROM c WHERE c.tenantId=@tenant AND c.organizerId=@organizer AND c.type='manifest' AND c.status='published' AND c.expiresAt>@now ORDER BY c.acceptedAt DESC OFFSET {page * 20} LIMIT 21",
            self._parameters(),
        )
        return {
            "transcripts": [
                dict(
                    transcript=self._locator(m),
                    createdAt=m.get("createdAt"),
                    acceptedAt=m["acceptedAt"],
                    expiresAt=m["expiresAt"],
                    pages=m["pageCount"],
                    utterances=m["utteranceCount"],
                )
                for m in rows[:20]
                if self._live(m)
            ],
            "nextPage": page + 1 if len(rows) > 20 else None,
            "retentionDays": 7,
        }

    def _manifest(self, transcript):
        if not re.fullmatch(r"[a-f0-9]{64}\.[a-f0-9]{64}", transcript):
            raise ValueError(
                "Use the transcript identifier returned by list_transcripts or search_transcripts."
            )
        meeting, key = transcript.split(".")
        manifest = self._read("manifest:" + key, meeting)
        if not manifest or not manifest.get("activeGeneration"):
            raise Unavailable(
                "This transcript is unavailable, still ingesting, or has expired."
            )
        return manifest

    def get_transcript(self, transcript, page=0, generation=""):
        manifest = self._manifest(transcript)
        active = manifest["activeGeneration"]
        if generation and generation != active:
            raise Unavailable(
                "The transcript generation changed. Restart from page 0 for complete coverage."
            )
        if (
            isinstance(page, bool)
            or not isinstance(page, int)
            or not 0 <= page < manifest["pageCount"]
        ):
            raise ValueError("page is outside this transcript")
        prefix = manifest["transcriptKey"] + ":" + active
        item = self._read(f"utterances:{prefix}:{page:06d}", manifest["meetingKey"])
        if not item or item.get("generation") != active:
            raise Unavailable(
                "A transcript page is unavailable. Coverage is partial; do not present a complete summary."
            )
        # Recheck publication after the page read, covering expiry and a concurrent profile migration.
        latest = self._manifest(transcript)
        if latest["activeGeneration"] != active:
            raise Unavailable("The transcript generation changed. Restart from page 0.")
        return {
            "transcript": transcript,
            "generation": active,
            "page": page,
            "pageCount": manifest["pageCount"],
            "nextPage": page + 1 if page + 1 < manifest["pageCount"] else None,
            "coverage": "complete" if manifest["pageCount"] == 1 else "partial_page",
            "utterances": item["utterances"],
            "expiresAt": manifest["expiresAt"],
        }

    def _embed(self, text):
        try:
            result = self.session.post(
                self.embedding_url,
                json={"input": [text], "dimensions": 505},
                headers={"api-key": self.embedding_key},
                timeout=(5, 60),
                allow_redirects=False,
            )
            if result.status_code != 200:
                raise ValueError
            vector = result.json()["data"][0]["embedding"]
            if len(vector) != 505 or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) for v in vector
            ):
                raise ValueError
            vector = [struct.unpack("f", struct.pack("f", v))[0] for v in vector]
            if not all(math.isfinite(v) for v in vector) or not any(vector):
                raise ValueError
            return vector
        except Exception:
            raise Unavailable(
                "Transcript search is temporarily unavailable. Try again later."
            ) from None

    def search_transcripts(self, query):
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ValueError("Provide a search query of 1–2000 characters.")
        parameters = self._parameters() + [
            {"name": "@embedding", "value": self._embed(query)}
        ]
        terms = list(dict.fromkeys(re.findall(r"\w+", query.lower(), re.UNICODE)))[:10]
        parameters += [
            {"name": f"@kw{i}", "value": term} for i, term in enumerate(terms)
        ]
        vector = "VectorDistance(c.embedding, @embedding)"
        rank = (
            f"ORDER BY RANK RRF({vector}, FullTextScore(c.text, {', '.join(f'@kw{i}' for i in range(len(terms)))}))"
            if terms
            else f"ORDER BY {vector}"
        )
        rows = self._query(
            f"SELECT TOP 30 c.id, c.meetingKey, c.transcriptKey, c.generation, c.profile, c.text, c.locators, c.expiresAt, {vector} AS similarity FROM c WHERE c.tenantId=@tenant AND c.organizerId=@organizer AND c.type='chunk' AND c.expiresAt>@now {rank}",
            parameters,
        )
        matches, manifests = [], {}
        for row in rows:
            locator = self._locator(row)
            if locator not in manifests:
                try:
                    manifests[locator] = self._manifest(locator)
                except Unavailable as exc:
                    if "temporarily" in str(exc):
                        raise
                    manifests[locator] = None
            manifest = manifests[locator]
            if (
                not manifest
                or row["generation"] != manifest["activeGeneration"]
                or row["profile"] != manifest["profile"]
                or not self._live(row)
                or not isinstance(row.get("similarity"), (int, float))
                or not math.isfinite(row["similarity"])
                or row["similarity"] < MIN_SIMILARITY
            ):
                continue
            matches.append(
                dict(
                    transcript=locator,
                    generation=row["generation"],
                    text=row["text"],
                    citations=[
                        dict(transcript=locator, **loc) for loc in row["locators"]
                    ],
                )
            )
            if len(matches) == 8:
                break
        return {
            "matches": matches,
            "coverage": "excerpts_only",
            "message": None
            if matches
            else "No matching published transcripts were found in the seven-day corpus.",
        }
