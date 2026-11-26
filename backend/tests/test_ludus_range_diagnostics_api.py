"""Tests for the orphaned-ranges and range-diagnostics endpoints.

These power the Ranges tab's cleanup filter (hide ranges whose owning Ludus
user was deleted) and the "click an ERROR range for details" panel.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.ludus import router as ludus_router
from app.core.config import Settings, get_settings
from app.core.deps import get_current_user, get_ludus_client_registry
from app.models.user import User
from app.services.exceptions import LudusError
from app.services.ludus import LudusClient


@pytest.fixture
def settings() -> Settings:
    return Settings(
        app_env="testing",
        app_secret_key="unit-test-secret",
        admin_email="instructor@example.com",
        admin_password="super-secret-test-pw",
        ludus_default_url="https://ludus.test:8080",
        ludus_default_api_key="unit-test-api-key",
        _env_file=None,
    )


@pytest.fixture
def fake_admin() -> User:
    return User(
        email="instructor@example.com",
        password_hash="irrelevant-for-these-tests",
        role="instructor",
    )


@pytest.fixture
def mock_ludus() -> MagicMock:
    return MagicMock(spec=LudusClient)


class _MockRegistry:
    """Wrap a MagicMock so it quacks like ``LudusClientRegistry``."""

    def __init__(self, mock: MagicMock) -> None:
        self._mock = mock

    def get(self, name: str = "default") -> MagicMock:
        if name != "default":
            raise ValueError(f"Unknown Ludus server '{name}'")
        return self._mock

    @property
    def server_names(self) -> list[str]:
        return ["default"]


def _build_app(
    settings: Settings,
    mock_ludus: MagicMock,
    current_user: User | None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(ludus_router)

    def _override_get_settings() -> Settings:
        return settings

    registry = _MockRegistry(mock_ludus)

    app.dependency_overrides[get_settings] = _override_get_settings
    app.dependency_overrides[get_ludus_client_registry] = lambda: registry

    if current_user is not None:

        def _override_get_current_user() -> User:
            return current_user

        app.dependency_overrides[get_current_user] = _override_get_current_user

    return app


@pytest.fixture
def client(
    settings: Settings,
    mock_ludus: MagicMock,
    fake_admin: User,
) -> Iterator[TestClient]:
    app = _build_app(settings, mock_ludus, fake_admin)
    with TestClient(app) as tc:
        yield tc


@pytest.fixture
def anon_client(
    settings: Settings,
    mock_ludus: MagicMock,
) -> Iterator[TestClient]:
    app = _build_app(settings, mock_ludus, current_user=None)
    with TestClient(app) as tc:
        yield tc


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_orphaned_ranges_without_auth_returns_401(anon_client: TestClient) -> None:
    resp = anon_client.get("/api/ludus/ranges/orphaned")
    assert resp.status_code == 401


def test_range_diagnostics_without_auth_returns_401(anon_client: TestClient) -> None:
    resp = anon_client.get("/api/ludus/ranges/GHOST/diagnostics")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/ludus/ranges/orphaned
# ---------------------------------------------------------------------------


def test_orphaned_ranges_identifies_ranges_with_no_users(
    client: TestClient, mock_ludus: MagicMock
) -> None:
    mock_ludus.range_list.return_value = [
        {"rangeID": "JM", "rangeNumber": 1},
        {"rangeID": "GHOST", "rangeNumber": 2},
    ]

    def _range_users(range_id: str) -> list[dict]:
        if range_id == "JM":
            return [{"userID": "JM", "name": "Jesse"}]
        return []  # Ludus returns null for an orphan; the client normalises to []

    mock_ludus.range_users.side_effect = _range_users

    resp = client.get("/api/ludus/ranges/orphaned")
    assert resp.status_code == 200
    assert resp.json() == {"range_ids": ["GHOST"]}


def test_orphaned_ranges_treats_lookup_failure_as_unknown_not_orphaned(
    client: TestClient, mock_ludus: MagicMock
) -> None:
    """A transient range_users() failure must never cause a live range to be
    misreported as orphaned and hidden."""
    mock_ludus.range_list.return_value = [{"rangeID": "FLAKY", "rangeNumber": 1}]
    mock_ludus.range_users.side_effect = LudusError("Ludus unreachable", status_code=502)

    resp = client.get("/api/ludus/ranges/orphaned")
    assert resp.status_code == 200
    assert resp.json() == {"range_ids": []}


def test_orphaned_ranges_list_failure_returns_502(
    client: TestClient, mock_ludus: MagicMock
) -> None:
    mock_ludus.range_list.side_effect = LudusError("Ludus down", status_code=502)
    resp = client.get("/api/ludus/ranges/orphaned")
    assert resp.status_code == 502


# ---------------------------------------------------------------------------
# GET /api/ludus/ranges/{range_id}/diagnostics
# ---------------------------------------------------------------------------


def test_range_diagnostics_orphaned_range(client: TestClient, mock_ludus: MagicMock) -> None:
    mock_ludus.range_users.return_value = []
    mock_ludus.range_list.return_value = [
        {"rangeID": "GHOST", "rangeNumber": 2, "rangeState": "ERROR"},
    ]
    mock_ludus.range_get_vms.side_effect = LudusError(
        "unable to get pool by ID: 500 pool 'GHOST' does not exist", status_code=500
    )
    mock_ludus.range_logs_history.return_value = []

    resp = client.get("/api/ludus/ranges/GHOST/diagnostics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["orphaned"] is True
    assert body["owners"] == []
    assert body["range_state"] == "ERROR"
    assert "does not exist" in body["detail_error"]
    assert body["log_history"] == []
    assert body["latest_log_content"] is None
    mock_ludus.range_log_entry.assert_not_called()


def test_range_diagnostics_owned_range_with_log_history(
    client: TestClient, mock_ludus: MagicMock
) -> None:
    mock_ludus.range_users.return_value = [{"userID": "JM", "name": "Jesse"}]
    mock_ludus.range_list.return_value = [
        {"rangeID": "JM", "rangeNumber": 1, "rangeState": "SUCCESS"},
    ]
    mock_ludus.range_get_vms.return_value = {"rangeID": "JM", "VMs": []}
    mock_ludus.range_logs_history.return_value = [
        {
            "id": "older",
            "status": "failure",
            "start": "2026-01-01T00:00:00.000Z",
            "created": "2026-01-01T00:00:00.000Z",
        },
        {
            "id": "newest",
            "status": "success",
            "start": "2026-11-07T22:23:37.606Z",
            "created": "2026-11-07T22:23:37.607Z",
        },
    ]
    mock_ludus.range_log_entry.return_value = {
        "id": "newest",
        "status": "success",
        "result": "PLAY [Deploy] ****\n",
    }

    resp = client.get("/api/ludus/ranges/JM/diagnostics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["orphaned"] is False
    assert body["owners"] == [{"userID": "JM", "name": "Jesse"}]
    assert body["detail_error"] is None
    assert len(body["log_history"]) == 2
    # Picks the entry with the latest `created`, not list order.
    mock_ludus.range_log_entry.assert_called_once_with("newest", range_id="JM")
    assert body["latest_log_status"] == "success"
    assert body["latest_log_content"] == "PLAY [Deploy] ****\n"
