"""Unit tests for `app.services.ludus.LudusClient` against the Ludus v2 contract.

Ludus v2 serves bare routes (no version prefix), addresses ranges by
``?rangeID=`` (independent of their owning user), shares ranges via
``/ranges/assign``/``/ranges/revoke``, and adds groups/whoami/log-history/
role-vars/role-scope/per-VM-delete endpoints that v1 lacked.

Uses pytest-httpx's ``httpx_mock`` fixture to intercept outbound requests.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pytest_httpx import HTTPXMock

from app.services.exceptions import (
    LudusAuthError,
    LudusError,
    LudusNotFound,
    LudusNotSupported,
    LudusRangeExists,
    LudusUserExists,
)
from app.services.ludus import API_BASE, LudusClient

BASE_URL = "https://ludus.test:8080"
API_KEY = "super-secret-ludus-key-do-not-log-me"


@pytest.fixture
def client() -> Iterator[LudusClient]:
    c = LudusClient(url=BASE_URL, api_key=API_KEY, verify_tls=False)
    try:
        yield c
    finally:
        c.close()


def _url(path: str) -> str:
    return f"{BASE_URL}{API_BASE}{path}"


# ---------------------------------------------------------------------------
# v2 uses bare routes (no version prefix)
# ---------------------------------------------------------------------------


def test_api_base_has_no_version_prefix() -> None:
    assert API_BASE == ""


def test_routes_are_bare(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", url=_url("/user/all"), json=[])
    client.user_list()
    sent = httpx_mock.get_request()
    assert sent is not None
    assert sent.url.path == "/user/all"
    assert sent.headers["X-API-KEY"] == API_KEY


# ---------------------------------------------------------------------------
# user management
# ---------------------------------------------------------------------------


def test_user_add_success(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url=_url("/user"),
        status_code=201,
        json={"userID": "alice", "name": "Alice", "apiKey": "alice-key", "isAdmin": False},
    )
    result = client.user_add("alice", "Alice", "alice@example.com")
    assert result["userID"] == "alice"
    assert result["apiKey"] == "alice-key"

    sent = httpx_mock.get_request()
    assert sent is not None
    body = sent.read().decode()
    assert '"userID": "alice"' in body or '"userID":"alice"' in body
    assert "alice@example.com" in body


def test_user_add_with_password(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST", url=_url("/user"), status_code=201,
        json={"userID": "alice", "name": "Alice", "isAdmin": False},
    )
    client.user_add("alice", "Alice", "alice@example.com", password="s3cret")
    body = httpx_mock.get_request().read().decode()
    assert "s3cret" in body


def test_user_add_conflict_raises_exists(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST", url=_url("/user"), status_code=409,
        json={"error": "User with that ID already exists"},
    )
    with pytest.raises(LudusUserExists):
        client.user_add("alice", "Alice", "alice@example.com")


def test_user_add_400_already_exists_raises_exists(
    client: LudusClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        method="POST", url=_url("/user"), status_code=400,
        json={"error": "User with that name already exists"},
    )
    with pytest.raises(LudusUserExists):
        client.user_add("alice", "Alice", "alice@example.com")


def test_user_add_400_host_system_pam_conflict_is_hard_error(
    client: LudusClient, httpx_mock: HTTPXMock
) -> None:
    """A leftover OS/PAM user is NOT an existing Ludus user - surface it.

    Regression: this 400 also contains 'already exists', but treating it as
    LudusUserExists made provisioning skip user creation and fail later with a
    confusing 'User not found' at range_deploy.
    """
    httpx_mock.add_response(
        method="POST", url=_url("/user"), status_code=400,
        json={
            "error": "User with that name already exists on the host system. "
            "Ludus uses the PAM for user authentication, so you must use a "
            "unique username for each Ludus user."
        },
    )
    with pytest.raises(LudusError) as exc:
        client.user_add("alice", "Alice", "alice@example.com")
    assert not isinstance(exc.value, LudusUserExists)
    assert "host system" in str(exc.value)


def test_user_rm_success(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="DELETE", url=_url("/user/alice"), status_code=200, json={})
    client.user_rm("alice")
    assert httpx_mock.get_request() is not None


def test_user_rm_delete_range(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="DELETE", url=_url("/user/alice?deleteDefaultRange=true"), json={}
    )
    client.user_rm("alice", delete_range=True)
    assert httpx_mock.get_request().url.params["deleteDefaultRange"] == "true"


def test_user_writes_use_admin_url(httpx_mock: HTTPXMock) -> None:
    # Some Ludus deployments still split user create/delete onto a dedicated
    # admin API (:8081), while everything else uses the primary URL (:8080).
    admin = "https://ludus.test:8081"
    c = LudusClient(url=BASE_URL, api_key=API_KEY, verify_tls=False, admin_url=admin)
    try:
        httpx_mock.add_response(
            method="POST", url=f"{admin}/user", status_code=201,
            json={"userID": "bob", "isAdmin": False, "name": "Bob"},
        )
        httpx_mock.add_response(method="DELETE", url=f"{admin}/user/bob", json={})
        httpx_mock.add_response(method="GET", url=f"{BASE_URL}/user/all", json=[])

        c.user_add("bob", "Bob", "bob@example.com")
        c.user_rm("bob")
        c.user_list()  # reads still go to the primary :8080 URL
    finally:
        c.close()

    reqs = httpx_mock.get_requests()
    assert str(reqs[0].url) == f"{admin}/user"          # add -> admin
    assert str(reqs[1].url) == f"{admin}/user/bob"      # rm  -> admin
    assert reqs[2].url.host == "ludus.test" and reqs[2].url.port == 8080  # list -> primary


def test_user_list_success(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url=_url("/user/all"),
        json=[{"userID": "JM", "isAdmin": True}, {"userID": "BOB", "isAdmin": False}],
    )
    users = client.user_list()
    assert [u["userID"] for u in users] == ["JM", "BOB"]


def test_user_wireguard_json_wrapped(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url=_url("/user/wireguard?userID=JM"),
        json={"result": {"wireGuardConfig": "[Interface]\nPrivateKey = x\n"}},
    )
    cfg = client.user_wireguard("JM")
    assert cfg.startswith("[Interface]")
    sent = httpx_mock.get_request()
    assert sent.url.params["userID"] == "JM"


def test_user_wireguard_raw_text_fallback(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url=_url("/user/wireguard?userID=JM"),
        text="[Interface]\nPrivateKey = y\n",
        headers={"content-type": "text/plain"},
    )
    assert client.user_wireguard("JM").startswith("[Interface]")


def test_user_default_range_get(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url=_url("/user/default-range"), json={"defaultRangeID": "JM"}
    )
    assert client.user_default_range_get() == "JM"


def test_user_default_range_set(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST", url=_url("/user/default-range"), json={"defaultRangeID": "JM"}
    )
    client.user_default_range_set("JM")
    assert "JM" in httpx_mock.get_request().read().decode()


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------


def test_whoami(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url=_url("/whoami"),
        json={"user": {"userID": "JM"}, "range": {"rangeID": "JM"}},
    )
    data = client.whoami()
    assert data["user"]["userID"] == "JM"


# ---------------------------------------------------------------------------
# range creation / sharing (v2)
# ---------------------------------------------------------------------------


def test_range_create_body(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST", url=_url("/ranges/create"), status_code=201,
        json={"result": {"rangeID": "JM", "rangeNumber": 2}},
    )
    result = client.range_create("Student range", "JM", user_ids=["JM"])
    assert result["rangeID"] == "JM"
    body = httpx_mock.get_request().read().decode()
    assert '"rangeID": "JM"' in body or '"rangeID":"JM"' in body
    assert "JM" in body


def test_range_create_conflict_raises_exists(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST", url=_url("/ranges/create"), status_code=400,
        json={"error": "Range with that ID already exists"},
    )
    with pytest.raises(LudusRangeExists):
        client.range_create("Student range", "JM")


def test_range_assign(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", url=_url("/ranges/assign/STU1/LEAD"), json={})
    client.range_assign("STU1", "LEAD")
    sent = httpx_mock.get_request()
    assert sent.url.path == "/ranges/assign/STU1/LEAD"


def test_range_revoke(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="DELETE", url=_url("/ranges/revoke/STU1/LEAD?force=true"), json={}
    )
    client.range_revoke("STU1", "LEAD", force=True)
    sent = httpx_mock.get_request()
    assert sent.url.path == "/ranges/revoke/STU1/LEAD"
    assert sent.url.params["force"] == "true"


def test_range_users(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url=_url("/ranges/LEAD/users"), json=[{"userID": "STU1"}]
    )
    users = client.range_users("LEAD")
    assert users[0]["userID"] == "STU1"


def test_ranges_accessible(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url=_url("/ranges/accessible"),
        json=[{"rangeID": "JM", "rangeNumber": 2, "accessType": "Direct"}],
    )
    ranges = client.ranges_accessible()
    assert ranges[0]["rangeID"] == "JM"


# ---------------------------------------------------------------------------
# range deploy / config / lifecycle (keyed by ?rangeID=)
# ---------------------------------------------------------------------------


def test_range_deploy_two_steps(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="PUT", url=_url("/range/config?rangeID=JM"), json={})
    httpx_mock.add_response(method="POST", url=_url("/range/deploy?rangeID=JM"), json={})
    client.range_deploy("JM", "ludus:\n  - vm_name: x\n")
    reqs = httpx_mock.get_requests()
    assert reqs[0].method == "PUT" and reqs[0].url.path == "/range/config"
    assert reqs[1].method == "POST" and reqs[1].url.path == "/range/deploy"
    assert reqs[0].url.params["rangeID"] == "JM"


def test_range_deploy_existing(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", url=_url("/range/deploy?rangeID=JM"), json={})
    client.range_deploy_existing(range_id="JM")
    assert httpx_mock.get_request().url.params["rangeID"] == "JM"


def test_range_deploy_existing_with_tags_and_force(
    client: LudusClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(method="POST", url=_url("/range/deploy?rangeID=JM"), json={})
    client.range_deploy_existing(range_id="JM", tags="dc", force=True, limit="JM-kali1")
    body = httpx_mock.get_request().read().decode()
    assert '"tags"' in body and "dc" in body
    assert '"force": true' in body or '"force":true' in body
    assert "JM-kali1" in body


def test_range_list(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url=_url("/range/all"),
        json=[{"rangeID": "JM", "rangeNumber": 2, "rangeState": "SUCCESS"}],
    )
    ranges = client.range_list()
    assert ranges[0]["rangeID"] == "JM"
    assert ranges[0]["rangeNumber"] == 2


def test_range_get_config_result_envelope(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url=_url("/range/config?rangeID=JM"),
        json={"result": "ludus:\n  - vm_name: dc\n"},
    )
    cfg = client.range_get_config(range_id="JM")
    assert cfg.startswith("ludus:")


def test_range_get_vms_returns_single_object(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url=_url("/range?rangeID=JM"),
        json={
            "rangeID": "JM", "rangeNumber": 2, "numberOfVMs": 1,
            "VMs": [{"ID": 3969, "proxmoxID": 114, "name": "dc", "poweredOn": True, "ip": "10.2.10.5"}],
        },
    )
    detail = client.range_get_vms(range_id="JM")
    assert isinstance(detail, dict)
    assert detail["rangeID"] == "JM"
    assert detail["VMs"][0]["proxmoxID"] == 114


def test_range_destroy_rangeid_and_force(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="DELETE", url=_url("/range?rangeID=JM&force=true"), json={})
    client.range_destroy(range_id="JM", force=True)
    sent = httpx_mock.get_request()
    assert sent.url.params["rangeID"] == "JM"
    assert sent.url.params["force"] == "true"


def test_range_delete_vms(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="DELETE", url=_url("/range/JM/vms"), json={})
    client.range_delete_vms("JM")
    assert httpx_mock.get_request().url.path == "/range/JM/vms"


def test_range_abort(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", url=_url("/range/abort?rangeID=JM"), json={"result": "ok"})
    client.range_abort(range_id="JM")
    assert httpx_mock.get_request().url.params["rangeID"] == "JM"


def test_range_tags(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", url=_url("/range/tags"), json={"tags": ["dc", "kali"]})
    assert client.range_tags() == ["dc", "kali"]


def test_range_config_example(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url=_url("/range/config/example"), json={"result": "ludus: []\n"}
    )
    assert client.range_config_example().startswith("ludus:")


# ---------------------------------------------------------------------------
# power management
# ---------------------------------------------------------------------------


def test_range_power_on(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="PUT", url=_url("/range/poweron?rangeID=JM"), json={})
    client.range_power_on(range_id="JM", machines=["dc"])
    sent = httpx_mock.get_request()
    assert sent.url.params["rangeID"] == "JM"
    assert '"dc"' in sent.read().decode()


def test_range_power_off_default_all(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="PUT", url=_url("/range/poweroff?rangeID=JM"), json={})
    client.range_power_off(range_id="JM")
    assert '"all"' in httpx_mock.get_request().read().decode()


# ---------------------------------------------------------------------------
# snapshots (v2 wraps list in {"snapshots": [...]})
# ---------------------------------------------------------------------------


def test_snapshot_list_unwraps_snapshots(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url=_url("/snapshots/list?rangeID=JM"),
        json={"errors": None, "snapshots": [{"name": "before", "vmid": 114}]},
    )
    snaps = client.snapshot_list(range_id="JM")
    assert isinstance(snaps, list)
    assert snaps[0]["name"] == "before"


def test_snapshot_list_empty_when_no_snapshots_key(
    client: LudusClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        method="GET", url=_url("/snapshots/list?rangeID=JM"), json={"errors": None}
    )
    assert client.snapshot_list(range_id="JM") == []


def test_snapshot_create_body(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", url=_url("/snapshots/create?rangeID=JM"), json={"result": "ok"})
    client.snapshot_create("snap1", range_id="JM", description="d", include_ram=False, vmids=[114])
    body = httpx_mock.get_request().read().decode()
    assert '"name": "snap1"' in body or '"name":"snap1"' in body
    assert '"includeRAM": false' in body or '"includeRAM":false' in body
    assert "114" in body


def test_snapshot_revert_body(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", url=_url("/snapshots/rollback?rangeID=JM"), json={})
    client.snapshot_revert("snap1", range_id="JM")
    sent = httpx_mock.get_request()
    assert sent.url.path == "/snapshots/rollback"
    assert "snap1" in sent.read().decode()


def test_snapshot_delete_body(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", url=_url("/snapshots/remove?rangeID=JM"), json={})
    client.snapshot_delete("snap1", range_id="JM")
    assert httpx_mock.get_request().url.path == "/snapshots/remove"


# ---------------------------------------------------------------------------
# testing (keyed by ?rangeID=)
# ---------------------------------------------------------------------------


def test_testing_start(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="PUT", url=_url("/testing/start?rangeID=JM"), json={"result": "ok"})
    client.testing_start(range_id="JM")
    assert httpx_mock.get_request().url.params["rangeID"] == "JM"


def test_testing_stop_force(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="PUT", url=_url("/testing/stop?rangeID=JM"), json={"result": "ok"})
    client.testing_stop(range_id="JM", force=True)
    assert '"force":true' in httpx_mock.get_request().read().decode()


def test_testing_allow_body(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", url=_url("/testing/allow?rangeID=JM"), json={})
    client.testing_allow(range_id="JM", domains=["example.com"], ips=["1.2.3.4"])
    body = httpx_mock.get_request().read().decode()
    assert "example.com" in body and "1.2.3.4" in body


def test_testing_deny_body(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", url=_url("/testing/deny?rangeID=JM"), json={})
    client.testing_deny(range_id="JM", domains=["evil.com"])
    assert "evil.com" in httpx_mock.get_request().read().decode()


def test_testing_update(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", url=_url("/testing/update?rangeID=JM"), json={})
    client.testing_update("dc", range_id="JM")
    assert "dc" in httpx_mock.get_request().read().decode()


# ---------------------------------------------------------------------------
# templates
# ---------------------------------------------------------------------------


def test_template_list(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", url=_url("/templates"), json=[{"name": "win2019"}])
    assert client.template_list()[0]["name"] == "win2019"


def test_template_build_body(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", url=_url("/templates"), json={})
    client.template_build(["win2019"], parallel=2)
    body = httpx_mock.get_request().read().decode()
    assert "win2019" in body and "2" in body


def test_template_abort(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", url=_url("/templates/abort"), json={})
    client.template_abort()
    assert httpx_mock.get_request().url.path == "/templates/abort"


def test_template_status(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", url=_url("/templates/status"), json=[{"template": "x"}])
    assert client.template_status()[0]["template"] == "x"


def test_template_logs(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", url=_url("/templates/logs"), json={"result": "log text"})
    assert client.template_logs() == "log text"


def test_template_delete(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="DELETE", url=_url("/template/win2019"), json={})
    client.template_delete("win2019")
    assert httpx_mock.get_request().url.path == "/template/win2019"


# ---------------------------------------------------------------------------
# range detail (read-only)
# ---------------------------------------------------------------------------


def test_range_logs(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", url=_url("/range/logs?rangeID=JM"), json={"result": "..."})
    assert client.range_logs(range_id="JM")["result"] == "..."


def test_range_logs_history(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url=_url("/range/logs/history?rangeID=JM"),
        json=[{"id": "abc123", "status": "success"}],
    )
    entries = client.range_logs_history(range_id="JM")
    assert entries[0]["id"] == "abc123"


def test_range_log_entry(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url=_url("/range/logs/history/abc123?rangeID=JM"),
        json={"id": "abc123", "result": "log text"},
    )
    entry = client.range_log_entry("abc123", range_id="JM")
    assert entry["result"] == "log text"


def test_range_etchosts(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url=_url("/range/etchosts?rangeID=JM"), json={"result": "10.2.10.5 dc"}
    )
    assert "dc" in client.range_etchosts(range_id="JM")


def test_range_sshconfig(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", url=_url("/range/sshconfig"), json={"result": "Host dc"})
    assert "Host dc" in client.range_sshconfig()


def test_range_rdpconfigs_bytes(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url=_url("/range/rdpconfigs?rangeID=JM"), content=b"PK\x03\x04zip",
    )
    assert client.range_rdpconfigs(range_id="JM") == b"PK\x03\x04zip"


def test_range_ansibleinventory(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url=_url("/range/ansibleinventory?rangeID=JM"), json={"result": "all:\n"}
    )
    assert client.range_ansibleinventory(range_id="JM").startswith("all:")


# ---------------------------------------------------------------------------
# ansible
# ---------------------------------------------------------------------------


def test_ansible_list(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", url=_url("/ansible?userID=JM"), json=[{"name": "role1"}])
    assert client.ansible_list(user_id="JM")[0]["name"] == "role1"


def test_ansible_role_body(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", url=_url("/ansible/role"), json={"result": "ok"})
    client.ansible_role("badsectorlabs.ludus_bloodhound_ce", "install", global_=True)
    body = httpx_mock.get_request().read().decode()
    assert '"action": "install"' in body or '"action":"install"' in body
    assert '"global": true' in body or '"global":true' in body


def test_ansible_collection(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", url=_url("/ansible/collection"), json={"result": "ok"})
    client.ansible_collection("community.general", version="1.0.0")
    assert "community.general" in httpx_mock.get_request().read().decode()


def test_ansible_role_from_tar(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="PUT", url=_url("/ansible/role/fromtar"), json={"result": "ok"})
    client.ansible_role_from_tar(b"tardata", "role.tar.gz", force=True)
    assert httpx_mock.get_request().url.path == "/ansible/role/fromtar"


def test_ansible_role_vars(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST", url=_url("/ansible/role/vars"),
        json={"roles": [{"name": "geerlingguy.docker", "global": False, "vars": {"x": 1}}]},
    )
    roles = client.ansible_role_vars(["geerlingguy.docker"])
    assert roles[0]["name"] == "geerlingguy.docker"
    assert "geerlingguy.docker" in httpx_mock.get_request().read().decode()


def test_ansible_role_scope(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="PATCH", url=_url("/ansible/role/scope"),
        json={"success": ["role1"], "errors": []},
    )
    result = client.ansible_role_scope(["role1"], global_=True, copy_roles=True)
    assert result["success"] == ["role1"]
    body = httpx_mock.get_request().read().decode()
    assert '"global": true' in body or '"global":true' in body
    assert '"copy": true' in body or '"copy":true' in body


def test_ansible_scope_roles_global_uses_role_scope(
    client: LudusClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        method="PATCH", url=_url("/ansible/role/scope"),
        json={"success": ["role1", "role2"], "errors": []},
    )
    client.ansible_scope_roles_global(["role1", "role2"])
    body = httpx_mock.get_request().read().decode()
    assert "role1" in body and "role2" in body
    assert '"global": true' in body or '"global":true' in body


def test_ansible_scope_roles_global_propagates_failure(
    client: LudusClient, httpx_mock: HTTPXMock
) -> None:
    # A failing call propagates so provisioning can record it as non-fatal.
    httpx_mock.add_response(
        method="PATCH", url=_url("/ansible/role/scope"), status_code=500, json={"error": "x"}
    )
    with pytest.raises(LudusError):
        client.ansible_scope_roles_global(["role1"])


# ---------------------------------------------------------------------------
# groups (v2)
# ---------------------------------------------------------------------------


def test_group_list(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", url=_url("/groups"), json=[{"name": "red-team"}])
    assert client.group_list()[0]["name"] == "red-team"


def test_group_create(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", url=_url("/groups"), status_code=201, json={"result": "ok"})
    client.group_create("red-team", description="Red team")
    body = httpx_mock.get_request().read().decode()
    assert "red-team" in body and "Red team" in body


def test_group_delete(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="DELETE", url=_url("/groups/red-team"), json={})
    client.group_delete("red-team")
    assert httpx_mock.get_request().url.path == "/groups/red-team"


def test_group_users(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url=_url("/groups/red-team/users"),
        json=[{"userID": "JM", "role": "manager"}],
    )
    users = client.group_users("red-team")
    assert users[0]["role"] == "manager"


def test_group_add_users_with_managers(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST", url=_url("/groups/red-team/users"),
        json={"success": ["JM", "BOB"], "errors": []},
    )
    result = client.group_add_users("red-team", ["JM", "BOB"], managers=["JM"])
    assert result["success"] == ["JM", "BOB"]
    body = httpx_mock.get_request().read().decode()
    assert '"userIDs"' in body and '"managers"' in body


def test_group_remove_users(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="DELETE", url=_url("/groups/red-team/users"), json={"success": ["JM"], "errors": []}
    )
    client.group_remove_users("red-team", ["JM"])
    assert "JM" in httpx_mock.get_request().read().decode()


def test_group_ranges(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url=_url("/groups/red-team/ranges"), json=[{"rangeID": "JM"}]
    )
    assert client.group_ranges("red-team")[0]["rangeID"] == "JM"


def test_group_add_ranges(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST", url=_url("/groups/red-team/ranges"),
        json={"success": ["JM"], "errors": []},
    )
    client.group_add_ranges("red-team", ["JM"])
    assert '"rangeIDs"' in httpx_mock.get_request().read().decode()


def test_group_remove_ranges(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="DELETE", url=_url("/groups/red-team/ranges"),
        json={"success": ["JM"], "errors": []},
    )
    client.group_remove_ranges("red-team", ["JM"])
    assert '"rangeIDs"' in httpx_mock.get_request().read().decode()


# ---------------------------------------------------------------------------
# VM management (v2)
# ---------------------------------------------------------------------------


def test_vm_destroy(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="DELETE", url=_url("/vm/114"), json={"result": "ok"})
    client.vm_destroy(114)
    assert httpx_mock.get_request().url.path == "/vm/114"


# ---------------------------------------------------------------------------
# error mapping
# ---------------------------------------------------------------------------


def test_auth_error_401(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", url=_url("/user/all"), status_code=401, json={"error": "no key"})
    with pytest.raises(LudusAuthError):
        client.user_list()


def test_not_found_404(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url=_url("/range/config?rangeID=NOPE"), status_code=404, json={"error": "no range"}
    )
    with pytest.raises(LudusNotFound):
        client.range_get_config(range_id="NOPE")


def test_generic_error_500(client: LudusClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", url=_url("/range/all"), status_code=500, json={"error": "boom"})
    with pytest.raises(LudusError):
        client.range_list()


# ---------------------------------------------------------------------------
# operations with no v2 equivalent -> LudusNotSupported (no HTTP call)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.ansible_subscription_roles(),
        lambda c: c.ansible_install_subscription_roles(["r"]),
    ],
)
def test_unsupported_operations_raise(client: LudusClient, call) -> None:
    with pytest.raises(LudusNotSupported):
        call(client)
