"""Promote exactly the candidate version after its database-only reader grant."""

import base64
import json
import os
import sys
import time
from deploy import AGENT_NAME, az, enrollment, neighbors, request, shared, token
from probe import probe


def main():
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        raise SystemExit("Usage: activate.py <candidate-version>")
    version = sys.argv[1]
    enrolled = enrollment()
    try:
        if os.environ.get("TEAMS_DELEGATED_ACCEPTANCE_TOKENS"):
            delegated = json.loads(os.environ["TEAMS_DELEGATED_ACCEPTANCE_TOKENS"])
        else:
            bearer = token()
            claims = json.loads(base64.urlsafe_b64decode(bearer.split(".")[1] + "=="))
            delegated = {claims["oid"]: bearer}
        if set(delegated) != set(enrolled["organizerIds"]):
            raise ValueError
        for organizer, bearer in delegated.items():
            claims = json.loads(base64.urlsafe_b64decode(bearer.split(".")[1] + "=="))
            if (
                claims.get("tid") != enrolled["tenantId"]
                or claims.get("oid") != organizer
                or not claims.get("scp")
                or claims.get("exp", 0) <= time.time()
            ):
                raise ValueError
    except Exception:
        raise SystemExit(
            "Fresh delegated Foundry tokens for every enrolled organizer are required; endpoint not promoted"
        ) from None
    # Token claims are only a preflight; the Foundry endpoint verifies each token.
    access_token = token()
    info = request("GET", f"agents/{AGENT_NAME}/versions/{version}", access_token)
    principal = (info.get("instance_identity") or {}).get("principal_id")
    values = shared()
    # Inspect only configuration fields in memory; never print version definitions.
    environment = (info.get("definition") or {}).get("environment_variables") or {}
    try:
        if (
            json.loads(environment["TEAMS_ORGANIZERS"]) != enrolled
            or environment["TEAMS_IDENTITY_ACCEPTANCE"] != "true"
        ):
            raise ValueError
        for setting, name in [
            ("COSMOS_ENDPOINT", "cosmosEndpoint"),
            ("TEAMS_DATABASE", "teamsTranscriptsDatabaseName"),
            ("TEAMS_CONTAINER", "teamsTranscriptsContainerName"),
        ]:
            if environment[setting] != values[name]:
                raise ValueError
        if sorted(json.loads(environment["TEAMS_IDENTITY_PROBE_NEIGHBORS"])) != sorted(
            neighbors(values)
        ):
            raise ValueError
    except Exception:
        raise SystemExit(
            "Candidate configuration differs from current enrollment/corpus/isolation inventory; create a new candidate before promotion"
        ) from None
    assignments = az(
        "cosmosdb",
        "sql",
        "role",
        "assignment",
        "list",
        "--account-name",
        values["cosmosAccountName"],
        "--resource-group",
        values["resourceGroup"],
    )
    grants = [a for a in assignments if a["principalId"] == principal]
    if (
        len(grants) != 1
        or not grants[0]["scope"].endswith("/dbs/teams-transcripts")
        or not grants[0]["roleDefinitionId"].endswith(
            "/00000000-0000-0000-0000-000000000001"
        )
    ):
        raise SystemExit(
            "Exactly one transcript database reader grant is required before promotion"
        )
    # Native reads use delegated caller tokens; the hosted identity performs its own data-plane denial probe.
    for bearer in delegated.values():
        probe(version, bearer)
    rule = {"type": "FixedRatio", "agent_version": version, "traffic_percentage": 100}
    request(
        "PATCH",
        f"agents/{AGENT_NAME}",
        access_token,
        {"agent_endpoint": {"version_selector": {"version_selection_rules": [rule]}}},
    )
    routed = request("GET", f"agents/{AGENT_NAME}", access_token)
    if (routed.get("agent_endpoint") or {}).get("version_selector", {}).get(
        "version_selection_rules"
    ) != [rule]:
        raise SystemExit("Teams endpoint selector read-back failed")
    print(f"Teams endpoint promoted to verified candidate {version}")


if __name__ == "__main__":
    main()
