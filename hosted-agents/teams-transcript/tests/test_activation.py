"""Application automation may build/grant, but promotion needs both delegated users."""

import base64
import importlib.util
import json
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

ENROLLMENT = json.loads(
    (Path(__file__).resolve().parent / "fixtures/enrollment.json").read_text()
)


@pytest.fixture
def activation(monkeypatch):
    harness = Path(__file__).resolve().parents[1] / "harness"
    monkeypatch.syspath_prepend(str(harness))
    spec = importlib.util.spec_from_file_location(
        "teams_activation", harness / "activate.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.sys, "argv", ["activate.py", "7"])
    monkeypatch.setattr(module, "token", Mock(return_value="application-token"))
    monkeypatch.setattr(
        module,
        "shared",
        Mock(
            return_value={
                "cosmosAccountName": "test",
                "resourceGroup": "test-rg",
                "cosmosEndpoint": "https://synthetic.documents.azure.com/",
                "teamsTranscriptsDatabaseName": "teams-transcripts",
                "teamsTranscriptsContainerName": "transcripts",
            }
        ),
    )
    monkeypatch.setattr(module, "enrollment", Mock(return_value=ENROLLMENT))
    monkeypatch.setattr(module, "neighbors", Mock(return_value=[]))
    monkeypatch.setattr(
        module,
        "az",
        Mock(
            return_value=[
                {
                    "principalId": "agent-principal",
                    "scope": "/dbs/teams-transcripts",
                    "roleDefinitionId": "/00000000-0000-0000-0000-000000000001",
                }
            ]
        ),
    )
    monkeypatch.setattr(module, "probe", Mock())
    rule = {"type": "FixedRatio", "agent_version": "7", "traffic_percentage": 100}

    def request(method, path, *args):
        if "/versions/" in path:
            return {
                "instance_identity": {"principal_id": "agent-principal"},
                "definition": {
                    "environment_variables": {
                        "TEAMS_ORGANIZERS": json.dumps(module.enrollment()),
                        "TEAMS_IDENTITY_ACCEPTANCE": "true",
                        "COSMOS_ENDPOINT": "https://synthetic.documents.azure.com/",
                        "TEAMS_DATABASE": "teams-transcripts",
                        "TEAMS_CONTAINER": "transcripts",
                        "TEAMS_IDENTITY_PROBE_NEIGHBORS": "[]",
                    }
                },
            }
        return {
            "agent_endpoint": {"version_selector": {"version_selection_rules": [rule]}}
        }

    monkeypatch.setattr(module, "request", Mock(side_effect=request))
    return module


def bearer(oid, **overrides):
    claims = dict(
        oid=oid,
        tid=ENROLLMENT["tenantId"],
        scp="user_impersonation",
        exp=time.time() + 600,
    )
    claims.update(overrides)
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "test." + encoded + ".signature"


@pytest.mark.parametrize(
    "problem",
    ["missing", "only_one", "application", "expired", "wrong_tenant", "wrong_user"],
)
def test_unverified_delegated_acceptance_never_reads_or_promotes(
    activation, monkeypatch, problem
):
    module = activation
    tokens = {oid: bearer(oid) for oid in ENROLLMENT["organizerIds"]}
    first = ENROLLMENT["organizerIds"][0]
    if problem == "missing":
        monkeypatch.delenv("TEAMS_DELEGATED_ACCEPTANCE_TOKENS", raising=False)
    else:
        if problem == "only_one":
            tokens.pop(first)
        elif problem == "application":
            tokens[first] = bearer(first, scp=None)
        elif problem == "expired":
            tokens[first] = bearer(first, exp=0)
        elif problem == "wrong_tenant":
            tokens[first] = bearer(first, tid="foreign-tenant")
        elif problem == "wrong_user":
            tokens[first] = bearer("foreign-user")
        monkeypatch.setenv("TEAMS_DELEGATED_ACCEPTANCE_TOKENS", json.dumps(tokens))
    with pytest.raises(SystemExit, match="endpoint not promoted"):
        module.main()
    if problem != "missing":
        module.token.assert_not_called()
    module.request.assert_not_called()
    module.probe.assert_not_called()


def test_promotion_requires_both_authenticated_read_probes(activation, monkeypatch):
    tokens = {oid: bearer(oid) for oid in ENROLLMENT["organizerIds"]}
    monkeypatch.setenv("TEAMS_DELEGATED_ACCEPTANCE_TOKENS", json.dumps(tokens))
    activation.main()
    assert {call.args[1] for call in activation.probe.call_args_list} == set(
        tokens.values()
    )
    assert len(activation.probe.call_args_list) == 2
    assert all(
        call.args[0] == "7" and not call.kwargs
        for call in activation.probe.call_args_list
    )
    assert (
        sum(call.args[0] == "PATCH" for call in activation.request.call_args_list) == 1
    )


def test_failed_delegated_read_cannot_promote(activation, monkeypatch):
    monkeypatch.setenv(
        "TEAMS_DELEGATED_ACCEPTANCE_TOKENS",
        json.dumps({oid: bearer(oid) for oid in ENROLLMENT["organizerIds"]}),
    )
    activation.probe.side_effect = [None, SystemExit("endpoint not promoted")]
    with pytest.raises(SystemExit):
        activation.main()
    assert not any(
        call.args[0] == "PATCH" for call in activation.request.call_args_list
    )


def test_initial_single_organizer_uses_signed_in_cli_delegated_token(
    activation, monkeypatch
):
    first = ENROLLMENT["organizerIds"][0]
    monkeypatch.delenv("TEAMS_DELEGATED_ACCEPTANCE_TOKENS", raising=False)
    monkeypatch.setattr(
        activation,
        "enrollment",
        lambda: {"tenantId": ENROLLMENT["tenantId"], "organizerIds": [first]},
    )
    delegated = bearer(first)
    activation.token.return_value = delegated
    activation.main()
    activation.probe.assert_called_once_with("7", delegated)
    assert any(call.args[0] == "PATCH" for call in activation.request.call_args_list)


@pytest.mark.parametrize(
    "scope,role",
    [
        ("/", "/00000000-0000-0000-0000-000000000001"),
        ("/dbs/teams-transcripts", "/00000000-0000-0000-0000-000000000002"),
        ("/dbs/unrelated", "/00000000-0000-0000-0000-000000000001"),
    ],
)
def test_excess_or_wrong_database_grant_blocks_promotion(
    activation, monkeypatch, scope, role
):
    monkeypatch.setenv(
        "TEAMS_DELEGATED_ACCEPTANCE_TOKENS",
        json.dumps({oid: bearer(oid) for oid in ENROLLMENT["organizerIds"]}),
    )
    activation.az.return_value = [
        {"principalId": "agent-principal", "scope": scope, "roleDefinitionId": role}
    ]
    with pytest.raises(SystemExit, match="Exactly one"):
        activation.main()
    activation.probe.assert_not_called()
    assert not any(
        call.args[0] == "PATCH" for call in activation.request.call_args_list
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("TEAMS_ORGANIZERS", "{}"),
        ("TEAMS_IDENTITY_ACCEPTANCE", "false"),
        ("TEAMS_DATABASE", "different-database"),
        ("TEAMS_IDENTITY_PROBE_NEIGHBORS", '[ ["unrelated", "data"] ]'),
    ],
)
def test_stale_or_unverified_candidate_context_cannot_promote(
    activation, monkeypatch, field, value
):
    monkeypatch.setenv(
        "TEAMS_DELEGATED_ACCEPTANCE_TOKENS",
        json.dumps({oid: bearer(oid) for oid in ENROLLMENT["organizerIds"]}),
    )
    original = activation.request.side_effect

    def changed(method, path, *args):
        result = original(method, path, *args)
        if "/versions/" in path:
            result["definition"]["environment_variables"][field] = value
        return result

    activation.request.side_effect = changed
    with pytest.raises(SystemExit, match="Candidate configuration differs"):
        activation.main()
    activation.probe.assert_not_called()
    assert not any(
        call.args[0] == "PATCH" for call in activation.request.call_args_list
    )
