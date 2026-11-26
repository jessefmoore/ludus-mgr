"""Ludus discovery endpoints: list ranges, fetch range config.

All routes require an authenticated lab manager session (cookie-based).
These endpoints proxy to the Ludus API so the frontend can discover
deployed ranges and optionally import their configs as lab templates.
"""

from __future__ import annotations

import io
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session as DBSession

from app.core.config import Settings, get_settings
from app.core.deps import (
    LudusClientRegistry,
    get_current_user,
    get_db,
    get_ludus_client_registry,
)
from app.models.user import User
from app.schemas.ludus import (
    DeployRequest,
    LudusAccessibleRange,
    LudusAccessibleRangesResponse,
    LudusActionResponse,
    LudusCapacityResponse,
    LudusLogEntryDetailResponse,
    LudusLogHistoryEntry,
    LudusLogHistoryResponse,
    LudusOrphanedRangesResponse,
    LudusRange,
    LudusRangeConfigResponse,
    LudusRangeDetail,
    LudusRangeDetailResponse,
    LudusRangeDiagnosticsResponse,
    LudusRangeListResponse,
    LudusRangeLogsResponse,
    LudusRangeTagsResponse,
    LudusRangeUser,
    LudusRangeUsersResponse,
    LudusSnapshot,
    LudusSnapshotListResponse,
    LudusTemplate,
    LudusTemplateBuildStatus,
    LudusTemplateBuildStatusResponse,
    LudusTemplateListResponse,
    LudusTextResponse,
    LudusUser,
    LudusUserListResponse,
    PowerActionRequest,
    RangeAbortRequest,
    RangeAssignRequest,
    RangeCreateRequest,
    RangeRevokeRequest,
    SnapshotCreateRequest,
    SnapshotRevertRequest,
    TemplateBuildRequest,
    UserCreateRequest,
    UserCreateResponse,
)
from app.services import capacity as capacity_service
from app.services.exceptions import LudusError, LudusNotFound, LudusRangeExists, LudusUserExists
from app.services.ludus import LudusClient
from app.services.proxmox import ProxmoxClient, ProxmoxError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ludus", tags=["ludus"])


def _resolve_client(registry: LudusClientRegistry, server: str) -> LudusClient:
    """Resolve a ``LudusClient`` from the registry, raising 400 on unknown server."""
    try:
        return registry.get(server)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.get("/ranges", response_model=LudusRangeListResponse)
def list_ranges(
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusRangeListResponse:
    """List all ranges visible on the Ludus instance."""
    ludus = _resolve_client(registry, server)
    try:
        raw = ludus.range_list()
    except LudusError as exc:
        logger.warning("Ludus range_list failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    ranges = [LudusRange.model_validate(r) for r in raw]
    return LudusRangeListResponse(ranges=ranges)


@router.get("/capacity", response_model=LudusCapacityResponse)
def get_host_capacity(
    server: str = "default",
    db: DBSession = Depends(get_db),  # noqa: B008 -- FastAPI idiom
    settings: Settings = Depends(get_settings),  # noqa: B008 -- FastAPI idiom
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusCapacityResponse:
    """Host CPU/RAM/storage capacity vs real usage.

    Powers the dashboard's "how much can I still assign" card. Prefers real,
    measured Proxmox host stats (``PROXMOX_API_TOKEN`` configured, currently
    only wired for the ``default`` server) over the manually-configured
    capacity + live-Ludus-range-sum fallback used otherwise - see
    ``app.services.capacity`` for why that fallback alone used to understate
    real host load. Ludus's range list is fetched either way: for the
    fallback it's the whole computation, and for the Proxmox path it still
    drives the "all VMs busy at once" committed-CPU/RAM planning figure
    alongside the real, measured numbers.
    """
    ludus = _resolve_client(registry, server)
    try:
        ranges = ludus.range_list()
    except LudusError as exc:
        logger.warning("Ludus range_list failed while computing capacity: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    if server == "default" and settings.proxmox_api_token:
        proxmox = ProxmoxClient(
            url=settings.resolved_proxmox_url or "",
            api_token=settings.proxmox_api_token,
            verify_tls=settings.proxmox_verify_tls,
        )
        try:
            stats = proxmox.host_stats(settings.proxmox_node)
        except ProxmoxError as exc:
            logger.warning("Proxmox host_stats failed, falling back to range sum: %s", exc)
        else:
            view = capacity_service.build_proxmox_capacity_view(server, stats, ranges)
            return LudusCapacityResponse(**view.__dict__)
        finally:
            proxmox.close()

    view = capacity_service.build_ranges_capacity_view(db, settings, server, ranges)
    return LudusCapacityResponse(**view.__dict__)


@router.get("/ranges/{range_id}/config", response_model=LudusRangeConfigResponse)
def get_range_config(
    range_id: str,
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusRangeConfigResponse:
    """Fetch the range-config YAML for a range by its rangeID."""
    ludus = _resolve_client(registry, server)
    try:
        config_yaml = ludus.range_get_config(range_id=range_id)
    except LudusNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Range config not found for range {range_id}",
        ) from exc
    except LudusError as exc:
        logger.warning("Ludus range_get_config(range_id=%s) failed: %s", range_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    return LudusRangeConfigResponse(range_id=range_id, config_yaml=config_yaml)


@router.get("/users", response_model=LudusUserListResponse)
def list_users(
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusUserListResponse:
    """List all users on the Ludus instance."""
    ludus = _resolve_client(registry, server)
    try:
        raw = ludus.user_list()
    except LudusError as exc:
        logger.warning("Ludus user_list failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    users = [LudusUser.model_validate(u) for u in raw]
    return LudusUserListResponse(users=users)


@router.post("/users", response_model=UserCreateResponse)
def create_user(
    body: UserCreateRequest,
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> UserCreateResponse:
    """Create a new user on the Ludus instance."""
    ludus = _resolve_client(registry, server)
    try:
        data = ludus.user_add(body.user_id, body.name, body.email)
    except LudusUserExists as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"User '{body.user_id}' already exists",
        ) from exc
    except LudusError as exc:
        logger.warning("Ludus user_add(%s) failed: %s", body.user_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    return UserCreateResponse.model_validate(data)


@router.delete("/users/{user_id}", response_model=LudusActionResponse)
def delete_user(
    user_id: str,
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusActionResponse:
    """Delete a user from the Ludus instance."""
    ludus = _resolve_client(registry, server)
    try:
        ludus.user_rm(user_id)
    except LudusNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User '{user_id}' not found",
        ) from exc
    except LudusError as exc:
        logger.warning("Ludus user_rm(%s) failed: %s", user_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    return LudusActionResponse(status="ok", detail="User deleted")


@router.get("/users/{user_id}/wireguard")
def get_user_wireguard(
    user_id: str,
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> StreamingResponse:
    """Download a user's WireGuard config file."""
    ludus = _resolve_client(registry, server)
    try:
        config_text = ludus.user_wireguard(user_id)
    except LudusNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User '{user_id}' not found",
        ) from exc
    except LudusError as exc:
        logger.warning("Ludus user_wireguard(%s) failed: %s", user_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    return StreamingResponse(
        io.BytesIO(config_text.encode()),
        media_type="text/plain",
        headers={"Content-Disposition": f"attachment; filename={user_id}.conf"},
    )


@router.post("/ranges/{range_id}/deploy", response_model=LudusActionResponse)
def deploy_range(
    range_id: str,
    body: DeployRequest,
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusActionResponse:
    """Deploy an already-configured range by its rangeID."""
    ludus = _resolve_client(registry, server)
    try:
        ludus.range_deploy_existing(range_id=range_id, user_id=body.user_id)
    except LudusNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Range {range_id} not found",
        ) from exc
    except LudusError as exc:
        logger.warning("Ludus range_deploy_existing(range_id=%s) failed: %s", range_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    return LudusActionResponse(status="ok", detail="Deployment started")


@router.delete("/ranges/{range_id}", response_model=LudusActionResponse)
def destroy_range(
    range_id: str,
    server: str = "default",
    force: bool = False,
    user_id: str | None = None,
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusActionResponse:
    """Destroy a range by its rangeID (deletes the range + VMs, DB and Proxmox)."""
    ludus = _resolve_client(registry, server)
    try:
        ludus.range_destroy(range_id=range_id, user_id=user_id, force=force)
    except LudusNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Range {range_id} not found",
        ) from exc
    except LudusError as exc:
        logger.warning("Ludus range_destroy(range_id=%s) failed: %s", range_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    return LudusActionResponse(status="ok", detail="Range destroyed")


# -- Power management -------------------------------------------------------


@router.post("/ranges/{range_id}/power-on", response_model=LudusActionResponse)
def power_on_range(
    range_id: str,
    body: PowerActionRequest,
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusActionResponse:
    """Power on VMs in a range."""
    ludus = _resolve_client(registry, server)
    try:
        ludus.range_power_on(range_id=range_id, user_id=body.user_id, machines=body.machines)
    except LudusNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Range {range_id} not found",
        ) from exc
    except LudusError as exc:
        logger.warning("Ludus range_power_on(range_id=%s) failed: %s", range_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    return LudusActionResponse(status="ok", detail="Power on initiated")


@router.post("/ranges/{range_id}/power-off", response_model=LudusActionResponse)
def power_off_range(
    range_id: str,
    body: PowerActionRequest,
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusActionResponse:
    """Power off VMs in a range."""
    ludus = _resolve_client(registry, server)
    try:
        ludus.range_power_off(range_id=range_id, user_id=body.user_id, machines=body.machines)
    except LudusNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Range {range_id} not found",
        ) from exc
    except LudusError as exc:
        logger.warning("Ludus range_power_off(range_id=%s) failed: %s", range_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    return LudusActionResponse(status="ok", detail="Power off initiated")


# -- Snapshot management -----------------------------------------------------


@router.get("/snapshots", response_model=LudusSnapshotListResponse)
def list_snapshots(
    range_id: str | None = None,
    user_id: str | None = None,
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusSnapshotListResponse:
    """List snapshots, optionally filtered by range (or user, admin only)."""
    ludus = _resolve_client(registry, server)
    try:
        raw = ludus.snapshot_list(range_id=range_id, user_id=user_id)
    except LudusNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User or range not found",
        ) from exc
    except LudusError as exc:
        logger.warning("Ludus snapshot_list failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    # Ludus returns [{"snapshots": [{name, vmid, vmname, ...}, ...]}].
    # Flatten the per-VM entries and deduplicate by snapshot name.
    flat: list[dict] = []
    for item in raw:
        if isinstance(item, dict) and "snapshots" in item:
            flat.extend(item["snapshots"])
        else:
            flat.append(item)

    by_name: dict[str, dict] = {}
    for entry in flat:
        name = entry.get("name", "")
        if name not in by_name:
            by_name[name] = {
                "name": name,
                "description": entry.get("description"),
                "vmids": [],
            }
        vmid = entry.get("vmid")
        if vmid is not None:
            by_name[name]["vmids"].append(vmid)

    snapshots = [LudusSnapshot.model_validate(s) for s in by_name.values()]
    return LudusSnapshotListResponse(snapshots=snapshots)


@router.post("/snapshots", response_model=LudusActionResponse)
def create_snapshot(
    body: SnapshotCreateRequest,
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusActionResponse:
    """Create a new snapshot."""
    ludus = _resolve_client(registry, server)
    try:
        ludus.snapshot_create(
            body.name,
            range_id=body.range_id,
            user_id=body.user_id,
            description=body.description,
            include_ram=body.include_ram,
            vmids=body.vmids,
        )
    except LudusNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        ) from exc
    except LudusError as exc:
        logger.warning("Ludus snapshot_create failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    return LudusActionResponse(status="ok", detail="Snapshot creation started")


@router.post("/snapshots/revert", response_model=LudusActionResponse)
def revert_snapshot(
    body: SnapshotRevertRequest,
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusActionResponse:
    """Revert to a named snapshot."""
    ludus = _resolve_client(registry, server)
    try:
        ludus.snapshot_revert(
            body.name, range_id=body.range_id, user_id=body.user_id, vmids=body.vmids
        )
    except LudusNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Snapshot or user not found",
        ) from exc
    except LudusError as exc:
        logger.warning("Ludus snapshot_revert failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    return LudusActionResponse(status="ok", detail="Snapshot revert started")


@router.delete("/snapshots/{name}", response_model=LudusActionResponse)
def delete_snapshot(
    name: str,
    range_id: str | None = None,
    user_id: str | None = None,
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusActionResponse:
    """Delete a snapshot by name."""
    ludus = _resolve_client(registry, server)
    try:
        ludus.snapshot_delete(name, range_id=range_id, user_id=user_id)
    except LudusNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Snapshot '{name}' not found",
        ) from exc
    except LudusError as exc:
        logger.warning("Ludus snapshot_delete(%s) failed: %s", name, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    return LudusActionResponse(status="ok", detail="Snapshot deleted")


# -- Template management ----------------------------------------------------


@router.get("/templates", response_model=LudusTemplateListResponse)
def list_templates(
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusTemplateListResponse:
    """List all available VM templates."""
    ludus = _resolve_client(registry, server)
    try:
        raw = ludus.template_list()
    except LudusError as exc:
        logger.warning("Ludus template_list failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    templates = [LudusTemplate.model_validate(t) for t in raw]
    return LudusTemplateListResponse(templates=templates)


@router.delete("/templates/{name}", response_model=LudusActionResponse)
def delete_template(
    name: str,
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusActionResponse:
    """Delete a VM template by name."""
    ludus = _resolve_client(registry, server)
    try:
        ludus.template_delete(name)
    except LudusNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Template '{name}' not found",
        ) from exc
    except LudusError as exc:
        logger.warning("Ludus template_delete(%s) failed: %s", name, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    return LudusActionResponse(status="ok", detail="Template deleted")


@router.post("/templates/build", response_model=LudusActionResponse)
def build_templates(
    body: TemplateBuildRequest,
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusActionResponse:
    """Trigger a Packer build for the given templates."""
    ludus = _resolve_client(registry, server)
    try:
        ludus.template_build(body.templates, parallel=body.parallel)
    except LudusError as exc:
        logger.warning("Ludus template_build failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    return LudusActionResponse(status="ok", detail="Template build started")


@router.post("/templates/abort", response_model=LudusActionResponse)
def abort_template_build(
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusActionResponse:
    """Abort a running Packer template build."""
    ludus = _resolve_client(registry, server)
    try:
        ludus.template_abort()
    except LudusError as exc:
        logger.warning("Ludus template_abort failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    return LudusActionResponse(status="ok", detail="Template build aborted")


@router.get("/templates/build-status", response_model=LudusTemplateBuildStatusResponse)
def get_template_build_status(
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusTemplateBuildStatusResponse:
    """Get the active template build queue."""
    ludus = _resolve_client(registry, server)
    try:
        raw = ludus.template_status()
    except LudusError as exc:
        logger.warning("Ludus template_status failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    items = [LudusTemplateBuildStatus.model_validate(s) for s in raw]
    return LudusTemplateBuildStatusResponse(status=items)


@router.get("/templates/build-logs", response_model=LudusTextResponse)
def get_template_build_logs(
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusTextResponse:
    """Get live Packer build log output."""
    ludus = _resolve_client(registry, server)
    try:
        content = ludus.template_logs()
    except LudusError as exc:
        logger.warning("Ludus template_logs failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    return LudusTextResponse(content=content)


# -- Range detail / VM operations -------------------------------------------


@router.get("/range/vms", response_model=LudusRangeDetailResponse)
def get_range_vms(
    range_id: str | None = None,
    user_id: str | None = None,
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusRangeDetailResponse:
    """Get VMs for a range with power/testing state."""
    ludus = _resolve_client(registry, server)
    try:
        raw = ludus.range_get_vms(range_id=range_id, user_id=user_id)
    except LudusNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Range not found",
        ) from exc
    except LudusError as exc:
        logger.warning("Ludus range_get_vms failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    # GET /range returns a single range object; wrap it for the list response.
    ranges = [LudusRangeDetail.model_validate(raw)]
    return LudusRangeDetailResponse(ranges=ranges)


@router.delete("/vm/{vm_id}", response_model=LudusActionResponse)
def destroy_vm(
    vm_id: int,
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusActionResponse:
    """Destroy a single VM."""
    ludus = _resolve_client(registry, server)
    try:
        ludus.vm_destroy(vm_id)
    except LudusNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"VM {vm_id} not found",
        ) from exc
    except LudusError as exc:
        logger.warning("Ludus vm_destroy(%d) failed: %s", vm_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    return LudusActionResponse(status="ok", detail="VM destroyed")


@router.post("/range/abort", response_model=LudusActionResponse)
def abort_range(
    body: RangeAbortRequest,
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusActionResponse:
    """Abort a running range deployment."""
    ludus = _resolve_client(registry, server)
    try:
        ludus.range_abort(range_id=body.range_id, user_id=body.user_id)
    except LudusNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Range not found",
        ) from exc
    except LudusError as exc:
        logger.warning("Ludus range_abort failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    return LudusActionResponse(status="ok", detail="Abort initiated")


@router.delete("/range/{range_id}/vms", response_model=LudusActionResponse)
def delete_range_vms(
    range_id: str,
    user_id: str | None = None,
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusActionResponse:
    """Delete all VMs in a range (keeps the range object)."""
    ludus = _resolve_client(registry, server)
    try:
        ludus.range_delete_vms(range_id, user_id=user_id)
    except LudusNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Range {range_id} not found",
        ) from exc
    except LudusError as exc:
        logger.warning("Ludus range_delete_vms(%s) failed: %s", range_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    return LudusActionResponse(status="ok", detail="VMs deleted")


@router.get("/range/tags", response_model=LudusRangeTagsResponse)
def get_range_tags(
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusRangeTagsResponse:
    """List all range tags."""
    ludus = _resolve_client(registry, server)
    try:
        tags = ludus.range_tags()
    except LudusError as exc:
        logger.warning("Ludus range_tags failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    return LudusRangeTagsResponse(tags=tags)


@router.get("/range/config/example", response_model=LudusTextResponse)
def get_range_config_example(
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusTextResponse:
    """Get an example range config YAML."""
    ludus = _resolve_client(registry, server)
    try:
        content = ludus.range_config_example()
    except LudusError as exc:
        logger.warning("Ludus range_config_example failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    return LudusTextResponse(content=content)


@router.get("/range/logs", response_model=LudusRangeLogsResponse)
def get_range_logs(
    range_id: str | None = None,
    user_id: str | None = None,
    tail: int | None = None,
    cursor: int | None = None,
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusRangeLogsResponse:
    """Get deployment logs for a range."""
    ludus = _resolve_client(registry, server)
    try:
        data = ludus.range_logs(range_id=range_id, user_id=user_id, tail=tail, cursor=cursor)
    except LudusNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Range not found",
        ) from exc
    except LudusError as exc:
        logger.warning("Ludus range_logs failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    return LudusRangeLogsResponse.model_validate(data)


@router.get("/range/logs/history", response_model=LudusLogHistoryResponse)
def get_range_logs_history(
    range_id: str | None = None,
    user_id: str | None = None,
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusLogHistoryResponse:
    """Get deployment log history."""
    ludus = _resolve_client(registry, server)
    try:
        raw = ludus.range_logs_history(range_id=range_id, user_id=user_id)
    except LudusNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Range not found",
        ) from exc
    except LudusError as exc:
        logger.warning("Ludus range_logs_history failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    entries = [LudusLogHistoryEntry.model_validate(e) for e in raw]
    return LudusLogHistoryResponse(entries=entries)


@router.get("/range/logs/history/{log_id}", response_model=LudusLogEntryDetailResponse)
def get_range_log_entry(
    log_id: str,
    range_id: str | None = None,
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusLogEntryDetailResponse:
    """Get a specific deployment log entry."""
    ludus = _resolve_client(registry, server)
    try:
        data = ludus.range_log_entry(log_id, range_id=range_id)
    except LudusNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Log entry {log_id} not found",
        ) from exc
    except LudusError as exc:
        logger.warning("Ludus range_log_entry(%s) failed: %s", log_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    return LudusLogEntryDetailResponse.model_validate(data)


@router.get("/range/etchosts", response_model=LudusTextResponse)
def get_range_etchosts(
    range_id: str | None = None,
    user_id: str | None = None,
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusTextResponse:
    """Get /etc/hosts content for a range."""
    ludus = _resolve_client(registry, server)
    try:
        content = ludus.range_etchosts(range_id=range_id, user_id=user_id)
    except LudusNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Range not found",
        ) from exc
    except LudusError as exc:
        logger.warning("Ludus range_etchosts failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    return LudusTextResponse(content=content)


@router.get("/range/sshconfig", response_model=LudusTextResponse)
def get_range_sshconfig(
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusTextResponse:
    """Get SSH config for the range."""
    ludus = _resolve_client(registry, server)
    try:
        content = ludus.range_sshconfig()
    except LudusError as exc:
        logger.warning("Ludus range_sshconfig failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    return LudusTextResponse(content=content)


@router.get("/range/rdpconfigs")
def get_range_rdpconfigs(
    range_id: str | None = None,
    user_id: str | None = None,
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> StreamingResponse:
    """Get RDP configs as a zip file."""
    ludus = _resolve_client(registry, server)
    try:
        content = ludus.range_rdpconfigs(range_id=range_id, user_id=user_id)
    except LudusNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Range not found",
        ) from exc
    except LudusError as exc:
        logger.warning("Ludus range_rdpconfigs failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=rdpconfigs.zip"},
    )


@router.get("/range/ansibleinventory", response_model=LudusTextResponse)
def get_range_ansibleinventory(
    range_id: str | None = None,
    user_id: str | None = None,
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusTextResponse:
    """Get Ansible inventory YAML for a range."""
    ludus = _resolve_client(registry, server)
    try:
        content = ludus.range_ansibleinventory(range_id=range_id, user_id=user_id)
    except LudusNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Range not found",
        ) from exc
    except LudusError as exc:
        logger.warning("Ludus range_ansibleinventory failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    return LudusTextResponse(content=content)


@router.post("/ranges/create", response_model=LudusActionResponse)
def create_range(
    body: RangeCreateRequest,
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusActionResponse:
    """Create a new range."""
    ludus = _resolve_client(registry, server)
    try:
        ludus.range_create(
            body.name,
            body.range_id,
            description=body.description,
            purpose=body.purpose,
            user_ids=body.user_ids,
            range_number=body.range_number,
        )
    except LudusRangeExists as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Range '{body.range_id}' already exists",
        ) from exc
    except LudusError as exc:
        logger.warning("Ludus range_create failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    return LudusActionResponse(status="ok", detail="Range created")


@router.post("/ranges/assign", response_model=LudusActionResponse)
def assign_range(
    body: RangeAssignRequest,
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusActionResponse:
    """Assign a user to a range."""
    ludus = _resolve_client(registry, server)
    try:
        ludus.range_assign(body.user_id, body.range_id)
    except LudusNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User or range not found",
        ) from exc
    except LudusError as exc:
        detail = str(exc)
        if "already has access" in detail.lower():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=detail,
            ) from exc
        logger.warning("Ludus range_assign(%s, %s) failed: %s", body.user_id, body.range_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    return LudusActionResponse(status="ok", detail="User assigned to range")


@router.delete("/ranges/revoke", response_model=LudusActionResponse)
def revoke_range(
    body: RangeRevokeRequest,
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusActionResponse:
    """Revoke a user's access to a range."""
    ludus = _resolve_client(registry, server)
    try:
        ludus.range_revoke(body.user_id, body.range_id, force=body.force)
    except LudusNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User or range not found",
        ) from exc
    except LudusError as exc:
        logger.warning("Ludus range_revoke failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    return LudusActionResponse(status="ok", detail="Range access revoked")


@router.get("/ranges/{range_id}/users", response_model=LudusRangeUsersResponse)
def get_range_users(
    range_id: str,
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusRangeUsersResponse:
    """List users with access to a range."""
    ludus = _resolve_client(registry, server)
    try:
        raw = ludus.range_users(range_id)
    except LudusNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Range {range_id} not found",
        ) from exc
    except LudusError as exc:
        logger.warning("Ludus range_users(%s) failed: %s", range_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    users = [LudusRangeUser.model_validate(u) for u in raw]
    return LudusRangeUsersResponse(users=users)


@router.get("/ranges/orphaned", response_model=LudusOrphanedRangesResponse)
def list_orphaned_ranges(
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusOrphanedRangesResponse:
    """Find ranges with no Ludus user/group holding access to them.

    Calls ``GET /ranges/{rangeID}/users`` for every range Ludus reports, since
    a range's ``rangeID`` doesn't necessarily match any single owner's
    ``userID`` (ranges can be owned by a different user, or shared by
    several) - string-matching IDs would misclassify both directions. A
    per-range lookup failure is treated as "unknown", never "orphaned", so a
    transient error can't cause a live range to be hidden.
    """
    ludus = _resolve_client(registry, server)
    try:
        ranges = ludus.range_list()
    except LudusError as exc:
        logger.warning("Ludus range_list failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    orphaned: list[str] = []
    for r in ranges:
        range_id = r.get("rangeID")
        if not range_id:
            continue
        try:
            owners = ludus.range_users(range_id)
        except LudusError as exc:
            logger.warning("Ludus range_users(%s) failed: %s", range_id, exc)
            continue
        if not owners:
            orphaned.append(range_id)

    return LudusOrphanedRangesResponse(range_ids=orphaned)


@router.get("/ranges/{range_id}/diagnostics", response_model=LudusRangeDiagnosticsResponse)
def get_range_diagnostics(
    range_id: str,
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusRangeDiagnosticsResponse:
    """Bundle everything needed to explain a range's current state.

    Combines range ownership (orphaned?), the last direct-lookup error (if
    the range's Proxmox pool is gone), and the full deploy log history +
    latest log content - the same calls made by hand to diagnose a range
    showing ERROR or sitting orphaned, bundled into one request for the
    Ranges tab's "why" panel.
    """
    ludus = _resolve_client(registry, server)

    try:
        owners = [LudusRangeUser.model_validate(u) for u in ludus.range_users(range_id)]
    except LudusError as exc:
        logger.warning("Ludus range_users(%s) failed: %s", range_id, exc)
        owners = []

    range_state: str | None = None
    try:
        for r in ludus.range_list():
            if r.get("rangeID") == range_id:
                range_state = r.get("rangeState")
                break
    except LudusError as exc:
        logger.warning("Ludus range_list failed while diagnosing %s: %s", range_id, exc)

    detail_error: str | None = None
    try:
        ludus.range_get_vms(range_id=range_id)
    except LudusError as exc:
        detail_error = str(exc)

    try:
        history_raw = ludus.range_logs_history(range_id=range_id)
    except LudusError as exc:
        logger.warning("Ludus range_logs_history(%s) failed: %s", range_id, exc)
        history_raw = []
    log_history = [LudusLogHistoryEntry.model_validate(e) for e in history_raw]

    latest_log_status: str | None = None
    latest_log_content: str | None = None
    if log_history:
        latest = max(log_history, key=lambda e: e.created or e.start or "")
        if latest.id:
            try:
                entry = ludus.range_log_entry(latest.id, range_id=range_id)
            except LudusError as exc:
                logger.warning("Ludus range_log_entry(%s) failed: %s", latest.id, exc)
            else:
                latest_log_status = entry.get("status")
                latest_log_content = entry.get("result")

    return LudusRangeDiagnosticsResponse(
        range_id=range_id,
        range_state=range_state,
        owners=owners,
        orphaned=len(owners) == 0,
        detail_error=detail_error,
        log_history=log_history,
        latest_log_status=latest_log_status,
        latest_log_content=latest_log_content,
    )


@router.get("/ranges/accessible", response_model=LudusAccessibleRangesResponse)
def get_accessible_ranges(
    server: str = "default",
    _: User = Depends(get_current_user),  # noqa: B008 -- FastAPI idiom
    registry: LudusClientRegistry = Depends(get_ludus_client_registry),  # noqa: B008
) -> LudusAccessibleRangesResponse:
    """List ranges accessible to the current user."""
    ludus = _resolve_client(registry, server)
    try:
        raw = ludus.ranges_accessible()
    except LudusError as exc:
        logger.warning("Ludus ranges_accessible failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ludus error: {exc}",
        ) from exc

    ranges = [LudusAccessibleRange.model_validate(r) for r in raw]
    return LudusAccessibleRangesResponse(ranges=ranges)
