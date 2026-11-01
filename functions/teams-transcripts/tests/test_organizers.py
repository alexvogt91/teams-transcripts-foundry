"""Real trigger paths with a redacting Graph broker and organizer-private state."""

import json
from contextlib import nullcontext
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock

import pytest
from azure.core.exceptions import ResourceNotFoundError

import adapters
import function_app
from adapters import BlobStore
from core import GraphFailure, Processor, Reconciler, canonical


@pytest.mark.parametrize("redacted", [None, ""])
def test_redacted_graph_listings_keep_ids_across_timer_cycles(setup, redacted):
    now, config, store, graph, reconciler, *_ = setup
    reconciler.reconcile(now)
    initial = store.state["subscriptionId"]
    original_list = graph.list_subscriptions
    graph.list_subscriptions = lambda: [
        dict(s, clientState=redacted) for s in original_list()
    ]
    for minutes in (15, 30, 45, 1500, 1515):
        reconciler.reconcile(now + timedelta(minutes=minutes))
        assert store.state["subscriptionId"] == initial
    assert [c[0] for c in graph.calls].count("create") == 1
    assert not any(c[0] == "delete" for c in graph.calls)
    assert config.client_state not in json.dumps(store.state)


@pytest.mark.parametrize("change", ["secret", "url"])
def test_redacted_listings_replace_only_on_real_configuration_change(setup, change):
    now, config, store, graph, reconciler, *_ = setup
    reconciler.reconcile(now)
    updated = replace(
        config,
        **(
            {"client_state": "new-secret"}
            if change == "secret"
            else {"notification_url": "EventHub:https://new.test"}
        ),
    )
    graph.config = updated
    graph.subscriptions[0]["clientState"] = None
    reconciler = Reconciler(updated, graph, store)
    reconciler.reconcile(now)
    graph.subscriptions[0]["clientState"] = None
    reconciler.reconcile(now + timedelta(minutes=15))
    assert [c[0] for c in graph.calls].count("create") == 2
    assert [c[0] for c in graph.calls].count("delete") == 1
    assert store.state["configurationFingerprint"] == updated.fingerprint


def test_legacy_unverified_configuration_replaces_once_preserving_grace(setup):
    now, config, store, graph, reconciler, *_ = setup
    reconciler.reconcile(now)
    store.state.pop("configurationFingerprint")
    store.state["ingestionHandoverCompletedAt"] = now.isoformat()
    old = graph.subscriptions[0]
    old["id"] = "legacy-id"
    old["clientState"] = None
    reconciler.reconcile(now)
    reconciler.reconcile(now + timedelta(minutes=15))
    assert [c[0] for c in graph.calls].count("create") == 2
    assert {s["id"] for s in store.state["acceptedSubscriptions"]} == {
        "legacy-id",
        "sub-1",
    }
    assert store.state["ingestionHandoverCompletedAt"] == now.isoformat()


def fleet(setup, monkeypatch):
    now, config, store, graph, reconciler, processor, *_ = setup
    reconciler.reconcile(now)
    other = replace(config, organizer_id="organizer-2")
    other_store = type(store)()
    other_graph = type(graph)(other, now)
    other_graph.subscription = lambda subscription_id="sub-2", hours=48: dict(
        other.request(now), id=subscription_id, applicationId=other.app_id
    )
    other_reconciler = Reconciler(other, other_graph, other_store)
    other_reconciler.reconcile(now)
    other_processor = Processor(other, other_store, other_reconciler)
    processors = {
        config.organizer_id: (reconciler, processor),
        other.organizer_id: (other_reconciler, other_processor),
    }
    monkeypatch.setattr(
        function_app, "enrollment", lambda: (config.tenant_id, tuple(processors))
    )
    monkeypatch.setattr(
        function_app, "components", lambda oid: nullcontext(processors[oid])
    )
    return processors


def notices(processors):
    return [
        dict(
            tenantId=p.config.tenant_id,
            clientState=p.config.client_state,
            subscriptionId=p.store.state["subscriptionId"],
            changeType="created",
            resource=f"users/{oid}/onlineMeetings/meeting/transcripts/transcript",
        )
        for oid, (_, p) in processors.items()
    ]


def test_both_organizers_notifications_and_lifecycle_use_their_state(
    setup, monkeypatch
):
    processors = fleet(setup, monkeypatch)
    now = setup[0]
    function_app.process_notifications(
        canonical({"value": notices(processors)}), {}, now
    )
    for oid, (_, processor) in processors.items():
        assert len(processor.store.blobs) == 1
        assert (
            next(iter(processor.store.blobs.values()))["notification"]["organizerId"]
            == oid
        )
    lifecycle = dict(notices(processors)[1], lifecycleEvent="reauthorizationRequired")
    lifecycle.pop("resource")
    function_app.process_notifications(canonical({"value": [lifecycle]}), {}, now)
    assert any(c[0] == "renew" for c in processors["organizer-2"][0].graph.calls)
    assert not any(c[0] == "renew" for c in processors["organizer-1"][0].graph.calls)


@pytest.mark.parametrize("failure", ["reconcile", "archive", "initialization"])
def test_one_organizer_failure_does_not_starve_other(setup, monkeypatch, failure):
    processors = fleet(setup, monkeypatch)
    now = setup[0]
    first, second = processors.values()
    if failure == "reconcile":
        monkeypatch.setenv("TEAMS_RECONCILIATION_MODE", "enabled")
        first[0].graph.list_subscriptions = Mock(
            side_effect=GraphFailure(403, "AccessDenied")
        )
        previous_heartbeat = second[0].store.state["heartbeat"]
        with pytest.raises(RuntimeError):
            function_app.subscription_timer.build().get_user_function()(None)
        assert second[0].store.state["heartbeat"] != previous_heartbeat
    else:
        if failure == "archive":
            first[1].store.fail_write = True
        else:

            def components(oid):
                if oid == "organizer-1":
                    raise OSError("private remote detail")
                return nullcontext(processors[oid])

            monkeypatch.setattr(function_app, "components", components)
        with pytest.raises(RuntimeError):
            function_app.process_notifications(
                canonical({"value": notices(processors)}), {}, now
            )
        assert len(second[1].store.blobs) == 1


def test_cross_organizer_subscription_is_rejected(setup, monkeypatch):
    processors = fleet(setup, monkeypatch)
    notification = notices(processors)[0]
    notification["subscriptionId"] = "sub-2"
    function_app.process_notifications(
        canonical({"value": [notification]}), {}, setup[0]
    )
    assert not any(
        k[0] == "events" for pair in processors.values() for k in pair[1].store.blobs
    )


def test_state_adapter_migrates_only_matching_organizer_and_handover(
    setup, monkeypatch
):
    now, config, store, graph, reconciler, *_ = setup
    reconciler.reconcile(now)
    legacy = dict(store.state, ingestionHandoverCompletedAt=now.isoformat())
    service = Mock()
    state_blob, legacy_blob = Mock(), Mock()
    state_blob.download_blob.side_effect = ResourceNotFoundError("missing")
    legacy_blob.download_blob.return_value.readall.return_value = canonical(legacy)
    service.get_blob_client.side_effect = (
        lambda container, name: legacy_blob
        if name == "subscription.json"
        else state_blob
    )
    monkeypatch.setattr(adapters, "BlobServiceClient", Mock(return_value=service))
    for oid in (config.organizer_id, "organizer-2"):
        source = BlobStore("https://test.blob.core.windows.net", Mock(), oid)
        migrated = source.read_state()
        if oid == config.organizer_id:
            assert migrated == legacy
        else:
            assert migrated == {}
        assert (
            service.get_blob_client.call_args_list[-2].args[1]
            == f"organizers/{oid}/subscription.json"
        )


def test_archive_handover_filters_organizer(setup, monkeypatch):
    now, config, *_ = setup
    service = Mock()
    monkeypatch.setattr(adapters, "BlobServiceClient", Mock(return_value=service))
    source = BlobStore(
        "https://test.blob.core.windows.net", Mock(), config.organizer_id
    )
    state = {}
    source.locked_state = lambda: nullcontext(state)
    source.save_state = Mock()
    container = service.get_container_client.return_value
    container.list_blobs.return_value = [Mock(name="first"), Mock(name="second")]
    archived = [
        dict(
            schemaVersion=1,
            receivedAt=now.isoformat(),
            notification=dict(
                tenantId=config.tenant_id,
                organizerId=oid,
                meetingId="m",
                transcriptId="t",
                changeType="created",
            ),
        )
        for oid in (config.organizer_id, "organizer-2")
    ]
    container.download_blob.return_value.readall.side_effect = [
        canonical(item) for item in archived
    ]
    enqueue = Mock()
    source.handover_archived_notifications(enqueue, now)
    assert enqueue.call_count == 1
    assert enqueue.call_args.args[0]["organizerId"] == config.organizer_id
    assert state["ingestionHandoverCompletedAt"]


@pytest.mark.parametrize(
    "configuration",
    [
        {},
        {"tenantId": "bad", "organizerIds": []},
        {"tenantId": "11111111-1111-4111-8111-111111111111", "organizerIds": []},
    ],
)
def test_invalid_enrollment_fails_closed(monkeypatch, configuration):
    monkeypatch.setenv("TEAMS_ORGANIZERS", json.dumps(configuration))
    with pytest.raises((ValueError, KeyError)):
        function_app.enrollment()


@pytest.mark.parametrize(
    "field,value",
    [
        ("changeType", "updated"),
        ("includeResourceData", True),
        ("lifecycleNotificationUrl", "EventHub:https://old.test"),
    ],
)
def test_returned_configuration_drift_replaces_owned_subscription(setup, field, value):
    now, config, store, graph, reconciler, *_ = setup
    reconciler.reconcile(now)
    graph.subscriptions[0][field] = value
    graph.subscriptions[0]["clientState"] = None
    reconciler.reconcile(now)
    reconciler.reconcile(now + timedelta(minutes=15))
    assert [c[0] for c in graph.calls].count("delete") == 1
    assert [c[0] for c in graph.calls].count("create") == 2
