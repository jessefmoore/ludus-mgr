"""Pydantic schemas for Ludus discovery endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class LudusRange(BaseModel):
    """A single range reported by Ludus.

    Ludus v2 identifies a range by its own ``rangeID`` + ``rangeNumber`` -
    ranges are independent of users (one user may own/access several).
    ``userID`` is kept for backward compatibility with any v1 server still
    in the field; it will simply be absent from v2 responses. ``extra="allow"``
    keeps us forward-compatible with new fields Ludus might add.

    Field names match the camelCase keys returned by the Ludus API.
    """

    model_config = ConfigDict(extra="allow")

    userID: str | None = None  # noqa: N815 - matches Ludus JSON key (v1 range owner only)
    rangeID: str | None = None  # noqa: N815 - matches Ludus JSON key
    rangeNumber: int | None = None  # noqa: N815 - matches Ludus JSON key
    name: str | None = None
    numberOfVMs: int | None = None  # noqa: N815 - matches Ludus JSON key
    rangeState: str | None = None  # noqa: N815 - matches Ludus JSON key
    lastDeployment: str | None = None  # noqa: N815 - matches Ludus JSON key
    description: str | None = None
    testingEnabled: bool | None = None  # noqa: N815 - matches Ludus JSON key


class LudusRangeListResponse(BaseModel):
    """Wrapper returned by ``GET /api/ludus/ranges``."""

    ranges: list[LudusRange]


class LudusRangeConfigResponse(BaseModel):
    """Wrapper returned by ``GET /api/ludus/ranges/{range_id}/config``."""

    range_id: str
    config_yaml: str


class LudusUser(BaseModel):
    """A single user reported by Ludus.

    Field names match the camelCase keys returned by the Ludus API.
    ``extra="allow"`` keeps us forward-compatible with new fields.
    """

    model_config = ConfigDict(extra="allow")

    userID: str  # noqa: N815 - matches Ludus JSON key
    name: str | None = None
    dateCreated: str | None = None  # noqa: N815 - matches Ludus JSON key
    proxmoxUsername: str | None = None  # noqa: N815 - matches Ludus JSON key
    rangeNumber: int | None = None  # noqa: N815 - matches Ludus JSON key
    userNumber: int | None = None  # noqa: N815 - matches Ludus JSON key


class LudusUserListResponse(BaseModel):
    """Wrapper returned by ``GET /api/ludus/users``."""

    users: list[LudusUser]


class UserCreateRequest(BaseModel):
    """Request body for creating a Ludus user."""

    user_id: str
    name: str
    email: str


class UserCreateResponse(BaseModel):
    """Response from Ludus after creating a user.

    ``apiKey`` is only returned once at creation time - Ludus will not
    return it again.
    """

    model_config = ConfigDict(extra="allow")

    userID: str  # noqa: N815 - matches Ludus JSON key
    apiKey: str | None = None  # noqa: N815 - matches Ludus JSON key


class LudusActionResponse(BaseModel):
    """Generic success response for Ludus management actions."""

    status: str
    detail: str | None = None


# -- Power management -------------------------------------------------------


class PowerActionRequest(BaseModel):
    """Request body for power-on / power-off actions."""

    range_id: str | None = None
    user_id: str | None = None
    machines: list[str] = ["all"]


class DeployRequest(BaseModel):
    """Request body for range deploy action."""

    range_id: str | None = None
    user_id: str | None = None


# -- Snapshot management -----------------------------------------------------


class LudusSnapshot(BaseModel):
    """A single snapshot reported by Ludus.

    ``extra="allow"`` keeps us forward-compatible with new fields.
    """

    model_config = ConfigDict(extra="allow")

    name: str
    description: str | None = None
    vmids: list[int] | None = None


class LudusSnapshotListResponse(BaseModel):
    """Wrapper returned by ``GET /api/ludus/snapshots``."""

    snapshots: list[LudusSnapshot]


class SnapshotCreateRequest(BaseModel):
    """Request body for creating a snapshot."""

    range_id: str | None = None
    user_id: str | None = None
    name: str
    description: str = ""
    include_ram: bool = False
    vmids: list[int] | None = None


class SnapshotRevertRequest(BaseModel):
    """Request body for reverting to a snapshot."""

    range_id: str | None = None
    user_id: str | None = None
    name: str
    vmids: list[int] | None = None


class SnapshotDeleteRequest(BaseModel):
    """Request body for deleting a snapshot."""

    range_id: str | None = None
    user_id: str | None = None
    name: str
    vmids: list[int] | None = None


# -- Template management ----------------------------------------------------


class LudusTemplate(BaseModel):
    """A single VM template reported by Ludus.

    ``extra="allow"`` keeps us forward-compatible with new fields.
    """

    model_config = ConfigDict(extra="allow")

    name: str
    os: str | None = None
    built: bool | None = None
    status: str | None = None  # "built", "not_built", "building"


class LudusTemplateListResponse(BaseModel):
    """Wrapper returned by ``GET /api/ludus/templates``."""

    templates: list[LudusTemplate]


class TemplateBuildRequest(BaseModel):
    """Request body for building templates."""

    templates: list[str]
    parallel: int = 1


class LudusTemplateBuildStatus(BaseModel):
    """A single entry in the template build queue."""

    model_config = ConfigDict(extra="allow")

    template: str
    user: str | None = None


class LudusTemplateBuildStatusResponse(BaseModel):
    """Wrapper for template build status endpoint."""

    status: list[LudusTemplateBuildStatus]


# -- Range detail / VM operations -------------------------------------------


class LudusVM(BaseModel):
    """A single VM reported by Ludus range detail."""

    model_config = ConfigDict(extra="allow")

    vmID: int | None = None  # noqa: N815
    name: str | None = None
    hostname: str | None = None
    powerState: str | None = None  # noqa: N815
    testingState: str | None = None  # noqa: N815


class LudusRangeDetail(BaseModel):
    """Detailed range info including VMs."""

    model_config = ConfigDict(extra="allow")

    rangeID: str | None = None  # noqa: N815
    rangeNumber: int | None = None  # noqa: N815
    vms: list[LudusVM] = []


class LudusRangeDetailResponse(BaseModel):
    """Wrapper for range detail endpoint."""

    ranges: list[LudusRangeDetail]


class VMDestroyRequest(BaseModel):
    """Request body for VM destroy."""

    vm_id: int


class RangeAbortRequest(BaseModel):
    """Request body for range abort."""

    range_id: str | None = None
    user_id: str | None = None


class RangeDeleteVMsRequest(BaseModel):
    """Request body for deleting all VMs in a range (keeps the range object)."""

    range_id: str
    user_id: str | None = None


class RangeCreateRequest(BaseModel):
    """Request body for creating a new range."""

    name: str
    range_id: str
    description: str = ""
    purpose: str = ""
    user_ids: list[str] | None = None
    range_number: int | None = None


class RangeAssignRequest(BaseModel):
    """Request body for assigning a user to a range."""

    user_id: str
    range_id: str


class RangeRevokeRequest(BaseModel):
    """Request body for revoking range access."""

    user_id: str
    range_id: str
    force: bool = False


class LudusRangeTagsResponse(BaseModel):
    """Wrapper for range tags endpoint."""

    tags: list[str]


class LudusRangeLogsResponse(BaseModel):
    """Wrapper for range logs endpoint."""

    model_config = ConfigDict(extra="allow")

    result: str | None = None
    cursor: int | str | None = None


class LudusLogHistoryEntry(BaseModel):
    """A single log history entry."""

    model_config = ConfigDict(extra="allow")

    id: str | None = None
    status: str | None = None
    start: str | None = None
    end: str | None = None
    created: str | None = None
    template: str | None = None


class LudusLogHistoryResponse(BaseModel):
    """Wrapper for log history endpoint."""

    entries: list[LudusLogHistoryEntry]


class LudusLogEntryDetailResponse(BaseModel):
    """Wrapper for a single log entry."""

    model_config = ConfigDict(extra="allow")

    id: str | None = None
    status: str | None = None
    start: str | None = None
    end: str | None = None
    created: str | None = None
    result: str | None = None


class LudusTextResponse(BaseModel):
    """Response wrapper for text-returning endpoints (etchosts, sshconfig, etc.)."""

    content: str


class LudusRangeUser(BaseModel):
    """A user with access to a range."""

    model_config = ConfigDict(extra="allow")

    userID: str  # noqa: N815
    name: str | None = None


class LudusRangeUsersResponse(BaseModel):
    """Wrapper for range users endpoint."""

    users: list[LudusRangeUser]


class LudusAccessibleRange(BaseModel):
    """A range accessible to the current user."""

    model_config = ConfigDict(extra="allow")

    rangeID: str | None = None  # noqa: N815
    rangeNumber: int | None = None  # noqa: N815
    name: str | None = None


class LudusAccessibleRangesResponse(BaseModel):
    """Wrapper for accessible ranges endpoint."""

    ranges: list[LudusAccessibleRange]


# -- Testing state management -----------------------------------------------


class TestingStartRequest(BaseModel):
    """Request body for starting testing mode."""

    range_id: str | None = None
    user_id: str | None = None


class TestingStopRequest(BaseModel):
    """Request body for stopping testing mode."""

    range_id: str | None = None
    user_id: str | None = None
    force: bool = False


class TestingAllowDenyRequest(BaseModel):
    """Request body for allowing/denying domains/IPs during testing."""

    range_id: str | None = None
    user_id: str | None = None
    domains: list[str] | None = None
    ips: list[str] | None = None


class TestingAllowDenyResponse(BaseModel):
    """Response from allow/deny endpoints."""

    model_config = ConfigDict(extra="allow")

    result: str | None = None
    domains: list[str] | None = None
    ips: list[str] | None = None


class TestingUpdateRequest(BaseModel):
    """Request body for updating testing config."""

    name: str
    range_id: str | None = None
    user_id: str | None = None


# -- Group management -------------------------------------------------------


class GroupCreateRequest(BaseModel):
    """Request body for creating a group."""

    name: str
    description: str | None = None


class LudusGroup(BaseModel):
    """A single group reported by Ludus."""

    model_config = ConfigDict(extra="allow")

    name: str
    description: str | None = None


class LudusGroupListResponse(BaseModel):
    """Wrapper for group list endpoint."""

    groups: list[LudusGroup]


class LudusGroupUser(BaseModel):
    """A user in a group."""

    model_config = ConfigDict(extra="allow")

    userID: str  # noqa: N815
    name: str | None = None
    role: str | None = None  # "member" | "manager"


class LudusGroupUsersResponse(BaseModel):
    """Wrapper for group users endpoint."""

    users: list[LudusGroupUser]


class GroupModifyUsersRequest(BaseModel):
    """Request body for adding/removing users from a group.

    ``managers`` is the subset of ``user_ids`` that should be group
    managers (Ludus wants an array of userIDs here, not a bool).
    """

    user_ids: list[str]
    managers: list[str] | None = None


class GroupModifyUsersResponse(BaseModel):
    """Response from group user modification."""

    model_config = ConfigDict(extra="allow")

    success: list[str] = []
    errors: list[dict] = []


class GroupModifyRangesRequest(BaseModel):
    """Request body for adding/removing ranges from a group."""

    range_ids: list[str]


class GroupModifyRangesResponse(BaseModel):
    """Response from group range modification."""

    model_config = ConfigDict(extra="allow")

    success: list[str] = []
    errors: list[dict] = []


class LudusGroupRangesResponse(BaseModel):
    """Wrapper for group ranges endpoint."""

    model_config = ConfigDict(extra="allow")

    ranges: list[LudusAccessibleRange]


# -- Ansible management -----------------------------------------------------


class LudusSubscriptionRole(BaseModel):
    """A subscription role."""

    model_config = ConfigDict(extra="allow")

    name: str
    description: str | None = None


class LudusSubscriptionRolesResponse(BaseModel):
    """Wrapper for subscription roles endpoint."""

    roles: list[LudusSubscriptionRole]


class AnsibleInstallSubRolesRequest(BaseModel):
    """Request body for installing subscription roles."""

    roles: list[str]
    global_: bool = False
    force: bool = False


class AnsibleRoleVarsRequest(BaseModel):
    """Request body for getting role variables."""

    roles: list[str]


class LudusRoleVar(BaseModel):
    """The configurable variables for a single ansible role.

    Matches ``POST /ansible/role/vars``'s response shape: one entry per
    requested role, with its ``defaults/main.yml`` variables nested under
    ``vars``.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str
    global_: bool = Field(False, alias="global")
    vars: dict[str, Any] = {}


class LudusRoleVarsResponse(BaseModel):
    """Wrapper for role vars endpoint."""

    vars: list[LudusRoleVar]


class LudusInstalledRole(BaseModel):
    """An installed ansible role/collection."""

    model_config = ConfigDict(extra="allow")

    name: str
    version: str | None = None
    scope: str | None = None
    type: str | None = None


class LudusInstalledRolesResponse(BaseModel):
    """Wrapper for installed roles endpoint."""

    roles: list[LudusInstalledRole]


class AnsibleRoleScopeRequest(BaseModel):
    """Request body for changing role scope."""

    model_config = ConfigDict(populate_by_name=True)

    roles: list[str]
    global_: bool = False
    copy_roles: bool = Field(False, alias="copy")


class AnsibleRoleActionRequest(BaseModel):
    """Request body for role install/remove/update."""

    role: str
    action: str
    version: str | None = None
    force: bool = False
    global_: bool = False


class AnsibleCollectionRequest(BaseModel):
    """Request body for installing a collection."""

    collection: str
    version: str | None = None
    force: bool = False


class LudusCapacityResponse(BaseModel):
    """Host CPU/RAM capacity vs this app's committed session allocation.

    ``configured`` is False when no capacity has been set, in which case the
    ``*_available`` fields are null and the UI prompts the operator to
    configure the host's totals. ``*_allocated`` is the CPU/RAM committed by
    the app's live (active/provisioning) sessions on this server. Available may
    be negative when overcommitted - reported truthfully, not clamped.
    """

    server: str
    configured: bool
    cpu_capacity: int | None = None
    ram_capacity_gb: int | None = None
    cpu_allocated: int
    ram_allocated_gb: int
    cpu_available: int | None = None
    ram_available_gb: int | None = None
    session_count: int
