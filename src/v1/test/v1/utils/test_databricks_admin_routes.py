"""Tests for the ``/databricks/admin`` routes (the workspace registry's admin API).

A small FastAPI app includes the admin router and the /databricks router, with a
middleware that stamps a stub ``request.user`` (LangGraph's auth does not run here).
``databricks_registered`` and ``get_settings`` are replaced on the /databricks route
module (the admin routes read them there), the store on the admin module, and the
legacy setting on ``v1.core.databricks_workspaces``. The store is the in-memory one.

Runs standalone (``python test_databricks_admin_routes.py``).
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import requests
from azure.core.exceptions import ClientAuthenticationError
from databricks.sdk import WorkspaceClient
from databricks.sdk.config import Config
from databricks_test_support import LogCapture, captured_logs, run_tests
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from requests.adapters import BaseAdapter

import v1.api.routes.databricks as routes
import v1.api.routes.databricks_admin as admin
import v1.core.databricks_workspaces as dw
from v1.api.routes.databricks import create_databricks_router
from v1.api.routes.databricks_admin import create_databricks_admin_router
from v1.core.databricks_workspaces import validate_entry, workspace_fields, workspaces
from v1.core.workspace_registry.memory_store import MemoryWorkspaceStore
from v1.core.workspace_registry.models import Actor
from v1.core.workspace_registry.seed import load_seed_file
from v1.core.workspace_registry.snapshot import get_snapshot

# DATABRICKS_ADMIN_GROUPS takes object IDs only.
ADMIN_GROUP = "7f1e9c20-3b4a-4d5e-8f60-a1b2c3d4e5f6"
HOST = "https://adb-1.azuredatabricks.net"
NOT_ADMIN = {"detail": "Only workspace admins can do this.", "code": "not_admin"}
NOT_FOUND = {"detail": "Not found.", "code": "not_found"}
ADMIN_LOGGER = "v1.api.routes.databricks_admin"


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        databricks_admin_groups=[ADMIN_GROUP],
        databricks_jobs_timeout_seconds=5,
    )


def _admin_user(**fields: object) -> SimpleNamespace:
    values: dict = {
        "identity": "user:hashed-admin",
        "groups": [ADMIN_GROUP],
        "name": "Ada Admin",
        "email": "ada@example.com",
    }
    values.update(fields)
    return SimpleNamespace(**values)


def _plain_user() -> SimpleNamespace:
    return SimpleNamespace(identity="user:hashed-plain", groups=["someone"])


def _app(user: object) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def stamp_user(request: Request, call_next):
        request.scope["user"] = user
        return await call_next(request)

    app.include_router(create_databricks_router())
    app.include_router(create_databricks_admin_router())
    return app


@contextmanager
def _client(
    user: object,
    *,
    registered: bool = True,
    store: object = None,
    no_store: bool = False,
) -> Iterator[TestClient]:
    """A client as ``user`` with the lookups patched; restored after."""

    if store is None and not no_store:
        store = MemoryWorkspaceStore()
    saved_routes = {n: getattr(routes, n) for n in ("databricks_registered", "get_settings")}
    saved_store = admin.get_store
    saved_dw = dw.get_settings
    routes.databricks_registered = lambda: registered
    routes.get_settings = _settings
    admin.get_store = lambda: store
    dw.get_settings = lambda: SimpleNamespace(databricks_legacy_workspace=None)
    get_snapshot().replace(())
    try:
        yield TestClient(_app(user))
    finally:
        for name, value in saved_routes.items():
            setattr(routes, name, value)
        admin.get_store = saved_store
        dw.get_settings = saved_dw
        get_snapshot().replace(())


def _new(alias: str = "claims", **extra: Any) -> dict:
    return {"alias": alias, "host": HOST, "sql_warehouse_id": "wh-1", **extra}


# Every admin route, with a body that would be valid.
ROUTES: list[tuple[str, str, dict | None]] = [
    ("GET", "/databricks/admin/workspaces", None),
    ("POST", "/databricks/admin/workspaces", _new()),
    ("PATCH", "/databricks/admin/workspaces/claims", {"version": 1, "changes": {"label": "X"}}),
    ("POST", "/databricks/admin/workspaces/claims/retire", {"version": 1}),
    ("POST", "/databricks/admin/workspaces/claims/restore", {"version": 1}),
    ("PUT", "/databricks/admin/workspaces/order", {"registry_version": 1, "aliases": ["claims"]}),
    ("GET", "/databricks/admin/workspaces/claims/history", None),
    ("POST", "/databricks/admin/workspaces/test", {"host": HOST, "sql_warehouse_id": "wh-1"}),
    ("GET", "/databricks/admin/workspaces/export.yaml", None),
]

# A body FastAPI can't parse.
BAD_JSON = {"content": "{bad", "headers": {"content-type": "application/json"}}


def _logs() -> AbstractContextManager[LogCapture]:
    return captured_logs(ADMIN_LOGGER)


# -- access --------------------------------------------------------------------


def test_access_is_checked_first_on_every_route() -> None:
    """404 when this process doesn't run Databricks, 403 for anyone but an admin; both
    before a body FastAPI can't parse is a 422."""

    for user, kwargs, status, expected in (
        (_admin_user(), {"registered": False}, 404, NOT_FOUND),
        (_plain_user(), {}, 403, NOT_ADMIN),
    ):
        for method, path, body in ROUTES:
            sends = [{"json": body}] + ([BAD_JSON] if body is not None else [])
            with _client(user, **kwargs) as client:
                for send in sends:
                    response = client.request(method, path, **send)
                    assert response.status_code == status, (method, path, response.status_code)
                    assert response.json() == expected, (method, path, response.json())
    # Registered, but the store was never built (startup failed): also 404.
    with _client(_admin_user(), no_store=True) as client:
        assert client.get("/databricks/admin/workspaces").json() == NOT_FOUND
    # No user at all.
    with _client(None) as client:
        assert client.get("/databricks/admin/workspaces").json() == NOT_ADMIN
    # An admin still gets the 422.
    with _client(_admin_user()) as client:
        for method, path, body in ROUTES:
            if body is not None:
                response = client.request(method, path, **BAD_JSON)
                assert response.status_code == 422, (method, path, response.status_code)
                assert response.json()["code"] == "invalid", (method, path, response.json())


# -- happy path ----------------------------------------------------------------


def test_create_list_edit_retire_restore() -> None:
    with _client(_admin_user()) as client:
        # An admin who sees no workspace is still told they are an admin.
        caps = client.get("/databricks/capabilities").json()
        assert caps["is_admin"] is True and caps["enabled"] is False, caps

        # A null jobs_enabled means the default.
        response = client.post(
            "/databricks/admin/workspaces", json=_new(label="Invoices", jobs_enabled=None)
        )
        assert response.status_code == 201, response.text
        created = response.json()
        assert created["alias"] == "claims" and created["label"] == "Invoices", created
        assert created["jobs_enabled"] is True, created
        assert created["version"] == 1 and created["retired"] is False, created
        assert created["retired_at"] is None and "T" in created["created_at"], created
        assert "created_by" not in created and "updated_by" not in created, created
        assert [w.alias for w in workspaces()] == ["claims"]  # the snapshot follows writes

        listed = client.get("/databricks/admin/workspaces").json()
        assert listed["registry_version"] == 1, listed
        assert [w["alias"] for w in listed["workspaces"]] == ["claims"], listed

        response = client.patch(
            "/databricks/admin/workspaces/claims",
            json={"version": 1, "changes": {"label": "Invoices team", "jobs_enabled": False}},
        )
        assert response.status_code == 200, response.text
        edited = response.json()
        assert edited["label"] == "Invoices team" and edited["jobs_enabled"] is False, edited
        assert edited["version"] == 2, edited

        response = client.post("/databricks/admin/workspaces/claims/retire", json={"version": 2})
        assert response.status_code == 200 and response.json()["retired"] is True, response.text
        assert client.get("/databricks/admin/workspaces").json()["workspaces"] == []
        assert workspaces() == ()
        both = client.get("/databricks/admin/workspaces?include_retired=true").json()
        assert [w["alias"] for w in both["workspaces"]] == ["claims"], both

        response = client.post("/databricks/admin/workspaces/claims/restore", json={"version": 3})
        assert response.status_code == 200 and response.json()["retired"] is False, response.text

        client.post("/databricks/admin/workspaces", json=_new("ops", host=HOST))
        version = client.get("/databricks/admin/workspaces").json()["registry_version"]
        response = client.put(
            "/databricks/admin/workspaces/order",
            json={"registry_version": version, "aliases": ["ops", "claims"]},
        )
        assert response.status_code == 204, response.text
        listed = client.get("/databricks/admin/workspaces").json()
        assert [w["alias"] for w in listed["workspaces"]] == ["ops", "claims"], listed
        assert [w.alias for w in workspaces()] == ["ops", "claims"]

    # Capabilities say who is not an admin.
    with _client(_plain_user()) as client:
        assert client.get("/databricks/capabilities").json()["is_admin"] is False
    with _client(_admin_user(), registered=False) as client:
        assert client.get("/databricks/capabilities").json()["is_admin"] is False


# -- errors --------------------------------------------------------------------


def test_create_bad_host_is_422_with_field() -> None:
    with _client(_admin_user()) as client:
        response = client.post(
            "/databricks/admin/workspaces", json=_new(host="https://evil.example.com")
        )
        assert response.status_code == 422, response.text
        assert response.json() == {
            "detail": "host is invalid or missing.",
            "code": "invalid",
            "field": "host",
        }, response.json()
        # Missing, or the wrong type: the same shape.
        body = _new()
        del body["sql_warehouse_id"]
        response = client.post("/databricks/admin/workspaces", json=body)
        assert response.status_code == 422, response.text
        assert response.json()["field"] == "sql_warehouse_id", response.json()
        response = client.post("/databricks/admin/workspaces", json=_new(jobs_enabled="yes"))
        assert response.status_code == 422 and response.json()["field"] == "jobs_enabled"
        # An unknown key, and a non-editable one in an edit.
        response = client.post("/databricks/admin/workspaces", json=_new(position=3))
        assert response.status_code == 422 and response.json()["field"] == "position"
        client.post("/databricks/admin/workspaces", json=_new())
        response = client.patch(
            "/databricks/admin/workspaces/claims", json={"version": 1, "changes": {"host": HOST}}
        )
        assert response.status_code == 422 and response.json()["field"] == "host", response.text
        response = client.patch("/databricks/admin/workspaces/claims", json={"changes": {}})
        assert response.status_code == 422 and response.json()["field"] == "version"
        # The alias is taken (even retired).
        response = client.post("/databricks/admin/workspaces", json=_new())
        assert response.status_code == 409, response.text
        assert response.json() == {
            "detail": "A workspace with this alias already exists (it may be retired).",
            "code": "alias_taken",
        }
        # A bad order.
        response = client.put(
            "/databricks/admin/workspaces/order",
            json={"registry_version": 1, "aliases": ["nope"]},
        )
        assert response.status_code == 422 and response.json()["field"] == "order"
        # Unknown alias.
        response = client.post("/databricks/admin/workspaces/nope/retire", json={"version": 1})
        assert response.status_code == 404, response.text
        assert response.json() == {"detail": "No such workspace.", "code": "not_found"}
        response = client.get("/databricks/admin/workspaces/nope/history")
        assert response.status_code == 404 and response.json()["code"] == "not_found"


def test_stale_edit_is_409() -> None:
    stale = {
        "detail": "This workspace changed since you opened it. Reload and try again.",
        "code": "stale",
    }
    with _client(_admin_user()) as client:
        client.post("/databricks/admin/workspaces", json=_new())
        client.patch(
            "/databricks/admin/workspaces/claims", json={"version": 1, "changes": {"label": "A"}}
        )
        response = client.patch(
            "/databricks/admin/workspaces/claims", json={"version": 1, "changes": {"label": "B"}}
        )
        assert response.status_code == 409 and response.json() == stale, response.text
        response = client.post("/databricks/admin/workspaces/claims/retire", json={"version": 1})
        assert response.status_code == 409 and response.json() == stale, response.text
        response = client.put(
            "/databricks/admin/workspaces/order",
            json={"registry_version": 0, "aliases": ["claims"]},
        )
        assert response.status_code == 409 and response.json() == stale, response.text


def test_history_hides_identity() -> None:
    with _client(_admin_user()) as client:
        client.post("/databricks/admin/workspaces", json=_new())
        client.patch(
            "/databricks/admin/workspaces/claims", json={"version": 1, "changes": {"label": "A"}}
        )
        response = client.get("/databricks/admin/workspaces/claims/history")
        assert response.status_code == 200, response.text
        assert "user:" not in response.text, response.text
        entries = response.json()
        assert [e["action"] for e in entries] == ["edited", "created"], entries
        assert entries[0]["actor"] == {"name": "Ada Admin", "email": "ada@example.com"}
        assert entries[0]["before"] == {"label": "claims"} and entries[0]["after"] == {
            "label": "A"
        }, entries
        assert "T" in entries[0]["at"], entries
        listed = client.get("/databricks/admin/workspaces?include_retired=true")
        assert "user:" not in listed.text, listed.text


def test_write_logs_change_without_values() -> None:
    with _client(_admin_user()) as client:
        client.post("/databricks/admin/workspaces", json=_new())
        with _logs() as logs:
            client.patch(
                "/databricks/admin/workspaces/claims",
                json={"version": 1, "changes": {"label": "Secret Label Value"}},
            )
            # A write that changes nothing logs nothing.
            client.patch(
                "/databricks/admin/workspaces/claims",
                json={"version": 2, "changes": {"label": "Secret Label Value"}},
            )
    lines = [m for m in logs.messages if m.startswith("databricks_registry_change")]
    assert len(lines) == 1, logs.messages
    line = lines[0]
    assert "action=edited" in line and "alias=claims" in line, line
    assert "changed=label" in line and "Secret" not in line, line
    assert "actor=user:hashed-admin" in line, line


def test_reorder_to_the_same_order_logs_nothing() -> None:
    with _client(_admin_user()) as client:
        client.post("/databricks/admin/workspaces", json=_new("a"))
        client.post("/databricks/admin/workspaces", json=_new("b"))
        version = client.get("/databricks/admin/workspaces").json()["registry_version"]
        with _logs() as logs:
            response = client.put(
                "/databricks/admin/workspaces/order",
                json={"registry_version": version, "aliases": ["a", "b"]},
            )
            assert response.status_code == 204, response.text
            listed = client.get("/databricks/admin/workspaces").json()
            assert listed["registry_version"] == version, listed
            response = client.put(
                "/databricks/admin/workspaces/order",
                json={"registry_version": version, "aliases": ["b", "a"]},
            )
            assert response.status_code == 204, response.text
    lines = [m for m in logs.messages if m.startswith("databricks_registry_change")]
    assert len(lines) == 1 and "action=reordered" in lines[0], logs.messages


class _RacingStore(MemoryWorkspaceStore):
    """Another admin's edit lands right after each of this one's."""

    async def update(self, alias, changes, expected_version, actor):
        row = await super().update(alias, changes, expected_version, actor)
        if actor.identity != "user:other":
            await super().update(
                alias, {"sql_warehouse_id": "wh-other"}, row.version, Actor("user:other")
            )
        return row


def test_edit_logs_only_the_fields_that_changed() -> None:
    with _client(_admin_user(), store=_RacingStore()) as client:
        client.post("/databricks/admin/workspaces", json=_new(label="Invoices"))
        with _logs() as logs:
            response = client.patch(
                "/databricks/admin/workspaces/claims",
                # label unchanged, jobs_enabled already true: only groups changes.
                json={
                    "version": 1,
                    "changes": {"label": "Invoices", "jobs_enabled": True, "groups": ["g1"]},
                },
            )
            assert response.status_code == 200, response.text
    lines = [m for m in logs.messages if m.startswith("databricks_registry_change")]
    assert len(lines) == 1, logs.messages
    # Not the other admin's sql_warehouse_id, which is the newest history entry.
    assert lines[0].endswith("changed=groups"), lines[0]


# -- live test -----------------------------------------------------------------


class _Transport(BaseAdapter):
    """A requests transport: ``respond(url)`` gives ``(status, headers, json body)``;
    ``seen`` is every URL requested."""

    def __init__(self, respond: Callable[[str], tuple[int, dict, Any]]) -> None:
        super().__init__()
        self.respond = respond
        self.seen: list[str] = []

    def send(self, request, **kwargs):
        self.seen.append(request.url)
        status, headers, body = self.respond(request.url)
        response = requests.Response()
        response.request = request
        response.url = request.url
        response.status_code = status
        response.headers.update({"Content-Type": "application/json", **headers})
        response._content = b"" if body is None else json.dumps(body).encode()
        return response

    def close(self) -> None:
        pass


def _sdk_client(transport: _Transport) -> WorkspaceClient:
    """A real ``WorkspaceClient`` whose HTTP goes to ``transport`` (no host discovery)."""

    saved = Config._resolve_host_metadata
    Config._resolve_host_metadata = lambda self: None
    try:
        client = WorkspaceClient(config=Config(host=HOST, token="t", auth_type="pat"))
    finally:
        Config._resolve_host_metadata = saved
    client.api_client._api_client._session.mount("https://", transport)
    return client


@contextmanager
def _workspace_client(
    respond: Callable[[str], tuple[int, dict, Any]] | None = None,
    *,
    error: BaseException | None = None,
    client: Any = None,
) -> Iterator[tuple[list, _Transport]]:
    """Replace ``app_workspace_client`` with one building a real SDK client on a fake
    transport (or returning ``client``, or raising ``error``); yields the hosts it was
    asked for and the transport."""

    hosts: list = []
    transport = _Transport(respond or (lambda url: (500, {}, None)))
    saved = routes.app_workspace_client

    async def fake(host):
        hosts.append(host)
        if error is not None:
            raise error
        return client if client is not None else _sdk_client(transport)

    routes.app_workspace_client = fake
    try:
        yield hosts, transport
    finally:
        routes.app_workspace_client = saved


WAREHOUSE_URL = f"{HOST}/api/2.0/sql/warehouses/"


def _test(client: TestClient, host: str = HOST, warehouse: str = "wh-1"):
    return client.post(
        "/databricks/admin/workspaces/test", json={"host": host, "sql_warehouse_id": warehouse}
    )


def test_test_endpoint_refuses_bad_input_without_network() -> None:
    bad_host = {"host_ok": False, "message": "Host must be https://<name>.azuredatabricks.net."}
    bad_warehouse = {"host_ok": True, "message": "SQL warehouse ID is invalid."}
    hosts = ("https://evil.example.com", "http://adb-1.azuredatabricks.net", "x")
    cases = [(host, "wh-1", bad_host) for host in hosts]
    cases += [(HOST, wh, bad_warehouse) for wh in ("../x", "a/b", "", "  ", "wh?x=1", "x" * 65)]
    # Both bad: host_ok as computed.
    cases.append(("https://evil.example.com", "a/b", bad_host))
    with _client(_admin_user()) as client, _workspace_client() as (calls, _):
        for host, warehouse, expected in cases:
            response = _test(client, host=host, warehouse=warehouse)
            assert response.status_code == 200, response.text
            assert response.json() == {
                "reachable": False,
                "warehouse_found": False,
                "warehouse_state": None,
                **expected,
            }, (host, warehouse, response.json())
    assert calls == [], calls


def test_test_endpoint_reports_missing_warehouse() -> None:
    def respond(url):
        return 404, {}, {"error_code": "RESOURCE_DOES_NOT_EXIST", "message": "nope is gone"}

    with _client(_admin_user()) as client, _workspace_client(respond) as (_, transport):
        body = _test(client, warehouse="nope").json()
    assert body["host_ok"] is True and body["reachable"] is True, body
    assert body["warehouse_found"] is False and body["warehouse_state"] is None, body
    assert "nope" not in body["message"] and body["message"], body
    assert transport.seen == [WAREHOUSE_URL + "nope"], transport.seen


def test_test_endpoint_reports_ok() -> None:
    def respond(url):
        return 200, {}, {"id": "wh-1", "state": "RUNNING"}

    with _client(_admin_user()) as client, _workspace_client(respond) as (hosts, transport):
        response = _test(client, warehouse=" wh-1 ")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["host_ok"] and body["reachable"] and body["warehouse_found"], body
    assert body["warehouse_state"] == "RUNNING" and body["message"], body
    assert transport.seen == [WAREHOUSE_URL + "wh-1"], transport.seen
    assert hosts == [HOST], hosts


def test_admin_live_test_uses_app_client() -> None:
    """The live test runs as the app on the host being tested (it tests the workspace, not
    the admin's access); nothing of the admin's sign-in is passed to it."""

    def respond(url):
        return 200, {}, {"id": "wh-1", "state": "RUNNING"}

    with _client(_admin_user()) as client, _workspace_client(respond) as (app_client_hosts, _):
        body = _test(client).json()
    assert app_client_hosts == ["https://adb-1.azuredatabricks.net"], app_client_hosts
    assert body["reachable"] and body["warehouse_found"], body


def test_admin_live_test_writes_one_audit_line() -> None:
    """The live test is a Databricks call as the app: one audit line naming the admin, the
    tested host and warehouse; none when the client can't be built (no call was made)."""

    def respond(url):
        return 200, {}, {"id": "wh-1", "state": "RUNNING"}

    admin_user = _admin_user(object_id="oid-admin")
    with (
        _client(admin_user) as client,
        _workspace_client(respond),
        captured_logs("v1.utils.databricks_client") as audit,
    ):
        assert _test(client).status_code == 200
    assert audit.messages == [
        (
            f"databricks_call identity=user:hashed-admin oid=oid-admin workspace={HOST}"
            " action=admin_test detail=warehouse=wh-1"
        )
    ], audit.messages

    with (
        _client(admin_user) as client,
        _workspace_client(error=ClientAuthenticationError("no token")),
        captured_logs("v1.utils.databricks_client") as audit,
    ):
        assert _test(client).status_code == 200
    assert audit.messages == [], audit.messages


def test_test_endpoint_follows_no_redirect() -> None:
    evil = "https://evil.example.com/steal"
    for status in (301, 302, 307, 308):

        def respond(url, status=status):
            if url.startswith(evil):
                return 200, {}, {"id": "wh-1", "state": "RUNNING"}
            return status, {"Location": evil}, None

        with _client(_admin_user()) as client, _workspace_client(respond) as (_, transport):
            body = _test(client).json()
        assert body["host_ok"] is True and body["reachable"] is False, (status, body)
        assert body["warehouse_found"] is False, (status, body)
        assert transport.seen == [WAREHOUSE_URL + "wh-1"], (status, transport.seen)


def test_test_endpoint_fails_closed_when_redirects_cannot_be_disabled() -> None:
    gets: list = []
    # Not the SDK's shape: no session to turn redirects off on.
    odd = SimpleNamespace(warehouses=SimpleNamespace(get=gets.append))
    with _client(_admin_user()) as client, _workspace_client(client=odd), _logs() as logs:
        body = _test(client).json()
    assert body["host_ok"] is True and body["reachable"] is False, body
    assert gets == [], gets
    assert any("databricks_admin_test_failed" in m for m in logs.messages), logs.messages


def test_test_endpoint_unreachable() -> None:
    def respond(url):
        return 403, {}, {"error_code": "PERMISSION_DENIED", "message": f"secret detail {url}"}

    with _client(_admin_user()) as client, _workspace_client(respond), _logs() as logs:
        body = _test(client).json()
    assert body["host_ok"] is True and body["reachable"] is False, body
    assert body["warehouse_found"] is False and "secret" not in body["message"], body
    assert not any("secret" in m for m in logs.messages), logs.messages
    assert any("PermissionDenied" in m for m in logs.messages), logs.messages

    # Building the client failed (the SDK's config, or the app's credential): unreachable
    # too, logged by class only.
    for error in (ValueError("cfg"), ClientAuthenticationError("secret credential detail")):
        with (
            _client(_admin_user()) as client,
            _workspace_client(error=error),
            _logs() as logs,
        ):
            response = _test(client)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["host_ok"] is True and body["reachable"] is False, (error, body)
        assert any(type(error).__name__ in m for m in logs.messages), logs.messages
        assert not any("secret" in m for m in logs.messages), logs.messages


def test_test_endpoint_timeout_covers_building_the_client() -> None:
    saved = routes.app_workspace_client

    async def slow(host):
        await asyncio.sleep(5)
        raise AssertionError("not cancelled")

    routes.app_workspace_client = slow
    try:
        with _client(_admin_user()) as client, _logs() as logs:
            routes.get_settings = lambda: SimpleNamespace(
                **{**vars(_settings()), "databricks_jobs_timeout_seconds": 0.05}
            )
            started = time.monotonic()
            body = _test(client).json()
            elapsed = time.monotonic() - started
    finally:
        routes.app_workspace_client = saved
    assert body["host_ok"] is True and body["reachable"] is False, body
    assert elapsed < 2, elapsed
    assert any("TimeoutError" in m for m in logs.messages), logs.messages


# -- export --------------------------------------------------------------------


def test_export_round_trips_through_seed() -> None:
    with _client(_admin_user()) as client:
        client.post("/databricks/admin/workspaces", json=_new("claims"))
        client.post(
            "/databricks/admin/workspaces",
            json=_new(
                "ops",
                label="Ops team",
                data_allowlist="main.ops",
                jobs_enabled=False,
                groups=["g1", "g2"],
            ),
        )
        client.post("/databricks/admin/workspaces", json=_new("old"))
        client.post("/databricks/admin/workspaces/old/retire", json={"version": 1})
        version = client.get("/databricks/admin/workspaces").json()["registry_version"]
        client.put(
            "/databricks/admin/workspaces/order",
            json={"registry_version": version, "aliases": ["ops", "claims"]},
        )
        response = client.get("/databricks/admin/workspaces/export.yaml")
        listed = client.get("/databricks/admin/workspaces").json()["workspaces"]
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/yaml"), response.headers
    assert response.headers["content-disposition"] == (
        'attachment; filename="databricks-workspaces.yaml"'
    ), response.headers
    text = response.text
    # Defaults are left out; retired workspaces too.
    assert "old" not in text and "label: claims" not in text, text
    assert "jobs_enabled: true" not in text and "groups: []" not in text, text
    assert "data_allowlist: null" not in text, text

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "seed.yaml"
        path.write_text(text, encoding="utf-8")
        entries = load_seed_file(path)
    assert [alias for alias, _ in entries] == [w["alias"] for w in listed] == ["ops", "claims"]
    for (alias, entry), row in zip(entries, listed, strict=True):
        workspace = validate_entry(alias, entry)
        assert not isinstance(workspace, str), (alias, workspace)
        expected = {k: row[k] for k in workspace_fields(workspace)}
        assert workspace_fields(workspace) == expected, (alias, entry, row)


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
