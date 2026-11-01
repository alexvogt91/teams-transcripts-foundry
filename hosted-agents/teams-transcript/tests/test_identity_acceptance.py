from unittest.mock import Mock

import pytest
from azure.cosmos.exceptions import CosmosHttpResponseError

from agent.identity_acceptance import verify_identity


def source(write_status=403, neighbor_status=403):
    reader = Mock(tenant="tenant", organizer="organizer_a")
    reader.container.query_items.return_value = []
    if write_status:
        reader.container.create_item.side_effect = CosmosHttpResponseError(
            status_code=write_status
        )
    target = (
        reader.client.get_database_client.return_value.get_container_client.return_value
    )
    target.query_items.return_value = []
    if neighbor_status:
        target.query_items.side_effect = CosmosHttpResponseError(
            status_code=neighbor_status
        )
    return reader


def test_probe_proves_own_reads_forbidden_writes_and_every_neighbor_read():
    reader = source()
    neighbors = [
        ("neighbor-a", "items-a"),
        ("neighbor-b", "items-b"),
        ("neighbor-c", "items-c"),
    ]
    result = verify_identity(reader, neighbors)
    assert result == {
        "teamsRead": 200,
        "teamsWrite": 403,
        "neighborReadsDenied": 3,
        "neighborsChecked": 3,
    }
    assert reader.client.get_database_client.call_count == 3


@pytest.mark.parametrize("write,neighbor", [(0, 403), (401, 403), (403, 0), (403, 401)])
def test_probe_fails_closed_on_excess_access_or_missing_auth(write, neighbor):
    with pytest.raises(RuntimeError):
        verify_identity(source(write, neighbor), [("neighbor-b", "items-b")])
