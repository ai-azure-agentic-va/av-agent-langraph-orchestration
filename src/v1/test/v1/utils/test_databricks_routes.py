"""Tests for the ``/databricks`` routes and the per-request app client.

A small FastAPI app includes the router, with a middleware that stamps a stub
``request.user`` (LangGraph's auth does not run here). ``databricks_registered``,
``get_settings`` and ``app_workspace_client`` are replaced through the route
module, the legacy setting on ``v1.core.databricks_workspaces`` and the workspaces
through the process's registry snapshot. No network.

Runs standalone (``python test_databricks_routes.py``).
"""

from __future__ import annotations

import json
import re
import time
from contextlib import contextmanager
from types import SimpleNamespace

from azure.core.exceptions import ClientAuthenticationError
from databricks.sdk.errors import BadRequest, DatabricksError, Unauthenticated
from databricks.sdk.service.jobs import (
    BaseRun,
    NotebookTask,
    Run,
    RunLifeCycleState,
    RunOutput,
    RunResultState,
    RunState,
    RunTask,
)
from databricks_jobs_fakes import PARENT, FakeJobsClient, make_model
from databricks_test_support import FakeStatementClient, captured_logs, platform_user, run_tests
from databricks_test_support import patched_env as _env
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

import v1.api.routes.databricks as routes
import v1.core.databricks_workspaces as dw
import v1.utils.databricks_client as dc
from v1.api.routes.databricks import (
    create_databricks_router,
    request_workspace_client,
    requested_workspace,
    require_enabled,
)
from v1.core.databricks_workspaces import WORKSPACE_UNAVAILABLE_MESSAGE, Workspace
from v1.core.tools.databricks.jobs.client import JobsApiError
from v1.core.tools.databricks.jobs.explain import ExplanationCache
from v1.core.tools.databricks.jobs.tools import ACCESS_REFUSED, UNAVAILABLE
from v1.core.tools.databricks.jobs.traces import where_failed
from v1.core.tools.databricks.sql.scope import DataScope
from v1.core.tools.databricks.sql.statements import APP_TAG, StatementResult
from v1.core.workspace_registry.snapshot import get_snapshot
from v1.utils.databricks_client import CLIENT_UNAVAILABLE_MESSAGE, SIGN_IN_MESSAGE

NOT_AVAILABLE = "Databricks data and job lookup is not available for your access."
HOST = "https://adb-123.azuredatabricks.net"
# DATABRICKS_ADMIN_GROUPS takes object IDs only.
ADMIN_GROUP = "0b3c2a8e-5d41-4f6a-9e7b-1c2d3e4f5a6b"


def _workspace(
    alias: str,
    host: str,
    groups: tuple[str, ...] = (),
    *,
    label: str | None = None,
    warehouse: str = "w",
    scope: DataScope | None = None,
    jobs_enabled: bool = True,
) -> Workspace:
    return Workspace(
        alias=alias,
        label=label or alias,
        host=host,
        sql_warehouse_id=warehouse,
        data_allowlist=None,
        scope=scope or DataScope(),
        jobs_enabled=jobs_enabled,
        groups=groups,
    )


# One workspace everyone sees.
LEGACY_REGISTRY = (_workspace("main", HOST),)
# The same, with the job lookup off.
JOBS_OFF_REGISTRY = (_workspace("main", HOST, label="Databricks", jobs_enabled=False),)
JOBS_OFF = "Job lookup isn't enabled for Databricks."
LEGACY_ENTRY = {"alias": "main", "label": "main", "jobs_enabled": True}


def _err(detail: str, code: str) -> dict:
    """A Databricks 401/403 body: the user-safe ``detail`` and its stable ``code``."""

    return {"detail": detail, "code": code}


NOT_AVAILABLE_BODY = _err(NOT_AVAILABLE, "not_available")
SIGN_IN_BODY = _err(SIGN_IN_MESSAGE, "sign_in_required")
WORKSPACE_UNAVAILABLE_BODY = _err(WORKSPACE_UNAVAILABLE_MESSAGE, "workspace_unavailable")


def _caps(
    enabled: bool,
    jobs_enabled: bool,
    reason: str | None = None,
    workspaces: list[dict] | None = None,
    is_admin: bool = False,
) -> dict:
    """A capabilities body (no ``DATABRICKS_LEGACY_WORKSPACE``); an enabled caller on
    ``LEGACY_REGISTRY`` sees its one workspace."""

    if workspaces is None:
        workspaces = [LEGACY_ENTRY] if enabled else []
    return {
        "enabled": enabled,
        "jobs_enabled": jobs_enabled,
        "reason": reason,
        "workspaces": workspaces,
        "default_workspace": workspaces[0]["alias"] if workspaces else None,
        "legacy_workspace": None,
        "is_admin": is_admin,
    }


# -- harness -------------------------------------------------------------------


def _recorder():
    """An ``app_workspace_client`` stand-in; returns (the hosts it was asked for, it)."""

    calls: list = []

    async def client_for(host: str) -> str:
        calls.append(host)
        return "workspace-client"

    return calls, client_for


def _raising(exc: Exception):
    async def client_for(host: str) -> object:
        raise exc

    return client_for


# A ``user`` for ``_get`` meaning no auth middleware at all: ``request.user`` raises.
NO_AUTH = object()


# A group the stand-in access-control file blocks the databricks-agent for.
BLOCKED_GROUP = "blocked-group"


def _settings(**overrides: object) -> SimpleNamespace:
    values = {
        "databricks_admin_groups": [ADMIN_GROUP],
        "databricks_jobs_enabled": True,
        "databricks_data_allowlist": None,
        "databricks_host": HOST,
        "databricks_jobs_timeout_seconds": 30,
        "databricks_explain_timeout_seconds": 60,
        "databricks_sql_warehouse_id": "wh-1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _app(user: object) -> FastAPI:
    app = FastAPI()

    if user is not NO_AUTH:

        @app.middleware("http")
        async def stamp_user(request: Request, call_next):
            request.scope["user"] = user
            return await call_next(request)

    app.include_router(create_databricks_router())

    # Probe routes for the client helper, on the /databricks route class (so their errors
    # carry ``code`` too).
    probes = APIRouter(route_class=routes.DatabricksRoute)

    async def client(request: Request) -> dict:
        workspace = requested_workspace(request, None)
        return {"client": await request_workspace_client(request, workspace)}

    probes.get("/probe/client", dependencies=[Depends(require_enabled)])(client)
    # The client helper alone, with no dependency in front of it.
    probes.get("/probe/bare-client")(client)

    app.include_router(probes)

    # The same dependency on a plain route: other routes' errors are unchanged.
    @app.get("/probe/plain", dependencies=[Depends(require_enabled)])
    async def probe_plain() -> dict:
        return {"ok": True}

    return app


def _user(**fields: object) -> SimpleNamespace:
    values: dict = {"identity": "user:a", "object_id": "oid-a", "groups": ["some-group"]}
    values.update(fields)
    return SimpleNamespace(**values)


def _get(
    path: str,
    user: object,
    *,
    registered: bool = True,
    blocked: bool = False,
    settings: object = None,
    env: dict[str, str | None] | None = None,
    client_for: object = None,
    statement_factory: object = None,
    jobs_client: object = None,
    model: object = None,
    explanations: object = None,
    registry: tuple[Workspace, ...] = LEGACY_REGISTRY,
    legacy: str | None = None,
):
    """GET ``path`` as ``user`` with the route module's lookups patched; restore after.

    ``registry`` is the workspace registry and ``legacy`` its ``DATABRICKS_LEGACY_WORKSPACE``.
    ``blocked`` blocks the databricks-agent for the user whatever their groups (the
    access-control file always blocks it for ``BLOCKED_GROUP``).

    ``jobs_client`` replaces ``JobsClient`` (called as ``JobsClient(workspace_client,
    timeout_s=...)``), ``model`` the chat model, and ``explanations`` the explanation
    cache (a fresh one when not given).
    """

    saved = {
        name: getattr(routes, name)
        for name in (
            "databricks_registered",
            "get_settings",
            "app_workspace_client",
            "default_statement_client_factory",
            "JobsClient",
            "chat_model",
            "EXPLANATIONS",
        )
    }
    routes.databricks_registered = lambda: registered
    routes.get_settings = lambda: settings if settings is not None else _settings()
    # Never the real credential: a test that builds a client passes its own.
    routes.app_workspace_client = client_for if client_for is not None else _recorder()[1]
    if statement_factory is not None:
        routes.default_statement_client_factory = statement_factory
    if jobs_client is not None:
        routes.JobsClient = jobs_client
    if model is not None:
        routes.chat_model = lambda: model
    routes.EXPLANATIONS = explanations if explanations is not None else ExplanationCache()
    saved_settings = dw.get_settings
    saved_access = routes.access_control.caller_access
    # The access-control file, as a stand-in: BLOCKED_GROUP blocks the databricks-agent.
    routes.access_control.caller_access = lambda groups=None: SimpleNamespace(
        blocked_subagents=frozenset(
            {"databricks-agent"} if blocked or BLOCKED_GROUP in (groups or ()) else ()
        )
    )
    get_snapshot().replace(registry)
    dw.get_settings = lambda: SimpleNamespace(databricks_legacy_workspace=legacy)
    try:
        with _env({"AGENT_AUTH_MODE": None, "APP_ENV": None, **(env or {})}):
            return TestClient(_app(user)).get(path)
    finally:
        dw.get_settings = saved_settings
        routes.access_control.caller_access = saved_access
        get_snapshot().replace(())
        for name, value in saved.items():
            setattr(routes, name, value)


def _capabilities(user: object, **kwargs: object) -> dict:
    response = _get("/databricks/capabilities", user, **kwargs)
    assert response.status_code == 200, (response.status_code, response.text)
    return response.json()


# -- capabilities --------------------------------------------------------------


def test_capabilities_enabled_with_no_user_token() -> None:
    # The caller sees workspace "main"; no user token is involved anywhere.
    calls, client_for = _recorder()
    body = _capabilities(_user(), client_for=client_for)
    assert body == _caps(True, True), body
    assert body["enabled"] is True and body["reason"] is None
    assert calls == []  # no Databricks call
    admin = _user(groups=["some-group", ADMIN_GROUP])
    assert _capabilities(admin) == _caps(True, True, is_admin=True)


def test_capabilities_is_admin_regardless_of_enabled() -> None:
    """An admin group says ``is_admin`` whether or not the caller can use Databricks, but
    never when this process doesn't run it."""

    admin = _user(groups=[ADMIN_GROUP])
    assert _capabilities(admin, registry=()) == _caps(False, False, is_admin=True)
    blocked = _user(groups=[ADMIN_GROUP, "blocked-group"])
    assert _capabilities(blocked)["is_admin"] is True
    assert _capabilities(admin, registered=False)["is_admin"] is False
    assert _capabilities(None)["is_admin"] is False


def test_admin_group_display_name_never_grants_admin() -> None:
    """A display name in DATABRICKS_ADMIN_GROUPS is ignored (anyone can create a group
    with that name); an object ID grants admin, in any case."""

    named = _settings(databricks_admin_groups=["Databricks Admins", " ", ""])
    member = _user(groups=["Databricks Admins"])
    assert _capabilities(member, settings=named)["is_admin"] is False
    upper = _settings(databricks_admin_groups=["Databricks Admins", f" {ADMIN_GROUP.upper()} "])
    assert _capabilities(member, settings=upper)["is_admin"] is False
    admin = _user(groups=[ADMIN_GROUP])
    assert _capabilities(admin, settings=upper)["is_admin"] is True


def test_capabilities_disabled_when_not_registered_or_blocked() -> None:
    # No reason: Databricks is just off for them.
    body = _capabilities(_user(), registered=False)
    assert body == _caps(False, False), body
    body = _capabilities(_user(groups=["some-group", "blocked-group"]))
    assert body == _caps(False, False), body


def test_capabilities_disabled_group_still_blocked() -> None:
    """A group the access-control file blocks still turns Databricks off, though there is no
    token to be missing any more, and every route refuses before a client is built."""

    blocked = _user(groups=["blocked-group"])
    body = _capabilities(blocked)
    assert body["enabled"] is False and body["jobs_enabled"] is False, body
    assert body["workspaces"] == [] and body["reason"] is None, body
    calls, client_for = _recorder()
    for path in (
        "/databricks/tables?q=sales",
        "/databricks/job-failures/run?ref=1",
        "/databricks/job-failures/job?id=1",
        "/databricks/job-failures/explain?run=1",
        "/probe/client",
    ):
        response = _get(path, blocked, client_for=client_for)
        assert response.status_code == 403, (path, response.status_code)
        assert response.json() == NOT_AVAILABLE_BODY, (path, response.json())
    assert calls == []


def test_capabilities_ignore_the_local_dev_flag() -> None:
    """Local dev auth no longer changes anything: the same answer on any box."""

    for env in ({"AGENT_AUTH_MODE": "dev", "APP_ENV": "local"}, {"AGENT_AUTH_MODE": "dev"}):
        assert _capabilities(_user(), env=env) == _caps(True, True), env
        assert _capabilities(_user(), env=env, registry=()) == _caps(False, False), env


def test_capabilities_lists_visible_workspaces_in_order_with_default() -> None:
    registry = (
        _workspace("claims", "https://claims", ("CLAIMS",), label="Invoices · prod"),
        _workspace("hidden", "https://hidden", ("OTHER",)),
        _workspace("shared", "https://shared", label="Shared"),
        _workspace("ops", "https://ops", ("CLAIMS",), jobs_enabled=False),
    )
    body = _capabilities(_user(groups=["CLAIMS"]), registry=registry)
    assert body == {
        "enabled": True,
        "jobs_enabled": True,
        "reason": None,
        "workspaces": [
            {"alias": "claims", "label": "Invoices · prod", "jobs_enabled": True},
            {"alias": "shared", "label": "Shared", "jobs_enabled": True},
            {"alias": "ops", "label": "ops", "jobs_enabled": False},
        ],
        "default_workspace": "claims",
        "legacy_workspace": None,
        "is_admin": False,
    }, body
    # Another caller sees only what their groups allow; the first visible is the default.
    body = _capabilities(_user(), registry=registry)
    assert [w["alias"] for w in body["workspaces"]] == ["shared"], body
    assert body["default_workspace"] == "shared"
    # Job lookup is on when any visible workspace has it.
    only_ops = (registry[3], _workspace("off", "https://off", jobs_enabled=False))
    body = _capabilities(_user(groups=["CLAIMS"]), registry=only_ops)
    assert body["enabled"] is True and body["jobs_enabled"] is False, body


def test_capabilities_disabled_when_none_visible() -> None:
    claims_only = (_workspace("claims", "https://claims", ("CLAIMS",)),)
    for registry in (claims_only, ()):
        body = _capabilities(_user(), registry=registry)
        assert body == _caps(False, False), (registry, body)
    # And every route refuses them before any Databricks client is built.
    calls, client_for = _recorder()
    user = _user()
    for path in (
        "/databricks/tables?q=sales",
        "/databricks/job-failures/run?ref=1",
        "/databricks/job-failures/job?id=1",
        "/databricks/job-failures/explain?run=1",
        "/probe/client",
    ):
        response = _get(path, user, client_for=client_for, registry=claims_only)
        assert response.status_code == 403, (path, response.status_code)
        assert response.json() == NOT_AVAILABLE_BODY, (path, response.json())
    # The client helper alone refuses too.
    response = _get("/probe/bare-client", user, client_for=client_for, registry=claims_only)
    assert response.json() == WORKSPACE_UNAVAILABLE_BODY, response.json()
    assert calls == []


def test_capabilities_legacy_workspace() -> None:
    """The alias a chat with no saved workspace (or ``default``) resolves to for this caller,
    or null when the caller's default would stand in (tables dropped) or they're disabled."""

    user = _user(groups=["CLAIMS"])
    registry = (
        _workspace("shared", "https://shared"),
        _workspace("claims", "https://claims", ("CLAIMS",)),
        _workspace("eng", "https://eng", ("ENG",)),
    )
    # DATABRICKS_LEGACY_WORKSPACE names a workspace the caller sees.
    body = _capabilities(user, registry=registry, legacy="claims")
    assert body["legacy_workspace"] == "claims" and body["default_workspace"] == "shared", body
    # It names one hidden from them (such chats are refused) or one that isn't configured.
    for legacy in ("eng", "gone"):
        assert _capabilities(user, registry=registry, legacy=legacy)["legacy_workspace"] is None
    # Unset: the caller's default stands in, tables dropped.
    assert _capabilities(user, registry=registry)["legacy_workspace"] is None
    assert _capabilities(user)["legacy_workspace"] is None
    # A disabled caller (not registered, a disabled group): null.
    assert _capabilities(user, registered=False, legacy="claims")["legacy_workspace"] is None
    blocked = _user(groups=["CLAIMS"])
    body = _capabilities(blocked, blocked=True, registry=registry, legacy="claims")
    assert body["legacy_workspace"] is None


def test_sign_in_401_when_there_is_no_caller() -> None:
    """A request with no caller identity (none on the user, no user, or no auth middleware
    at all) is a 401 ``sign_in_required`` from every Databricks route, before any client is
    built."""

    calls, client_for = _recorder()
    for user in (_user(identity=None), _user(identity="  "), None, NO_AUTH):
        for path in (
            "/databricks/tables?q=sales",
            "/databricks/job-failures/run?ref=1",
            "/databricks/job-failures/job?id=1",
            "/databricks/job-failures/explain?run=1",
            "/probe/bare-client",
        ):
            response = _get(path, user, client_for=client_for)
            assert response.status_code == 401, (path, user, response.status_code)
            assert response.json() == SIGN_IN_BODY, (path, user, response.json())
    assert calls == []


def test_error_codes_only_on_databricks_routes() -> None:
    """A route outside the /databricks router keeps FastAPI's plain ``{"detail": ...}``."""

    response = _get("/probe/plain", _user(), registered=False)
    assert response.status_code == 403 and response.json() == {"detail": NOT_AVAILABLE}
    # Validation errors on the Databricks routes are unchanged too.
    response = _get("/databricks/job-failures/job?id=x", _user())
    assert response.status_code == 422 and response.json() == {"detail": "A job ID is a number."}


def test_jobs_enabled_follows_the_workspaces() -> None:
    user = _user()
    body = _capabilities(user, registry=JOBS_OFF_REGISTRY)
    entry = {"alias": "main", "label": "Databricks", "jobs_enabled": False}
    assert body == _caps(True, False, workspaces=[entry]), body
    # The old global setting no longer decides it.
    body = _capabilities(user, settings=_settings(databricks_jobs_enabled=False))
    assert body["jobs_enabled"] is True, body


def test_capabilities_through_the_platform_user_proxy() -> None:
    """``request.user`` as LangGraph builds it (``ProxyUser`` over ``to_langgraph_user()``)."""

    from v1.utils.auth import AuthenticatedPrincipal

    def proxy(**fields: object) -> object:
        principal = AuthenticatedPrincipal(subject="oid-a", auth_mode="jwt", **fields)
        return platform_user(principal.to_langgraph_user())

    assert _capabilities(proxy()) == _caps(True, True)
    body = _capabilities(proxy(groups=("blocked-group",)))
    assert body == _caps(False, False), body
    # The route's caller comes through the proxy too.
    calls, client_for = _recorder()
    response = _get("/probe/client", proxy(), client_for=client_for)
    assert response.status_code == 200 and calls == [HOST], (response.text, calls)


# -- dependencies and the per-request client -----------------------------------


def test_request_workspace_client_builds_the_app_client_and_audits() -> None:
    calls, client_for = _recorder()
    with captured_logs(dc.__name__) as logs:
        response = _get("/probe/client", _user(), client_for=client_for)
    assert response.status_code == 200 and response.json() == {"client": "workspace-client"}
    assert calls == [HOST]
    assert logs.messages == [
        "databricks_call identity=user:a oid=oid-a workspace=main action=/probe/client detail=-"
    ], logs.messages
    # On the caller's default workspace.
    registry = (
        _workspace("claims", "https://claims", ("CLAIMS",)),
        _workspace("shared", "https://shared"),
    )
    for groups, host in ((["CLAIMS"], "https://claims"), (["some-group"], "https://shared")):
        user = _user(groups=groups)
        response = _get("/probe/client", user, client_for=client_for, registry=registry)
        assert response.status_code == 200, response.text
        assert calls[-1] == host, (groups, calls)


def test_request_workspace_client_other_failure_is_a_503() -> None:
    secret = "https://adb-123.azuredatabricks.net/api/2.0 said no"
    with _captured_logs() as lines, captured_logs(dc.__name__) as audit:
        response = _get("/probe/client", _user(), client_for=_raising(ValueError(secret)))
    assert response.status_code == 503, response.status_code
    assert response.json() == {"detail": CLIENT_UNAVAILABLE_MESSAGE}
    assert any("ValueError" in line for line in lines), lines
    assert not any("adb-123" in line for line in lines), lines
    assert audit.messages == []  # no call was made


def test_route_credential_failure_is_503() -> None:
    """The app's credential failing (managed identity / ``az login`` unavailable) is the
    503 with ``CLIENT_UNAVAILABLE_MESSAGE`` on every route; its text is never logged."""

    failing = _raising(ClientAuthenticationError("secret credential detail"))
    for path in (
        "/databricks/tables?q=sales",
        "/databricks/job-failures/run?ref=1",
        "/databricks/job-failures/job?id=1",
        "/databricks/job-failures/explain?run=1",
    ):
        with _captured_logs() as lines:
            response = _get(path, _user(), client_for=failing)
        assert response.status_code == 503, (path, response.status_code)
        assert response.json() == {"detail": CLIENT_UNAVAILABLE_MESSAGE}, path
        assert any("ClientAuthenticationError" in line for line in lines), lines
        assert not any("secret" in line for line in lines), lines


@contextmanager
def _captured_logs():
    """Collect the route module's log messages (formatted) for the ``with`` body."""

    with captured_logs(routes.__name__) as capture:
        yield capture.messages


# -- tables --------------------------------------------------------------------


def _rows(n: int) -> list[list]:
    return [["main", "sales", f"t{i}", "MANAGED", f"table {i}"] for i in range(n)]


def _tables(
    path: str,
    result: StatementResult | None = None,
    *,
    allowlist: str | None = None,
    user: object = None,
    **kwargs: object,
):
    """GET ``path`` with a fake statement client; return (response, fake, workspace clients).

    ``allowlist`` is the legacy workspace's (open when None); pass ``registry`` instead for
    several workspaces. ``fake.factory_calls`` records each factory call's arguments.
    """

    fake = FakeStatementClient(default=result or StatementResult("completed", [], []), states=())
    fake.factory_calls = []
    made: list = []

    def factory(settings: object):
        def make(
            workspace_client: object, warehouse_id: str, query_tags: dict | None = None
        ) -> FakeStatementClient:
            made.append(workspace_client)
            fake.warehouses.append(warehouse_id)
            fake.factory_calls.append((workspace_client, warehouse_id, query_tags))
            return fake

        return make

    async def client_for(host: str) -> str:
        fake.hosts.append(host)
        return "workspace-client"

    scope = DataScope.parse(allowlist) if allowlist else DataScope()
    kwargs.setdefault("registry", (_workspace("main", HOST, scope=scope, warehouse="wh-1"),))
    response = _get(
        path,
        user if user is not None else _user(),
        client_for=client_for,
        statement_factory=factory,
        **kwargs,
    )
    return response, fake, made


def test_tables_allowlist_lists_all() -> None:
    response, fake, made = _tables(
        "/databricks/tables",
        StatementResult("completed", [], _rows(2)),
        allowlist="main.sales",
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "mode": "allowlist",
        "tables": [
            {"full_name": "main.sales.t0", "type": "MANAGED", "comment": "table 0"},
            {"full_name": "main.sales.t1", "type": "MANAGED", "comment": "table 1"},
        ],
        "truncated": False,
        "needs_query": False,
    }
    assert made == ["workspace-client"]
    [call] = fake.calls
    assert call["max_rows"] == 501 and "LIMIT 501" in call["sql"]
    # Scoped to the allowlist, with no keyword filter.
    assert call["parameters"] == {"s0_table_catalog": "main", "s0_table_schema": "sales"}

    # A query narrows the allowlist listing.
    _, fake, _ = _tables("/databricks/tables?q=orders", allowlist="main.sales")
    assert fake.calls[0]["parameters"]["k0"] == "%orders%"


def test_tables_route_tags_source_api() -> None:
    """The listing runs as the app with the caller in its ``query_tags`` (source "api"),
    after one audit line naming them and the query."""

    with captured_logs(dc.__name__) as audit:
        response, fake, _ = _tables("/databricks/tables?q=orders")
    assert response.status_code == 200, response.text
    factory_calls = fake.factory_calls
    assert len(factory_calls) == 1, factory_calls
    assert factory_calls[0][2]["source"] == "api"
    assert factory_calls[0][:2] == ("workspace-client", "wh-1"), factory_calls
    assert factory_calls[0][2] == {
        "app": APP_TAG,
        "source": "api",
        "workspace": "main",
        "user_oid": "oid-a",
    }, factory_calls
    assert audit.messages == [
        (
            "databricks_call identity=user:a oid=oid-a workspace=main action=/databricks/tables"
            " detail=orders"
        )
    ], audit.messages
    # No object ID on the caller: no ``user_oid`` tag (and "-" in the audit line).
    with captured_logs(dc.__name__) as audit:
        _, fake, _ = _tables("/databricks/tables?q=orders", user=_user(object_id=None))
    assert "user_oid" not in fake.factory_calls[0][2], fake.factory_calls
    assert audit.messages == [
        (
            "databricks_call identity=user:a oid=- workspace=main action=/databricks/tables"
            " detail=orders"
        )
    ], audit.messages


def test_tables_open_requires_query() -> None:
    for path in ("/databricks/tables", "/databricks/tables?q=", "/databricks/tables?q=a%20,"):
        response, fake, made = _tables(path)
        assert response.status_code == 200, (path, response.text)
        assert response.json() == {
            "mode": "open",
            "tables": [],
            "truncated": False,
            "needs_query": True,
        }
        assert fake.calls == [] and made == [], path

    response, fake, _ = _tables(
        "/databricks/tables?q=Sales%20orders", StatementResult("completed", [], _rows(1))
    )
    body = response.json()
    assert body["mode"] == "open" and body["needs_query"] is False, body
    assert body["tables"] == [
        {"full_name": "main.sales.t0", "type": "MANAGED", "comment": "table 0"}
    ]
    [call] = fake.calls
    assert call["max_rows"] == 51
    assert call["parameters"] == {"k0": "%sales%", "k1": "%orders%"}

    response, fake, _ = _tables("/databricks/tables?q=" + "x" * 201)
    assert response.status_code == 422 and fake.calls == []
    response, _, _ = _tables("/databricks/tables?q=" + "x" * 200)
    assert response.status_code == 200


def test_tables_truncated_at_limit() -> None:
    response, _, _ = _tables(
        "/databricks/tables?q=sales", StatementResult("completed", [], _rows(51))
    )
    body = response.json()
    assert body["truncated"] is True and len(body["tables"]) == 50, body["truncated"]
    response, _, _ = _tables(
        "/databricks/tables?q=sales", StatementResult("completed", [], _rows(50))
    )
    body = response.json()
    assert body["truncated"] is False and len(body["tables"]) == 50

    response, _, _ = _tables(
        "/databricks/tables", StatementResult("completed", [], _rows(501)), allowlist="main"
    )
    body = response.json()
    assert body["truncated"] is True and len(body["tables"]) == 500


def test_tables_permission_denied_403() -> None:
    message = "User does not have CAN USE on warehouse w"
    response, _, _ = _tables(
        "/databricks/tables?q=sales", StatementResult("permission_denied", message=message)
    )
    assert response.status_code == 403
    assert response.json() == _err(routes.WAREHOUSE_DENIED_MESSAGE, "warehouse_denied")


def test_tables_warehouse_401_and_403_are_warehouse_denied() -> None:
    """Through the real StatementClient: Databricks refusing the app (403) or its token
    (401) is a 403 ``warehouse_denied`` with the fixed message, none of Databricks' text."""

    from databricks.sdk.errors import PermissionDenied

    from v1.core.tools.databricks.sql.statements import StatementClient

    class FailingAPI:
        def __init__(self, error: Exception) -> None:
            self.error = error

        def execute_statement(self, **kwargs: object) -> None:
            raise self.error

    def factory(settings: object):
        def make(workspace_client, warehouse_id, query_tags=None):
            return StatementClient(
                workspace_client,
                warehouse_id,
                timeout_s=5,
                poll_interval_s=0,
                query_tags=query_tags,
            )

        return make

    for error in (PermissionDenied("SDK-TEXT no CAN USE"), Unauthenticated("SDK-TEXT bad token")):

        async def client_for(host: str, error: Exception = error) -> object:
            return SimpleNamespace(statement_execution=FailingAPI(error))

        response = _get(
            "/databricks/tables?q=sales",
            _user(),
            client_for=client_for,
            statement_factory=factory,
            registry=(_workspace("main", HOST, warehouse="wh-1"),),
        )
        assert response.status_code == 403, (error, response.text)
        assert response.json() == _err(routes.WAREHOUSE_DENIED_MESSAGE, "warehouse_denied")
        assert "SDK-TEXT" not in response.text, (error, response.text)


def test_tables_failure_502_with_ref() -> None:
    for status in ("failed", "timeout", "permission_or_not_found"):
        with _captured_logs() as lines:
            response, _, _ = _tables(
                "/databricks/tables?q=sales", StatementResult(status, message="boom")
            )
        assert response.status_code == 502, (status, response.status_code)
        body = response.json()
        assert set(body) == {"message", "ref"}, body
        assert body["message"] == "Couldn't load tables. Try again."
        assert re.fullmatch(r"[0-9a-f]{8}", body["ref"]), body
        assert any(body["ref"] in line and status in line for line in lines), (status, lines)


def test_tables_forbidden_when_disabled() -> None:
    user = _user()
    for kwargs in (
        {"registered": False},
        {"blocked": True},
    ):
        response, fake, made = _tables("/databricks/tables?q=sales", user=user, **kwargs)
        assert response.status_code == 403, (kwargs, response.status_code)
        assert response.json() == NOT_AVAILABLE_BODY
        assert fake.calls == [] and made == []


def test_tables_uses_workspace_param_scope() -> None:
    registry = (
        _workspace("open", "https://open", warehouse="wh-open"),
        _workspace(
            "sales", "https://sales", warehouse="wh-sales", scope=DataScope.parse("main.sales")
        ),
    )
    rows = StatementResult("completed", [], _rows(1))
    response, fake, _ = _tables("/databricks/tables?workspace=sales", rows, registry=registry)
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "allowlist", response.json()
    assert fake.warehouses == ["wh-sales"] and fake.hosts == ["https://sales"], fake.warehouses
    [call] = fake.calls
    assert call["parameters"] == {"s0_table_catalog": "main", "s0_table_schema": "sales"}
    # The global allowlist and warehouse play no part.
    settings = _settings(databricks_data_allowlist="other.x", databricks_sql_warehouse_id="g")
    _, fake, _ = _tables(
        "/databricks/tables?workspace=sales", rows, registry=registry, settings=settings
    )
    assert fake.warehouses == ["wh-sales"], fake.warehouses
    assert fake.calls[0]["parameters"]["s0_table_catalog"] == "main"
    # No workspace: the caller's default (the first visible), with its own scope.
    response, fake, _ = _tables("/databricks/tables?q=orders", rows, registry=registry)
    assert response.json()["mode"] == "open", response.json()
    assert fake.warehouses == ["wh-open"] and fake.hosts == ["https://open"]


def test_tables_hidden_workspace_403() -> None:
    registry = (
        _workspace("shared", "https://shared"),
        _workspace("claims", "https://claims", ("CLAIMS",)),
    )
    # Hidden and unknown read the same: no hint about which aliases exist. A blank alias
    # that was sent is refused too, never read as missing.
    for alias in ("claims", "nope", "", "%20%20"):
        response, fake, made = _tables(
            f"/databricks/tables?q=sales&workspace={alias}", registry=registry
        )
        assert response.status_code == 403, (alias, response.status_code)
        assert response.json() == WORKSPACE_UNAVAILABLE_BODY, alias
        assert fake.calls == [] and made == [] and fake.hosts == [], alias
    response, fake, _ = _tables(
        "/databricks/tables?q=sales&workspace=" + "x" * 101, registry=registry
    )
    assert response.status_code == 422 and fake.calls == []
    # Visible to a CLAIMS member.
    response, _, _ = _tables(
        "/databricks/tables?q=sales&workspace=claims",
        registry=registry,
        user=_user(groups=["CLAIMS"]),
    )
    assert response.status_code == 200, response.text


# -- job failures --------------------------------------------------------------

JOB, RUN = 42, 100
EXPLAINED = {"why": "Row 2 has amount 'n/a'."}
ENTER_A_REF = "Enter a job ID, run ID or run link."


def _state(result: RunResultState = RunResultState.FAILED) -> RunState:
    return RunState(
        life_cycle_state=RunLifeCycleState.TERMINATED, result_state=result, state_message="x"
    )


def _failed_run(result: RunResultState = RunResultState.FAILED) -> Run:
    task = RunTask(
        task_key="parent",
        run_id=RUN + 1,
        notebook_task=NotebookTask(notebook_path="/Users/dev@example.com/fixture/example_parent"),
        start_time=1_000,
        end_time=int(time.time() * 1000),  # output is kept 60 days; older reads as expired
        state=_state(),
    )
    return Run(
        run_id=RUN,
        job_id=JOB,
        start_time=1_000,
        end_time=2_000,
        state=_state(result),
        run_page_url=f"{HOST}/jobs/{JOB}/runs/{RUN}",
        tasks=[task],
    )


def _readable_failed_run(fake: FakeJobsClient) -> None:
    fake.runs[RUN] = _failed_run()
    fake.outputs[RUN + 1] = RunOutput(error=PARENT["error"], error_trace=PARENT["error_trace"])


def _reply() -> AIMessage:
    return AIMessage(content=json.dumps(EXPLAINED))


def _jobs(path: str, fake: FakeJobsClient | None = None, *, user: object = None, **kwargs):
    """GET ``path`` with ``fake`` as the app's JobsClient; return (response, fake, made).

    ``made`` records the (workspace client, timeout_s) each JobsClient was built with.
    """

    fake = fake if fake is not None else FakeJobsClient()
    made: list = []

    def jobs_client(workspace_client: object, *, timeout_s: float) -> FakeJobsClient:
        made.append((workspace_client, timeout_s))
        return fake

    async def client_for(host: str) -> str:
        return "workspace-client"

    kwargs.setdefault("client_for", client_for)
    response = _get(
        path,
        user if user is not None else _user(),
        jobs_client=jobs_client,
        **kwargs,
    )
    return response, fake, made


def _lookup_line(lines: list[str]) -> str:
    [line] = [x for x in lines if x.startswith("databricks_job_lookup ")]
    return line


def test_run_lookup_returns_where() -> None:
    fake = FakeJobsClient()
    _readable_failed_run(fake)
    with _captured_logs() as lines:
        response, _, made = _jobs(f"/databricks/job-failures/run?ref={RUN}", fake)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"result", "detail", "where"}, body
    assert body["result"]["status"] == "failed" and body["result"]["run_id"] == RUN
    [task] = body["detail"]["tasks"]
    assert task["task_key"] == "parent" and task["error"] and task["trace"]
    assert body["where"] == [where_failed(task)]
    assert body["where"][0]["task_key"] == "parent"
    # The app's client, with the jobs timeout.
    assert made == [("workspace-client", 30)]
    # One metadata-only lookup line; no job output in the logs.
    line = _lookup_line(lines)
    assert "kind=run" in line and f"run_id={RUN}" in line and f"job_id={JOB}" in line, line
    assert "status=failed" in line and "latency_ms=" in line, line
    assert PARENT["error"].splitlines()[0] not in "\n".join(lines)

    # A run link from this workspace works; one from another is rejected without a call.
    link = f"{HOST}/jobs/{JOB}/runs/{RUN}"
    response, _, _ = _jobs(f"/databricks/job-failures/run?ref={link}", fake)
    assert response.json()["result"]["run_id"] == RUN
    foreign = "https://adb-999.azuredatabricks.net/jobs/1/runs/2"
    response, other, _ = _jobs(f"/databricks/job-failures/run?ref={foreign}")
    assert response.json()["result"]["status"] == "rejected" and other.calls == []

    # A run that didn't fail has no detail and nowhere to point at.
    fake = FakeJobsClient()
    fake.runs[RUN] = _failed_run(RunResultState.SUCCESS)
    body = _jobs(f"/databricks/job-failures/run?ref={RUN}", fake)[0].json()
    assert body["result"]["status"] == "succeeded", body
    assert body["detail"] is None and body["where"] == []


def test_lookup_input_is_checked_before_any_call() -> None:
    blank = ("", "%20%20", "1" * 4001)
    for path, values, detail in (
        ("run?ref=", blank, ENTER_A_REF),
        ("job?id=", blank, ENTER_A_REF),
        ("explain?run=", blank, ENTER_A_REF),
        ("job?id=", ("nightly%20load", "%2B5", "1%202", "0"), "A job ID is a number."),
        ("explain?run=", ("abc", f"{HOST}/jobs/{JOB}/runs/{RUN}", "0"), "A run ID is a number."),
    ):
        for value in values:
            response, fake, made = _jobs(f"/databricks/job-failures/{path}{value}")
            assert response.status_code == 422, (path, value, response.status_code)
            assert response.json() == {"detail": detail}, (path, value, response.json())
            assert fake.calls == [] and made == []
    # Trimmed.
    fake = FakeJobsClient()
    fake.runs[RUN] = _failed_run(RunResultState.SUCCESS)
    response, _, _ = _jobs(f"/databricks/job-failures/run?ref=%20{RUN}%20", fake)
    assert response.json()["result"]["run_id"] == RUN


def test_job_lookup_lists_failed_runs() -> None:
    fake = FakeJobsClient()
    fake.names[JOB] = "Nightly load"
    fake.job_runs[JOB] = [
        BaseRun(run_id=1, job_id=JOB, start_time=1_000, state=_state()),
        BaseRun(run_id=2, job_id=JOB, start_time=2_000, state=_state(RunResultState.SUCCESS)),
    ]
    with _captured_logs() as lines:
        response, _, made = _jobs(f"/databricks/job-failures/job?id={JOB}", fake)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"result"}
    assert body["result"]["status"] == "completed", body
    assert [r["run_id"] for r in body["result"]["failed_runs"]] == [1]
    assert made == [("workspace-client", 30)]
    line = _lookup_line(lines)
    assert "kind=job" in line and f"job_id={JOB}" in line and "status=completed" in line, line
    assert "Nightly" not in "\n".join(lines)


def test_explain_failed_run() -> None:
    fake = FakeJobsClient()
    _readable_failed_run(fake)
    model = make_model(_reply())
    with _captured_logs() as lines:
        response, _, made = _jobs(f"/databricks/job-failures/explain?run={RUN}", fake, model=model)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["result"]["status"] == "failed"
    assert body["fixed"] == {"status": "one_off"}  # job 42 isn't a saved job in the fake
    assert body["explanation"] == EXPLAINED and body["explanation_error"] is False
    assert len(model.seen) == 1
    assert made == [("workspace-client", 30)]
    line = _lookup_line(lines)
    assert "kind=explain" in line and f"run_id={RUN}" in line and "status=failed" in line, line
    assert "n/a" not in "\n".join(lines)


def test_explain_not_failed_returns_early() -> None:
    model = make_model(_reply())
    fake = FakeJobsClient()
    fake.runs[RUN] = _failed_run(RunResultState.SUCCESS)
    body = _jobs(f"/databricks/job-failures/explain?run={RUN}", fake, model=model)[0].json()
    assert body["result"]["status"] == "succeeded", body
    assert body["fixed"] is None and body["explanation"] is None
    assert body["explanation_error"] is False
    # Not found: the same, and nothing past the lookup runs.
    fake = FakeJobsClient()
    body = _jobs(f"/databricks/job-failures/explain?run={RUN}", fake, model=model)[0].json()
    assert body["result"]["status"] == "not_found" and body["fixed"] is None, body
    assert [name for name, _ in fake.calls] == ["get_run"]
    assert model.seen == []


def test_explain_caches_per_run_and_content() -> None:
    cache = ExplanationCache()
    model = make_model(_reply(), _reply(), _reply())
    fake = FakeJobsClient()
    _readable_failed_run(fake)
    fake.names[JOB] = "Nightly load"  # a saved job, so the fixed check lists its runs
    path = f"/databricks/job-failures/explain?run={RUN}"
    first = _jobs(path, fake, model=model, explanations=cache)[0].json()
    second = _jobs(path, fake, model=model, explanations=cache)[0].json()
    assert first["explanation"] == second["explanation"] == EXPLAINED
    assert len(model.seen) == 1
    # Cached, but the run and its fixed status are read again each time.
    names = [name for name, _ in fake.calls]
    assert names.count("list_runs") == 2 and names.count("get_run") >= 2

    # Different content for the same run (another error) is explained afresh.
    fake.outputs[RUN + 1] = RunOutput(error="KeyError: 'id'", error_trace="KeyError")
    _jobs(path, fake, model=model, explanations=cache)
    assert len(model.seen) == 2


def test_explain_error_flag_on_bad_reply() -> None:
    cache = ExplanationCache()
    model = make_model(AIMessage(content="not json"), _reply())
    fake = FakeJobsClient()
    _readable_failed_run(fake)
    path = f"/databricks/job-failures/explain?run={RUN}"
    with _captured_logs() as lines:
        first = _jobs(path, fake, model=model, explanations=cache)[0].json()
    assert first["result"]["status"] == "failed"
    assert first["explanation"] is None and first["explanation_error"] is True
    assert any(line.startswith(f"explanation_failed run_id={RUN} ") for line in lines), lines
    assert "explanation_error=True" in _lookup_line(lines)
    assert "not json" not in "\n".join(lines)
    # A failed explanation isn't cached: the next request asks again.
    second = _jobs(path, fake, model=model, explanations=cache)[0].json()
    assert second["explanation"] == EXPLAINED and second["explanation_error"] is False
    assert len(model.seen) == 2


def test_explain_model_construction_failure_keeps_fixed() -> None:
    """A chat model that can't be built is an explanation error, not a 500."""

    def broken_model() -> object:
        raise RuntimeError("missing deployment: secret detail")

    fake = FakeJobsClient()
    _readable_failed_run(fake)
    saved = routes.chat_model
    routes.chat_model = broken_model
    try:
        with _captured_logs() as lines:
            response, _, _ = _jobs(f"/databricks/job-failures/explain?run={RUN}", fake)
    finally:
        routes.chat_model = saved
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["result"]["status"] == "failed"
    assert body["fixed"] == {"status": "one_off"}, body
    assert body["explanation"] is None and body["explanation_error"] is True
    assert any(
        line.startswith(f"explanation_failed run_id={RUN} ") and "RuntimeError" in line
        for line in lines
    ), lines
    assert "secret" not in "\n".join(lines)


def test_explain_fixed_check_error_still_explains() -> None:
    fake = FakeJobsClient()
    _readable_failed_run(fake)
    fake.names[JOB] = "Nightly load"
    fake.fail["list_runs"] = JobsApiError("boom: secret detail")
    with _captured_logs() as lines:
        response, _, _ = _jobs(
            f"/databricks/job-failures/explain?run={RUN}", fake, model=make_model(_reply())
        )
    body = response.json()
    assert body["fixed"] is None and body["explanation"] == EXPLAINED, body
    assert any("JobsApiError" in line for line in lines), lines
    assert "secret" not in "\n".join(lines)


def test_job_lookups_on_a_refusing_workspace_say_access_refused() -> None:
    # A 200 with an "error" result, like the "try again later" answer the page already shows.
    def wrapped(cause: Exception) -> JobsApiError:
        error = JobsApiError("secret detail")
        error.__cause__ = cause
        return error

    for cause, message in (
        (Unauthenticated("secret 401"), ACCESS_REFUSED),
        (BadRequest("unable to parse response. secret <html>"), ACCESS_REFUSED),
        (DatabricksError("secret"), UNAVAILABLE),
    ):
        for path, method in (
            (f"run?ref={RUN}", "get_run"),
            (f"job?id={JOB}", "job_name"),
            (f"explain?run={RUN}", "get_run"),
        ):
            fake = FakeJobsClient()
            fake.fail[method] = wrapped(cause)
            with _captured_logs() as lines:
                response, _, _ = _jobs(f"/databricks/job-failures/{path}", fake)
            assert response.status_code == 200, (path, response.text)
            result = response.json()["result"]
            assert result == {"status": "error", "message": message}, (path, cause, result)
            assert "status=error" in _lookup_line(lines), lines
            assert "secret" not in "\n".join(lines)


def test_job_routes_forbidden_when_jobs_disabled() -> None:
    user = _user()
    for path in ("run?ref=1", "job?id=1", "explain?run=1"):
        for kwargs, body in (
            ({"registry": JOBS_OFF_REGISTRY}, _err(JOBS_OFF, "jobs_disabled")),
            ({"registered": False}, NOT_AVAILABLE_BODY),
            ({"blocked": True}, NOT_AVAILABLE_BODY),
        ):
            response, fake, made = _jobs(f"/databricks/job-failures/{path}", user=user, **kwargs)
            assert response.status_code == 403, (path, kwargs, response.status_code)
            assert response.json() == body
            assert fake.calls == [] and made == []
    # Jobs off is a 403 even when the caller is also unknown.
    response, _, _ = _jobs(
        "/databricks/job-failures/run?ref=1",
        user=_user(identity=None),
        registry=JOBS_OFF_REGISTRY,
    )
    assert response.status_code == 403 and response.json() == _err(JOBS_OFF, "jobs_disabled")


OTHER_HOST = "https://adb-999.azuredatabricks.net"


def test_job_routes_jobs_off_403() -> None:
    registry = (
        _workspace("on", HOST),
        _workspace("off", OTHER_HOST, label="Invoices · prod", jobs_enabled=False),
        _workspace("claims", "https://claims", ("CLAIMS",)),
    )
    for path in ("run?ref=1", "job?id=1", "explain?run=1"):
        # The chosen workspace has the job lookup off: its own message, nothing built.
        response, fake, made = _jobs(
            f"/databricks/job-failures/{path}&workspace=off", registry=registry
        )
        assert response.status_code == 403, (path, response.status_code)
        assert response.json() == _err(
            "Job lookup isn't enabled for Invoices · prod.", "jobs_disabled"
        )
        assert fake.calls == [] and made == []
        # Hidden, unknown or blank: the workspace message.
        for alias in ("claims", "nope", "", "%20"):
            response, fake, made = _jobs(
                f"/databricks/job-failures/{path}&workspace={alias}", registry=registry
            )
            assert response.status_code == 403, (path, alias, response.status_code)
            assert response.json() == WORKSPACE_UNAVAILABLE_BODY
            assert fake.calls == [] and made == []
        response, _, made = _jobs(
            f"/databricks/job-failures/{path}&workspace=" + "x" * 101, registry=registry
        )
        assert response.status_code == 422 and made == [], path
        # Jobs on there: looked up.
        response, _, made = _jobs(
            f"/databricks/job-failures/{path}&workspace=on", registry=registry
        )
        assert response.status_code == 200 and made, (path, response.status_code)


def test_job_routes_use_the_chosen_workspace_host() -> None:
    registry = (_workspace("a", HOST), _workspace("b", OTHER_HOST))
    fake = FakeJobsClient()
    fake.runs[RUN] = _failed_run(RunResultState.SUCCESS)
    for path in (f"run?ref={RUN}", "job?id=1", f"explain?run={RUN}"):
        for query, host in (("&workspace=b", OTHER_HOST), ("", HOST)):
            calls, client_for = _recorder()
            _jobs(
                f"/databricks/job-failures/{path}{query}",
                fake,
                registry=registry,
                client_for=client_for,
            )
            assert calls == [host], (path, query, calls)


def test_run_link_other_visible_workspace_returns_hint_without_lookup() -> None:
    registry = (_workspace("a", HOST), _workspace("b", OTHER_HOST))
    calls, client_for = _recorder()
    link = f"{OTHER_HOST}/jobs/{JOB}/runs/{RUN}"
    with _captured_logs() as lines:
        response, fake, made = _jobs(
            f"/databricks/job-failures/run?ref={link}&workspace=a",
            registry=registry,
            client_for=client_for,
        )
    assert response.status_code == 200, response.text
    assert response.json() == {"result": {"status": "other_workspace"}, "workspace_hint": "b"}
    # Never looked up anywhere: no client built, no Jobs call, and the link isn't logged.
    assert fake.calls == [] and made == [] and calls == []
    assert not any("adb-999" in line for line in lines), lines
    # The caller's default workspace when none is given.
    response, _, _ = _jobs(f"/databricks/job-failures/run?ref={link}", registry=registry)
    assert response.json()["workspace_hint"] == "b"
    # The link is on the chosen workspace (the hint already followed): a normal lookup, so a
    # hint never points at the current workspace.
    fake = FakeJobsClient()
    fake.runs[RUN] = _failed_run(RunResultState.SUCCESS)
    response, _, _ = _jobs(
        f"/databricks/job-failures/run?ref={link}&workspace=b", fake, registry=registry
    )
    body = response.json()
    assert "workspace_hint" not in body and body["result"]["run_id"] == RUN, body


def test_run_link_elsewhere_gets_no_hint() -> None:
    """A link on a hidden workspace, on one with the job lookup off (following the hint would
    only be refused) or on a host no workspace has: rejected, no hint."""

    a = _workspace("a", HOST)
    link = f"{OTHER_HOST}/jobs/{JOB}/runs/{RUN}"
    for registry in (
        (a, _workspace("b", OTHER_HOST, ("CLAIMS",))),
        (a, _workspace("b", OTHER_HOST, jobs_enabled=False)),
        (a,),
    ):
        response, fake, _ = _jobs(f"/databricks/job-failures/run?ref={link}", registry=registry)
        body = response.json()
        assert response.status_code == 200 and "workspace_hint" not in body, body
        assert body["result"]["status"] == "rejected" and fake.calls == [], body


def test_explanation_cache_keyed_by_workspace() -> None:
    registry = (_workspace("a", HOST), _workspace("b", OTHER_HOST))
    cache = ExplanationCache()
    model = make_model(_reply(), _reply(), _reply())
    fake = FakeJobsClient()
    _readable_failed_run(fake)
    path = f"/databricks/job-failures/explain?run={RUN}"

    def explain(alias: str) -> dict:
        response, _, _ = _jobs(
            f"{path}&workspace={alias}", fake, model=model, explanations=cache, registry=registry
        )
        return response.json()

    explain("a")
    assert len(model.seen) == 1
    # The same run ID and content in another workspace is another run: explained afresh.
    assert explain("b")["explanation"] == EXPLAINED and len(model.seen) == 2
    # Each workspace's explanation is then cached on its own.
    explain("a")
    explain("b")
    assert len(model.seen) == 2
    assert {key[0] for key in cache._items} == {"a", "b"}, list(cache._items)


def test_job_routes_401_without_a_caller() -> None:
    model = make_model(_reply())
    calls, client_for = _recorder()
    for path in ("run?ref=1", "job?id=1", "explain?run=1"):
        response, fake, made = _jobs(
            f"/databricks/job-failures/{path}",
            user=_user(identity=None),
            client_for=client_for,
            model=model,
        )
        assert response.status_code == 401, (path, response.status_code)
        assert response.json() == SIGN_IN_BODY
        assert fake.calls == [] and made == []
    assert calls == [] and model.seen == []


def test_job_routes_audit_the_ref() -> None:
    """Each job lookup writes one audit line: the caller, workspace, route and what was
    asked about (the run ref, job ID or run ID)."""

    fake = FakeJobsClient()
    fake.runs[RUN] = _failed_run(RunResultState.SUCCESS)
    for path, detail in (
        (f"run?ref={RUN}", str(RUN)),
        (f"job?id={JOB}", str(JOB)),
        (f"explain?run={RUN}", str(RUN)),
    ):
        with captured_logs(dc.__name__) as audit:
            response, _, _ = _jobs(f"/databricks/job-failures/{path}", fake)
        assert response.status_code == 200, (path, response.text)
        route = "/databricks/job-failures/" + path.split("?")[0]
        assert audit.messages == [
            (
                f"databricks_call identity=user:a oid=oid-a workspace=main action={route}"
                f" detail={detail}"
            )
        ], audit.messages


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
