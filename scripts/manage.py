"""Staged one-subscription deployment. No action runs unless explicitly selected."""

import argparse
import json
import logging
import shutil
import subprocess
import sys
from urllib.parse import quote
from uuid import UUID

from common import (
    ROOT,
    DeploymentError,
    az,
    config,
    enrollment,
    operator_credential,
    save_state,
    state,
    state_exists,
)

DEPLOYMENT = "teams-transcripts"
GRAPH_APP = "00000003-0000-0000-c000-000000000000"
GRAPH_CHANGE_TRACKING_APP = "0bf30f3b-4a52-48df-9a82-234910c4a086"
TRANSCRIPT_ROLE = "a4a80d8d-d283-4bd8-8504-555ec3870630"


def preflight():
    settings, enrolled = config(), enrollment()
    account = az("account", "show")
    if account["tenantId"].lower() != enrolled["tenantId"]:
        raise DeploymentError("Subscription and organizer tenant differ")
    operator = az("ad", "signed-in-user", "show")["id"]
    if operator not in enrolled["organizerIds"]:
        raise DeploymentError(
            "Start with the signed-in deployment operator enrolled as an organizer"
        )
    for oid in enrolled["organizerIds"]:
        az("ad", "user", "show", "--id", oid, "--query", "id")
    models = az(
        "cognitiveservices", "model", "list", "--location", settings["location"]
    )
    for name, version, sku, capacity in [
        (
            settings["chatModelName"],
            settings["chatModelVersion"],
            settings["chatModelSku"],
            settings["chatModelCapacity"],
        ),
        (
            "text-embedding-3-large",
            settings["embeddingModelVersion"],
            settings["embeddingModelSku"],
            settings["embeddingModelCapacity"],
        ),
    ]:
        found = [
            m
            for m in models
            if m.get("model", m).get("name") == name
            and m.get("model", m).get("version") == version
        ]
        if not found or not any(
            s.get("name") == sku for m in found for s in m.get("skus", [])
        ):
            raise DeploymentError(
                f"Model/version/SKU unavailable in {settings['location']}: {name}"
            )
        if type(capacity) is not int or capacity < 1:
            raise DeploymentError("Model capacities must be positive integers")
    locations = az("functionapp", "list-flexconsumption-locations")
    normalized = settings["location"].replace(" ", "").lower()
    if not any(
        normalized
        == str(item.get("name", item.get("displayName", ""))).replace(" ", "").lower()
        for item in locations
    ):
        raise DeploymentError("Region does not advertise Flex Consumption")
    if settings.get("enableFreeTier"):
        accounts = az("cosmosdb", "list")
        own_name = state().get("cosmosAccountName") if state_exists() else None
        if any(
            a.get("enableFreeTier", a.get("properties", {}).get("enableFreeTier"))
            and a["name"] != own_name
            for a in accounts
        ):
            raise DeploymentError(
                "This subscription already has a Cosmos free-tier account"
            )
    print(
        "Tenant, enrolled operator, model/version/SKU and Flex region verified. Review hosted-agent regional availability and quota in Foundry before provisioning."
    )
    return str(UUID(operator))


def deploy(*, deploy_function=False, mode="disabled", ingestion=False):
    settings = config()
    if mode not in ("disabled", "enabled", "rollback"):
        raise DeploymentError("Unsupported reconciliation mode")
    if ingestion and mode != "enabled":
        raise DeploymentError("Ingestion requires enabled reconciliation")
    previous = state() if state_exists() else {}
    if previous.get("deployFunction") and not deploy_function:
        raise DeploymentError(
            "Function is already deployed; use an explicit Function or rollback stage"
        )
    if previous and (
        previous.get("enableFreeTier", settings["enableFreeTier"])
        != settings["enableFreeTier"]
        or previous.get("location", settings["location"]) != settings["location"]
    ):
        raise DeploymentError(
            "Account free-tier selection and deployment region cannot change in place"
        )
    operator = previous.get("operatorPrincipalId") or preflight()
    parameters = {
        key: settings[key]
        for key in [
            "location",
            "chatModelName",
            "chatModelVersion",
            "chatDeploymentName",
            "chatModelSku",
            "chatModelCapacity",
            "embeddingModelVersion",
            "embeddingModelSku",
            "embeddingModelCapacity",
            "enableFreeTier",
            "alertEmail",
        ]
    }
    parameters.update(
        organizers=enrollment(),
        operatorPrincipalId=operator,
        deployFunction=deploy_function,
        reconciliationMode=mode,
        ingestionEnabled=ingestion,
        appClientId=previous.get("appClientId", ""),
        appPrincipalId=previous.get("appServicePrincipalId", ""),
        graphChangeTrackingPrincipalId=previous.get(
            "graphChangeTrackingPrincipalId", ""
        ),
    )
    if deploy_function and not all(
        parameters[k]
        for k in ["appClientId", "appPrincipalId", "graphChangeTrackingPrincipalId"]
    ):
        raise DeploymentError(
            "Complete identity bootstrap before deploying the Function"
        )
    az(
        "group",
        "create",
        "--name",
        settings["resourceGroup"],
        "--location",
        settings["location"],
    )
    outputs = az(
        "deployment",
        "group",
        "create",
        "--name",
        DEPLOYMENT,
        "--resource-group",
        settings["resourceGroup"],
        "--template-file",
        str(ROOT / "infra/main.bicep"),
        "--parameters",
        str(ROOT / "infra/main.bicepparam"),
        "--parameters",
        *[
            f"{k}={json.dumps(v) if isinstance(v, (dict, bool)) else v}"
            for k, v in parameters.items()
        ],
        "--query",
        "properties.outputs",
    )
    values = {
        **previous,
        **{k: v["value"] for k, v in outputs.items()},
        "subscriptionId": settings["subscriptionId"],
        "resourceGroup": settings["resourceGroup"],
        "operatorPrincipalId": operator,
        "reconciliationMode": mode,
        "ingestionEnabled": ingestion,
        "deployFunction": deploy_function,
        "enableFreeTier": settings["enableFreeTier"],
        "location": settings["location"],
    }
    save_state(values)
    print(f"Infrastructure deployed; reconciliation={mode}, ingestion={ingestion}.")


def bootstrap_identity():
    """Create a secretless Graph application and trust the dedicated Function identity."""
    values, enrolled = state(), enrollment()
    display_name = "teams-transcripts-" + values["functionIdentityClientId"]
    apps = az("ad", "app", "list", "--display-name", display_name)
    if len(apps) > 1:
        raise DeploymentError("Ambiguous application inventory")
    app = (
        apps[0]
        if apps
        else az(
            "ad",
            "app",
            "create",
            "--display-name",
            display_name,
            "--sign-in-audience",
            "AzureADMyOrg",
            "--required-resource-accesses",
            json.dumps(
                [
                    {
                        "resourceAppId": GRAPH_APP,
                        "resourceAccess": [{"id": TRANSCRIPT_ROLE, "type": "Role"}],
                    }
                ]
            ),
        )
    )
    principal = az("ad", "sp", "list", "--filter", f"appId eq '{app['appId']}'")
    principal = (
        principal[0] if principal else az("ad", "sp", "create", "--id", app["appId"])
    )
    federated = {
        "name": "function-managed-identity",
        "issuer": f"https://login.microsoftonline.com/{enrolled['tenantId']}/v2.0",
        "subject": values["functionIdentityPrincipalId"],
        "audiences": ["api://AzureADTokenExchange"],
    }
    credentials = az("ad", "app", "federated-credential", "list", "--id", app["id"])
    existing = next((c for c in credentials if c["name"] == federated["name"]), None)
    if existing:
        if any(existing[k] != federated[k] for k in ["issuer", "subject", "audiences"]):
            raise DeploymentError(
                "Existing federation differs; review it before proceeding"
            )
    else:
        az(
            "ad",
            "app",
            "federated-credential",
            "create",
            "--id",
            app["id"],
            "--parameters",
            json.dumps(federated),
        )
    tracking = az(
        "ad", "sp", "list", "--filter", f"appId eq '{GRAPH_CHANGE_TRACKING_APP}'"
    )
    tracking = (
        tracking[0]
        if tracking
        else az("ad", "sp", "create", "--id", GRAPH_CHANGE_TRACKING_APP)
    )
    values.update(
        appClientId=app["appId"],
        appServicePrincipalId=principal["id"],
        graphChangeTrackingPrincipalId=tracking["id"],
    )
    save_state(values)
    # Secretless identity recorded before reapplying the hub sender assignments.
    deploy()
    sys.path.insert(0, str(ROOT / "scripts/teams-transcripts"))
    from operations import bootstrap_secret

    bootstrap_secret(state()["keyVaultName"])
    bootstrap_embedding()
    print(
        "Federation and dedicated secrets ready. Grant Graph admin consent separately; no consent was granted by this command."
    )


def bootstrap_embedding():
    from azure.keyvault.secrets import SecretClient

    values = state()
    key = az(
        "cognitiveservices",
        "account",
        "keys",
        "list",
        "--name",
        values["foundryAccountName"],
        "--resource-group",
        values["resourceGroup"],
        "--query",
        "key1",
    )
    if not isinstance(key, str) or not key or key.strip() != key:
        raise DeploymentError("Embedding credential invalid")
    with (
        operator_credential() as credential,
        SecretClient(
            vault_url=f"https://{values['keyVaultName']}.vault.azure.net",
            credential=credential,
            logging_enable=False,
        ) as client,
    ):
        client.set_secret("teams-transcripts-embedding-key", key)
        if client.get_secret("teams-transcripts-embedding-key").value != key:
            raise DeploymentError("Embedding secret read-back failed")
    print("Embedding credential stored and verified in Key Vault without a local file.")


def package_function():
    """Linux-only packaging prevents publishing macOS/ARM extension wheels to Flex."""
    if sys.platform != "linux" or __import__("platform").machine() not in (
        "x86_64",
        "AMD64",
    ):
        raise DeploymentError(
            "Package on Linux/amd64 (use the documented Docker command)"
        )
    package = ROOT / ".local/function-package"
    if package.exists():
        shutil.rmtree(package)
    package.mkdir(parents=True)
    source = ROOT / "functions/teams-transcripts"
    for path in [
        *source.glob("*.py"),
        source / "host.json",
        source / "requirements.txt",
    ]:
        shutil.copy2(path, package / path.name)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--only-binary=:all:",
            "-r",
            str(source / "requirements.txt"),
            "--target",
            str(package / ".python_packages/lib/site-packages"),
        ],
        capture_output=True,
    )
    if result.returncode:
        raise DeploymentError("Function dependency packaging failed")
    shutil.make_archive(str(ROOT / ".local/teams-transcripts-function"), "zip", package)
    print(
        "Linux/amd64 Function package created in .local/teams-transcripts-function.zip"
    )


def publish_function():
    values = state()
    package = ROOT / ".local/teams-transcripts-function.zip"
    if not package.exists():
        raise DeploymentError("Package the Function first")
    az(
        "functionapp",
        "deployment",
        "source",
        "config-zip",
        "--name",
        values["functionAppName"],
        "--resource-group",
        values["resourceGroup"],
        "--src",
        str(package),
        "--build-remote",
        "false",
    )
    print(
        "Function package published. Collection remains at the configured staged settings."
    )


def grant_agent(version):
    sys.path.insert(0, str(ROOT / "hosted-agents/teams-transcript/harness"))
    from deploy import AGENT_NAME, request, token

    values = state()
    info = request(
        "GET", f"agents/{AGENT_NAME}/versions/{quote(version, safe='')}", token()
    )
    principal = str(UUID(info["instance_identity"]["principal_id"]))
    az(
        "deployment",
        "group",
        "create",
        "--name",
        "teams-transcripts-reader-" + version,
        "--resource-group",
        values["resourceGroup"],
        "--template-file",
        str(ROOT / "infra/cosmos-role.bicep"),
        "--parameters",
        str(ROOT / "infra/agent-reader.bicepparam"),
        "--parameters",
        f"cosmosAccountName={values['cosmosAccountName']}",
        f"principalId={principal}",
    )
    values.update(candidateVersion=version, candidatePrincipalId=principal)
    save_state(values)
    print("Candidate database reader grant applied. Verify before promotion.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=[
            "preflight",
            "provision",
            "bootstrap-identity",
            "bootstrap-embedding",
            "deploy-function",
            "package-function",
            "publish-function",
            "grant-agent",
            "enable-reconciliation",
            "enable-ingestion",
            "rollback",
            "disable",
        ],
    )
    parser.add_argument("--version")
    args = parser.parse_args()
    if args.action == "preflight":
        preflight()
    elif args.action == "provision":
        preflight()
        deploy()
    elif args.action == "bootstrap-identity":
        bootstrap_identity()
    elif args.action == "bootstrap-embedding":
        bootstrap_embedding()
    elif args.action == "deploy-function":
        deploy(deploy_function=True)
    elif args.action == "package-function":
        package_function()
    elif args.action == "publish-function":
        publish_function()
    elif args.action == "grant-agent":
        if not args.version or not args.version.isdigit():
            parser.error("--version <candidate-version> is required")
        grant_agent(args.version)
    else:
        sys.path.insert(0, str(ROOT / "scripts/teams-transcripts"))
        from operations import configure_ingestion

        modes = {
            "enable-reconciliation": ("enabled", "false"),
            "enable-ingestion": ("enabled", "true"),
            "rollback": ("rollback", "false"),
            "disable": ("disabled", "false"),
        }
        mode, enabled = modes[args.action]
        configure_ingestion(mode, enabled, state()["keyVaultName"])


if __name__ == "__main__":
    logging.getLogger("azure").setLevel(logging.CRITICAL)
    try:
        main()
    except DeploymentError as exc:
        raise SystemExit(str(exc)) from None
    except (Exception, KeyboardInterrupt):
        raise SystemExit(
            "Selected stage failed. Check local configuration and Azure operation status; credential-bearing errors are suppressed."
        ) from None
