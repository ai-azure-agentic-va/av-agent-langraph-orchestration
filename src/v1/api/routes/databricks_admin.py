"""The workspace registry's admin API, under ``/databricks/admin``.

Workspace admins (a group in ``DATABRICKS_ADMIN_GROUPS``) list, create, edit, retire,
restore and reorder the Databricks workspaces, read a workspace's history, test a host
and SQL warehouse live as the app (what users' calls will run as), and export the live workspaces
as a seed file. Every route depends on :func:`require_admin`: 404 ``not_found`` when
this process doesn't run Databricks (or its registry store was never built), 403
``not_admin`` for anyone else, also before a body FastAPI can't parse is a 422.

Writes carry the version the admin read (a row's ``version``, or the
``registry_version`` for a reorder); a stale one is a 409. Every error is
``{"detail": <user-safe text>, "code": <code>}``, plus ``"field"`` on a 422 (a body
FastAPI can't parse included). After a write the process's snapshot is refreshed, so
this replica serves the change at once (the others within the poll interval), and one
INFO line names the change: the action, alias, the admin's hashed identity and the
changed field names, never their values. Responses never carry the hashed identity.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Any

import requests
import yaml
from databricks.sdk.errors import NotFound as DatabricksNotFound
from databricks.sdk.errors import ResourceDoesNotExist
from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict

import v1.api.routes.databricks as routes
from v1.api.routes.databricks import (
    DatabricksError,
    DatabricksRoute,
    _request_caller,
    _user_field,
    is_admin,
)
from v1.core.databricks_workspaces import check_host, check_warehouse_id
from v1.core.workspace_registry.models import (
    EDITABLE_FIELDS,
    Actor,
    AliasTaken,
    InvalidWorkspace,
    NotFound,
    RegistryError,
    StaleVersion,
    WorkspaceRow,
)
from v1.core.workspace_registry.snapshot import get_snapshot, get_store
from v1.core.workspace_registry.store import WorkspaceStore
from v1.utils.databricks_client import Caller, audit_log

# ``get_store`` is looked up on this module at call time, and ``databricks_registered``,
# ``get_settings`` and ``app_workspace_client`` on the /databricks route module, so tests
# can replace them.

logger = logging.getLogger(__name__)

NOT_FOUND_MESSAGE = "Not found."
NOT_ADMIN_MESSAGE = "Only workspace admins can do this."
ALIAS_TAKEN_MESSAGE = "A workspace with this alias already exists (it may be retired)."
STALE_MESSAGE = "This workspace changed since you opened it. Reload and try again."
NO_SUCH_WORKSPACE_MESSAGE = "No such workspace."
INVALID_MESSAGE = "{field} is invalid or missing."

BAD_HOST_MESSAGE = "Host must be https://<name>.azuredatabricks.net."
BAD_WAREHOUSE_MESSAGE = "SQL warehouse ID is invalid."
TEST_OK_MESSAGE = "Connected. The SQL warehouse was found."
TEST_NO_WAREHOUSE_MESSAGE = "Connected, but there is no SQL warehouse with this ID."
TEST_UNREACHABLE_MESSAGE = "Couldn't reach the workspace or check the SQL warehouse."
EXPORT_FILENAME = "databricks-workspaces.yaml"

CODE_NOT_FOUND = "not_found"
CODE_NOT_ADMIN = "not_admin"


def _invalid(field: str) -> DatabricksError:
    return DatabricksError(422, INVALID_MESSAGE.format(field=field), "invalid", field)


def _registry_error(exc: RegistryError) -> DatabricksError:
    if isinstance(exc, AliasTaken):
        return DatabricksError(409, ALIAS_TAKEN_MESSAGE, exc.code)
    if isinstance(exc, StaleVersion):
        return DatabricksError(409, STALE_MESSAGE, exc.code)
    if isinstance(exc, NotFound):
        return DatabricksError(404, NO_SUCH_WORKSPACE_MESSAGE, exc.code)
    if isinstance(exc, InvalidWorkspace):
        return _invalid(exc.field)
    raise exc


class DatabricksAdminRoute(DatabricksRoute):
    """:class:`DatabricksRoute`, with a :class:`RegistryError` mapped to its status and code,
    and a request FastAPI can't validate a 422 in the ``{"detail", "code", "field"}`` shape
    (``field`` the first error's last name).

    FastAPI decodes the body before it runs dependencies, so a body that isn't JSON fails
    before :func:`require_admin`: its checks run first here, so only an admin sees a 422.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def route_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except RegistryError as exc:
                return _registry_error(exc).response()
            except RequestValidationError as exc:
                try:
                    require_admin(request)
                except DatabricksError as denied:
                    return denied.response()
                errors = exc.errors()
                loc = errors[0].get("loc", ()) if errors else ()
                names = [part for part in loc if isinstance(part, str)]
                return _invalid(names[-1] if names else "body").response()

        return route_handler


def require_admin(request: Request) -> Actor:
    """Dependency: the admin making the request, as the registry records them.

    404 when this process doesn't run Databricks or has no registry store; 403 unless the
    caller is an admin (:func:`is_admin`) with a known identity.
    """

    if not routes.databricks_registered() or get_store() is None:
        raise DatabricksError(404, NOT_FOUND_MESSAGE, CODE_NOT_FOUND)
    identity = _user_field(request, "identity")
    if not is_admin(request) or identity is None:
        raise DatabricksError(403, NOT_ADMIN_MESSAGE, CODE_NOT_ADMIN)
    return Actor(identity, _user_field(request, "name"), _user_field(request, "email"))


def _store() -> WorkspaceStore:
    store = get_store()
    if store is None:  # require_admin checked; the store went at shutdown
        raise DatabricksError(404, NOT_FOUND_MESSAGE, CODE_NOT_FOUND)
    return store


# -- bodies and responses ------------------------------------------------------


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateWorkspace(_Body):
    # Typed loosely: the registry's own checks name the bad field.
    alias: Any
    host: Any
    sql_warehouse_id: Any
    label: Any = None
    data_allowlist: Any = None
    jobs_enabled: Any = None
    groups: Any = None


class EditWorkspace(_Body):
    version: int
    changes: dict[str, Any]


class VersionBody(_Body):
    version: int


class OrderBody(_Body):
    registry_version: int
    aliases: list[str]


class TestWorkspace(_Body):
    host: str
    sql_warehouse_id: str


class TestResult(BaseModel):
    host_ok: bool
    reachable: bool
    warehouse_found: bool
    warehouse_state: str | None
    message: str


def _tested(
    message: str,
    *,
    host_ok: bool = True,
    reachable: bool = False,
    found: bool = False,
    state: str | None = None,
) -> TestResult:
    return TestResult(
        host_ok=host_ok,
        reachable=reachable,
        warehouse_found=found,
        warehouse_state=state,
        message=message,
    )


class AdminWorkspace(BaseModel):
    alias: str
    label: str
    host: str
    sql_warehouse_id: str
    data_allowlist: str | None
    jobs_enabled: bool
    groups: list[str]
    position: int
    retired: bool
    retired_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


class AdminWorkspaces(BaseModel):
    registry_version: int
    workspaces: list[AdminWorkspace]


class HistoryActor(BaseModel):
    name: str | None
    email: str | None


class AdminHistoryEntry(BaseModel):
    at: datetime
    actor: HistoryActor
    action: str
    before: dict | None
    after: dict | None


def _admin_workspace(row: WorkspaceRow) -> AdminWorkspace:
    # Not ``created_by`` / ``updated_by``: they are hashed identities.
    return AdminWorkspace(
        alias=row.alias,
        label=row.label,
        host=row.host,
        sql_warehouse_id=row.sql_warehouse_id,
        data_allowlist=row.data_allowlist,
        jobs_enabled=row.jobs_enabled,
        groups=list(row.groups),
        position=row.position,
        retired=row.retired_at is not None,
        retired_at=row.retired_at,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _changed(before: WorkspaceRow, after: WorkspaceRow) -> list[str]:
    """The editable fields that differ between two versions of a row."""

    old, new = before.fields(), after.fields()
    return [name for name in EDITABLE_FIELDS if old[name] != new[name]]


def _seed_entry(row: WorkspaceRow) -> dict[str, Any]:
    """The row as a seed-file entry: only the keys that differ from the defaults."""

    entry: dict[str, Any] = {"host": row.host, "sql_warehouse_id": row.sql_warehouse_id}
    if row.label != row.alias:
        entry["label"] = row.label
    if row.data_allowlist is not None:
        entry["data_allowlist"] = row.data_allowlist
    if not row.jobs_enabled:
        entry["jobs_enabled"] = False
    if row.groups:
        entry["groups"] = list(row.groups)
    return entry


def _state(warehouse: Any) -> str | None:
    state = getattr(warehouse, "state", None)
    if state is None:
        return None
    return str(getattr(state, "value", state))


class RedirectRefused(Exception):
    """The workspace answered the test with a redirect: it is not followed."""


class RedirectsNotDisabled(Exception):
    """The SDK client has no session to turn redirects off on: the test is not made."""


def _refuse_redirect(response: requests.Response, *args: Any, **kwargs: Any) -> None:
    if 300 <= response.status_code < 400:
        raise RedirectRefused()


def _without_redirects(client: Any) -> None:
    """Make ``client`` follow no redirect, so the app's token goes to the tested host
    only; :class:`RedirectsNotDisabled` when the SDK's internals aren't as expected."""

    base = getattr(getattr(client, "api_client", None), "_api_client", None)
    session = getattr(base, "_session", None)
    hooks = getattr(session, "hooks", None)
    if not isinstance(session, requests.Session) or not isinstance(hooks, dict):
        raise RedirectsNotDisabled()
    session.max_redirects = 0  # requests raises TooManyRedirects before following one
    hooks.setdefault("response", []).append(_refuse_redirect)  # any 3xx, Location or not


async def _test_workspace(caller: Caller, host: str, warehouse_id: str) -> TestResult:
    """Look ``warehouse_id`` up on ``host`` as the app (the workspace is being tested, not
    a user's access), after one audit line naming ``caller`` once the client is built. A
    failure, the credential's included, is "unreachable" and logs its class only: the
    message can carry the URL or credential detail."""

    async def lookup() -> Any:
        client = await routes.app_workspace_client(host)
        audit_log(caller, workspace=host, action="admin_test", detail=f"warehouse={warehouse_id}")
        _without_redirects(client)
        # The SDK is synchronous: off the event loop.
        return await asyncio.to_thread(client.warehouses.get, warehouse_id)

    timeout_s = routes.get_settings().databricks_jobs_timeout_seconds
    try:
        # One timeout for building the client and the lookup.
        warehouse = await asyncio.wait_for(lookup(), timeout_s)
    except (DatabricksNotFound, ResourceDoesNotExist):
        return _tested(TEST_NO_WAREHOUSE_MESSAGE, reachable=True)
    except Exception as exc:  # noqa: BLE001 - any other failure: not reachable
        logger.warning("databricks_admin_test_failed error=%s", type(exc).__name__)
        return _tested(TEST_UNREACHABLE_MESSAGE)
    return _tested(TEST_OK_MESSAGE, reachable=True, state=_state(warehouse), found=True)


async def _written(
    store: WorkspaceStore, action: str, alias: str, actor: Actor, changed: list[str]
) -> None:
    """Serve the change from this replica at once, and log it (names, never values)."""

    try:
        await get_snapshot().refresh(store)
    except Exception as exc:  # noqa: BLE001 - the write stands; the poller catches up
        logger.error(
            "databricks workspace registry refresh failed (%s); keeping the last workspaces",
            type(exc).__name__,
        )
    logger.info(
        "databricks_registry_change action=%s alias=%s actor=%s changed=%s",
        action,
        alias,
        actor.identity,
        ",".join(changed) or "-",
    )


def create_databricks_admin_router() -> APIRouter:
    """Build the /databricks/admin router (auth as for the /databricks router)."""

    router = APIRouter(
        prefix="/databricks/admin",
        tags=["databricks-admin"],
        route_class=DatabricksAdminRoute,
        dependencies=[Depends(require_admin)],
    )

    @router.get("/workspaces")
    async def list_workspaces(include_retired: bool = False) -> AdminWorkspaces:
        store = _store()
        # The version first: the rows are never older than it, so a reorder sent with it
        # can't overwrite a change the admin didn't see.
        version = await store.registry_version()
        rows = await store.list(include_retired=include_retired)
        return AdminWorkspaces(
            registry_version=version, workspaces=[_admin_workspace(r) for r in rows]
        )

    @router.post("/workspaces", status_code=201)
    async def create_workspace(
        body: CreateWorkspace, actor: Actor = Depends(require_admin)
    ) -> AdminWorkspace:
        store = _store()
        fields = body.model_dump(exclude_unset=True, exclude={"alias"})
        row = await store.create(body.alias, fields, actor)
        await _written(store, "created", row.alias, actor, sorted(fields))
        return _admin_workspace(row)

    @router.patch("/workspaces/{alias}")
    async def edit_workspace(
        alias: str, body: EditWorkspace, actor: Actor = Depends(require_admin)
    ) -> AdminWorkspace:
        store = _store()
        # Read first, to name the changed fields by diffing (the newest history entry may
        # already be another admin's edit).
        rows = await store.list(include_retired=True)
        before = next((r for r in rows if r.alias == alias), None)
        row = await store.update(alias, body.changes, body.version, actor)
        if row.version != body.version:  # an edit that changed nothing records nothing
            if before is not None and before.version == body.version:
                changed = _changed(before, row)
            else:  # the read was of another version: name what was asked for
                changed = sorted(k for k in body.changes if k in EDITABLE_FIELDS)
            await _written(store, "edited", alias, actor, changed)
        return _admin_workspace(row)

    @router.post("/workspaces/{alias}/retire")
    async def retire_workspace(
        alias: str, body: VersionBody, actor: Actor = Depends(require_admin)
    ) -> AdminWorkspace:
        store = _store()
        row = await store.retire(alias, body.version, actor)
        if row.version != body.version:
            await _written(store, "retired", alias, actor, ["retired"])
        return _admin_workspace(row)

    @router.post("/workspaces/{alias}/restore")
    async def restore_workspace(
        alias: str, body: VersionBody, actor: Actor = Depends(require_admin)
    ) -> AdminWorkspace:
        store = _store()
        row = await store.restore(alias, body.version, actor)
        if row.version != body.version:
            await _written(store, "restored", alias, actor, ["retired"])
        return _admin_workspace(row)

    @router.put("/workspaces/order", status_code=204)
    async def reorder_workspaces(
        body: OrderBody, actor: Actor = Depends(require_admin)
    ) -> Response:
        store = _store()
        moved = await store.reorder(body.aliases, body.registry_version, actor)
        if moved:  # the order they already had records nothing
            await _written(store, "reordered", "*", actor, ["position"])
        return Response(status_code=204)

    @router.post("/workspaces/test")
    async def test_workspace(request: Request, body: TestWorkspace) -> TestResult:
        # 401 with the sign-in message without a caller, as on the /databricks routes
        # (require_admin already needs an identity, so an admin always has one).
        caller = _request_caller(request)
        # Neither check passed: no network call, so no token goes anywhere.
        host = check_host(body.host)
        if host is None:
            return _tested(BAD_HOST_MESSAGE, host_ok=False)
        warehouse_id = check_warehouse_id(body.sql_warehouse_id)
        if warehouse_id is None:
            return _tested(BAD_WAREHOUSE_MESSAGE)
        return await _test_workspace(caller, host, warehouse_id)

    @router.get("/workspaces/export.yaml")
    async def export_workspaces() -> Response:
        rows = await _store().list()  # the live ones, in menu order
        entries = {row.alias: _seed_entry(row) for row in rows}
        text = yaml.safe_dump(entries, sort_keys=False, allow_unicode=True)
        return Response(
            content=text,
            media_type="text/yaml",
            headers={"Content-Disposition": f'attachment; filename="{EXPORT_FILENAME}"'},
        )

    @router.get("/workspaces/{alias}/history")
    async def workspace_history(alias: str) -> list[AdminHistoryEntry]:
        store = _store()
        if alias not in {r.alias for r in await store.list(include_retired=True)}:
            raise DatabricksError(404, NO_SUCH_WORKSPACE_MESSAGE, CODE_NOT_FOUND)
        # The actor's name and email only, never their hashed identity.
        entries = await store.history(alias)
        return [AdminHistoryEntry.model_validate(e, from_attributes=True) for e in entries]

    return router


__all__ = ["create_databricks_admin_router", "is_admin", "require_admin"]
