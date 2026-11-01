import copy
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import Config, GraphFailure, Processor, Reconciler, UTC, timestamp


class Store:
    def __init__(self):
        self.state = {}
        self.blobs = {}
        self.fail_write = False
        self.fail_state = False
        self.busy = False

    @contextmanager
    def locked_state(self):
        yield None if self.busy else copy.deepcopy(self.state)

    def read_state(self):
        return copy.deepcopy(self.state)

    def save_state(self, state):
        if self.fail_state:
            raise OSError("private storage error")
        self.state = copy.deepcopy(state)

    def write_once(self, container, name, data):
        if self.fail_write:
            raise OSError("private storage error")
        return self.blobs.setdefault((container, name), copy.deepcopy(data))


class Graph:
    def __init__(self, config, now):
        self.config, self.now = config, now
        self.subscriptions = []
        self.calls = []
        self.conflict = False
        self.missing_renewal = False

    def subscription(self, subscription_id="sub-1", hours=48):
        return dict(
            self.config.request(self.now),
            id=subscription_id,
            applicationId=self.config.app_id,
            expirationDateTime=timestamp(self.now + timedelta(hours=hours)),
        )

    def list_subscriptions(self):
        self.calls.append(("list",))
        return copy.deepcopy(self.subscriptions)

    def create(self, body):
        self.calls.append(("create", body))
        s = self.subscription()
        self.subscriptions.append(s)
        if self.conflict:
            raise GraphFailure(409, "Conflict")
        return copy.deepcopy(s)

    def renew(self, subscription_id, expiration, fresh=False):
        self.calls.append(("renew", subscription_id, expiration, fresh))
        if self.missing_renewal:
            self.subscriptions = []
            self.missing_renewal = False
            raise GraphFailure(404, "NotFound")
        s = next(s for s in self.subscriptions if s["id"] == subscription_id)
        s["expirationDateTime"] = expiration
        return copy.deepcopy(s)

    def delete(self, subscription_id):
        self.calls.append(("delete", subscription_id))
        self.subscriptions = [
            s for s in self.subscriptions if s["id"] != subscription_id
        ]


@pytest.fixture
def setup():
    now = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
    config = Config(
        "tenant-1",
        "organizer-1",
        "app-1",
        "private-verification-secret",
        "EventHub:https://namespace.servicebus.windows.net/eventhubname/teams-transcripts-001?tenantId=example.onmicrosoft.com",
    )
    store = Store()
    graph = Graph(config, now)
    signals = []

    def emit(name, **kw):
        signals.append((name, kw))

    reconciler = Reconciler(config, graph, store, emit)
    processor = Processor(config, store, reconciler, emit)
    return now, config, store, graph, reconciler, processor, signals


@pytest.fixture
def notification(setup):
    now, config, store, graph, reconciler, processor, signals = setup
    reconciler.reconcile(now)
    return {
        "tenantId": config.tenant_id,
        "clientState": config.client_state,
        "subscriptionId": "sub-1",
        "changeType": "created",
        "resource": "users/organizer-1/onlineMeetings('meeting-1')/transcripts('transcript-1')",
        "resourceData": {
            "@odata.type": "#Microsoft.Graph.callTranscript",
            "id": "transcript-1",
        },
    }
