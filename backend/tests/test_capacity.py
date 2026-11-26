"""Tests for the host-capacity service (app.services.capacity).

Allocation is real CPU/RAM summed from Ludus's own live range list (``GET
/range/all``) - every non-DESTROYED range on the target server, not just
ranges this app created. Capacity comes from env (default) or the
ludus_servers row (managed).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.db import Base
from app.models.ludus_server import LudusServer
from app.services import capacity as cap
from app.services.proxmox import ProxmoxHostStats
from app.services.resources import RangeResources


def _vm(cpu: int, ram: int) -> dict:
    return {"cpu": cpu, "ram": ram}


def _range(range_id: str, vms: list[dict], *, state: str = "SUCCESS") -> dict:
    return {"rangeID": range_id, "rangeState": state, "VMs": vms}


@pytest.fixture
def db_session() -> Iterator[OrmSession]:
    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _settings(**over) -> Settings:
    base = {
        "app_env": "testing",
        "app_secret_key": "unit-test-secret",
        "admin_email": "a@example.com",
        "admin_password": "super-secret-test-pw",
        "ludus_default_url": "https://ludus.test:8080",
        "ludus_default_api_key": "unit-test-api-key",
        "_env_file": None,
    }
    base.update(over)
    return Settings(**base)


def test_allocation_sums_live_vms_across_ranges() -> None:
    ranges = [
        _range("JM", [_vm(4, 8), _vm(2, 4)]),
        _range("team1", [_vm(1, 2)]),
    ]
    alloc = cap.compute_live_allocation(ranges)
    assert alloc.resources == RangeResources(cpus=7, ram_gb=14, vm_count=3, range_count=2)
    assert alloc.range_count == 2


def test_allocation_excludes_destroyed_ranges() -> None:
    ranges = [
        _range("JM", [_vm(4, 8)]),
        _range("GHOST", [], state="DESTROYED"),
    ]
    alloc = cap.compute_live_allocation(ranges)
    assert alloc.resources == RangeResources(cpus=4, ram_gb=8, vm_count=1, range_count=1)


def test_allocation_includes_ranges_not_created_by_this_app() -> None:
    """The whole point of the live-data switch: ranges this app never
    provisioned (e.g. created directly in Ludus) still count."""
    ranges = [_range("legacy-range", [_vm(6, 12), _vm(2, 2)])]
    alloc = cap.compute_live_allocation(ranges)
    assert alloc.resources.cpus == 8
    assert alloc.resources.ram_gb == 14


def test_allocation_empty_ranges_is_zero() -> None:
    alloc = cap.compute_live_allocation([])
    assert alloc.resources == RangeResources()
    assert alloc.range_count == 0


def test_resolve_capacity_default_and_db(db_session: OrmSession) -> None:
    s = _settings(ludus_default_cpu_capacity=16, ludus_default_ram_capacity_gb=100)
    assert cap.resolve_capacity(db_session, s, "default") == (16, 100)
    db_session.add(
        LudusServer(
            name="lab2", url="https://x", api_key_encrypted="enc",
            cpu_capacity=32, ram_capacity_gb=256,
        )
    )
    db_session.commit()
    assert cap.resolve_capacity(db_session, s, "lab2") == (32, 256)
    assert cap.resolve_capacity(db_session, _settings(), "default") == (None, None)


def test_build_view_available_and_range_count(db_session: OrmSession) -> None:
    s = _settings(ludus_default_cpu_capacity=16, ludus_default_ram_capacity_gb=100)
    ranges = [_range("JM", [_vm(4, 8), _vm(2, 4)]), _range("team1", [_vm(6, 12)])]
    view = cap.build_ranges_capacity_view(db_session, s, "default", ranges)
    assert view.configured is True
    assert view.cpu_allocated == 12 and view.ram_allocated_gb == 24
    assert view.cpu_available == 4  # 16 - 12
    assert view.ram_available_gb == 76  # 100 - 24
    assert view.range_count == 2


def test_build_view_unconfigured(db_session: OrmSession) -> None:
    ranges = [_range("team1", [_vm(6, 12)])]
    view = cap.build_ranges_capacity_view(db_session, _settings(), "default", ranges)
    assert view.configured is False
    assert view.cpu_available is None and view.ram_available_gb is None
    assert view.cpu_allocated == 6  # allocation still computed


def test_build_view_overcommit_negative(db_session: OrmSession) -> None:
    s = _settings(ludus_default_cpu_capacity=4, ludus_default_ram_capacity_gb=8)
    ranges = [_range("JM", [_vm(4, 8), _vm(2, 4)]), _range("team1", [_vm(6, 12)])]
    view = cap.build_ranges_capacity_view(db_session, s, "default", ranges)
    assert view.cpu_available == -8  # 4 - 12
    assert view.ram_available_gb == -16  # 8 - 24


# ---------------------------------------------------------------------------
# Proxmox-sourced capacity view
# ---------------------------------------------------------------------------

_GIB = 1024**3


def test_proxmox_view_converts_bytes_and_fraction() -> None:
    stats = ProxmoxHostStats(
        node="pve",
        cpu_count=16,
        cpu_fraction=0.25,
        mem_total_bytes=round(93.61 * _GIB),
        mem_used_bytes=round(25.14 * _GIB),
        storage_total_bytes=round(8.95 * 1024**4),
        storage_used_bytes=round(5.58 * 1024**4),
    )
    ranges = [_range("JM", [_vm(4, 8), _vm(2, 4)]), _range("team1", [_vm(1, 2)])]
    view = cap.build_proxmox_capacity_view("default", stats, ranges)
    assert view.source == "proxmox"
    assert view.node == "pve"
    assert view.configured is True
    assert view.cpu_capacity == 16
    assert view.cpu_allocated == 4  # round(0.25 * 16)
    assert view.cpu_available == 12
    assert view.ram_capacity_gb == pytest.approx(93.61, abs=0.01)
    assert view.ram_allocated_gb == pytest.approx(25.14, abs=0.01)
    assert view.ram_available_gb == pytest.approx(68.47, abs=0.01)
    assert view.storage_capacity_gb == pytest.approx(9164.8, abs=1)
    assert view.storage_used_gb == pytest.approx(5713.9, abs=1)
    assert view.storage_available_gb == pytest.approx(3450.9, abs=1)
    # "All VMs busy at once" planning figure, independent of live load above.
    assert view.range_count == 2
    assert view.vm_count == 3
    assert view.committed_cpus == 7
    assert view.committed_ram_gb == 14


def test_proxmox_view_with_no_ranges_has_zero_committed() -> None:
    stats = ProxmoxHostStats(
        node="pve", cpu_count=16, cpu_fraction=0.0,
        mem_total_bytes=0, mem_used_bytes=0,
        storage_total_bytes=0, storage_used_bytes=0,
    )
    view = cap.build_proxmox_capacity_view("default", stats, [])
    assert view.vm_count == 0
    assert view.committed_cpus == 0
    assert view.committed_ram_gb == 0
    assert view.range_count == 0
