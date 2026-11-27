"""Tests for VPN (WireGuard) login tracking.

Covers the `wg show dump` parser, the .conf IP parser, and the
offline->online login-transition logic in reconcile_vpn_status.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.db import Base
from app.models import (
    LabTemplate,
    LabTemplateMode,
    SessionMode,
    SessionStatus,
    Student,
    StudentStatus,
)
from app.models import Session as SessionRow
from app.models.event import Event
from app.services import vpn as vpn_service
from app.services.vpn import PeerStat


class FakeMonitor:
    """Stand-in for WireGuardMonitor: returns a fixed snapshot."""

    def __init__(self, snapshot: dict[str, PeerStat]) -> None:
        self._snapshot = snapshot
        self.calls = 0

    def snapshot(self) -> dict[str, PeerStat]:
        self.calls += 1
        return self._snapshot


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="testing",
        app_secret_key="unit-test-secret",
        admin_email="admin@example.com",
        admin_password="pw",
        ludus_default_url="https://ludus.test:8080",
        ludus_default_api_key="k",
        public_base_url="https://mgr.test",
        config_storage_dir=str(tmp_path),
        vpn_tracking_enabled=True,
        _env_file=None,
    )


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


def _seed_student(
    db: OrmSession, *, vpn_ip: str | None, userid: str = "alice"
) -> tuple[SessionRow, Student]:
    template = LabTemplate(
        name="Lab",
        description="",
        range_config_yaml="ludus:\n  - vm_name: KALI\n",
        default_mode=LabTemplateMode.dedicated,
        ludus_server="default",
        entry_point_vm="KALI",
    )
    db.add(template)
    db.flush()
    session_row = SessionRow(
        name="S",
        lab_template_id=template.id,
        mode=SessionMode.dedicated,
        shared_range_id=None,
        status=SessionStatus.active,
    )
    db.add(session_row)
    db.flush()
    student = Student(
        session_id=session_row.id,
        full_name=userid,
        email=f"{userid}@x.test",
        ludus_userid=userid,
        invite_token=userid.ljust(32, "t"),
        status=StudentStatus.ready,
        range_id=userid,
        vpn_ip=vpn_ip,
    )
    db.add(student)
    db.commit()
    return session_row, student


def _count_login_events(db: OrmSession) -> int:
    return len(
        db.execute(
            select(Event).where(Event.action == "student.vpn_connected")
        ).scalars().all()
    )


# -- parsers ----------------------------------------------------------------


def test_parse_vpn_ip_from_conf() -> None:
    conf = (
        "[Interface]\n"
        "PrivateKey = abc\n"
        "Address = 198.51.100.20/32\n"
        "\n"
        "[Peer]\n"
        "PublicKey = def\n"
    )
    assert vpn_service.parse_vpn_ip(conf) == "198.51.100.20"


def test_parse_vpn_ip_missing() -> None:
    assert vpn_service.parse_vpn_ip("[Interface]\nPrivateKey = abc\n") is None


def test_parse_wg_dump() -> None:
    dump = (
        "PRIVKEY\tPUBKEY\t51820\toff\n"  # interface line, skipped
        "peerA\t(none)\t203.0.113.5:51820\t198.51.100.20/32\t1700000000\t100\t200\t25\n"
        "peerB\t(none)\t(none)\t198.51.100.21/32\t0\t0\t0\toff\n"
    )
    peers = vpn_service.parse_wg_dump(dump)
    assert set(peers) == {"198.51.100.20", "198.51.100.21"}
    assert peers["198.51.100.20"].latest_handshake == 1700000000
    assert peers["198.51.100.20"].endpoint == "203.0.113.5:51820"
    assert peers["198.51.100.21"].latest_handshake == 0
    assert peers["198.51.100.21"].endpoint is None


def test_parse_wg_dump_ignores_malformed() -> None:
    dump = "IFACE\tLINE\t1\t2\nnot-a-peer\n"
    assert vpn_service.parse_wg_dump(dump) == {}


# -- reconcile --------------------------------------------------------------


def _now_ts() -> int:
    return int(datetime.now(UTC).timestamp())


def test_offline_to_online_emits_one_login(
    db_session: OrmSession, settings: Settings
) -> None:
    _, student = _seed_student(db_session, vpn_ip="198.51.100.20")
    monitor = FakeMonitor({"198.51.100.20": PeerStat("k", "203.0.113.5:51820", _now_ts())})

    res = vpn_service.reconcile_vpn_status(
        db_session, student.session_id, monitor=monitor, settings=settings
    )

    assert res.online == 1 and res.logins == 1
    assert res.students[0].online is True
    assert _count_login_events(db_session) == 1
    db_session.refresh(student)
    assert student.vpn_last_seen is not None


def test_staying_online_no_duplicate_event(
    db_session: OrmSession, settings: Settings
) -> None:
    _, student = _seed_student(db_session, vpn_ip="198.51.100.20")
    # Already seen online 60s ago (well within the 600s login gap).
    student.vpn_last_seen = datetime.now(UTC) - timedelta(seconds=60)
    db_session.commit()
    monitor = FakeMonitor({"198.51.100.20": PeerStat("k", "e", _now_ts())})

    res = vpn_service.reconcile_vpn_status(
        db_session, student.session_id, monitor=monitor, settings=settings
    )

    assert res.online == 1 and res.logins == 0
    assert _count_login_events(db_session) == 0


def test_reconnect_after_gap_emits_new_login(
    db_session: OrmSession, settings: Settings
) -> None:
    _, student = _seed_student(db_session, vpn_ip="198.51.100.20")
    # Last seen well beyond the login gap -> a fresh handshake is a new login.
    student.vpn_last_seen = datetime.now(UTC) - timedelta(seconds=2000)
    db_session.commit()
    monitor = FakeMonitor({"198.51.100.20": PeerStat("k", "e", _now_ts())})

    res = vpn_service.reconcile_vpn_status(
        db_session, student.session_id, monitor=monitor, settings=settings
    )

    assert res.logins == 1
    assert _count_login_events(db_session) == 1


def test_stale_handshake_is_offline(
    db_session: OrmSession, settings: Settings
) -> None:
    _, student = _seed_student(db_session, vpn_ip="198.51.100.20")
    stale = _now_ts() - 10_000  # far outside the 180s online window
    monitor = FakeMonitor({"198.51.100.20": PeerStat("k", "e", stale)})

    res = vpn_service.reconcile_vpn_status(
        db_session, student.session_id, monitor=monitor, settings=settings
    )

    assert res.online == 0 and res.logins == 0
    assert res.students[0].online is False


def test_no_peer_is_offline(db_session: OrmSession, settings: Settings) -> None:
    _, student = _seed_student(db_session, vpn_ip="198.51.100.20")
    monitor = FakeMonitor({})  # student's IP not present

    res = vpn_service.reconcile_vpn_status(
        db_session, student.session_id, monitor=monitor, settings=settings
    )

    assert res.online == 0
    assert res.students[0].online is False


def test_lazy_backfill_vpn_ip_from_conf(
    db_session: OrmSession, settings: Settings, tmp_path: Path
) -> None:
    session_row, student = _seed_student(db_session, vpn_ip=None)
    conf = tmp_path / "alice.conf"
    conf.write_text("[Interface]\nPrivateKey = x\nAddress = 198.51.100.77/32\n")
    student.wg_config_path = str(conf)
    db_session.commit()
    monitor = FakeMonitor({"198.51.100.77": PeerStat("k", "e", _now_ts())})

    res = vpn_service.reconcile_vpn_status(
        db_session, session_row.id, monitor=monitor, settings=settings
    )

    assert res.online == 1
    db_session.refresh(student)
    assert student.vpn_ip == "198.51.100.77"


def test_session_not_found(db_session: OrmSession, settings: Settings) -> None:
    monitor = FakeMonitor({})
    with pytest.raises(vpn_service.SessionNotFound):
        vpn_service.reconcile_vpn_status(
            db_session, 999, monitor=monitor, settings=settings
        )


def test_reconcile_all_spans_sessions(
    db_session: OrmSession, settings: Settings
) -> None:
    # Two students in two separate sessions; only the first is online.
    _, online_student = _seed_student(db_session, vpn_ip="198.51.100.20", userid="alice")
    _, offline_student = _seed_student(db_session, vpn_ip="198.51.100.30", userid="bob")
    assert online_student.session_id != offline_student.session_id
    monitor = FakeMonitor({"198.51.100.20": PeerStat("k", "e", _now_ts())})

    res = vpn_service.reconcile_all_vpn_status(
        db_session, monitor=monitor, settings=settings
    )

    assert len(res.students) == 2
    assert res.online == 1 and res.logins == 1
    assert monitor.calls == 1  # one snapshot covers all sessions
    by_id = {s.student.id: s.online for s in res.students}
    assert by_id[online_student.id] is True
    assert by_id[offline_student.id] is False
    event = db_session.execute(
        select(Event).where(Event.action == "student.vpn_connected")
    ).scalars().all()
    assert len(event) == 1
    assert event[0].session_id == online_student.session_id
