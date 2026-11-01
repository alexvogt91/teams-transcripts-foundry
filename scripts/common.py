"""Portable deployment context. State and config contain identifiers, never credentials."""

import json
import os
import re
import subprocess
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".local/state.json"


class DeploymentError(ValueError):
    """A local validation message that is safe to show without remote response details."""


def config():
    path = Path(os.environ.get("TEAMS_CONFIG", ROOT / "configuration/deployment.json"))
    values = json.loads(path.read_text())
    values["subscriptionId"] = str(UUID(values["subscriptionId"]))
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,90}", values["resourceGroup"]):
        raise DeploymentError(
            "Use a dedicated resource group name with letters, digits, - or _"
        )
    return values


def enrollment():
    values = json.loads((ROOT / config()["organizersFile"]).read_text())
    tenant = str(UUID(values["tenantId"]))
    ids = [str(UUID(oid)) for oid in values["organizerIds"]]
    if not ids or len(set(ids)) != len(ids):
        raise DeploymentError("Supply distinct organizer Entra object IDs")
    return {"tenantId": tenant, "organizerIds": ids}


def az(*args):
    result = subprocess.run(
        [
            "az",
            *map(str, args),
            "--subscription",
            config()["subscriptionId"],
            "-o",
            "json",
            "--only-show-errors",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(
            f"Azure operation failed: {' '.join(args[:2])}. Inspect the operation in Azure; output is suppressed to protect credentials."
        )
    return json.loads(result.stdout) if result.stdout.strip() else None


def state_exists():
    return STATE.exists()


def state():
    values = json.loads(STATE.read_text())
    if (
        values["subscriptionId"] != config()["subscriptionId"]
        or values["resourceGroup"] != config()["resourceGroup"]
    ):
        raise DeploymentError(
            "Deployment state belongs to a different subscription/resource group"
        )
    return values


def save_state(values):
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(values, indent=2) + "\n")


def project_endpoint():
    return state()["foundryProjectEndpoint"]


def operator_credential():
    from azure.identity import AzureCliCredential

    return AzureCliCredential(
        tenant_id=enrollment()["tenantId"], subscription=config()["subscriptionId"]
    )
