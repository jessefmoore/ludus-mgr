"""VPN (WireGuard) overview + report endpoints.

Powers the dedicated VPN stats page: a single ``wg show`` snapshot mapped to
every provisioned student across all sessions, enriched with per-user login
counts (from the audit-event history) and a headline summary. The same data
is downloadable as a PDF. See app.services.vpn / app.services.vpn_report.
"""

from datetime import UTC, date, datetime, time

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession

from app.core.config import Settings, get_settings
from app.core.deps import get_current_user, get_db
from app.models.event import Event
from app.models.user import User
from app.services import vpn as vpn_service
from app.services import vpn_report

router = APIRouter(prefix="/api/vpn", tags=["vpn"])

# Audit action emitted on each offline->online transition; login counts are
# just the history of these.
_LOGIN_ACTION = "student.vpn_connected"


class VpnUserStatus(BaseModel):
    """One provisioned student's VPN connection state + lifetime login count."""

    student_id: int
    full_name: str
    email: str
    ludus_userid: str
    range_id: str | None = None
    session_id: int
    session_name: str
    online: bool
    last_seen: datetime | None = None
    login_count: int = 0


class VpnSummary(BaseModel):
    """Headline totals across all provisioned users."""

    total_users: int = 0
    online_now: int = 0
    total_logins: int = 0
    most_active_userid: str | None = None
    most_active_logins: int = 0


class VpnOverviewResponse(BaseModel):
    """Response body for ``POST /api/vpn/overview``.

    ``enabled`` is False (and the rest empty) when VPN tracking is turned off,
    so the page can render a "configure" state rather than erroring.
    """

    enabled: bool
    summary: VpnSummary = VpnSummary()
    users: list[VpnUserStatus] = []


def _login_counts(
    db: DBSession, *, start: date | None = None, end: date | None = None
) -> dict[int, int]:
    """``student.vpn_connected`` count per student id, within [start, end].

    ``start``/``end`` are calendar dates interpreted in UTC and inclusive of
    the whole end day; either may be None for an open bound (None/None = all
    time).
    """
    stmt = select(Event.student_id, func.count()).where(Event.action == _LOGIN_ACTION)
    if start is not None:
        stmt = stmt.where(Event.created_at >= datetime.combine(start, time.min, tzinfo=UTC))
    if end is not None:
        stmt = stmt.where(Event.created_at <= datetime.combine(end, time.max, tzinfo=UTC))
    rows = db.execute(stmt.group_by(Event.student_id)).all()
    return {sid: count for sid, count in rows if sid is not None}


def _collect_overview(
    db: DBSession,
    settings: Settings,
    *,
    start: date | None = None,
    end: date | None = None,
) -> VpnOverviewResponse:
    """Take one live snapshot, enrich with login counts, and summarise.

    Connection state (online / last seen) is always live; only the login
    counts and their derived summary honour the [start, end] date range.
    Shared by the JSON overview and the PDF report so both always agree.
    Raises :class:`vpn_service.VpnMonitorError` if the host read fails.
    """
    monitor = vpn_service.WireGuardMonitor(settings)
    result = vpn_service.reconcile_all_vpn_status(db=db, monitor=monitor, settings=settings)
    counts = _login_counts(db, start=start, end=end)

    users = [
        VpnUserStatus(
            student_id=s.student.id,
            full_name=s.student.full_name,
            email=s.student.email,
            ludus_userid=s.student.ludus_userid,
            range_id=s.student.range_id,
            session_id=s.student.session_id,
            session_name=s.student.session.name,
            online=s.online,
            last_seen=s.last_seen,
            login_count=counts.get(s.student.id, 0),
        )
        for s in result.students
    ]

    summary = VpnSummary(
        total_users=len(users),
        online_now=sum(1 for u in users if u.online),
        total_logins=sum(u.login_count for u in users),
    )
    if users:
        top = max(users, key=lambda u: u.login_count)
        if top.login_count > 0:
            summary.most_active_userid = top.ludus_userid
            summary.most_active_logins = top.login_count

    return VpnOverviewResponse(enabled=True, summary=summary, users=users)


def _validate_range(start: date | None, end: date | None) -> None:
    if start is not None and end is not None and start > end:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="start date must not be after end date",
        )


def _period_label(start: date | None, end: date | None) -> str:
    """Human-readable description of the login-count window for the report."""
    if start is None and end is None:
        return "All time"
    if start is not None and end is not None:
        return f"{start.isoformat()} to {end.isoformat()}"
    if start is not None:
        return f"Since {start.isoformat()}"
    return f"Up to {end.isoformat()}"


@router.post("/overview", response_model=VpnOverviewResponse)
def vpn_overview(
    start: date | None = Query(default=None),  # noqa: B008 -- FastAPI idiom
    end: date | None = Query(default=None),  # noqa: B008 -- FastAPI idiom
    db: DBSession = Depends(get_db),  # noqa: B008 -- FastAPI idiom
    settings: Settings = Depends(get_settings),  # noqa: B008 -- FastAPI idiom
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
) -> VpnOverviewResponse:
    """Report VPN connection state + login counts for every provisioned student.

    Reads one live WireGuard snapshot and logs a ``student.vpn_connected`` event
    per new login. ``start``/``end`` (inclusive UTC dates) scope the login
    counts + summary; connection state is always live. Returns
    ``enabled=False`` when VPN tracking is disabled.
    """
    if not settings.vpn_tracking_enabled:
        return VpnOverviewResponse(enabled=False)
    _validate_range(start, end)
    try:
        return _collect_overview(db, settings, start=start, end=end)
    except vpn_service.VpnMonitorError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"VPN read failed: {exc}"
        ) from exc


def _fmt_last_seen(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "Never"


@router.post("/report.pdf")
def vpn_report_pdf(
    start: date | None = Query(default=None),  # noqa: B008 -- FastAPI idiom
    end: date | None = Query(default=None),  # noqa: B008 -- FastAPI idiom
    db: DBSession = Depends(get_db),  # noqa: B008 -- FastAPI idiom
    settings: Settings = Depends(get_settings),  # noqa: B008 -- FastAPI idiom
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
) -> Response:
    """Download the VPN activity overview as a PDF (login counts scoped to the
    optional [start, end] date range)."""
    if not settings.vpn_tracking_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="VPN tracking is disabled; nothing to report.",
        )
    _validate_range(start, end)
    try:
        data = _collect_overview(db, settings, start=start, end=end)
    except vpn_service.VpnMonitorError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"VPN read failed: {exc}"
        ) from exc

    generated_at = datetime.now(UTC)
    rows = [
        {
            "ludus_userid": u.ludus_userid,
            "full_name": u.full_name,
            "range_id": u.range_id or "-",
            "session_name": u.session_name,
            "status": "Online" if u.online else "Offline",
            "login_count": u.login_count,
            "last_seen": _fmt_last_seen(u.last_seen),
        }
        for u in data.users
    ]
    pdf_bytes = vpn_report.render_overview_pdf(
        generated_at=generated_at,
        period=_period_label(start, end),
        summary=data.summary.model_dump(),
        rows=rows,
    )
    filename = f"vpn-activity-{generated_at.strftime('%Y%m%d-%H%M%S')}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
