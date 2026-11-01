"""Offline deployment contracts using synthetic configuration and command responses."""

import importlib.util
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

import common
import manage

ROOT = Path(__file__).resolve().parents[1]
TENANT = "11111111-1111-4111-8111-111111111111"
ORGANIZER = "22222222-2222-4222-8222-222222222222"
SUBSCRIPTION = "66666666-6666-4666-8666-666666666666"


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    settings = json.loads((ROOT / "configuration/deployment.example.json").read_text())
    settings["chatModelVersion"] = "synthetic-version"
    config_path = tmp_path / "deployment.json"
    config_path.write_text(json.dumps(settings))
    organizer_path = tmp_path / "organizers.json"
    organizer_path.write_text(
        json.dumps({"tenantId": TENANT, "organizerIds": [ORGANIZER]})
    )
    settings["organizersFile"] = str(organizer_path)
    config_path.write_text(json.dumps(settings))
    monkeypatch.setenv("TEAMS_CONFIG", str(config_path))
    monkeypatch.setattr(common, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(manage, "ROOT", tmp_path)
    (tmp_path / ".local").mkdir()
    return settings


def model_inventory():
    return [
        {
            "model": {"name": name, "version": version},
            "skus": [{"name": "GlobalStandard"}],
        }
        for name, version in [
            ("gpt-6-luna", "synthetic-version"),
            ("text-embedding-3-large", "1"),
        ]
    ]


def preflight_azure(args):
    if args[:2] == ("account", "show"):
        return {"tenantId": TENANT}
    if args[:3] == ("ad", "signed-in-user", "show"):
        return {"id": ORGANIZER}
    if args[:3] == ("ad", "user", "show"):
        return ORGANIZER
    if args[:3] == ("cognitiveservices", "model", "list"):
        return model_inventory()
    if args[:2] == ("functionapp", "list-flexconsumption-locations"):
        return [{"name": "swedencentral"}]
    raise AssertionError(args)


def test_preflight_uses_independent_local_config(deployment, monkeypatch):
    calls = Mock(side_effect=lambda *args: preflight_azure(args))
    monkeypatch.setattr(manage, "az", calls)
    assert manage.preflight() == ORGANIZER
    assert common.enrollment() == {"tenantId": TENANT, "organizerIds": [ORGANIZER]}
    assert all(call.args[0] != "deployment" for call in calls.call_args_list)


@pytest.mark.parametrize(
    "problem", ["tenant", "operator", "model", "sku", "region", "capacity"]
)
def test_preflight_rejects_unusable_configuration_before_mutation(
    deployment, monkeypatch, problem
):
    if problem == "capacity":
        monkeypatch.setattr(
            manage, "config", lambda: {**deployment, "chatModelCapacity": True}
        )

    def azure(*args):
        result = preflight_azure(args)
        if problem == "tenant" and args[:2] == ("account", "show"):
            return {"tenantId": "33333333-3333-4333-8333-333333333333"}
        if problem == "operator" and args[:3] == ("ad", "signed-in-user", "show"):
            return {"id": "33333333-3333-4333-8333-333333333333"}
        if problem == "model" and args[:3] == ("cognitiveservices", "model", "list"):
            return []
        if problem == "sku" and args[:3] == ("cognitiveservices", "model", "list"):
            return [{**item, "skus": [{"name": "Standard"}]} for item in result]
        if problem == "region" and args[:2] == (
            "functionapp",
            "list-flexconsumption-locations",
        ):
            return []
        return result

    monkeypatch.setattr(manage, "az", azure)
    with pytest.raises(ValueError):
        manage.preflight()


def test_provision_sends_only_portable_parameters_and_disables_collection(
    deployment, monkeypatch
):
    monkeypatch.setattr(manage, "preflight", lambda: ORGANIZER)
    calls = Mock(side_effect=[{}, {"cosmosAccountName": {"value": "synthetic-cosmos"}}])
    monkeypatch.setattr(manage, "az", calls)
    manage.deploy()
    params = calls.call_args_list[-1].args
    assert (
        "reconciliationMode=disabled" in params and "ingestionEnabled=false" in params
    )
    assert "deployFunction=false" in params
    assert "operatorPrincipalId=" + ORGANIZER in params
    assert common.state()["cosmosAccountName"] == "synthetic-cosmos"
    assert common.state()["resourceGroup"] == deployment["resourceGroup"]
    assert not any(
        "key=" in str(p).lower() or "token=" in str(p).lower() for p in params
    )


def test_missing_graph_bootstrap_blocks_function_before_mutation(
    deployment, monkeypatch
):
    monkeypatch.setattr(manage, "preflight", lambda: ORGANIZER)
    azure = Mock()
    monkeypatch.setattr(manage, "az", azure)
    with pytest.raises(ValueError, match="identity bootstrap"):
        manage.deploy(deploy_function=True)
    azure.assert_not_called()


@pytest.mark.parametrize("mode", ["disabled", "rollback"])
def test_ingestion_requires_enabled_reconciliation(deployment, mode):
    with pytest.raises(ValueError, match="Ingestion requires"):
        manage.deploy(mode=mode, ingestion=True)


def test_cli_failure_never_echoes_credential_bearing_remote_output(
    deployment, monkeypatch, capsys
):
    call = Mock(
        return_value=subprocess.CompletedProcess(
            [], 1, "secret-from-stdout", "secret-from-stderr"
        )
    )
    monkeypatch.setattr(common.subprocess, "run", call)
    with pytest.raises(RuntimeError) as error:
        common.az("account", "show")
    assert "secret-from" not in str(error.value) + capsys.readouterr().out
    assert ["--subscription", SUBSCRIPTION] == call.call_args.args[0][-5:-3]


def test_state_refuses_cross_subscription_or_resource_group(deployment):
    common.save_state(
        {
            "subscriptionId": "77777777-7777-4777-8777-777777777777",
            "resourceGroup": deployment["resourceGroup"],
        }
    )
    with pytest.raises(ValueError, match="different subscription"):
        common.state()


def test_repository_has_no_environment_specific_service_dependency():
    # Inspect publishable files, excluding local environments/caches/state.
    forbidden = [
        "dev.azure.com",
        "##vso[",
        "br/private",
    ]
    for folder in ["infra", "functions", "hosted-agents", "scripts", "configuration"]:
        for path in (ROOT / folder).rglob("*"):
            if (
                not path.is_file()
                or "__pycache__" in path.parts
                or path.suffix
                not in {".py", ".ps1", ".bicep", ".bicepparam", ".md", ".json", ".txt"}
            ):
                continue
            if path.name in {"deployment.json", "teams-transcript-organizers.json"}:
                continue
            assert not any(value in path.read_text() for value in forbidden), path


def test_bicep_outputs_exclude_secrets_and_corpus_configuration_is_preserved():
    source = (ROOT / "infra/main.bicep").read_text()
    outputs = [line for line in source.splitlines() if line.startswith("output ")]
    assert not any(
        word in line.lower()
        for line in outputs
        for word in ["key1", "clientstate", "token", "connectionstring"]
    )
    for required in [
        "dimensions: 505",
        "distanceFunction: 'cosine'",
        "type: 'flat'",
        "fullTextIndexes",
        "maxThroughput: 1000",
        "disableLocalAuth: true",
        "allowSharedKeyAccess: false",
        "reconciliationMode string = 'disabled'",
        "ingestionEnabled bool = false",
    ]:
        assert required in source


def test_function_packaging_rejects_non_linux_native_wheels(monkeypatch):
    monkeypatch.setattr(manage.sys, "platform", "darwin")
    with pytest.raises(ValueError, match="Linux/amd64"):
        manage.package_function()


@pytest.fixture
def operations(monkeypatch):
    path = ROOT / "scripts/teams-transcripts/operations.py"
    spec = importlib.util.spec_from_file_location("portable_operations", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module,
        "state",
        lambda: {
            "resourceGroup": "synthetic-rg",
            "functionAppName": "synthetic-function",
            "appClientId": "synthetic-app",
        },
    )
    return module


@pytest.mark.parametrize(
    "mode,enabled,expected",
    [
        ("disabled", "true", False),
        ("rollback", "true", False),
        ("enabled", "false", False),
        ("enabled", "true", True),
    ],
)
def test_enablement_and_rollback_do_not_depend_on_telemetry(
    operations, monkeypatch, mode, enabled, expected
):
    deploy = Mock()
    monkeypatch.setattr(manage, "deploy", deploy)
    azure = Mock()
    monkeypatch.setattr(operations, "az", azure)
    monkeypatch.setattr(operations, "preflight_corpus", Mock())
    consent = Mock()
    monkeypatch.setattr(operations, "verify_consent", consent)
    monkeypatch.setattr(
        operations,
        "verify_heartbeat",
        Mock(side_effect=AssertionError("no telemetry gate")),
    )
    operations.configure_ingestion(mode, enabled, "synthetic-vault")
    deploy.assert_called_once_with(deploy_function=True, mode=mode, ingestion=expected)
    assert consent.call_count == int(expected)
    assert azure.call_args.args[:2] == ("functionapp", "restart")


def test_missing_corpus_blocks_ingestion_before_deployment(operations, monkeypatch):
    deploy = Mock()
    monkeypatch.setattr(manage, "deploy", deploy)
    monkeypatch.setattr(operations, "az", Mock(return_value={}))
    with pytest.raises(RuntimeError, match="Deployment must publish"):
        operations.configure_ingestion("enabled", "true", "synthetic-vault")
    deploy.assert_not_called()


@pytest.mark.parametrize("change", ["enableFreeTier", "location", "function_stage"])
def test_reapplying_bootstrap_cannot_misreport_active_function_or_mutate_account(
    deployment, monkeypatch, change
):
    prior = {
        "subscriptionId": SUBSCRIPTION,
        "resourceGroup": deployment["resourceGroup"],
        "operatorPrincipalId": ORGANIZER,
        "enableFreeTier": False,
        "location": "swedencentral",
    }
    if change == "enableFreeTier":
        prior["enableFreeTier"] = True
    elif change == "location":
        prior["location"] = "eastus"
    else:
        prior["deployFunction"] = True
    common.save_state(prior)
    azure = Mock()
    monkeypatch.setattr(manage, "az", azure)
    with pytest.raises(ValueError):
        manage.deploy()
    azure.assert_not_called()


def test_operator_credential_uses_configured_subscription_and_tenant(
    deployment, monkeypatch
):
    from azure import identity

    credential = Mock()
    monkeypatch.setattr(identity, "AzureCliCredential", credential)
    common.operator_credential()
    credential.assert_called_once_with(tenant_id=TENANT, subscription=SUBSCRIPTION)


def test_client_state_bootstrap_uses_only_in_memory_key_vault_values(
    operations, monkeypatch, capsys
):
    from azure.core.exceptions import ResourceNotFoundError
    from azure.keyvault import secrets as vault_secrets
    from types import SimpleNamespace

    generated = "synthetic-notification-verification-value-never-printed"
    client = Mock()
    client.get_secret.side_effect = ResourceNotFoundError("missing")
    client.set_secret.return_value = SimpleNamespace(value=generated)
    factory = Mock()
    factory.return_value.__enter__ = Mock(return_value=client)
    factory.return_value.__exit__ = Mock(return_value=False)
    credential = Mock()
    credential.__enter__ = Mock(return_value=credential)
    credential.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(operations, "operator_credential", lambda: credential)
    monkeypatch.setattr(vault_secrets, "SecretClient", factory)
    monkeypatch.setattr(operations.secrets, "token_urlsafe", lambda length: generated)
    operations.bootstrap_secret("synthetic-vault")
    client.set_secret.assert_called_once_with(
        "teams-transcripts-client-state", generated
    )
    assert generated not in capsys.readouterr().out
