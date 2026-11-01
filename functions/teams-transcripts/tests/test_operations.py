import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location(
    "teams_operations",
    Path(__file__).resolve().parents[3] / "scripts/teams-transcripts/operations.py",
)
operations = importlib.util.module_from_spec(spec)
spec.loader.exec_module(operations)


@pytest.fixture(autouse=True)
def portable_state(monkeypatch):
    monkeypatch.setattr(
        operations, "enrollment", lambda: {"organizerIds": ["organizer-a"]}
    )
    monkeypatch.setattr(
        operations,
        "state",
        Mock(
            return_value={
                "appInsightsName": "test-insights",
                "resourceGroup": "test-rg",
            }
        ),
    )


NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)


def response(age_minutes=0, hours=48, message=None):
    return {
        "tables": [
            {
                "columns": [{"name": "timestamp"}, {"name": "message"}],
                "rows": [
                    [
                        (NOW - timedelta(minutes=age_minutes)).isoformat(),
                        message
                        or 'teams_transcripts timer_heartbeat {"organizerId": "organizer-a", "expiryHours": '
                        + str(hours)
                        + "}",
                    ]
                ],
            }
        ]
    }


@pytest.mark.parametrize("age,hours", [(0, 24), (15, 48), (44, 25)])
def test_heartbeat_accepts_fresh_subscription_with_remaining_day(
    monkeypatch, age, hours
):
    monkeypatch.setattr(operations, "az", Mock(return_value=response(age, hours)))
    operations.verify_heartbeat(NOW)


@pytest.mark.parametrize(
    "result",
    [
        {"tables": [{"columns": [], "rows": []}]},
        response(45),
        response(-1),
        response(hours=23.99),
        response(15, 24),
        response(hours="NaN"),
        response(hours="Infinity"),
        response(message="teams_transcripts reconciliation_failure {}"),
        response(message="teams_transcripts timer_heartbeat malformed"),
    ],
)
def test_heartbeat_refuses_missing_stale_failed_or_short_subscription(
    monkeypatch, result
):
    monkeypatch.setattr(operations, "az", Mock(return_value=result))
    with pytest.raises(RuntimeError, match="Subscription heartbeat check failed"):
        operations.verify_heartbeat(NOW)


@pytest.mark.parametrize("missing,stale", [(True, False), (False, True)])
def test_heartbeat_checks_every_enrolled_organizer(monkeypatch, missing, stale):
    monkeypatch.setattr(
        operations,
        "enrollment",
        lambda: {"organizerIds": ["organizer-a", "organizer-b"]},
    )
    result = response()
    if not missing:
        other = response(45 if stale else 0)
        other["tables"][0]["rows"][0][1] = other["tables"][0]["rows"][0][1].replace(
            "organizer-a", "organizer-b"
        )
        result["tables"][0]["rows"] += other["tables"][0]["rows"]
    monkeypatch.setattr(operations, "az", Mock(return_value=result))
    with pytest.raises(RuntimeError, match="every enrolled organizer"):
        operations.verify_heartbeat(NOW)


def test_heartbeat_accepts_all_healthy_organizers(monkeypatch):
    monkeypatch.setattr(
        operations,
        "enrollment",
        lambda: {"organizerIds": ["organizer-a", "organizer-b"]},
    )
    result = response()
    other = response(15)
    other["tables"][0]["rows"][0][1] = other["tables"][0]["rows"][0][1].replace(
        "organizer-a", "organizer-b"
    )
    result["tables"][0]["rows"] += other["tables"][0]["rows"]
    monkeypatch.setattr(operations, "az", Mock(return_value=result))
    operations.verify_heartbeat(NOW)
