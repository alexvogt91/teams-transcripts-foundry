"""Azure adapters. Exceptions and diagnostics never include remote bodies or headers."""

from contextlib import contextmanager
import json
import logging
import re
import threading
from urllib.parse import urlparse, quote

import requests
from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceNotFoundError,
)
from azure.identity import ClientAssertionCredential, ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient, ContentSettings

from core import GraphFailure, RetryDelivery, canonical


def emit(name, **dimensions):
    logging.info(
        "teams_transcripts %s %s", name, json.dumps(dimensions, sort_keys=True)
    )


class BlobStore:
    def __init__(self, endpoint, credential, organizer_id=None):
        self.service = BlobServiceClient(
            endpoint, credential=credential, logging_enable=False
        )
        self.organizer_id = organizer_id
        self.state_blob = self.service.get_blob_client(
            "operations",
            f"organizers/{organizer_id}/subscription.json"
            if organizer_id
            else "subscription.json",
        )
        self.lease = None

    def read_state(self):
        try:
            state = json.loads(self.state_blob.download_blob().readall())
            return state or self._legacy_state()
        except ResourceNotFoundError:
            return self._legacy_state()

    def _legacy_state(self):
        if not self.organizer_id:
            return {}
        try:
            legacy = json.loads(
                self.service.get_blob_client("operations", "subscription.json")
                .download_blob()
                .readall()
            )
        except ResourceNotFoundError:
            return {}
        resource = f"users/{self.organizer_id}/onlineMeetings/getAllTranscripts".lower()
        accepted = [
            s
            for s in legacy.get("acceptedSubscriptions", [])
            if s.get("resource", "").lower() == resource
        ]
        if not accepted:
            return {}
        # Copy only this organizer's operational state, including the immutable handover marker.
        state = dict(legacy, acceptedSubscriptions=accepted)
        if state.get("subscriptionId") not in {s["id"] for s in accepted}:
            state.pop("subscriptionId", None)
            state.pop("configurationFingerprint", None)
        return state

    @contextmanager
    def locked_state(self):
        try:
            self.state_blob.upload_blob(b"{}", overwrite=False)
        except ResourceExistsError:
            pass
        try:
            lease = self.state_blob.acquire_lease(lease_duration=60)
        except HttpResponseError as exc:
            if exc.status_code == 409:
                yield None
                return
            raise RetryDelivery("state_lease_unavailable") from None
        stopped, lost = threading.Event(), threading.Event()

        def renew():
            while not stopped.wait(20):
                try:
                    lease.renew()
                except Exception:
                    lost.set()
                    return

        thread = threading.Thread(target=renew, daemon=True)
        thread.start()
        self.lease = lease
        self.lease_lost = lost
        try:
            yield self.read_state()
            if lost.is_set():
                raise RetryDelivery("state_lease_lost")
        finally:
            stopped.set()
            thread.join(timeout=15)
            self.lease = None
            try:
                lease.release()
            except HttpResponseError:
                pass

    def save_state(self, state):
        if self.lease is None or self.lease_lost.is_set():
            raise RetryDelivery("state_lease_lost")
        self.state_blob.upload_blob(
            canonical(state),
            overwrite=True,
            lease=self.lease,
            content_settings=ContentSettings(content_type="application/json"),
        )

    def write_once(self, container, name, data):
        try:
            self.service.get_blob_client(container, name).upload_blob(
                canonical(data),
                overwrite=False,
                content_settings=ContentSettings(content_type="application/json"),
            )
            return data
        except ResourceExistsError:
            # The first accepted delivery owns the immutable expiry, including after enqueue failure.
            return json.loads(
                self.service.get_blob_client(container, name).download_blob().readall()
            )
        except Exception:
            emit("archive_failure", organizerId=self.organizer_id)
            raise RetryDelivery("archive_storage_unavailable") from None

    def handover_archived_notifications(self, enqueue, now):
        from ingestion import handover

        with self.locked_state() as state:
            if state is None:
                raise RetryDelivery("subscription_lease_busy")
            if state.get("ingestionHandoverCompletedAt"):
                return
            count = 0
            container = self.service.get_container_client("events")
            for blob in container.list_blobs():
                archived = json.loads(container.download_blob(blob.name).readall())
                work = handover(archived)
                if work and (
                    not self.organizer_id
                    or work.get("organizerId") == self.organizer_id
                ):
                    enqueue(work, now)
                    count += 1
            state["ingestionHandoverCompletedAt"] = now.isoformat()
            self.save_state(state)
            emit(
                "ingestion_handover_complete",
                organizerId=self.organizer_id,
                notifications=count,
            )


class GraphClient:
    def __init__(self, config, managed_identity, session=None):
        self.config = config
        self.identity = managed_identity
        self.session = session or requests.Session()
        self.credential = self.new_credential()

    def close(self):
        self.credential.close()
        self.session.close()

    def new_credential(self):
        return ClientAssertionCredential(
            self.config.tenant_id,
            self.config.app_id,
            lambda: self.identity.get_token(
                "api://AzureADTokenExchange/.default"
            ).token,
            logging_enable=False,
        )

    def request(self, method, path, body=None, fresh=False, content_format=None):
        url = (
            "https://graph.microsoft.com/v1.0" + path if path.startswith("/") else path
        )
        parsed = urlparse(url)
        transcript_path = re.fullmatch(
            rf"/v1.0/users/{quote(self.config.organizer_id, safe='')}/onlineMeetings/[^/]+/transcripts/[^/]+(?:/content)?",
            parsed.path,
        )
        if (
            parsed.scheme != "https"
            or parsed.netloc != "graph.microsoft.com"
            or not (
                parsed.path == "/v1.0/subscriptions"
                or parsed.path.startswith("/v1.0/subscriptions/")
                or (method == "GET" and transcript_path and not parsed.query)
            )
        ):
            raise GraphFailure(500, "invalid_graph_url")
        try:
            if fresh:
                self.credential.close()
                self.credential = self.new_credential()
            token = self.credential.get_token(
                "https://graph.microsoft.com/.default"
            ).token
        except Exception as exc:
            codes = re.findall(r"AADSTS\d{5,}", str(exc))
            emit(
                "identity_exchange_failure",
                aadCode=codes[0] if codes else "unclassified",
            )
            raise GraphFailure(401, "identity_trust_failed") from None
        try:
            response = self.session.request(
                method,
                url,
                json=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": content_format or "application/json",
                },
                timeout=(5, 60 if content_format else 10),
                allow_redirects=False,
                stream=bool(content_format),
            )
        except requests.RequestException:
            raise GraphFailure(503, "graph_unavailable") from None
        if not 200 <= response.status_code < 300:
            code = "unknown"
            try:
                error = response.json().get("error", {})
                # Tenant policy code can be nested more than once.
                while isinstance(error, dict):
                    if error.get("code") in (
                        "GraphAccessToTranscriptsDisabled",
                        "SpeakerAttributionNotAllowed",
                        "Authorization_RequestDenied",
                        "InvalidAuthenticationToken",
                        "Forbidden",
                        "BadRequest",
                        "NotFound",
                        "ExtensionError",
                        "InvalidRequest",
                    ):
                        code = error["code"]
                        if code in (
                            "GraphAccessToTranscriptsDisabled",
                            "SpeakerAttributionNotAllowed",
                        ):
                            break
                    message = str(error.get("message", "")).lower()
                    if "application access policy" in message:
                        code = "TeamsApplicationAccessPolicy"
                        break
                    if any(
                        phrase in message
                        for phrase in (
                            "validate the notification",
                            "notification url validation",
                            "notificationurl validation",
                            "validation request",
                        )
                    ):
                        code = "NotificationValidationFailed"
                        break
                    if "authorization" in message and "event hub" in message:
                        code = "EventHubAuthorizationFailed"
                        break
                    error = error.get("innerError", error.get("innererror"))
            except (ValueError, AttributeError):
                pass
            emit(
                "graph_failure",
                httpStatus=response.status_code,
                code=code,
                operation="transcript_read"
                if transcript_path
                else "subscription_" + method.lower(),
            )
            response.close()
            raise GraphFailure(response.status_code, code)
        if content_format:
            from corpus import CorpusError

            with response:
                data = bytearray()
                for part in response.iter_content(65536):
                    data.extend(part)
                    if len(data) > 16 * 1024 * 1024:
                        raise CorpusError("transcript_download_too_large")
                raw = bytes(data)
                if (
                    content_format == "application/vnd.microsoft.graph.transcript+text"
                    and not raw.lstrip(b"\xef\xbb\xbf").startswith(b"WEBVTT")
                ):
                    raw = b"WEBVTT\n\n" + raw
                return raw
        return response.json() if response.content else {}

    def transcript_path(self, meeting, transcript):
        return f"/users/{quote(self.config.organizer_id, safe='')}/onlineMeetings/{quote(meeting, safe='')}/transcripts/{quote(transcript, safe='')}"

    def transcript_metadata(self, meeting, transcript):
        return self.request("GET", self.transcript_path(meeting, transcript))

    def transcript_content(self, meeting, transcript):
        path = self.transcript_path(meeting, transcript) + "/content"
        try:
            return self.request("GET", path, content_format="text/vtt")
        except GraphFailure as exc:
            if exc.code != "SpeakerAttributionNotAllowed":
                raise
            return self.request(
                "GET",
                path,
                content_format="application/vnd.microsoft.graph.transcript+text",
            )

    def list_subscriptions(self):
        path = "/subscriptions"
        for _ in range(20):
            result = self.request("GET", path)
            yield from result.get("value", [])
            path = result.get("@odata.nextLink")
            if not path:
                return
        raise GraphFailure(503, "subscription_page_limit")

    def create(self, body):
        return self.request("POST", "/subscriptions", body)

    def renew(self, subscription_id, expiration, fresh=False):
        return self.request(
            "PATCH",
            f"/subscriptions/{subscription_id}",
            {"expirationDateTime": expiration},
            fresh=fresh,
        )

    def delete(self, subscription_id):
        try:
            self.request("DELETE", f"/subscriptions/{subscription_id}")
        except GraphFailure as exc:
            if exc.status != 404:
                raise


def managed_identity(client_id):
    return ManagedIdentityCredential(client_id=client_id, logging_enable=False)


class IngestQueue:
    def __init__(self, endpoint, credential):
        from azure.storage.queue import QueueClient, TextBase64EncodePolicy

        self.client = QueueClient(
            endpoint,
            "transcript-ingest",
            credential=credential,
            message_encode_policy=TextBase64EncodePolicy(),
            logging_enable=False,
        )

    def enqueue(self, work, now):
        from corpus import remaining, CorpusError

        try:
            ttl = remaining(work["expiresAt"], now)
        except CorpusError as exc:
            if str(exc) == "transcript_expired":
                return
            raise
        try:
            self.client.send_message(json.dumps(work), time_to_live=ttl)
        except Exception:
            raise RetryDelivery("ingestion_queue_unavailable") from None
