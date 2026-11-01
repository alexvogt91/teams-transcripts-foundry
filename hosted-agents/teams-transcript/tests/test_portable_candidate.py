import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent.identity_acceptance import verify_identity
from test_identity_acceptance import source


@pytest.fixture
def deploy(monkeypatch):
    harness = Path(__file__).resolve().parents[1] / "harness"
    monkeypatch.syspath_prepend(str(harness))
    spec = importlib.util.spec_from_file_location(
        "portable_candidate", harness / "deploy.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dedicated_empty_corpus_still_proves_reader_and_writer_denial():
    reader = source()
    result = verify_identity(reader, [])
    assert result == {
        "teamsRead": 200,
        "teamsWrite": 403,
        "neighborReadsDenied": 0,
        "neighborsChecked": 0,
    }
    reader.client.get_database_client.assert_not_called()


def test_dedicated_account_needs_no_production_neighbors(deploy, monkeypatch):
    monkeypatch.setattr(
        deploy, "az", Mock(return_value=[{"name": "teams-transcripts"}])
    )
    assert (
        deploy.neighbors(
            {
                "cosmosAccountName": "sample",
                "resourceGroup": "sample",
                "teamsTranscriptsDatabaseName": "teams-transcripts",
            }
        )
        == []
    )


def test_inventory_checks_all_neighbor_containers_without_fixed_count(
    deploy, monkeypatch
):
    calls = Mock(
        side_effect=[
            [
                {"name": "teams-transcripts"},
                {"name": "neighbor-a"},
                {"name": "empty-neighbor"},
            ],
            [{"name": "sample-a"}, {"name": "sample-b"}],
            [],
        ]
    )
    monkeypatch.setattr(deploy, "az", calls)
    assert deploy.neighbors(
        {
            "cosmosAccountName": "sample",
            "resourceGroup": "sample",
            "teamsTranscriptsDatabaseName": "teams-transcripts",
        }
    ) == [["neighbor-a", "sample-a"], ["neighbor-a", "sample-b"]]


def test_incomplete_inventory_cannot_silently_skip_isolation(deploy, monkeypatch):
    monkeypatch.setattr(deploy, "az", Mock(return_value=[]))
    with pytest.raises(SystemExit, match="database absent"):
        deploy.neighbors(
            {
                "cosmosAccountName": "sample",
                "resourceGroup": "sample",
                "teamsTranscriptsDatabaseName": "teams-transcripts",
            }
        )


def test_dry_run_does_not_fetch_credentials_or_create_version(
    deploy, monkeypatch, capsys
):
    monkeypatch.setattr(
        deploy.sys, "argv", ["deploy.py", "sample.azurecr.io/agent:v1", "--dry-run"]
    )
    monkeypatch.setattr(
        deploy, "shared", lambda: {"registryLoginServer": "sample.azurecr.io"}
    )
    monkeypatch.setattr(deploy, "neighbors", lambda values: [])
    request, token = Mock(), Mock()
    monkeypatch.setattr(deploy, "request", request)
    monkeypatch.setattr(deploy, "token", token)
    deploy.main()
    request.assert_not_called()
    token.assert_not_called()
    assert "0 neighboring" in capsys.readouterr().out
