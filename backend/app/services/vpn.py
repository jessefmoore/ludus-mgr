"""VPN (WireGuard) login tracking.

Ranges are reached over WireGuard. The Ludus HTTP API exposes no connection
state, so the only source of truth for "who is connected" is ``wg show
<iface> dump`` on the Ludus host. WireGuard is stateless - a peer is
"connected" when its *latest handshake* is recent - so we define a **login**
as an observed transition from offline -> online.

Collection is on-demand (the UI polls :func:`reconcile_vpn_status`, mirroring
:mod:`app.services.deploy_status`). Each poll that observes an offline->online
transition emits a ``student.vpn_connected`` audit event; the live "online"
flag is derived from handshake freshness. Because polling only happens while a
session view is open, history has gaps when nobody is watching, and we do not
try to record disconnect events (their timing is unreliable).

``wg show`` requires root / ``CAP_NET_ADMIN`` on the Ludus host: the local
user (local mode) or the SSH user (ssh mode) must be able to run it, e.g. via
sudo.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.models import Session as SessionRow
from app.models import Student
from app.models.event import Event

if TYPE_CHECKING:
    from sqlalchemy.orm import Session as DBSession

    from app.core.config import Settings

logger = logging.getLogger(__name__)

# wg runs over the default timeout; a healthy `wg show` returns instantly.
_WG_TIMEOUT_SECONDS = 15


class VpnMonitorError(Exception):
    """Raised when the WireGuard state could not be read from the host."""


class SessionNotFound(Exception):  # noqa: N818 -- matches deploy_status
    """Raised when ``session_id`` does not correspond to an existing session."""


@dataclass(frozen=True)
class PeerStat:
    """A single WireGuard peer's liveness, from ``wg show ... dump``."""

    public_key: str
    endpoint: str | None
    # Unix seconds of the last handshake; 0/None means "never".
    latest_handshake: int


def parse_vpn_ip(conf_text: str) -> str | None:
    """Extract the peer's VPN IP from a WireGuard client ``.conf``.

    Returns the bare IP from the ``[Interface]`` ``Address = <ip>/<mask>``
    line (e.g. ``198.51.100.20``), or None if absent. This IP is what appears
    in the server peer's ``allowed-ips`` and is how we map a ``wg show`` peer
    back to a student.
    """
    for line in conf_text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("address"):
            _, _, value = stripped.partition("=")
            first = value.split(",")[0].strip()
            ip = first.split("/")[0].strip()
            if ip:
                return ip
    return None


def parse_wg_dump(dump: str) -> dict[str, PeerStat]:
    """Parse ``wg show <iface> dump`` output into ``{vpn_ip: PeerStat}``.

    The first (interface) line is skipped. Each peer line is tab-separated:
    ``public-key  preshared-key  endpoint  allowed-ips  latest-handshake
    rx  tx  persistent-keepalive``. A peer is indexed under each of its
    allowed IPs (CIDR mask stripped) so callers can look up by a student's
    VPN IP. Malformed lines are ignored.
    """
    peers: dict[str, PeerStat] = {}
    lines = dump.splitlines()
    # Drop the interface summary line (first non-empty line).
    for line in lines[1:]:
        fields = line.split("\t")
        if len(fields) < 5:
            continue
        public_key, _preshared, endpoint, allowed_ips, handshake = fields[:5]
        try:
            latest_handshake = int(handshake)
        except ValueError:
            continue
        stat = PeerStat(
            public_key=public_key,
            endpoint=endpoint if endpoint and endpoint != "(none)" else None,
            latest_handshake=latest_handshake,
        )
        for cidr in allowed_ips.split(","):
            ip = cidr.strip().split("/")[0].strip()
            if ip:
                peers[ip] = stat
    return peers


class WireGuardMonitor:
    """Reads WireGuard peer state from the Ludus host via ``wg show``.

    Two transports, selected by settings: ``local`` shells out to ``wg``
    directly (app co-located on the Ludus host), ``ssh`` runs the same command
    over SSH to a remote host.
    """

    def __init__(self, settings: Settings) -> None:
        self._interface = settings.vpn_wg_interface
        self._mode = settings.vpn_wg_mode
        self._ssh_host = settings.vpn_ssh_host
        self._ssh_user = settings.vpn_ssh_user
        self._ssh_key_path = settings.vpn_ssh_key_path
        self._ssh_port = settings.vpn_ssh_port

    def _command(self) -> list[str]:
        wg_cmd = f"wg show {self._interface} dump"
        if self._mode == "ssh":
            if not self._ssh_host:
                raise VpnMonitorError("vpn_wg_mode=ssh but VPN_SSH_HOST is unset")
            target = f"{self._ssh_user}@{self._ssh_host}" if self._ssh_user else self._ssh_host
            cmd = ["ssh", "-p", str(self._ssh_port), "-o", "BatchMode=yes"]
            if self._ssh_key_path:
                cmd += ["-i", self._ssh_key_path]
            cmd += [target, wg_cmd]
            return cmd
        # local
        return ["wg", "show", self._interface, "dump"]

    def snapshot(self) -> dict[str, PeerStat]:
        """Return ``{vpn_ip: PeerStat}`` for every current WireGuard peer.

        Raises :class:`VpnMonitorError` if the command fails or times out.
        """
        cmd = self._command()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_WG_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError as exc:
            raise VpnMonitorError(f"command not found: {cmd[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise VpnMonitorError(f"`wg show` timed out after {_WG_TIMEOUT_SECONDS}s") from exc
        if result.returncode != 0:
            raise VpnMonitorError(
                f"`wg show` failed (exit {result.returncode}): {result.stderr.strip()}"
            )
        return parse_wg_dump(result.stdout)


@dataclass
class VpnStudentStatus:
    """Per-student VPN liveness returned by :func:`reconcile_vpn_status`."""

    student: Student
    online: bool
    last_seen: datetime | None


@dataclass
class VpnStatusResult:
    """Tally returned by :func:`reconcile_vpn_status`."""

    online: int = 0
    logins: int = 0
    students: list[VpnStudentStatus] = field(default_factory=list)


def _as_utc(dt: datetime | None) -> datetime | None:
    """Normalise a possibly-naive DB timestamp to an aware UTC datetime."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _ensure_vpn_ip(student: Student) -> str | None:
    """Return the student's VPN IP, lazily backfilling it from its ``.conf``.

    Students provisioned before VPN tracking existed have ``vpn_ip`` unset;
    parse it from the cached WireGuard config on first use.
    """
    if student.vpn_ip:
        return student.vpn_ip
    if not student.wg_config_path:
        return None
    try:
        text = Path(student.wg_config_path).read_text()
    except OSError:
        return None
    ip = parse_vpn_ip(text)
    if ip:
        student.vpn_ip = ip
    return ip


def _reconcile_student(
    db: DBSession,
    student: Student,
    snapshot: dict[str, PeerStat],
    *,
    now: datetime,
    window: int,
    gap: int,
    result: VpnStatusResult,
) -> None:
    """Classify one student against a snapshot, logging a login on transition.

    Appends a :class:`VpnStudentStatus` to ``result`` and bumps its counters.
    Emits a ``student.vpn_connected`` event (not committed here) when the
    student goes offline->online after at least ``gap`` seconds unseen.
    """
    vpn_ip = _ensure_vpn_ip(student)
    peer = snapshot.get(vpn_ip) if vpn_ip else None
    handshake = (
        datetime.fromtimestamp(peer.latest_handshake, UTC)
        if peer and peer.latest_handshake > 0
        else None
    )
    online = handshake is not None and (now - handshake).total_seconds() <= window

    if online:
        prior = _as_utc(student.vpn_last_seen)
        is_new_login = prior is None or (handshake - prior).total_seconds() > gap
        if is_new_login:
            result.logins += 1
            db.add(
                Event(
                    session_id=student.session_id,
                    student_id=student.id,
                    action="student.vpn_connected",
                    details_json={
                        "student_id": student.id,
                        "session_id": student.session_id,
                        "userid": student.ludus_userid,
                        "range_id": student.range_id,
                        "vpn_ip": vpn_ip,
                        "endpoint": peer.endpoint if peer else None,
                        "via": "vpn_poll",
                    },
                )
            )
        student.vpn_last_seen = handshake
        result.online += 1

    result.students.append(
        VpnStudentStatus(
            student=student,
            online=online,
            last_seen=_as_utc(student.vpn_last_seen),
        )
    )


def reconcile_vpn_status(
    db: DBSession,
    session_id: int,
    *,
    monitor: WireGuardMonitor,
    settings: Settings,
) -> VpnStatusResult:
    """Observe current VPN liveness for a single session's students.

    Reads a single ``wg show`` snapshot, marks each student online/offline by
    handshake freshness (``vpn_online_window_seconds``), and emits a
    ``student.vpn_connected`` event when a student transitions offline->online
    after at least ``vpn_login_gap_seconds`` of not being seen online.
    Idempotent and safe to poll repeatedly.
    """
    stmt = (
        select(SessionRow)
        .options(joinedload(SessionRow.students))
        .where(SessionRow.id == session_id)
    )
    session_row = db.execute(stmt).unique().scalar_one_or_none()
    if session_row is None:
        raise SessionNotFound(f"session id={session_id} does not exist")

    snapshot = monitor.snapshot()
    now = datetime.now(UTC)
    result = VpnStatusResult()
    for student in sorted(session_row.students, key=lambda s: s.id):
        _reconcile_student(
            db, student, snapshot,
            now=now,
            window=settings.vpn_online_window_seconds,
            gap=settings.vpn_login_gap_seconds,
            result=result,
        )

    db.commit()
    logger.info(
        "vpn_status session=%s online=%s logins=%s",
        session_id, result.online, result.logins,
    )
    return result


def reconcile_all_vpn_status(
    db: DBSession,
    *,
    monitor: WireGuardMonitor,
    settings: Settings,
) -> VpnStatusResult:
    """Observe VPN liveness for every provisioned student across all sessions.

    One ``wg show`` snapshot covers all peers, so this is as cheap as the
    single-session poll. Only students that have (or can derive) a VPN IP are
    considered - i.e. those with a WireGuard config. Emits login events the
    same way as :func:`reconcile_vpn_status`.
    """
    snapshot = monitor.snapshot()
    now = datetime.now(UTC)
    stmt = (
        select(Student)
        .options(joinedload(Student.session))
        .where(
            Student.vpn_ip.is_not(None) | Student.wg_config_path.is_not(None)
        )
        .order_by(Student.id)
    )
    students = db.execute(stmt).scalars().all()

    result = VpnStatusResult()
    for student in students:
        _reconcile_student(
            db, student, snapshot,
            now=now,
            window=settings.vpn_online_window_seconds,
            gap=settings.vpn_login_gap_seconds,
            result=result,
        )

    db.commit()
    logger.info(
        "vpn_status all online=%s logins=%s total=%s",
        result.online, result.logins, len(students),
    )
    return result


__all__ = [
    "PeerStat",
    "SessionNotFound",
    "VpnMonitorError",
    "VpnStatusResult",
    "VpnStudentStatus",
    "WireGuardMonitor",
    "parse_vpn_ip",
    "parse_wg_dump",
    "reconcile_all_vpn_status",
    "reconcile_vpn_status",
]
