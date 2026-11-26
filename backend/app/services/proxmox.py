"""Thin, read-only Proxmox VE API client for host resource stats.

Powers the dashboard's host-capacity card with real, measured CPU/RAM/
storage usage - a Ludus server tracks VMs, not host-level resource
consumption, so this talks to the underlying Proxmox host directly instead.

Deliberately read-only: every method here is a GET, and the expected
credential is a Proxmox API token scoped to the built-in ``PVEAuditor``
role (see README for setup steps) - never a full admin account. Nothing in
this module can create, modify, or delete anything on the Proxmox host.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from types import TracebackType

import httpx

logger = logging.getLogger(__name__)


class ProxmoxError(Exception):
    """Raised on any Proxmox API failure (bad auth, unreachable, bad response)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class ProxmoxHostStats:
    """Real, measured resource usage for one Proxmox node."""

    node: str
    cpu_count: int
    # Live load fraction (0.0-1.0+), as reported by Proxmox - a point-in-time
    # measurement, not a capacity commitment like the Ludus-range-based figure
    # this replaced.
    cpu_fraction: float
    mem_total_bytes: int
    mem_used_bytes: int
    # Summed across every storage whose `content` includes VM disk images
    # (skips ISO/template-only mounts) - the pool(s) that actually bound how
    # many more VMs can be created.
    storage_total_bytes: int
    storage_used_bytes: int


class ProxmoxClient:
    """Synchronous HTTP wrapper for the subset of the Proxmox API this app needs."""

    def __init__(
        self,
        url: str,
        api_token: str,
        verify_tls: bool = False,
        timeout: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=url.rstrip("/"),
            verify=verify_tls,
            timeout=timeout,
            headers={"Authorization": f"PVEAPIToken={api_token}"},
        )

    def __enter__(self) -> ProxmoxClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _get(self, path: str) -> object:
        try:
            response = self._client.get(path)
        except httpx.RequestError as exc:
            raise ProxmoxError(f"Proxmox unreachable: {exc}") from exc
        if response.status_code != 200:
            raise ProxmoxError(
                f"Proxmox {path} returned {response.status_code}: {response.text}",
                status_code=response.status_code,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ProxmoxError(f"Proxmox returned invalid JSON for {path}") from exc
        return body.get("data")

    def list_nodes(self) -> list[dict]:
        """``GET /nodes`` - every node in the cluster (one, for a single-host lab)."""
        data = self._get("/api2/json/nodes")
        return data if isinstance(data, list) else []

    def node_status(self, node: str) -> dict:
        """``GET /nodes/{node}/status`` - live cpu/memory for one node."""
        data = self._get(f"/api2/json/nodes/{node}/status")
        return data if isinstance(data, dict) else {}

    def node_storage(self, node: str) -> list[dict]:
        """``GET /nodes/{node}/storage`` - every configured storage on one node."""
        data = self._get(f"/api2/json/nodes/{node}/storage")
        return data if isinstance(data, list) else []

    def resolve_node(self, configured_node: str | None) -> str:
        """Return the node to query: *configured_node* if set, else auto-detect.

        Auto-detection only succeeds when the cluster has exactly one node -
        ambiguous otherwise, so callers must set ``PROXMOX_NODE`` explicitly
        for multi-node clusters.
        """
        if configured_node:
            return configured_node
        nodes = self.list_nodes()
        if len(nodes) == 1:
            return nodes[0]["node"]
        if not nodes:
            raise ProxmoxError("Proxmox reported no nodes")
        names = ", ".join(n.get("node", "?") for n in nodes)
        raise ProxmoxError(
            f"Multiple Proxmox nodes found ({names}); set PROXMOX_NODE to pick one"
        )

    def host_stats(self, configured_node: str | None = None) -> ProxmoxHostStats:
        """Assemble real CPU/RAM/storage usage for one node."""
        node = self.resolve_node(configured_node)
        status = self.node_status(node)
        cpuinfo = status.get("cpuinfo") or {}
        memory = status.get("memory") or {}

        storage_total = 0
        storage_used = 0
        for s in self.node_storage(node):
            content = s.get("content") or ""
            if "images" not in content and "rootdir" not in content:
                continue  # ISO/template/backup-only mounts don't bound VM capacity
            storage_total += int(s.get("total") or 0)
            storage_used += int(s.get("used") or 0)

        return ProxmoxHostStats(
            node=node,
            cpu_count=int(cpuinfo.get("cpus") or 0),
            cpu_fraction=float(status.get("cpu") or 0.0),
            mem_total_bytes=int(memory.get("total") or 0),
            mem_used_bytes=int(memory.get("used") or 0),
            storage_total_bytes=storage_total,
            storage_used_bytes=storage_used,
        )


__all__ = ["ProxmoxClient", "ProxmoxError", "ProxmoxHostStats"]
