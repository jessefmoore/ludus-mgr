"""Single integration point between the platform and a Ludus server.

Every Ludus HTTP call in the backend MUST go through `LudusClient`. This
makes it trivial to swap transports, apply retries/timeouts/logging in one
place, and mock Ludus in tests.

================================================================
TARGET: Ludus v2 (verified against a live 2.3.10+fccf931e server)
================================================================
Base path:              (none - routes are bare, e.g. /user, /range)
Auth header:            X-API-KEY: <api_key>
Admin impersonation:    append ?userID=<userID> query param (admin key only)

Ludus v2 decouples ranges from users: a range is addressed by its own
``rangeID`` (``?rangeID=``), not by its owner's ``userID``. One user can
own/access multiple ranges, and ranges are no longer auto-created alongside
a user - ``POST /ranges/create`` must be called explicitly.

This platform deliberately keeps a 1:1 "one range per student" convention
(see callers in app.services.provision/teardown) by always using the
student's Ludus userID as that student's rangeID. That is a *product*
choice, not a protocol requirement - nothing here stops a caller from using
a different rangeID.

Routes used here (verified against the live v2 OpenAPI spec):
    POST   /user                                   -> user_add   {userID,name,email,isAdmin,password?}
    DELETE /user/{userID}[?deleteDefaultRange=]     -> user_rm
    GET    /user/all                                -> user_list
    GET    /user/wireguard?userID=<id>              -> user_wireguard  {result:{wireGuardConfig}}
    GET    /user/default-range?userID=              -> user_default_range_get {defaultRangeID}
    POST   /user/default-range?userID=              -> user_default_range_set
    GET    /whoami?userID=&rangeID=                 -> whoami
    POST   /ranges/create                           -> range_create  {name,rangeID,description,purpose,userID[],rangeNumber}
    POST   /ranges/assign/{userID}/{rangeID}        -> range_assign
    DELETE /ranges/revoke/{userID}/{rangeID}[?force=] -> range_revoke
    GET    /ranges/{rangeID}/users                  -> range_users
    GET    /ranges/accessible                       -> ranges_accessible
    PUT    /range/config?rangeID=<id>               -> range_deploy step 1 (multipart file, force)
    POST   /range/deploy?rangeID=<id>               -> range_deploy step 2 / range_deploy_existing
    DELETE /range?rangeID=<id>[&force=true]         -> range_destroy (destroys VMs + Proxmox pool;
                                                        the range row itself is NOT removed from Ludus's
                                                        DB - it persists forever as rangeState=DESTROYED,
                                                        numberOfVMs=0. The UI must filter these out itself;
                                                        see isLiveRange() in LudusManagement.tsx)
    DELETE /range/{rangeID}/vms?userID=             -> range_delete_vms (VM-only, keeps the range object)
    GET    /range/all                               -> range_list
    GET    /range?rangeID=<id>                      -> range_get_vms  (single obj with VMs[])
    GET    /range/config?rangeID=<id>               -> range_get_config  {result:<yaml>}
    GET    /range/config/example                    -> range_config_example
    POST   /range/abort?rangeID=<id>                -> range_abort
    PUT    /range/poweron?rangeID=<id>              -> range_power_on   {machines:[...]}
    PUT    /range/poweroff?rangeID=<id>             -> range_power_off  {machines:[...]}
    GET    /range/tags                              -> range_tags
    GET    /range/logs?rangeID=<id>[&tail=&cursor=] -> range_logs
    GET    /range/logs/history?rangeID=<id>         -> range_logs_history
    GET    /range/logs/history/{logID}?rangeID=     -> range_log_entry
    GET    /range/etchosts?rangeID=<id>             -> range_etchosts
    GET    /range/sshconfig                         -> range_sshconfig
    GET    /range/rdpconfigs?rangeID=<id>           -> range_rdpconfigs
    GET    /range/ansibleinventory?rangeID=<id>     -> range_ansibleinventory
    GET    /snapshots/list?rangeID=<id>             -> snapshot_list  {snapshots:[...]}
    POST   /snapshots/create?rangeID=<id>           -> snapshot_create
    POST   /snapshots/rollback?rangeID=<id>         -> snapshot_revert
    POST   /snapshots/remove?rangeID=<id>           -> snapshot_delete
    PUT    /testing/start?rangeID=<id>              -> testing_start
    PUT    /testing/stop?rangeID=<id>               -> testing_stop
    POST   /testing/allow?rangeID=<id>              -> testing_allow
    POST   /testing/deny?rangeID=<id>               -> testing_deny
    POST   /testing/update?rangeID=<id>             -> testing_update
    GET    /templates                               -> template_list
    POST   /templates                               -> template_build  {templates:[...], parallel}
    POST   /templates/abort                         -> template_abort
    GET    /templates/status                        -> template_status
    GET    /templates/logs                          -> template_logs
    DELETE /template/{name}                          -> template_delete
    GET    /ansible?userID=<id>                     -> ansible_list
    POST   /ansible/role                            -> ansible_role  {role,action,global,force,version}
    POST   /ansible/collection                      -> ansible_collection
    PUT    /ansible/role/fromtar                    -> ansible_role_from_tar
    POST   /ansible/role/vars                       -> ansible_role_vars  {roles:[...]}
    PATCH  /ansible/role/scope?userID=              -> ansible_role_scope {roles,global,copy}
    GET    /groups                                  -> group_list
    POST   /groups                                  -> group_create  {name,description}
    DELETE /groups/{groupName}                       -> group_delete
    GET    /groups/{groupName}/users                -> group_users
    POST   /groups/{groupName}/users                -> group_add_users    {userIDs,managers?}
    DELETE /groups/{groupName}/users                -> group_remove_users {userIDs}
    GET    /groups/{groupName}/ranges               -> group_ranges
    POST   /groups/{groupName}/ranges               -> group_add_ranges    {rangeIDs}
    DELETE /groups/{groupName}/ranges               -> group_remove_ranges {rangeIDs}
    DELETE /vm/{vmID}                                -> vm_destroy

NOT AVAILABLE on Ludus v2 (method raises LudusNotSupported - no endpoint
in the current OpenAPI spec):
    ansible_subscription_roles, ansible_install_subscription_roles.
================================================================
"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Any

import httpx

from app.core.config import get_settings
from app.services.exceptions import (
    LudusAuthError,
    LudusError,
    LudusNotFound,
    LudusNotSupported,
    LudusRangeExists,
    LudusTimeout,
    LudusUserExists,
)

logger = logging.getLogger(__name__)

# Ludus v2 mounts its REST API under /api/v2 (confirmed live against a
# 192.168.0.191 deployment on 2026-11-26 - every route 404s at the bare
# root path, e.g. GET /user/all 404s but GET /api/v2/user/all succeeds).
API_BASE = "/api/v2"


def _extract_error_detail(response: httpx.Response) -> str:
    """Pull a human-readable error message from a Ludus response."""
    try:
        data = response.json()
    except ValueError:
        return response.text or response.reason_phrase or "Ludus error"
    if isinstance(data, dict):
        for key in ("error", "message", "detail", "result"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
    return response.text or "Ludus error"


def _raise_for_status(
    response: httpx.Response,
    *,
    on_conflict_user_exists: bool = False,
    on_conflict_range_exists: bool = False,
) -> None:
    """Translate a Ludus HTTP response into the appropriate typed exception.

    This is the single place where HTTP status codes are converted to
    `LudusError` subclasses. Callers pass `on_conflict_user_exists=True`
    for user_add / `on_conflict_range_exists=True` for range_create so a
    409 (or a 400 whose body says "already exists") becomes the matching
    typed exception instead of a generic `LudusError`.

    Exception: a 400 saying the name "already exists on the host system" is a
    *PAM/OS* username collision (a leftover Linux user), NOT an existing Ludus
    user. Treating it as "exists" makes the app skip user creation and then
    fail confusingly at range_deploy with "User not found", so it is surfaced
    as a real error instead.
    """
    status = response.status_code
    if 200 <= status < 300:
        return

    detail = _extract_error_detail(response)
    detail_lower = detail.lower()
    conflict_like = status == 409 or (
        status == 400 and "already exists" in detail_lower and "host system" not in detail_lower
    )

    if status in (401, 403):
        raise LudusAuthError(detail, status_code=status)
    if status == 404:
        raise LudusNotFound(detail, status_code=status)
    if on_conflict_user_exists and conflict_like:
        raise LudusUserExists(detail, status_code=status)
    if on_conflict_range_exists and conflict_like:
        raise LudusRangeExists(detail, status_code=status)

    raise LudusError(detail, status_code=status)


def _range_params(
    range_id: str | None = None,
    user_id: str | None = None,
    **extra: Any,
) -> dict[str, Any] | None:
    """Build a query-param dict addressing a range (``rangeID``) and/or
    impersonating a user (``userID``, admin key only).

    Returns ``None`` when there are no params so httpx omits the query
    string entirely (acting as the API key's own default range/user).
    """
    params: dict[str, Any] = {}
    if range_id is not None:
        params["rangeID"] = range_id
    if user_id is not None:
        params["userID"] = user_id
    for key, value in extra.items():
        if value is not None:
            params[key] = value
    return params or None


class LudusClient:
    """Synchronous HTTP wrapper for the Ludus v2 REST API.

    The client is intentionally thin - each method maps to one logical
    Ludus operation and never leaks `httpx` types to callers. Errors are
    always raised as subclasses of `LudusError`. The handful of operations
    with no v2 equivalent raise `LudusNotSupported`.
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        verify_tls: bool = False,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
        admin_url: str | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        # Allow dependency injection for tests (e.g. httpx.MockTransport).
        if client is not None:
            self._client = client
        else:
            # Note: we intentionally do NOT set a default Content-Type
            # header on the Client. httpx auto-generates the correct
            # value per request (application/json when `json=` is set,
            # multipart/form-data with a unique boundary when `files=`
            # is set). Setting a default header would override the
            # multipart boundary and break uploads.
            self._client = httpx.Client(
                base_url=self._url,
                verify=verify_tls,
                timeout=timeout,
                headers={
                    "X-API-KEY": api_key,
                    "Accept": "application/json",
                },
            )
        self._owns_client = client is None

        # Some Ludus deployments still split user management onto a
        # separate admin-only listener. When ``admin_url`` is set, user
        # writes go there; otherwise they use the primary client.
        if admin_url and client is None and admin_url.rstrip("/") != self._url:
            self._admin_url = admin_url.rstrip("/")
            self._admin_client = httpx.Client(
                base_url=self._admin_url,
                verify=verify_tls,
                timeout=timeout,
                headers={"X-API-KEY": api_key, "Accept": "application/json"},
            )
            self._owns_admin_client = True
        else:
            self._admin_url = self._url
            self._admin_client = self._client
            self._owns_admin_client = False
        logger.debug(
            "LudusClient initialised for %s (admin: %s)", self._url, self._admin_url
        )

    # -- context manager / lifecycle -------------------------------------

    def __enter__(self) -> LudusClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying httpx client(s) if we own them."""
        if self._owns_client:
            self._client.close()
        if self._owns_admin_client:
            self._admin_client.close()

    # -- low-level helpers -----------------------------------------------

    @staticmethod
    def _safe_url_for_log(path: str) -> str:
        """Return a path without any query string - avoids leaking rangeIDs
        or other parameters into logs."""
        return path.split("?", 1)[0]

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        on_conflict_user_exists: bool = False,
        on_conflict_range_exists: bool = False,
        client: httpx.Client | None = None,
        timeout: Any = httpx.USE_CLIENT_DEFAULT,
    ) -> httpx.Response:
        """Dispatch a request through an httpx.Client (primary by default).

        Pass ``client=self._admin_client`` for user-management writes that
        some Ludus deployments only serve on the admin API. ``timeout``
        overrides the client default for a single slow call (e.g. user
        deletion, which tears down a Proxmox user/pool). Handles timeout
        translation and delegates status checking to `_raise_for_status`.
        Never logs the api key or raw query string.
        """
        http = client or self._client
        try:
            response = http.request(
                method,
                path,
                json=json,
                params=params,
                files=files,
                data=data,
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            logger.warning(
                "Ludus request timed out: %s %s",
                method,
                self._safe_url_for_log(path),
            )
            raise LudusTimeout(f"Ludus request timed out: {exc}") from exc
        except httpx.RequestError as exc:
            logger.warning(
                "Ludus request transport error: %s %s",
                method,
                self._safe_url_for_log(path),
            )
            raise LudusError(f"Ludus transport error: {exc}") from exc

        logger.debug(
            "Ludus %s %s -> %d",
            method,
            self._safe_url_for_log(path),
            response.status_code,
        )
        _raise_for_status(
            response,
            on_conflict_user_exists=on_conflict_user_exists,
            on_conflict_range_exists=on_conflict_range_exists,
        )
        return response

    def _json(self, response: httpx.Response, op: str) -> Any:
        """Parse a JSON response body or raise a descriptive LudusError."""
        try:
            return response.json()
        except ValueError as exc:
            raise LudusError(
                f"Ludus returned invalid JSON for {op}",
                status_code=response.status_code,
            ) from exc

    @staticmethod
    def _as_list(data: Any) -> list[dict]:
        """Normalise a list-or-single-object response to a list of dicts."""
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
        return []

    @staticmethod
    def _unwrap_result_str(response: httpx.Response) -> str:
        """Return a string payload from a ``{"result": "..."}`` envelope
        or raw text, whichever the server sent."""
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                body = response.json()
            except ValueError:
                return response.text
            if isinstance(body, dict):
                result = body.get("result")
                if isinstance(result, str):
                    return result
            return response.text
        return response.text

    def _try_json(self, response: httpx.Response) -> dict:
        """Best-effort JSON parse for fire-and-forget action endpoints."""
        try:
            return response.json()
        except ValueError:
            return {"result": "ok"}

    # -- user management -------------------------------------------------

    # Creating a Ludus user provisions a Proxmox user + pool synchronously
    # ("this can take up to a minute" per the docs), which exceeds the default
    # request timeout on a busy host.
    USER_ADD_TIMEOUT = 120.0

    def user_add(
        self,
        userid: str,
        name: str,
        email: str,
        *,
        is_admin: bool = False,
        password: str | None = None,
    ) -> dict:
        """Create a Ludus user.

        Route:  POST /user   body {userID, name, email, isAdmin, password?}
        The response contains the user's plaintext ``apiKey`` (only ever
        returned here; otherwise reset via /user/apikey).

        Raises LudusUserExists on 400/409 "already exists", LudusAuthError
        on 401/403, LudusError otherwise.
        """
        payload: dict[str, Any] = {
            "userID": userid,
            "name": name,
            "email": email,
            "isAdmin": is_admin,
        }
        if password is not None:
            payload["password"] = password
        response = self._request(
            "POST",
            f"{API_BASE}/user",
            json=payload,
            on_conflict_user_exists=True,
            client=self._admin_client,
            timeout=self.USER_ADD_TIMEOUT,
        )
        data = self._json(response, "user_add")
        if not isinstance(data, dict):
            raise LudusError(
                f"Unexpected user_add response shape: {type(data).__name__}",
                status_code=response.status_code,
            )
        return data

    # Deleting a Ludus user tears down its Proxmox user/pool, which can take
    # well over the default request timeout on a busy host.
    USER_DELETE_TIMEOUT = 180.0

    def user_rm(self, userid: str, *, delete_range: bool = False) -> None:
        """Delete a Ludus user.  Route: DELETE /user/{userID}.

        When *delete_range* is True, ``deleteDefaultRange=true`` is passed so
        Ludus atomically destroys the user's default range (and Proxmox pool)
        as part of removing the user. This is the safe way to fully remove a
        user that still owns VMs: a separate range delete is asynchronous and
        would race the user's pool delete, orphaning VMs.
        """
        params = {"deleteDefaultRange": "true"} if delete_range else None
        self._request(
            "DELETE",
            f"{API_BASE}/user/{userid}",
            params=params,
            client=self._admin_client,
            timeout=self.USER_DELETE_TIMEOUT,
        )

    def user_list(self) -> list[dict]:
        """List all users.  Route: GET /user/all -> list of user dicts."""
        response = self._request("GET", f"{API_BASE}/user/all")
        return self._as_list(self._json(response, "user_list"))

    def user_wireguard(self, userid: str) -> str:
        """Return the WireGuard .conf text for a user.

        Route:  GET /user/wireguard?userID=<id>
        Response: JSON ``{"result": {"wireGuardConfig": "<...>"}}`` (verified),
        with a raw-text fallback for older/alternate builds.
        """
        response = self._request(
            "GET",
            f"{API_BASE}/user/wireguard",
            params={"userID": userid},
        )
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            body = self._json(response, "user_wireguard")
            if isinstance(body, dict):
                result = body.get("result")
                if isinstance(result, dict):
                    cfg = result.get("wireGuardConfig")
                    if isinstance(cfg, str):
                        return cfg
                cfg = body.get("wireGuardConfig")
                if isinstance(cfg, str):
                    return cfg
            raise LudusError(
                "Ludus user_wireguard response missing wireGuardConfig",
                status_code=response.status_code,
            )
        return response.text

    def user_default_range_get(self, *, user_id: str | None = None) -> str | None:
        """Return a user's default rangeID.  Route: GET /user/default-range?userID=."""
        response = self._request(
            "GET",
            f"{API_BASE}/user/default-range",
            params=_range_params(user_id=user_id),
        )
        data = self._json(response, "user_default_range_get")
        return data.get("defaultRangeID") if isinstance(data, dict) else None

    def user_default_range_set(self, range_id: str, *, user_id: str | None = None) -> None:
        """Set a user's default rangeID.  Route: POST /user/default-range?userID=."""
        self._request(
            "POST",
            f"{API_BASE}/user/default-range",
            params=_range_params(user_id=user_id),
            json={"defaultRangeID": range_id},
        )

    # -- identity ----------------------------------------------------------

    def whoami(self, *, user_id: str | None = None, range_id: str | None = None) -> dict:
        """Return the user+range that would be used for a request.

        Route:  GET /whoami?userID=&rangeID=
        """
        response = self._request(
            "GET",
            f"{API_BASE}/whoami",
            params=_range_params(range_id, user_id),
        )
        return self._json(response, "whoami")

    # -- range creation / sharing -----------------------------------------

    def range_create(
        self,
        name: str,
        range_id: str,
        *,
        description: str = "",
        purpose: str = "",
        user_ids: list[str] | None = None,
        range_number: int | None = None,
    ) -> dict:
        """Create a new range.  Route: POST /ranges/create.

        ``user_ids`` pre-assigns access to the new range at creation time
        (used by provisioning to hand the range straight to its owning
        student). Raises ``LudusRangeExists`` on 400/409 "already exists".
        """
        body: dict[str, Any] = {"name": name, "rangeID": range_id}
        if description:
            body["description"] = description
        if purpose:
            body["purpose"] = purpose
        if user_ids is not None:
            body["userID"] = user_ids
        if range_number is not None:
            body["rangeNumber"] = range_number
        response = self._request(
            "POST",
            f"{API_BASE}/ranges/create",
            json=body,
            on_conflict_range_exists=True,
        )
        data = self._json(response, "range_create")
        return data.get("result", data) if isinstance(data, dict) else data

    def range_assign(self, user_id: str, range_id: str) -> None:
        """Grant *user_id* access to *range_id* (admin only).

        Route:  POST /ranges/assign/{userID}/{rangeID}
        """
        self._request("POST", f"{API_BASE}/ranges/assign/{user_id}/{range_id}", json={})

    def range_revoke(self, user_id: str, range_id: str, *, force: bool = False) -> None:
        """Revoke *user_id*'s access to *range_id* (admin only).

        Route:  DELETE /ranges/revoke/{userID}/{rangeID}[?force=]
        """
        params = {"force": "true"} if force else None
        self._request(
            "DELETE", f"{API_BASE}/ranges/revoke/{user_id}/{range_id}", params=params
        )

    def range_users(self, range_id: str) -> list[dict]:
        """List users with access to a range (admin only).

        Route:  GET /ranges/{rangeID}/users
        """
        response = self._request("GET", f"{API_BASE}/ranges/{range_id}/users")
        return self._as_list(self._json(response, "range_users"))

    def ranges_accessible(self) -> list[dict]:
        """List ranges accessible to the current (API key's) user.

        Route:  GET /ranges/accessible
        """
        response = self._request("GET", f"{API_BASE}/ranges/accessible")
        return self._as_list(self._json(response, "ranges_accessible"))

    # -- range deploy / config / lifecycle -------------------------------

    def range_deploy(self, range_id: str, config_yaml: str) -> None:
        """Upload a range config and deploy it for *range_id*.

        Two-step Ludus flow:
            1. PUT  /range/config?rangeID=<id>   multipart file=<yaml>, force=true
            2. POST /range/deploy?rangeID=<id>   body {}

        The range must already exist (``range_create``) - v2 no longer
        auto-creates a range alongside a user.
        """
        params = {"rangeID": range_id}
        self._request(
            "PUT",
            f"{API_BASE}/range/config",
            params=params,
            files={"file": ("range-config.yml", config_yaml, "application/x-yaml")},
            data={"force": "true"},
        )
        self._request(
            "POST",
            f"{API_BASE}/range/deploy",
            params=params,
            json={},
        )

    def range_deploy_existing(
        self,
        *,
        range_id: str | None = None,
        user_id: str | None = None,
        tags: str | None = None,
        force: bool = False,
        only_roles: list[str] | None = None,
        limit: str | None = None,
    ) -> None:
        """Deploy an already-configured range.  Route: POST /range/deploy."""
        body: dict[str, Any] = {}
        if tags is not None:
            body["tags"] = tags
        if force:
            body["force"] = True
        if only_roles is not None:
            body["only_roles"] = only_roles
        if limit is not None:
            body["limit"] = limit
        self._request(
            "POST",
            f"{API_BASE}/range/deploy",
            params=_range_params(range_id, user_id),
            json=body,
        )

    def range_list(self) -> list[dict]:
        """List summary info for all ranges.  Route: GET /range/all.

        Each item is ``{rangeID, rangeNumber, name, lastDeployment,
        numberOfVMs, testingEnabled, rangeState, ...}``. Admins get every
        range on the host.
        """
        response = self._request("GET", f"{API_BASE}/range/all")
        return self._as_list(self._json(response, "range_list"))

    def range_get_config(
        self, *, range_id: str | None = None, user_id: str | None = None
    ) -> str:
        """Return the range-config YAML for a range.

        Route:  GET /range/config?rangeID=<id>  -> {"result": "<yaml>"} or raw.
        """
        response = self._request(
            "GET",
            f"{API_BASE}/range/config",
            params=_range_params(range_id, user_id),
        )
        return self._unwrap_result_str(response)

    def range_get_vms(
        self, *, range_id: str | None = None, user_id: str | None = None
    ) -> dict:
        """Return a range with VM power/state.

        Route:  GET /range?rangeID=<id>
        Response is a single object: ``{rangeID, rangeNumber, numberOfVMs,
        testingEnabled, rangeState, VMs:[{ID, proxmoxID, name, poweredOn,
        ip}], ...}``.
        """
        response = self._request(
            "GET", f"{API_BASE}/range", params=_range_params(range_id, user_id)
        )
        data = self._json(response, "range_get_vms")
        if isinstance(data, dict):
            return data
        raise LudusError(
            f"Unexpected range_get_vms response shape: {type(data).__name__}",
            status_code=response.status_code,
        )

    def range_destroy(
        self, *, range_id: str | None = None, user_id: str | None = None, force: bool = False
    ) -> None:
        """Destroy a range's VMs and Proxmox pool.

        Route:  DELETE /range?rangeID=[&force=]

        Unlike Ludus v1's ``range rm``, this tears down more than VMs - the
        range must be re-created via ``range_create`` before it can be
        deployed again. ``force=True`` is required if the range still has
        VMs; use ``range_delete_vms`` instead if you only want to wipe VMs
        and keep the range object.

        Confirmed live against a Ludus v2 server: despite the name, this does
        NOT remove the range's row from ``GET /range/all`` - it persists
        forever with ``rangeState: "DESTROYED"``, ``numberOfVMs: 0``, tied to
        the owning Ludus user. Callers that list ranges for display must
        filter these out themselves (see ``isLiveRange()`` in
        ``LudusManagement.tsx``).
        """
        self._request(
            "DELETE",
            f"{API_BASE}/range",
            params=_range_params(range_id, user_id, force=True if force else None),
        )

    def range_delete_vms(self, range_id: str, *, user_id: str | None = None) -> None:
        """Stop and delete all VMs in a range, keeping the range object.

        Route:  DELETE /range/{rangeID}/vms?userID=
        """
        self._request(
            "DELETE",
            f"{API_BASE}/range/{range_id}/vms",
            params=_range_params(user_id=user_id),
        )

    def range_abort(
        self, *, range_id: str | None = None, user_id: str | None = None
    ) -> dict:
        """Abort a running deployment.  Route: POST /range/abort?rangeID=."""
        response = self._request(
            "POST", f"{API_BASE}/range/abort", params=_range_params(range_id, user_id)
        )
        return self._try_json(response)

    def range_tags(self) -> list[str]:
        """List available deploy tags.  Route: GET /range/tags -> {tags:[...]}."""
        response = self._request("GET", f"{API_BASE}/range/tags")
        data = self._json(response, "range_tags")
        if isinstance(data, dict):
            return data.get("tags", [])
        if isinstance(data, list):
            return data
        return []

    def range_config_example(self) -> str:
        """Return an example range config.  Route: GET /range/config/example."""
        response = self._request("GET", f"{API_BASE}/range/config/example")
        return self._unwrap_result_str(response)

    # -- power management ------------------------------------------------

    def range_power_on(
        self,
        *,
        range_id: str | None = None,
        user_id: str | None = None,
        machines: list[str] | None = None,
    ) -> None:
        """Power on VMs.  Route: PUT /range/poweron?rangeID=  body {machines}."""
        self._request(
            "PUT",
            f"{API_BASE}/range/poweron",
            params=_range_params(range_id, user_id),
            json={"machines": machines or ["all"]},
        )

    def range_power_off(
        self,
        *,
        range_id: str | None = None,
        user_id: str | None = None,
        machines: list[str] | None = None,
    ) -> None:
        """Power off VMs.  Route: PUT /range/poweroff?rangeID=  body {machines}."""
        self._request(
            "PUT",
            f"{API_BASE}/range/poweroff",
            params=_range_params(range_id, user_id),
            json={"machines": machines or ["all"]},
        )

    # -- snapshot management ---------------------------------------------

    def snapshot_list(
        self, *, range_id: str | None = None, user_id: str | None = None
    ) -> list[dict]:
        """List snapshots for a range.

        Route:  GET /snapshots/list?rangeID=<id>
        Response: ``{"errors": ..., "snapshots": [ {name, includesRAM,
        description, snaptime, parent, vmid, vmname}, ... ]}``.  Returns the
        inner ``snapshots`` list.
        """
        response = self._request(
            "GET",
            f"{API_BASE}/snapshots/list",
            params=_range_params(range_id, user_id),
        )
        data = self._json(response, "snapshot_list")
        if isinstance(data, dict):
            snaps = data.get("snapshots")
            if isinstance(snaps, list):
                return snaps
            return []
        return self._as_list(data)

    def snapshot_create(
        self,
        name: str,
        *,
        range_id: str | None = None,
        user_id: str | None = None,
        description: str = "",
        include_ram: bool = True,
        vmids: list[int] | None = None,
    ) -> dict:
        """Create a snapshot.  Route: POST /snapshots/create?rangeID=.

        Note: Ludus includes RAM by default (CLI ``--noRAM`` to exclude),
        so ``include_ram`` defaults to True to match server behaviour.
        ``vmids`` limits to specific Proxmox VM IDs (default: all in range).
        """
        body: dict[str, Any] = {
            "name": name,
            "description": description,
            "includeRAM": include_ram,
        }
        if vmids is not None:
            body["vmids"] = vmids
        response = self._request(
            "POST",
            f"{API_BASE}/snapshots/create",
            params=_range_params(range_id, user_id),
            json=body,
        )
        return self._try_json(response)

    def snapshot_revert(
        self,
        name: str,
        *,
        range_id: str | None = None,
        user_id: str | None = None,
        vmids: list[int] | None = None,
    ) -> None:
        """Revert a range to a named snapshot.

        Route:  POST /snapshots/rollback?rangeID=<id>  body {name, vmids?}
        """
        body: dict[str, Any] = {"name": name}
        if vmids is not None:
            body["vmids"] = vmids
        self._request(
            "POST",
            f"{API_BASE}/snapshots/rollback",
            params=_range_params(range_id, user_id),
            json=body,
        )

    def snapshot_delete(
        self,
        name: str,
        *,
        range_id: str | None = None,
        user_id: str | None = None,
        vmids: list[int] | None = None,
    ) -> None:
        """Delete a snapshot.  Route: POST /snapshots/remove?rangeID=  body {name, vmids?}."""
        body: dict[str, Any] = {"name": name}
        if vmids is not None:
            body["vmids"] = vmids
        self._request(
            "POST",
            f"{API_BASE}/snapshots/remove",
            params=_range_params(range_id, user_id),
            json=body,
        )

    # -- testing state management ----------------------------------------

    def testing_start(
        self, *, range_id: str | None = None, user_id: str | None = None
    ) -> dict:
        """Enter testing mode.  Route: PUT /testing/start?rangeID=."""
        response = self._request(
            "PUT", f"{API_BASE}/testing/start", params=_range_params(range_id, user_id)
        )
        return self._try_json(response)

    def testing_stop(
        self,
        *,
        range_id: str | None = None,
        user_id: str | None = None,
        force: bool = False,
    ) -> dict:
        """Exit testing mode.  Route: PUT /testing/stop?rangeID=  body {force?}."""
        response = self._request(
            "PUT",
            f"{API_BASE}/testing/stop",
            params=_range_params(range_id, user_id),
            json={"force": force} if force else {},
        )
        return self._try_json(response)

    def testing_allow(
        self,
        *,
        range_id: str | None = None,
        user_id: str | None = None,
        domains: list[str] | None = None,
        ips: list[str] | None = None,
    ) -> dict:
        """Allow domains/IPs during testing.  Route: POST /testing/allow?rangeID=."""
        body: dict[str, Any] = {}
        if domains is not None:
            body["domains"] = domains
        if ips is not None:
            body["ips"] = ips
        response = self._request(
            "POST",
            f"{API_BASE}/testing/allow",
            params=_range_params(range_id, user_id),
            json=body,
        )
        return self._try_json(response)

    def testing_deny(
        self,
        *,
        range_id: str | None = None,
        user_id: str | None = None,
        domains: list[str] | None = None,
        ips: list[str] | None = None,
    ) -> dict:
        """Deny domains/IPs during testing.  Route: POST /testing/deny?rangeID=."""
        body: dict[str, Any] = {}
        if domains is not None:
            body["domains"] = domains
        if ips is not None:
            body["ips"] = ips
        response = self._request(
            "POST",
            f"{API_BASE}/testing/deny",
            params=_range_params(range_id, user_id),
            json=body,
        )
        return self._try_json(response)

    def testing_update(
        self, name: str, *, range_id: str | None = None, user_id: str | None = None
    ) -> dict:
        """Update a VM/group's testing config.  Route: POST /testing/update?rangeID=."""
        response = self._request(
            "POST",
            f"{API_BASE}/testing/update",
            params=_range_params(range_id, user_id),
            json={"name": name},
        )
        return self._try_json(response)

    # -- template management ---------------------------------------------

    def template_list(self) -> list[dict]:
        """List VM templates.  Route: GET /templates."""
        response = self._request("GET", f"{API_BASE}/templates")
        return self._as_list(self._json(response, "template_list"))

    def template_delete(self, name: str) -> None:
        """Delete a template.  Route: DELETE /template/{name}."""
        self._request("DELETE", f"{API_BASE}/template/{name}")

    def template_build(self, templates: list[str], *, parallel: int = 1) -> None:
        """Build templates via Packer.  Route: POST /templates  body {templates, parallel}."""
        self._request(
            "POST",
            f"{API_BASE}/templates",
            json={"templates": templates, "parallel": parallel},
        )

    def template_abort(self) -> None:
        """Abort a running Packer build.  Route: POST /templates/abort."""
        self._request("POST", f"{API_BASE}/templates/abort")

    def template_status(self) -> list[dict]:
        """Get the Packer build queue/status.  Route: GET /templates/status."""
        response = self._request("GET", f"{API_BASE}/templates/status")
        return self._as_list(self._json(response, "template_status"))

    def template_logs(self) -> str:
        """Get live Packer build logs.  Route: GET /templates/logs."""
        response = self._request("GET", f"{API_BASE}/templates/logs")
        return self._unwrap_result_str(response)

    # -- range detail (read-only) ----------------------------------------

    def range_logs(
        self,
        *,
        range_id: str | None = None,
        user_id: str | None = None,
        tail: int | None = None,
        cursor: int | None = None,
    ) -> dict:
        """Get the latest deploy logs.  Route: GET /range/logs?rangeID=[&tail=&cursor=]."""
        response = self._request(
            "GET",
            f"{API_BASE}/range/logs",
            params=_range_params(range_id, user_id, tail=tail, cursor=cursor),
        )
        return self._try_json(response)

    def range_logs_history(
        self, *, range_id: str | None = None, user_id: str | None = None
    ) -> list[dict]:
        """List historical deploy log entries.  Route: GET /range/logs/history?rangeID=."""
        response = self._request(
            "GET",
            f"{API_BASE}/range/logs/history",
            params=_range_params(range_id, user_id),
        )
        return self._as_list(self._json(response, "range_logs_history"))

    def range_log_entry(self, log_id: str, *, range_id: str | None = None) -> dict:
        """Get a specific historical log entry's full content.

        Route:  GET /range/logs/history/{logID}?rangeID=
        """
        response = self._request(
            "GET",
            f"{API_BASE}/range/logs/history/{log_id}",
            params=_range_params(range_id),
        )
        return self._json(response, "range_log_entry")

    def range_etchosts(
        self, *, range_id: str | None = None, user_id: str | None = None
    ) -> str:
        """Get /etc/hosts for a range.  Route: GET /range/etchosts?rangeID=."""
        response = self._request(
            "GET", f"{API_BASE}/range/etchosts", params=_range_params(range_id, user_id)
        )
        return self._unwrap_result_str(response)

    def range_sshconfig(self) -> str:
        """Get the SSH config for the range.  Route: GET /range/sshconfig."""
        response = self._request("GET", f"{API_BASE}/range/sshconfig")
        return self._unwrap_result_str(response)

    def range_rdpconfigs(
        self, *, range_id: str | None = None, user_id: str | None = None
    ) -> bytes:
        """Get RDP configs as a zip.  Route: GET /range/rdpconfigs?rangeID=."""
        response = self._request(
            "GET", f"{API_BASE}/range/rdpconfigs", params=_range_params(range_id, user_id)
        )
        return response.content

    def range_ansibleinventory(
        self, *, range_id: str | None = None, user_id: str | None = None
    ) -> str:
        """Get the Ansible inventory.  Route: GET /range/ansibleinventory?rangeID=."""
        response = self._request(
            "GET",
            f"{API_BASE}/range/ansibleinventory",
            params=_range_params(range_id, user_id),
        )
        return self._unwrap_result_str(response)

    # -- ansible management ----------------------------------------------

    def ansible_list(self, *, user_id: str | None = None) -> list[dict]:
        """List installed roles/collections.  Route: GET /ansible?userID=.

        Roles are scoped per-user (and optionally global), not per-range.
        """
        response = self._request(
            "GET", f"{API_BASE}/ansible", params=_range_params(user_id=user_id)
        )
        return self._as_list(self._json(response, "ansible_list"))

    def ansible_role(
        self,
        role: str,
        action: str = "install",
        *,
        version: str = "",
        force: bool = False,
        global_: bool = False,
    ) -> dict:
        """Install or remove an Ansible role.

        Route:  POST /ansible/role
        Body:   {"role","action":"install"|"remove","global","force","version"}
        """
        body: dict[str, Any] = {
            "role": role,
            "action": action,
            "global": global_,
            "force": force,
            "version": version,
        }
        response = self._request("POST", f"{API_BASE}/ansible/role", json=body)
        return self._try_json(response)

    def ansible_collection(
        self,
        collection: str,
        *,
        version: str | None = None,
        force: bool = False,
    ) -> dict:
        """Install an Ansible collection.  Route: POST /ansible/collection."""
        body: dict[str, Any] = {"collection": collection}
        if version is not None:
            body["version"] = version
        if force:
            body["force"] = True
        response = self._request("POST", f"{API_BASE}/ansible/collection", json=body)
        return self._try_json(response)

    def ansible_role_from_tar(
        self,
        file_data: bytes,
        filename: str,
        *,
        force: bool = False,
    ) -> dict:
        """Install a role from a tar file.  Route: PUT /ansible/role/fromtar."""
        files_payload = {"file": (filename, file_data, "application/gzip")}
        data_payload: dict[str, str] = {}
        if force:
            data_payload["force"] = "true"
        response = self._request(
            "PUT",
            f"{API_BASE}/ansible/role/fromtar",
            files=files_payload,
            data=data_payload or None,
        )
        return self._try_json(response)

    def ansible_role_vars(self, roles: list[str]) -> list[dict]:
        """Return the configurable variables for one or more Ansible roles.

        Route:  POST /ansible/role/vars  body {roles:[...]}
        Response: ``{"roles": [{"name","global","vars"}, ...]}``.
        """
        response = self._request(
            "POST", f"{API_BASE}/ansible/role/vars", json={"roles": roles}
        )
        data = self._json(response, "ansible_role_vars")
        if isinstance(data, dict):
            return self._as_list(data.get("roles"))
        return self._as_list(data)

    def ansible_role_scope(
        self,
        roles: list[str],
        *,
        global_: bool,
        copy_roles: bool = False,
        user_id: str | None = None,
    ) -> dict:
        """Move or copy roles between global and local installation scopes.

        Route:  PATCH /ansible/role/scope?userID=  body {roles,global,copy}
        Admin only.
        """
        response = self._request(
            "PATCH",
            f"{API_BASE}/ansible/role/scope",
            params=_range_params(user_id=user_id),
            json={"roles": roles, "global": global_, "copy": copy_roles},
        )
        return self._try_json(response)

    def ansible_scope_roles_global(self, roles: list[str], *, force: bool = False) -> None:
        """Make each role in *roles* available globally (all users).

        *force* is accepted for call-site symmetry with the old v1 shim but
        is not sent - ``role/scope`` has no force flag. ``copy=True`` keeps
        each role's local/per-user copy intact, so this is purely additive.
        Raises ``LudusError`` if the call fails so the caller can
        record/handle it (provisioning treats this as non-fatal and emits a
        ``session.role_scope_failed`` event).
        """
        self.ansible_role_scope(roles, global_=True, copy_roles=True)

    # -- group management ---------------------------------------------------

    def group_list(self) -> list[dict]:
        """List all groups.  Route: GET /groups."""
        response = self._request("GET", f"{API_BASE}/groups")
        return self._as_list(self._json(response, "group_list"))

    def group_create(self, name: str, *, description: str | None = None) -> dict:
        """Create a group.  Route: POST /groups  body {name, description?}."""
        body: dict[str, Any] = {"name": name}
        if description is not None:
            body["description"] = description
        response = self._request("POST", f"{API_BASE}/groups", json=body)
        return self._try_json(response)

    def group_delete(self, group_name: str) -> None:
        """Delete a group.  Route: DELETE /groups/{groupName}."""
        self._request("DELETE", f"{API_BASE}/groups/{group_name}")

    def group_users(self, group_name: str) -> list[dict]:
        """List members of a group.  Route: GET /groups/{groupName}/users."""
        response = self._request("GET", f"{API_BASE}/groups/{group_name}/users")
        return self._as_list(self._json(response, "group_users"))

    def group_add_users(
        self, group_name: str, user_ids: list[str], *, managers: list[str] | None = None
    ) -> dict:
        """Add users to a group.

        Route:  POST /groups/{groupName}/users  body {userIDs, managers?}
        ``managers`` is the subset of *user_ids* that should be group
        managers (not a bool - Ludus wants an array of userIDs).
        """
        body: dict[str, Any] = {"userIDs": user_ids}
        if managers is not None:
            body["managers"] = managers
        response = self._request(
            "POST", f"{API_BASE}/groups/{group_name}/users", json=body
        )
        return self._try_json(response)

    def group_remove_users(self, group_name: str, user_ids: list[str]) -> dict:
        """Remove users from a group.  Route: DELETE /groups/{groupName}/users."""
        response = self._request(
            "DELETE", f"{API_BASE}/groups/{group_name}/users", json={"userIDs": user_ids}
        )
        return self._try_json(response)

    def group_ranges(self, group_name: str) -> list[dict]:
        """List ranges assigned to a group.  Route: GET /groups/{groupName}/ranges."""
        response = self._request("GET", f"{API_BASE}/groups/{group_name}/ranges")
        return self._as_list(self._json(response, "group_ranges"))

    def group_add_ranges(self, group_name: str, range_ids: list[str]) -> dict:
        """Grant a group access to ranges.  Route: POST /groups/{groupName}/ranges."""
        response = self._request(
            "POST",
            f"{API_BASE}/groups/{group_name}/ranges",
            json={"rangeIDs": range_ids},
        )
        return self._try_json(response)

    def group_remove_ranges(self, group_name: str, range_ids: list[str]) -> dict:
        """Revoke a group's access to ranges.  Route: DELETE /groups/{groupName}/ranges."""
        response = self._request(
            "DELETE",
            f"{API_BASE}/groups/{group_name}/ranges",
            json={"rangeIDs": range_ids},
        )
        return self._try_json(response)

    # -- VM management --------------------------------------------------

    def vm_destroy(self, vm_id: int) -> None:
        """Halt and destroy a single VM.  Route: DELETE /vm/{vmID}."""
        self._request("DELETE", f"{API_BASE}/vm/{vm_id}")

    # -- operations with no v2 equivalent --------------------------------

    def _unsupported(self, name: str, alternative: str = "") -> LudusNotSupported:
        msg = f"'{name}' has no equivalent in the Ludus v2 OpenAPI spec"
        if alternative:
            msg += f" ({alternative})"
        return LudusNotSupported(msg)

    def ansible_subscription_roles(self, *args: Any, **kwargs: Any) -> None:
        raise self._unsupported("ansible_subscription_roles")

    def ansible_install_subscription_roles(self, *args: Any, **kwargs: Any) -> None:
        raise self._unsupported("ansible_install_subscription_roles")


def get_ludus_client() -> LudusClient:
    """FastAPI dependency: build a `LudusClient` from app settings."""
    settings = get_settings()
    return LudusClient(
        url=settings.ludus_default_url,
        api_key=settings.ludus_default_api_key,
        verify_tls=settings.ludus_default_verify_tls,
        admin_url=getattr(settings, "ludus_default_admin_url", None),
    )
