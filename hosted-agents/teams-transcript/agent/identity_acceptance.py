"""Deployment-only data-plane probe, executed with the actual hosted agent identity."""

import json
import logging
from datetime import datetime, timedelta, timezone

from azure.cosmos.exceptions import CosmosHttpResponseError


def verify_identity(reader, neighbors):
    # Even an empty corpus must allow an authenticated query in the Teams database.
    list(
        reader.container.query_items(
            "SELECT TOP 1 VALUE c.id FROM c",
            partition_key=[reader.tenant, reader.organizer],
            enable_cross_partition_query=True,
        )
    )
    expires = datetime.now(timezone.utc) + timedelta(seconds=30)
    try:
        reader.container.create_item(
            {
                "id": "deployment-reader-denial-probe",
                "tenantId": reader.tenant,
                "organizerId": reader.organizer,
                "meetingKey": "0" * 64,
                "type": "deployment_probe",
                "expiresAt": expires.isoformat(),
                "ttl": 30,
            }
        )
    except CosmosHttpResponseError as exc:
        if exc.status_code != 403:
            raise RuntimeError(
                "Teams identity write-denial probe unavailable"
            ) from None
    else:
        raise RuntimeError(
            "Teams identity unexpectedly permits writes; do not promote this version"
        )
    denied = 0
    for database, container in neighbors:
        target = reader.client.get_database_client(database).get_container_client(
            container
        )
        try:
            list(
                target.query_items(
                    "SELECT TOP 1 VALUE c.id FROM c", enable_cross_partition_query=True
                )
            )
        except CosmosHttpResponseError as exc:
            if exc.status_code != 403:
                raise RuntimeError(
                    "Neighboring database read-denial probe unavailable"
                ) from None
            denied += 1
        else:
            raise RuntimeError(
                "Teams identity can read a neighboring database; do not promote this version"
            )
    result = {
        "teamsRead": 200,
        "teamsWrite": 403,
        "neighborReadsDenied": denied,
        "neighborsChecked": len(neighbors),
    }
    logging.info(
        "teams_transcript_identity_acceptance %s", json.dumps(result, sort_keys=True)
    )
    print(
        "teams_transcript_identity_acceptance " + json.dumps(result, sort_keys=True),
        flush=True,
    )
    return result
