"""Metadata-only Graph subscription reconciliation and notification processing."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

UTC = timezone.utc


def timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone_required")
    return result.astimezone(UTC)


def canonical(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


class RetryDelivery(RuntimeError):
    """The broker must retain this event until operational state is available."""


class GraphFailure(RuntimeError):
    def __init__(self, status: int, code: str):
        # Only fixed classifications reach logs; never Graph response bodies.
        if code == "GraphAccessToTranscriptsDisabled":
            reason = "tenant_policy_disables_transcripts"
        elif status in (401, 403):
            reason = "check_admin_consent_OnlineMeetingTranscript_Read_All_and_identity_trust"
        elif status == 409:
            reason = "subscription_creation_conflict"
        else:
            reason = "graph_request_failed"
        super().__init__(reason)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class Config:
    tenant_id: str
    organizer_id: str
    app_id: str
    client_state: str
    notification_url: str

    def __post_init__(self):
        if (
            not self.client_state
            or len(self.client_state) > 128
            or self.client_state.startswith("@Microsoft.KeyVault(")
        ):
            raise ValueError("invalid_client_state_configuration")

    @property
    def resource(self) -> str:
        return f"users/{self.organizer_id}/onlineMeetings/getAllTranscripts"

    def request(self, now: datetime) -> dict:
        return {
            "changeType": "created",
            "resource": self.resource,
            "includeResourceData": False,
            "notificationUrl": self.notification_url,
            "lifecycleNotificationUrl": self.notification_url,
            "expirationDateTime": timestamp(now + timedelta(hours=48)),
            "clientState": self.client_state,
        }

    def owns(self, subscription: dict) -> bool:
        # GET /subscriptions is app-owned; applicationId additionally checks drift.
        return (
            subscription.get("applicationId", self.app_id) == self.app_id
            and subscription.get("resource", "").lower() == self.resource.lower()
        )

    @property
    def fingerprint(self) -> str:
        # Only a one-way fingerprint is persisted; notification secrets never enter state.
        return digest(
            dict(
                self.request(datetime(2000, 1, 1, tzinfo=UTC)),
                expirationDateTime=None,
                tenantId=self.tenant_id,
                applicationId=self.app_id,
            )
        )

    def matches(self, subscription: dict, state=None) -> bool:
        return (
            self.owns(subscription)
            and subscription.get("changeType") == "created"
            and subscription.get("notificationUrl") == self.notification_url
            and subscription.get("lifecycleNotificationUrl") == self.notification_url
            and subscription.get("includeResourceData", False) is False
            and (
                (
                    isinstance(subscription.get("clientState"), str)
                    and hmac.compare_digest(
                        subscription["clientState"].encode(), self.client_state.encode()
                    )
                )
                or (
                    subscription.get("clientState") in (None, "")
                    and state is not None
                    and state.get("subscriptionId") == subscription.get("id")
                    and state.get("configurationFingerprint") == self.fingerprint
                )
            )
        )


class Reconciler:
    def __init__(self, config, graph, store, emit=lambda *a, **k: None):
        self.config, self.graph, self.store, self.emit = config, graph, store, emit

    def reconcile(self, now: datetime, *, force=False, rollback=False):
        with self.store.locked_state() as state:
            if state is None:
                raise RetryDelivery("subscription_lease_busy")
            existing = list(self.graph.list_subscriptions())
            owned = [s for s in existing if self.config.owns(s)]
            known = {
                s["id"]: s
                for s in state.get("acceptedSubscriptions", [])
                if parse_time(s["acceptUntil"]) > now
            }

            def remember(s):
                known[s["id"]] = {
                    "id": s["id"],
                    "resource": self.config.resource,
                    "acceptUntil": timestamp(
                        parse_time(s["expirationDateTime"]) + timedelta(days=7)
                    ),
                }

            # Register identities before deleting drift/duplicates, to accept in-flight events.
            for s in owned:
                remember(s)
            if rollback:
                for s in owned:
                    self.graph.delete(s["id"])
                state.update(
                    subscriptionId=None,
                    expirationDateTime=None,
                    acceptedSubscriptions=list(known.values()),
                    heartbeat=timestamp(now),
                )
                self.store.save_state(state)
                self.emit("rollback_complete")
                return state

            state["acceptedSubscriptions"] = list(known.values())
            if owned:
                self.store.save_state(state)
            matches = [s for s in owned if self.config.matches(s, state)]
            current = next(
                (s for s in matches if s["id"] == state.get("subscriptionId")), None
            )
            current = current or (matches[0] if matches else None)
            for s in owned:
                if current is None or s["id"] != current["id"]:
                    self.graph.delete(s["id"])
            if current is not None and (
                force
                or parse_time(current["expirationDateTime"]) - now < timedelta(hours=24)
            ):
                try:
                    current = self.graph.renew(
                        current["id"],
                        self.config.request(now)["expirationDateTime"],
                        fresh=True,
                    )
                except GraphFailure as exc:
                    if exc.status != 404:
                        raise
                    current = None
            if current is None:
                try:
                    current = self.graph.create(self.config.request(now))
                except GraphFailure as exc:
                    if exc.status != 409:
                        raise
                    # Lost POST response or concurrent creation: discover and renew the exact app-owned subscription.
                    current = next(
                        (
                            s
                            for s in self.graph.list_subscriptions()
                            if self.config.matches(s)
                        ),
                        None,
                    )
                    if current is None:
                        raise RetryDelivery(
                            "subscription_conflict_not_yet_visible"
                        ) from None
                    current = self.graph.renew(
                        current["id"],
                        self.config.request(now)["expirationDateTime"],
                        fresh=True,
                    )
            remember(current)
            state.update(
                subscriptionId=current["id"],
                expirationDateTime=current["expirationDateTime"],
                acceptedSubscriptions=list(known.values()),
                heartbeat=timestamp(now),
                configurationFingerprint=self.config.fingerprint,
            )
            self.store.save_state(state)
            self.emit(
                "timer_heartbeat",
                expiryHours=(
                    parse_time(current["expirationDateTime"]) - now
                ).total_seconds()
                / 3600,
            )
            return state


# Graph emits both slash syntax and OData key syntax (including users/{id}/onlineMeetings('id')).
_KEY = r"(?:/([^/()'?]+)|\('([^']+)'\))"
_RESOURCE = re.compile(
    r"^users" + _KEY + r"/onlineMeetings" + _KEY + r"/transcripts" + _KEY + r"$", re.I
)


def transcript_resource(resource: str):
    match = _RESOURCE.fullmatch(unquote(resource))
    if match is None:
        return None
    groups = match.groups()
    return tuple(groups[i] or groups[i + 1] for i in (0, 2, 4))


class Processor:
    def __init__(
        self, config, store, reconciler, emit=lambda *a, **k: None, enqueue=None
    ):
        self.enqueue = enqueue
        self.config, self.store, self.reconciler, self.emit = (
            config,
            store,
            reconciler,
            emit,
        )

    def process(self, body: bytes, broker: dict, now: datetime):
        try:
            envelope = json.loads(body)
        except (ValueError, UnicodeError):
            self.reject(hashlib.sha256(body).hexdigest(), "invalid_json", broker, now)
            return
        if (
            not isinstance(envelope, dict)
            or not isinstance(envelope.get("value"), list)
            or not envelope["value"]
        ):
            self.reject(digest(envelope), "invalid_envelope", broker, now)
            return
        for notification in envelope["value"]:
            self.process_one(notification, broker, now)

    def reject(self, message_hash, reason, broker, now):
        # Invalid bytes, secrets, tokens and arbitrary untrusted fields are never retained.
        self.store.write_once(
            "quarantine",
            f"{message_hash}.json",
            {
                "schemaVersion": 1,
                "hash": message_hash,
                "reason": reason,
                "receivedAt": timestamp(now),
                "broker": broker,
            },
        )
        self.emit("notification_rejected", reason=reason)

    def process_one(self, n, broker, now):
        if not isinstance(n, dict):
            self.reject(digest(n), "invalid_notification", broker, now)
            return
        if (
            n.get("subscriptionId") == "NA"
            and n.get("clientState") == "NA"
            and n.get("resource") == "NA"
            and isinstance(n.get("changeType"), str)
            and n["changeType"].startswith(
                "Validation: Testing client application reachability"
            )
        ):
            self.emit("validation_ignored")
            return
        if (
            not isinstance(n.get("tenantId"), str)
            or n["tenantId"].lower() != self.config.tenant_id.lower()
        ):
            return self.reject(digest(n), "wrong_tenant", broker, now)
        secret = n.get("clientState")
        if not isinstance(secret, str) or not hmac.compare_digest(
            secret.encode(), self.config.client_state.encode()
        ):
            return self.reject(digest(n), "wrong_client_state", broker, now)
        state = self.store.read_state()
        if not state or not any(
            s["id"] == n.get("subscriptionId")
            for s in state.get("acceptedSubscriptions", [])
        ):
            # Recover the POST -> state-write race and state loss before rejecting the identity.
            state = self.reconciler.reconcile(now)
        accepted = next(
            (
                s
                for s in state.get("acceptedSubscriptions", [])
                if s["id"] == n.get("subscriptionId")
                and parse_time(s["acceptUntil"]) > now
                and s["resource"].lower() == self.config.resource.lower()
            ),
            None,
        )
        if not accepted:
            return self.reject(digest(n), "wrong_subscription", broker, now)
        lifecycle = n.get("lifecycleEvent")
        if lifecycle:
            if lifecycle not in (
                "reauthorizationRequired",
                "subscriptionRemoved",
                "missed",
            ):
                return self.reject(digest(n), "unsupported_lifecycle", broker, now)
            if "resource" in n and n["resource"] != self.config.resource:
                parsed = (
                    transcript_resource(n["resource"])
                    if isinstance(n["resource"], str)
                    else None
                )
                if (
                    parsed is None
                    or parsed[0].lower() != self.config.organizer_id.lower()
                ):
                    return self.reject(digest(n), "wrong_organizer", broker, now)
            clean = {
                "tenantId": self.config.tenant_id,
                "subscriptionId": n["subscriptionId"],
                "lifecycleEvent": lifecycle,
            }
            # Retry lifecycle actions even when the archive already exists from a prior attempt.
            self.archive(clean, broker, now)
            self.reconciler.reconcile(now, force=True)
            if lifecycle == "missed":
                self.emit("delivery_gap")
            return
        resource = n.get("resource")
        parsed = (
            transcript_resource(resource)
            if isinstance(resource, str) and len(resource) <= 4096
            else None
        )
        if parsed is None:
            return self.reject(digest(n), "invalid_resource", broker, now)
        if parsed[0].lower() != self.config.organizer_id.lower():
            return self.reject(digest(n), "wrong_organizer", broker, now)
        if n.get("changeType") != "created":
            return self.reject(digest(n), "wrong_change_type", broker, now)
        # Normalize both forms to one replay-safe semantic identity; no transcript is fetched.
        clean = {
            "tenantId": self.config.tenant_id,
            "subscriptionId": n["subscriptionId"],
            "changeType": "created",
            "organizerId": self.config.organizer_id,
            "meetingId": parsed[1],
            "transcriptId": parsed[2],
            "resource": f"users/{self.config.organizer_id}/onlineMeetings/{parsed[1]}/transcripts/{parsed[2]}",
        }
        archived = self.archive(clean, broker, now)
        if self.enqueue is not None:
            from ingestion import handover

            self.enqueue(handover(archived), now)

    def archive(self, notification, broker, now):
        archived = self.store.write_once(
            "events",
            f"{digest(notification)}.json",
            {
                "schemaVersion": 1,
                "receivedAt": timestamp(now),
                "notification": notification,
                "broker": broker,
            },
        )
        enqueued = broker.get("enqueuedAt")
        delay = max(0, (now - parse_time(enqueued)).total_seconds()) if enqueued else 0
        self.emit("event_archived", processingDelaySeconds=delay)
        return archived
