"""Host resource-capacity view for the dashboard.

Two ways to answer "how much CPU/RAM/storage is left on this host", in
preference order:

1. **Proxmox-sourced** (:func:`build_proxmox_capacity_view`): real, measured
   CPU load / RAM / storage pulled straight from the Proxmox host via
   :mod:`app.services.proxmox`. Total capacity comes from Proxmox itself, not
   manual configuration, so it can't drift out of sync with the hardware.
   Currently only wired for the ``default`` server (set ``PROXMOX_API_TOKEN``
   to enable it; see README).

2. **Ludus-range-sum fallback** (:func:`build_ranges_capacity_view`): manually
   configured capacity (env for ``default``, DB row for other servers) vs.
   real per-VM ``cpu``/``ram`` summed from Ludus's own live range list (``GET
   /range/all``) across every non-``DESTROYED`` range on the server -
   including ranges this app didn't create. Used for any server without a
   Proxmox token configured. An earlier version of this fallback summed only
   this app's own active/provisioning sessions using their lab templates'
   *declared* YAML cost, which silently understated real host load for any
   server with ranges from other tools/users - the common case once a server
   has been in use for a while.

``available = capacity - allocated`` in both cases (may go negative when
overcommitted; the router/UI surface that rather than clamping the truth).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.services.resources import RangeResources, compute_live_range_resources

if TYPE_CHECKING:
    from sqlalchemy.orm import Session as DBSession

    from app.core.config import Settings
    from app.services.proxmox import ProxmoxHostStats

logger = logging.getLogger(__name__)

_BYTES_PER_GIB = 1024**3


@dataclass(frozen=True)
class Allocation:
    """Summed resources across every live range Ludus reports for a server."""

    resources: RangeResources
    range_count: int


def compute_live_allocation(ranges: list[dict]) -> Allocation:
    """Sum real CPU/RAM/VM/range totals across every live range on the host.

    *ranges* is the raw list from ``LudusClient.range_list()`` (``GET
    /range/all``) for the target server - the caller fetches it so this
    module stays DB/HTTP-free and easy to test with plain dicts.
    """
    resources = compute_live_range_resources(ranges)
    return Allocation(resources=resources, range_count=resources.range_count)


def resolve_capacity(
    db: DBSession,
    settings: Settings,
    server: str,
) -> tuple[int | None, int | None]:
    """Return (cpu_capacity, ram_capacity_gb) for *server* (None = unset).

    The ``default`` server's capacity comes from env settings; DB-managed
    servers store theirs on the ``ludus_servers`` row.
    """
    if server == "default":
        return settings.ludus_default_cpu_capacity, settings.ludus_default_ram_capacity_gb

    from app.models.ludus_server import LudusServer

    row = db.query(LudusServer).filter(LudusServer.name == server).first()
    if row is None:
        return None, None
    return row.cpu_capacity, row.ram_capacity_gb


@dataclass(frozen=True)
class CapacityView:
    """Everything the dashboard needs for one server's capacity card."""

    server: str
    configured: bool
    # "proxmox" (real measured host stats) or "ranges" (manual capacity vs.
    # live Ludus range sum, the fallback for servers without a Proxmox token).
    source: str
    cpu_capacity: int | None
    ram_capacity_gb: float | None
    cpu_allocated: int
    ram_allocated_gb: float
    cpu_available: int | None
    ram_available_gb: float | None
    range_count: int
    # Only populated when source == "proxmox" - Ludus has no concept of host
    # disk capacity, so the "ranges" fallback can never report this.
    node: str | None = None
    storage_capacity_gb: float | None = None
    storage_used_gb: float | None = None
    storage_available_gb: float | None = None
    # VM-declared CPU/RAM committed across every live range (same figure the
    # "ranges" source uses as its primary number) - a planning companion to
    # the real-time Proxmox figures above: live load can sit near zero while
    # VMs that are merely powered on but idle still reserve this much, so
    # this is what "all VMs busy at once" would actually cost. Only
    # populated when source == "proxmox"; for the "ranges" source,
    # cpu_allocated/ram_allocated_gb already are this number.
    vm_count: int = 0
    committed_cpus: int | None = None
    committed_ram_gb: float | None = None


def build_ranges_capacity_view(
    db: DBSession,
    settings: Settings,
    server: str,
    ranges: list[dict],
) -> CapacityView:
    """Fallback capacity view: manual capacity vs. live Ludus range sum.

    *ranges* is the live range list fetched by the caller (see
    :func:`compute_live_allocation`). Used for any server without a Proxmox
    token configured (see :func:`build_proxmox_capacity_view`).
    """
    cpu_cap, ram_cap = resolve_capacity(db, settings, server)
    alloc = compute_live_allocation(ranges)
    cpu_used = alloc.resources.cpus
    ram_used = alloc.resources.ram_gb
    return CapacityView(
        server=server,
        configured=cpu_cap is not None or ram_cap is not None,
        source="ranges",
        cpu_capacity=cpu_cap,
        ram_capacity_gb=ram_cap,
        cpu_allocated=cpu_used,
        ram_allocated_gb=ram_used,
        cpu_available=(cpu_cap - cpu_used) if cpu_cap is not None else None,
        ram_available_gb=(ram_cap - ram_used) if ram_cap is not None else None,
        range_count=alloc.range_count,
    )


def build_proxmox_capacity_view(
    server: str, stats: ProxmoxHostStats, ranges: list[dict]
) -> CapacityView:
    """Capacity view from real, measured Proxmox host stats.

    CPU is reported by Proxmox as a live load fraction, not a count of
    reserved cores - ``cpu_allocated`` here is that fraction converted to a
    core-equivalent (``round(fraction * cpu_count)``) purely so it fits the
    same allocated-vs-capacity bar the RAM/storage figures use; it's a
    snapshot of current load, not a commitment total.

    *ranges* (Ludus's live range list) additionally drives
    ``committed_cpus``/``committed_ram_gb``/``vm_count`` - the VM-declared
    total if every existing VM were busy at once, for capacity planning
    independent of whatever the live load happens to be right now.
    """
    mem_total_gb = stats.mem_total_bytes / _BYTES_PER_GIB
    mem_used_gb = stats.mem_used_bytes / _BYTES_PER_GIB
    storage_total_gb = stats.storage_total_bytes / _BYTES_PER_GIB
    storage_used_gb = stats.storage_used_bytes / _BYTES_PER_GIB
    cpu_used = round(stats.cpu_fraction * stats.cpu_count)
    alloc = compute_live_allocation(ranges)
    return CapacityView(
        server=server,
        configured=True,
        source="proxmox",
        node=stats.node,
        cpu_capacity=stats.cpu_count,
        ram_capacity_gb=round(mem_total_gb, 2),
        cpu_allocated=cpu_used,
        ram_allocated_gb=round(mem_used_gb, 2),
        cpu_available=stats.cpu_count - cpu_used,
        ram_available_gb=round(mem_total_gb - mem_used_gb, 2),
        range_count=alloc.range_count,
        storage_capacity_gb=round(storage_total_gb, 2),
        storage_used_gb=round(storage_used_gb, 2),
        storage_available_gb=round(storage_total_gb - storage_used_gb, 2),
        vm_count=alloc.resources.vm_count,
        committed_cpus=alloc.resources.cpus,
        committed_ram_gb=alloc.resources.ram_gb,
    )


__all__ = [
    "Allocation",
    "CapacityView",
    "build_proxmox_capacity_view",
    "build_ranges_capacity_view",
    "compute_live_allocation",
    "resolve_capacity",
]
