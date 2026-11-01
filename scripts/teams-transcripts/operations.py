"""Secret bootstrap and read-only rollout checks; never grant Graph admin consent."""

import argparse
import json
import math
import secrets
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
TRANSCRIPT_ROLE_ID = "a4a80d8d-d283-4bd8-8504-555ec3870630"


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import az, enrollment, operator_credential, state  # noqa: E402


def bootstrap_secret(vault):
    """Generate/read the verification secret in memory; never persist its value locally."""
    import logging
    from azure.keyvault.secrets import SecretClient
    from azure.core.exceptions import ResourceNotFoundError

    logging.getLogger("azure").setLevel(logging.CRITICAL)
    with (
        operator_credential() as credential,
        SecretClient(
            vault_url=f"https://{vault}.vault.azure.net",
            credential=credential,
            logging_enable=False,
        ) as client,
    ):
        try:
            value = client.get_secret("teams-transcripts-client-state").value
        except ResourceNotFoundError:
            value = client.set_secret(
                "teams-transcripts-client-state", secrets.token_urlsafe(48)
            ).value
        if (
            not isinstance(value, str)
            or not 32 <= len(value) <= 128
            or value.strip() != value
        ):
            raise RuntimeError("clientState length/whitespace validation failed")
    print("clientState exists and passes length/whitespace validation")


def verify_consent(app_client_id):
    principal = az("ad", "sp", "show", "--id", app_client_id)
    graph = az("ad", "sp", "show", "--id", GRAPH_APP_ID)
    grants = az(
        "rest",
        "--method",
        "GET",
        "--url",
        f"https://graph.microsoft.com/v1.0/servicePrincipals/{principal['id']}/appRoleAssignments",
    )
    if not any(
        g["resourceId"] == graph["id"] and g["appRoleId"] == TRANSCRIPT_ROLE_ID
        for g in grants["value"]
    ):
        raise RuntimeError(
            "Admin consent is required for the transcript application: OnlineMeetingTranscript.Read.All"
        )
    print("Graph application admin consent verified")


def verify_archive(storage_name):
    account = az(
        "storage",
        "account",
        "show",
        "--name",
        storage_name,
        "--resource-group",
        state()["resourceGroup"],
    )
    assert account["allowSharedKeyAccess"] is False, (
        "Shared key access must be disabled"
    )
    assert account["allowBlobPublicAccess"] is False, (
        "Anonymous blob access must be disabled"
    )
    policy = az(
        "storage",
        "account",
        "management-policy",
        "show",
        "--account-name",
        storage_name,
        "--resource-group",
        state()["resourceGroup"],
    )
    rules = policy["policy"]["rules"]
    assert any(
        r["enabled"]
        and r["definition"]["filters"]["prefixMatch"] == ["events/", "quarantine/"]
        and r["definition"]["actions"]["baseBlob"]["delete"][
            "daysAfterCreationGreaterThan"
        ]
        == 7
        for r in rules
    ), "Seven-day event/quarantine lifecycle policy missing"
    print("Private Entra-only archive and seven-day lifecycle verified")


def verify_hub(app_principal_id, identity_principal_id):
    namespace = az(
        "eventhubs",
        "namespace",
        "show",
        "--resource-group",
        state()["resourceGroup"],
        "--name",
        state()["eventHubNamespaceName"],
    )
    assert namespace["sku"]["name"] == "Basic" and namespace["sku"]["capacity"] == 1
    hub = az(
        "eventhubs",
        "eventhub",
        "show",
        "--resource-group",
        state()["resourceGroup"],
        "--namespace-name",
        state()["eventHubNamespaceName"],
        "--name",
        state()["eventHubName"],
    )
    assert hub["partitionCount"] == 2
    assert hub["retentionDescription"]["retentionTimeInHours"] == 24
    graph_principal = az(
        "ad", "sp", "show", "--id", "0bf30f3b-4a52-48df-9a82-234910c4a086"
    )["id"]
    grants = az("role", "assignment", "list", "--scope", hub["id"])
    expected = {
        (graph_principal, "2b629674-e913-4c01-ae53-ef4638d8f975"),
        (app_principal_id, "2b629674-e913-4c01-ae53-ef4638d8f975"),
        (identity_principal_id, "a638d3c7-ab3a-418d-83e6-5f17a39d4fde"),
    }
    actual = {
        (g["principalId"], g["roleDefinitionId"].rsplit("/", 1)[1])
        for g in grants
        if g["scope"].lower() == hub["id"].lower()
    }
    assert expected <= actual, "Dedicated hub sender/receiver grants missing"
    print(
        "Basic namespace, dedicated two-partition hub, 24-hour retention and hub-scoped grants verified"
    )


def preflight_corpus():
    outputs = az(
        "deployment",
        "group",
        "show",
        "--resource-group",
        state()["resourceGroup"],
        "--name",
        "teams-transcripts",
        "--query",
        "properties.outputs",
    )
    required = [
        "cosmosAccountName",
        "cosmosEndpoint",
        "teamsTranscriptsDatabaseName",
        "teamsTranscriptsContainerName",
        "teamsTranscriptsRetentionDays",
    ]
    missing = [name for name in required if not outputs.get(name, {}).get("value")]
    if missing:
        raise RuntimeError(
            "Deployment must publish required outputs: " + ", ".join(missing)
        )
    values = {name: outputs[name]["value"] for name in required}
    if (
        values["teamsTranscriptsDatabaseName"] != "teams-transcripts"
        or values["teamsTranscriptsContainerName"] != "transcripts"
        or values["teamsTranscriptsRetentionDays"] != 7
    ):
        raise RuntimeError("Unexpected Teams corpus scope or retention")
    print("Teams corpus output contract verified")
    return values


def verify_heartbeat(now=None):
    """Optional post-deployment diagnostic; never a prerequisite for configuration."""
    result = az(
        "monitor",
        "app-insights",
        "query",
        "--app",
        state()["appInsightsName"],
        "--resource-group",
        state()["resourceGroup"],
        "--analytics-query",
        "traces | where timestamp > ago(45m) "
        '| where message startswith "teams_transcripts timer_heartbeat " '
        'or message startswith "teams_transcripts reconciliation_failure " '
        '| extend organizerId=tostring(parse_json(extract("teams_transcripts [^ ]+ (.*)", 1, message)).organizerId) '
        "| summarize arg_max(timestamp, message) by organizerId | project timestamp, message",
    )
    try:
        table = result["tables"][0]
        now = now or datetime.now(timezone.utc)
        expected = set(enrollment()["organizerIds"])
        healthy = set()
        prefix = "teams_transcripts timer_heartbeat "
        for values in table["rows"]:
            row = dict(zip((c["name"] for c in table["columns"]), values))
            message = row["message"]
            if not message.startswith(prefix):
                raise ValueError("latest_reconciliation_failed")
            dimensions = json.loads(message[len(prefix) :])
            organizer = dimensions["organizerId"]
            if organizer not in expected or organizer in healthy:
                raise ValueError("unexpected_organizer")
            heartbeat = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            hours = float(dimensions["expiryHours"])
            age = now - heartbeat
            if (
                heartbeat.tzinfo is None
                or not math.isfinite(hours)
                or not timedelta(0) <= age < timedelta(minutes=45)
                or hours - age.total_seconds() / 3600 < 24
            ):
                raise ValueError("unhealthy_subscription")
            healthy.add(organizer)
        if healthy != expected:
            raise ValueError("missing_organizer_heartbeat")
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        raise RuntimeError(
            "Subscription heartbeat check failed: expected a successful heartbeat for every enrolled organizer within 45 minutes, "
            "at least 24 hours of remaining expiry and no later reconciliation failure. "
            "Allow Function startup and telemetry ingestion, then inspect timer health."
        ) from None
    print("Fresh successful subscription heartbeat and >=24-hour expiry verified")


def configure_ingestion(mode, ingestion_enabled, vault):
    # Full Bicep reapplication preserves all settings and keeps collection staged.
    from manage import deploy

    enabled = ingestion_enabled.lower() == "true" and mode == "enabled"
    if enabled:
        preflight_corpus()
        verify_consent(state()["appClientId"])
    deploy(deploy_function=True, mode=mode, ingestion=enabled)
    az(
        "functionapp",
        "restart",
        "--name",
        state()["functionAppName"],
        "--resource-group",
        state()["resourceGroup"],
    )
    print(f"Teams mode {mode}; ingestion enabled {enabled}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=[
            "bootstrap-secret",
            "verify-consent",
            "verify-archive",
            "verify-hub",
            "preflight-corpus",
            "verify-heartbeat",
            "configure-ingestion",
        ],
    )
    parser.add_argument(
        "--mode", choices=["disabled", "enabled", "rollback"], default="disabled"
    )
    parser.add_argument(
        "--ingestion-enabled", choices=["true", "false"], default="false"
    )
    args = parser.parse_args()
    try:
        values = state()
        if args.action == "bootstrap-secret":
            bootstrap_secret(values["keyVaultName"])
        elif args.action == "verify-consent":
            verify_consent(values["appClientId"])
        elif args.action == "verify-archive":
            verify_archive(values["storageAccountName"])
        elif args.action == "verify-hub":
            verify_hub(
                values["appServicePrincipalId"], values["functionIdentityPrincipalId"]
            )
        elif args.action == "preflight-corpus":
            preflight_corpus()
        elif args.action == "verify-heartbeat":
            verify_heartbeat()
        else:
            configure_ingestion(
                args.mode, args.ingestion_enabled, values["keyVaultName"]
            )
    except Exception:
        raise SystemExit(
            "Operational check failed. Inspect configuration and Azure operation status; credentials and remote details are suppressed."
        ) from None
