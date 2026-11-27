"""VPN (WireGuard) overview endpoint.

Powers the dedicated VPN stats page: a single ``wg show`` snapshot mapped to
every provisioned student across all sessions. See app.services.vpn.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session as DBSession

from app.core.config import Settings, get_settings
from app.core.deps import get_current_user, get_db
from app.models.user import User
from app.services import vpn as vpn_service

router = APIRouter(prefix="/api/vpn", tags=["vpn"])


class VpnUserStatus(BaseModel):
    """One provisioned student's VPN connection state."""

    student_id: int
    full_name: str
    email: str
    ludus_userid: str
    range_id: str | None = None
    session_id: int
    session_name: str
    online: bool
    last_seen: datetime | None = None


class VpnOverviewResponse(BaseModel):
    """Response body for ``POST /api/vpn/overview``.

    ``enabled`` is False (and lists empty) when VPN tracking is turned off, so
    the page can render a "configure" state rather than erroring.
    """

    enabled: bool
    online: int = 0
    total: int = 0
    users: list[VpnUserStatus] = []


@router.post("/overview", response_model=VpnOverviewResponse)
def vpn_overview(
    db: DBSession = Depends(get_db),  # noqa: B008 -- FastAPI idiom
    settings: Settings = Depends(get_settings),  # noqa: B008 -- FastAPI idiom
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
) -> VpnOverviewResponse:
    """Report VPN connection state for every provisioned student.

    Reads one live WireGuard snapshot from the Ludus host and logs a
    ``student.vpn_connected`` event per new login. Returns ``enabled=False``
    when VPN tracking is disabled.
    """
    if not settings.vpn_tracking_enabled:
        return VpnOverviewResponse(enabled=False)

    monitor = vpn_service.WireGuardMonitor(settings)
    try:
        result = vpn_service.reconcile_all_vpn_status(
            db=db, monitor=monitor, settings=settings
        )
    except vpn_service.VpnMonitorError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"VPN read failed: {exc}"
        ) from exc

    return VpnOverviewResponse(
        enabled=True,
        online=result.online,
        total=len(result.students),
        users=[
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
            )
            for s in result.students
        ],
    )
