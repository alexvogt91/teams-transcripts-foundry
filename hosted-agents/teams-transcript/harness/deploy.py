"""Create a portable immutable candidate; reader grant and verification precede promotion."""

import json
import logging
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from common import (
    az,
    enrollment,
    operator_credential,
    project_endpoint,
    save_state,
    state,
)

AGENT_NAME = "teams-transcript-agent"


def token():
    return az("account", "get-access-token", "--resource", "https://ai.azure.com")[
        "accessToken"
    ]


def request(method, path, access_token, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"{project_endpoint()}/{path}?api-version=v1",
        data=data,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/merge-patch+json"
            if method == "PATCH"
            else "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise SystemExit(
            f"Foundry {method} failed with HTTP {exc.code}; response suppressed"
        ) from None
    except Exception:
        raise SystemExit("Foundry operation unavailable; response suppressed") from None


def shared():
    values = state()
    required = [
        "cosmosAccountName",
        "cosmosEndpoint",
        "teamsTranscriptsDatabaseName",
        "teamsTranscriptsContainerName",
        "teamsTranscriptsRetentionDays",
    ]
    if (
        any(not values.get(k) for k in required)
        or values["teamsTranscriptsDatabaseName"] != "teams-transcripts"
        or values["teamsTranscriptsContainerName"] != "transcripts"
        or values["teamsTranscriptsRetentionDays"] != 7
    ):
        raise SystemExit("Portable corpus state is missing or incompatible")
    return values


def neighbors(values):
    databases = az(
        "cosmosdb",
        "sql",
        "database",
        "list",
        "--account-name",
        values["cosmosAccountName"],
        "--resource-group",
        values["resourceGroup"],
    )
    if values["teamsTranscriptsDatabaseName"] not in [d["name"] for d in databases]:
        raise SystemExit("Transcript database absent from inventory")
    result = []
    for database in databases:
        name = database["name"]
        if name == values["teamsTranscriptsDatabaseName"]:
            continue
        containers = az(
            "cosmosdb",
            "sql",
            "container",
            "list",
            "--account-name",
            values["cosmosAccountName"],
            "--resource-group",
            values["resourceGroup"],
            "--database-name",
            name,
        )
        result.extend([[name, c["name"]] for c in containers])
    return result


def main():
    if len(sys.argv) < 2:
        raise SystemExit("Usage: deploy.py <registry/image:tag> [--dry-run]")
    values = shared()
    if (
        not sys.argv[1].startswith(values["registryLoginServer"] + "/")
        or ":" not in sys.argv[1]
    ):
        raise SystemExit("Supply a tagged image from the dedicated registry")
    inventory = neighbors(values)
    if "--dry-run" in sys.argv:
        print(
            f"Candidate context validated; {len(inventory)} neighboring containers discovered; no credentials fetched or version created."
        )
        return
    from azure.keyvault.secrets import SecretClient

    logging.getLogger("azure").setLevel(logging.CRITICAL)
    with (
        operator_credential() as credential,
        SecretClient(
            vault_url=f"https://{values['keyVaultName']}.vault.azure.net",
            credential=credential,
            logging_enable=False,
        ) as vault,
    ):
        key = vault.get_secret("teams-transcripts-embedding-key").value
    if not isinstance(key, str) or not key or key.strip() != key:
        raise SystemExit("Embedding credential validation failed")
    body = {
        "definition": {
            "kind": "hosted",
            "protocol_versions": [{"protocol": "responses", "version": "2.0.0"}],
            "cpu": "0.5",
            "memory": "1Gi",
            "container_configuration": {"image": sys.argv[1]},
            "session_configuration": {"idle_timeout_seconds": 600},
            "environment_variables": {
                "FOUNDRY_PROJECT_ENDPOINT": values["foundryProjectEndpoint"],
                "MODEL_DEPLOYMENT_NAME": values["modelDeploymentName"],
                "COSMOS_ENDPOINT": values["cosmosEndpoint"],
                "TEAMS_DATABASE": values["teamsTranscriptsDatabaseName"],
                "TEAMS_CONTAINER": values["teamsTranscriptsContainerName"],
                "TEAMS_ORGANIZERS": json.dumps(enrollment()),
                "TEAMS_EMBEDDING_ENDPOINT": values["embeddingEndpoint"],
                "AZURE_AI_ACCOUNT_KEY": key,
                "TEAMS_IDENTITY_ACCEPTANCE": "true",
                "TEAMS_IDENTITY_PROBE_NEIGHBORS": json.dumps(inventory),
            },
        },
        "description": "Private transcript search and complete summaries with timestamp citations",
        "metadata": {"image": sys.argv[1]},
    }
    created = request("POST", f"agents/{AGENT_NAME}/versions", token(), body)
    version = created.get("version")
    if not version or not str(version).isdigit():
        raise SystemExit("Candidate version missing; endpoint not promoted")
    # Version definitions contain a credential. Persist only public identifiers.
    for _ in range(60):
        info = request("GET", f"agents/{AGENT_NAME}/versions/{version}", token())
        if info.get("status") == "active":
            principal = str(UUID(info["instance_identity"]["principal_id"]))
            values.update(candidateVersion=str(version), candidatePrincipalId=principal)
            save_state(values)
            print(
                json.dumps(
                    {
                        "candidateVersion": str(version),
                        "principalId": principal,
                        "promoted": False,
                    }
                )
            )
            return
        if info.get("status") in ("failed", "deleted"):
            raise SystemExit("Candidate failed; endpoint not promoted")
        time.sleep(5)
    raise SystemExit("Candidate activation timed out; endpoint not promoted")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit(
            "Candidate deployment failed; credentials and remote details suppressed"
        ) from None
