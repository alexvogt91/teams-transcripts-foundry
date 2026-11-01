import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def candidate_probe(monkeypatch):
    harness = Path(__file__).resolve().parents[1] / "harness"
    monkeypatch.syspath_prepend(str(harness))
    spec = importlib.util.spec_from_file_location(
        "teams_candidate_probe", harness / "probe.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module,
        "project_endpoint",
        lambda: "https://test.services.ai.azure.com/api/projects/test",
    )
    return module


def stream(
    module,
    monkeypatch,
    *,
    version="2",
    called=True,
    marker="TEAMS_CORPUS_READ_OK",
    completed=True,
):
    events = []
    if called:
        events.append(
            {
                "type": "response.output_item.added",
                "item": {"type": "function_call", "name": "list_transcripts"},
            }
        )
    events.append({"type": "response.output_text.delta", "delta": marker})
    if completed:
        events.append(
            {
                "type": "response.completed",
                "response": {"agent_reference": {"version": version}},
            }
        )
    response = Mock()
    response.__enter__ = Mock(
        return_value=[
            ("data: " + json.dumps(event) + "\n").encode() for event in events
        ]
    )
    response.__exit__ = Mock(return_value=False)
    call = Mock(return_value=response)
    monkeypatch.setattr(module.urllib.request, "urlopen", call)
    return call


def test_candidate_requires_actual_native_read_and_pinned_version(
    candidate_probe, monkeypatch, capsys
):
    call = stream(candidate_probe, monkeypatch)
    candidate_probe.probe("2", "private-access-token")
    body = json.loads(call.call_args.args[0].data)
    assert body["agent_reference"]["version"] == "2"
    assert body["store"] is False
    assert body["input"][0]["role"] == "user"
    assert "private" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "options",
    [
        {"version": "1"},
        {"version": None},
        {"called": False},
        {"marker": "TEAMS_CORPUS_UNAVAILABLE"},
        {"completed": False},
    ],
)
def test_candidate_never_promotes_unproven_reads(candidate_probe, monkeypatch, options):
    stream(candidate_probe, monkeypatch, **options)
    with pytest.raises(SystemExit, match="not promoted"):
        candidate_probe.probe("2", "private-access-token")


def test_application_denial_probe_requires_native_unavailability(
    candidate_probe, monkeypatch
):
    stream(candidate_probe, monkeypatch, marker="TEAMS_CORPUS_UNAVAILABLE")
    candidate_probe.probe("2", "application-access-token", expect_read=False)
    stream(candidate_probe, monkeypatch, marker="TEAMS_CORPUS_READ_OK")
    with pytest.raises(SystemExit, match="not promoted"):
        candidate_probe.probe("2", "application-access-token", expect_read=False)
