import copy
import json
from datetime import timedelta

import pytest

from core import GraphFailure, RetryDelivery, canonical, timestamp


def test_request_is_metadata_only(setup):
    now, config, *_ = setup
    assert config.request(now) == {
        "changeType": "created",
        "resource": "users/organizer-1/onlineMeetings/getAllTranscripts",
        "includeResourceData": False,
        "notificationUrl": config.notification_url,
        "lifecycleNotificationUrl": config.notification_url,
        "expirationDateTime": "2026-10-25T09:00:00Z",
        "clientState": config.client_state,
    }


@pytest.mark.parametrize(
    "hours,renew", [(48, False), (24, False), (23.9, True), (-1, True)]
)
def test_renewal_threshold(setup, hours, renew):
    now, config, store, graph, reconciler, *_ = setup
    graph.subscriptions = [graph.subscription(hours=hours)]
    reconciler.reconcile(now)
    assert any(c[0] == "renew" for c in graph.calls) is renew
    if renew:
        assert graph.calls[-1][3] is True


def test_restart_after_create_response_lost(setup):
    now, config, store, graph, reconciler, *_ = setup
    store.fail_state = True
    with pytest.raises(OSError):
        reconciler.reconcile(now)
    assert len(graph.subscriptions) == 1
    store.fail_state = False
    reconciler.reconcile(now)
    assert sum(c[0] == "create" for c in graph.calls) == 1
    assert store.state["subscriptionId"] == "sub-1"
    assert config.client_state not in json.dumps(store.state)


def test_conflict_recovers_existing_subscription(setup):
    now, config, store, graph, reconciler, *_ = setup
    graph.conflict = True
    reconciler.reconcile(now)
    assert store.state["subscriptionId"] == "sub-1"
    assert graph.calls[-1][0] == "renew"
    assert graph.calls[-1][3] is True


def test_renewal_404_creates_replacement(setup):
    now, config, store, graph, reconciler, *_ = setup
    graph.subscriptions = [graph.subscription(hours=1)]
    graph.missing_renewal = True
    reconciler.reconcile(now)
    assert any(c[0] == "create" for c in graph.calls)


def test_lease_contention_does_not_mutate_graph(setup):
    now, config, store, graph, reconciler, *_ = setup
    store.busy = True
    with pytest.raises(RetryDelivery):
        reconciler.reconcile(now)
    assert not graph.calls


def test_duplicate_drift_and_unrelated_subscriptions(setup):
    now, config, store, graph, reconciler, *_ = setup
    good, duplicate, drift, unrelated = [
        graph.subscription(i) for i in ("good", "dup", "drift", "other")
    ]
    drift["clientState"] = "old-secret"
    unrelated["resource"] = "users/another/onlineMeetings/getAllTranscripts"
    graph.subscriptions = [good, duplicate, drift, unrelated]
    reconciler.reconcile(now)
    assert {s["id"] for s in graph.subscriptions} == {"good", "other"}
    assert {s["id"] for s in store.state["acceptedSubscriptions"]} == {
        "good",
        "dup",
        "drift",
    }


def test_rollback_deletes_only_owned_subscription(setup):
    now, config, store, graph, reconciler, *_ = setup
    unrelated = graph.subscription("other")
    unrelated["applicationId"] = "other-app"
    graph.subscriptions = [graph.subscription(), unrelated]
    reconciler.reconcile(now, rollback=True)
    assert graph.subscriptions == [unrelated]
    assert store.state["subscriptionId"] is None
    assert not any(c[0] == "create" for c in graph.calls)


@pytest.mark.parametrize(
    "path",
    [
        "users/organizer-1/onlineMeetings/meeting-1/transcripts/transcript-1",
        "users/organizer-1/onlineMeetings('meeting-1')/transcripts('transcript-1')",
        "users('organizer-1')/onlineMeetings('meeting-1')/transcripts('transcript-1')",
        "users/organizer-1/onlineMeetings(%27meeting-1%27)/transcripts(%27transcript-1%27)",
    ],
)
def test_graph_resource_forms(setup, notification, path):
    now, config, store, graph, reconciler, processor, signals = setup
    notification["resource"] = path
    processor.process(canonical({"value": [notification]}), {"sequenceNumber": 42}, now)
    key, data = next(iter(store.blobs.items()))
    assert key[0] == "events"
    assert data["notification"]["meetingId"] == "meeting-1"
    assert data["broker"]["sequenceNumber"] == 42
    assert config.client_state not in json.dumps(data)


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("tenantId", "wrong", "wrong_tenant"),
        ("tenantId", None, "wrong_tenant"),
        ("tenantId", {}, "wrong_tenant"),
        ("clientState", "wrong", "wrong_client_state"),
        ("clientState", "不正な値", "wrong_client_state"),
        ("clientState", None, "wrong_client_state"),
        ("subscriptionId", "wrong", "wrong_subscription"),
        ("resource", "users/other/onlineMeetings/m/transcripts/t", "wrong_organizer"),
        (
            "resource",
            "communications/onlineMeetings/m/transcripts/t",
            "invalid_resource",
        ),
        (
            "resource",
            "users/organizer-1/adhocCalls/c/transcripts/t",
            "invalid_resource",
        ),
        (
            "resource",
            "users/organizer-1/onlineMeetings/m/recordings/r",
            "invalid_resource",
        ),
        ("resource", "https://example.test/transcripts", "invalid_resource"),
        ("resource", {}, "invalid_resource"),
        ("changeType", "updated", "wrong_change_type"),
    ],
)
def test_rejections_are_hashed_and_sanitized(setup, notification, field, value, reason):
    now, config, store, graph, reconciler, processor, signals = setup
    notification[field] = value
    notification["access_token"] = "never-save-this"
    processor.process(canonical({"value": [notification]}), {}, now)
    key, entry = next(iter(store.blobs.items()))
    assert key[0] == "quarantine"
    assert entry["reason"] == reason
    assert len(entry["hash"]) == 64
    assert "never-save-this" not in json.dumps(entry)
    assert config.client_state not in json.dumps(entry)


@pytest.mark.parametrize(
    "body", [b"not json", b"\xff", b"[]", b"{}", b'{"value": []}', b'{"value": [null]}']
)
def test_malformed_events_do_not_block_partition(setup, body):
    now, config, store, graph, reconciler, processor, _ = setup
    processor.process(body, {}, now)
    assert next(iter(store.blobs))[0] == "quarantine"


def test_graph_validation_is_ignored_without_state(setup):
    now, config, store, graph, reconciler, processor, signals = setup
    processor.process(
        canonical(
            {
                "value": [
                    {
                        "subscriptionId": "NA",
                        "clientState": "NA",
                        "resource": "NA",
                        "changeType": "Validation: Testing client application reachability for subscription Request-Id: 123",
                    }
                ]
            }
        ),
        {},
        now,
    )
    assert not store.blobs
    assert not graph.calls
    assert signals == [("validation_ignored", {})]


def test_duplicate_delivery_and_path_forms_have_one_archive(setup, notification):
    now, config, store, graph, reconciler, processor, _ = setup
    processor.process(canonical({"value": [notification]}), {"sequenceNumber": 1}, now)
    notification["resource"] = (
        "users/organizer-1/onlineMeetings/meeting-1/transcripts/transcript-1"
    )
    processor.process(
        canonical({"value": [notification]}),
        {"sequenceNumber": 2},
        now + timedelta(minutes=5),
    )
    assert len(store.blobs) == 1
    assert next(iter(store.blobs.values()))["broker"]["sequenceNumber"] == 1


def test_storage_failure_replays_entire_envelope_safely(setup, notification):
    now, config, store, graph, reconciler, processor, _ = setup
    body = canonical({"value": [notification, copy.deepcopy(notification)]})
    store.fail_write = True
    with pytest.raises(OSError):
        processor.process(body, {}, now)
    store.fail_write = False
    processor.process(body, {}, now)
    assert len(store.blobs) == 1


def test_state_loss_before_first_event_recovers_from_graph(setup, notification):
    now, config, store, graph, reconciler, processor, _ = setup
    store.state = {}
    processor.process(canonical({"value": [notification]}), {}, now)
    assert next(iter(store.blobs))[0] == "events"
    assert sum(c[0] == "create" for c in graph.calls) == 1


@pytest.mark.parametrize(
    "lifecycle", ["reauthorizationRequired", "subscriptionRemoved", "missed"]
)
def test_lifecycle_renews_with_fresh_token(setup, notification, lifecycle):
    now, config, store, graph, reconciler, processor, signals = setup
    n = {k: notification[k] for k in ("tenantId", "clientState", "subscriptionId")}
    n["lifecycleEvent"] = lifecycle
    if lifecycle == "subscriptionRemoved":
        graph.subscriptions = []
    processor.process(canonical({"value": [n]}), {}, now)
    assert (
        next(iter(store.blobs.values()))["notification"]["lifecycleEvent"] == lifecycle
    )
    assert graph.calls[-1][0] == (
        "create" if lifecycle == "subscriptionRemoved" else "renew"
    )
    if lifecycle != "subscriptionRemoved":
        assert graph.calls[-1][3] is True
    assert any(s[0] == "delivery_gap" for s in signals) is (lifecycle == "missed")


def test_lifecycle_retry_repeats_action_after_archive(setup, notification, monkeypatch):
    now, config, store, graph, reconciler, processor, signals = setup
    n = {k: notification[k] for k in ("tenantId", "clientState", "subscriptionId")}
    n["lifecycleEvent"] = "reauthorizationRequired"
    renew = graph.renew
    monkeypatch.setattr(
        graph,
        "renew",
        lambda *a, **kw: (_ for _ in ()).throw(GraphFailure(503, "Unavailable")),
    )
    with pytest.raises(GraphFailure):
        processor.process(canonical({"value": [n]}), {}, now)
    assert len(store.blobs) == 1
    monkeypatch.setattr(graph, "renew", renew)
    processor.process(canonical({"value": [n]}), {}, now)
    assert len(store.blobs) == 1
    assert graph.calls[-1][0] == "renew"


def test_archived_metadata_excludes_all_extra_content(setup, notification):
    now, config, store, graph, reconciler, processor, signals = setup
    notification.update(
        encryptedContent={"data": "private"},
        validationTokens=["token"],
        transcript="text",
    )
    processor.process(
        canonical({"value": [notification]}),
        {"enqueuedAt": timestamp(now - timedelta(seconds=600))},
        now,
    )
    assert "private" not in json.dumps(list(store.blobs.values()))
    assert "validationTokens" not in json.dumps(list(store.blobs.values()))
    assert signals[-1] == ("event_archived", {"processingDelaySeconds": 600})
