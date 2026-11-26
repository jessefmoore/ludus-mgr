"""Unit tests for `app.services.proxmox.ProxmoxClient`.

Uses pytest-httpx's `httpx_mock` fixture to intercept outbound requests -
no real Proxmox host involved.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pytest_httpx import HTTPXMock

from app.services.proxmox import ProxmoxClient, ProxmoxError

BASE_URL = "https://proxmox.test:8006"
API_TOKEN = "root@pam!ludus-mgr=aaaa-bbbb-cccc"


@pytest.fixture
def client() -> Iterator[ProxmoxClient]:
    c = ProxmoxClient(url=BASE_URL, api_token=API_TOKEN, verify_tls=False)
    try:
        yield c
    finally:
        c.close()


def test_sends_pve_api_token_header(client: ProxmoxClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE_URL}/api2/json/nodes",
        json={"data": [{"node": "pve"}]},
    )
    client.list_nodes()
    sent = httpx_mock.get_request()
    assert sent is not None
    assert sent.headers["Authorization"] == f"PVEAPIToken={API_TOKEN}"


def test_list_nodes(client: ProxmoxClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE_URL}/api2/json/nodes",
        json={"data": [{"node": "pve"}, {"node": "pve2"}]},
    )
    nodes = client.list_nodes()
    assert [n["node"] for n in nodes] == ["pve", "pve2"]


def test_resolve_node_auto_detects_single_node(
    client: ProxmoxClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        method="GET", url=f"{BASE_URL}/api2/json/nodes", json={"data": [{"node": "pve"}]}
    )
    assert client.resolve_node(None) == "pve"


def test_resolve_node_explicit_skips_discovery(client: ProxmoxClient) -> None:
    # No httpx_mock response registered - a call would raise if this hit the network.
    assert client.resolve_node("pve-configured") == "pve-configured"


def test_resolve_node_ambiguous_without_config_raises(
    client: ProxmoxClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE_URL}/api2/json/nodes",
        json={"data": [{"node": "pve"}, {"node": "pve2"}]},
    )
    with pytest.raises(ProxmoxError, match="Multiple Proxmox nodes"):
        client.resolve_node(None)


def test_resolve_node_no_nodes_raises(client: ProxmoxClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", url=f"{BASE_URL}/api2/json/nodes", json={"data": []})
    with pytest.raises(ProxmoxError, match="no nodes"):
        client.resolve_node(None)


def test_host_stats_filters_non_image_storage(
    client: ProxmoxClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE_URL}/api2/json/nodes/pve/status",
        json={
            "data": {
                "cpuinfo": {"cpus": 16},
                "cpu": 0.0532,
                "memory": {"total": 100_000_000_000, "used": 27_000_000_000},
            }
        },
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE_URL}/api2/json/nodes/pve/storage",
        json={
            "data": [
                {"storage": "local", "content": "iso,vztmpl", "total": 50, "used": 10},
                {"storage": "local-lvm", "content": "images,rootdir", "total": 900, "used": 500},
            ]
        },
    )
    stats = client.host_stats("pve")
    assert stats.node == "pve"
    assert stats.cpu_count == 16
    assert stats.cpu_fraction == 0.0532
    assert stats.mem_total_bytes == 100_000_000_000
    assert stats.mem_used_bytes == 27_000_000_000
    # "local" (iso-only) excluded; only local-lvm counts.
    assert stats.storage_total_bytes == 900
    assert stats.storage_used_bytes == 500


def test_host_stats_sums_multiple_image_storages(
    client: ProxmoxClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE_URL}/api2/json/nodes/pve/status",
        json={"data": {"cpuinfo": {"cpus": 8}, "cpu": 0, "memory": {"total": 1, "used": 0}}},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE_URL}/api2/json/nodes/pve/storage",
        json={
            "data": [
                {"storage": "a", "content": "images", "total": 100, "used": 10},
                {"storage": "b", "content": "images", "total": 200, "used": 20},
            ]
        },
    )
    stats = client.host_stats("pve")
    assert stats.storage_total_bytes == 300
    assert stats.storage_used_bytes == 30


def test_non_200_raises_proxmox_error(client: ProxmoxClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url=f"{BASE_URL}/api2/json/nodes", status_code=401, text="Unauthorized"
    )
    with pytest.raises(ProxmoxError):
        client.list_nodes()
