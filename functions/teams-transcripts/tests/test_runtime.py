import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import azure.functions as func
from azure.core.exceptions import ResourceExistsError
import pytest

import adapters
import function_app
from adapters import BlobStore, GraphClient
from core import GraphFailure, RetryDelivery, canonical

DEFINITIONS = function_app.app.get_functions()


def test_runtime_indexes_three_triggers_with_infinite_retry():
    definitions = DEFINITIONS
    assert {f.get_function_name() for f in definitions} == {
        "subscription_timer",
        "transcript_notifications",
        "transcript_ingest",
    }
    event = next(
        f for f in definitions if f.get_function_name() == "transcript_notifications"
    )
    config = json.loads(event.get_function_json())
    assert event.get_settings_dict("retry_policy") == {
        "setting_name": "retry_policy",
        "strategy": "fixed_delay",
        "max_retry_count": "-1",
        "delay_interval": "00:00:10",
    }
    assert config["bindings"][0]["consumerGroup"] == "$Default"
    assert config["bindings"][0]["cardinality"].lower() == "one"
    assert all(
        b["type"] != "httpTrigger"
        for f in definitions
        for b in json.loads(f.get_function_json())["bindings"]
    )


def test_event_trigger_storage_failure_then_broker_replay(
    setup, notification, monkeypatch
):
    now, config, store, graph, reconciler, processor, _ = setup
    monkeypatch.setattr(
        function_app, "enrollment", lambda: (config.tenant_id, (config.organizer_id,))
    )
    monkeypatch.setattr(
        function_app,
        "components",
        lambda oid=None: nullcontext((reconciler, processor)),
    )
    event = func.EventHubEvent(
        body=canonical({"value": [notification]}),
        enqueued_time=now,
        sequence_number=100,
        offset="200",
    )
    trigger = next(
        f.get_user_function()
        for f in DEFINITIONS
        if f.get_function_name() == "transcript_notifications"
    )
    store.fail_write = True
    with pytest.raises(RuntimeError, match="notification_processing_unavailable"):
        trigger(event)
    assert not store.blobs
    store.fail_write = False
    trigger(event)
    trigger(event)
    assert len(store.blobs) == 1
    assert next(iter(store.blobs.values()))["broker"]["sequenceNumber"] == 100


def test_timer_disabled_is_inert(monkeypatch):
    monkeypatch.setenv("TEAMS_RECONCILIATION_MODE", "disabled")
    monkeypatch.setattr(
        function_app, "components", Mock(side_effect=AssertionError("must not connect"))
    )
    timer = next(
        f.get_user_function()
        for f in DEFINITIONS
        if f.get_function_name() == "subscription_timer"
    )
    timer(None)


def test_azure_blob_duplicate_is_success_but_network_failure_retries(monkeypatch):
    service = Mock()
    blob = service.get_blob_client.return_value
    monkeypatch.setattr(adapters, "BlobServiceClient", Mock(return_value=service))
    store = BlobStore("https://archive.blob.core.windows.net", Mock())
    blob.download_blob.return_value.readall.return_value = (
        b'{"notification":"metadata"}'
    )
    blob.upload_blob.side_effect = ResourceExistsError("already exists")
    store.write_once("events", "event.json", {"notification": "metadata"})
    assert blob.upload_blob.call_args.kwargs["overwrite"] is False
    blob.upload_blob.side_effect = OSError("private server response with token")
    with pytest.raises(RetryDelivery, match="archive_storage_unavailable") as caught:
        store.write_once("events", "event.json", {})
    assert "token" not in str(caught.value)


def graph_client(setup, monkeypatch, status=200, payload=None):
    config = setup[1]
    credential = Mock()
    credential.get_token.return_value = SimpleNamespace(token="private-access-token")
    factory = Mock(return_value=credential)
    monkeypatch.setattr(adapters, "ClientAssertionCredential", factory)
    response = Mock(status_code=status, content=b"json")
    response.json.return_value = payload or {"value": []}
    session = Mock()
    session.request.return_value = response
    return GraphClient(config, Mock(), session), session, factory


def test_speaker_policy_retries_only_unattributed_content(setup, monkeypatch):
    client, session, _ = graph_client(setup, monkeypatch)
    denied = Mock(status_code=403)
    denied.json.return_value = {
        "error": {"innerError": {"code": "SpeakerAttributionNotAllowed"}}
    }
    accepted = Mock(status_code=200)
    accepted.__enter__ = Mock(return_value=accepted)
    accepted.__exit__ = Mock(return_value=False)
    accepted.iter_content.return_value = [
        b"00:00:01.000 --> 00:00:02.000\nNo speakers.\n"
    ]
    session.request.side_effect = [denied, accepted]
    assert (
        client.transcript_content("meeting/+id", "transcript=")
        == b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nNo speakers.\n"
    )
    assert [c.kwargs["headers"]["Accept"] for c in session.request.call_args_list] == [
        "text/vtt",
        "application/vnd.microsoft.graph.transcript+text",
    ]
    assert "%2F%2Bid" in session.request.call_args.args[1]


def test_graph_metadata_route_cannot_read_other_organizers(setup, monkeypatch):
    client, session, _ = graph_client(setup, monkeypatch)
    with pytest.raises(GraphFailure, match="graph_request_failed"):
        client.request(
            "GET", "/users/other/onlineMeetings/meeting/transcripts/transcript"
        )
    session.request.assert_not_called()


@pytest.mark.parametrize(
    "status,payload,classification",
    [
        (
            403,
            {
                "error": {
                    "code": "Forbidden",
                    "innerError": {"code": "GraphAccessToTranscriptsDisabled"},
                }
            },
            "tenant_policy_disables_transcripts",
        ),
        (
            403,
            {"error": {"message": "private-verification-secret"}},
            "check_admin_consent",
        ),
        (401, {"error": {"message": "private-access-token"}}, "check_admin_consent"),
        (429, {"error": {"message": "private-access-token"}}, "graph_request_failed"),
    ],
)
def test_graph_failures_are_explicit_and_never_leak_secrets(
    setup, monkeypatch, status, payload, classification
):
    client, session, factory = graph_client(setup, monkeypatch, status, payload)
    with pytest.raises(GraphFailure, match=classification) as caught:
        client.list_subscriptions().__next__()
    assert "private" not in str(caught.value)


@pytest.mark.parametrize(
    "message,code",
    [
        (
            "No application access policy found for private-id",
            "TeamsApplicationAccessPolicy",
        ),
        (
            "Failed to validate the notification URL private-url",
            "NotificationValidationFailed",
        ),
        (
            "Event hub authorization failed with private-token",
            "EventHubAuthorizationFailed",
        ),
    ],
)
def test_graph_subscription_diagnostics_classify_without_remote_content(
    setup, monkeypatch, message, code
):
    events = Mock()
    monkeypatch.setattr(adapters, "emit", events)
    client, *_ = graph_client(
        setup,
        monkeypatch,
        403,
        {"error": {"code": "ExtensionError", "message": message}},
    )
    with pytest.raises(GraphFailure):
        client.create(setup[1].request(setup[0]))
    assert events.call_args.kwargs == {
        "httpStatus": 403,
        "code": code,
        "operation": "subscription_post",
    }
    assert "private" not in str(events.call_args)


def test_fresh_token_renewal_replaces_cached_credential(setup, monkeypatch):
    client, session, factory = graph_client(setup, monkeypatch)
    client.renew("subscription-id", "2026-10-25T00:00:00Z", fresh=True)
    assert factory.call_count == 2
    request = session.request.call_args
    assert request.args == (
        "PATCH",
        "https://graph.microsoft.com/v1.0/subscriptions/subscription-id",
    )
    assert request.kwargs["json"] == {"expirationDateTime": "2026-10-25T00:00:00Z"}
    assert request.kwargs["allow_redirects"] is False
    assert factory.call_args.args[:2] == (setup[1].tenant_id, setup[1].app_id)


def test_graph_pagination_rejects_token_exfiltration_url(setup, monkeypatch):
    client, session, factory = graph_client(
        setup,
        monkeypatch,
        payload={"value": [], "@odata.nextLink": "https://attacker.test/subscriptions"},
    )
    with pytest.raises(GraphFailure):
        list(client.list_subscriptions())
    assert session.request.call_count == 1
