"""Databricks endpoints for the chat UI.

``GET /databricks/capabilities`` tells the UI whether this caller can use
Databricks (the table picker, the Job failures page), which workspaces they see
(config order; the first is their default), and whether they are a workspace admin
(``is_admin``; the admin API is :mod:`v1.api.routes.databricks_admin`). Its ``reason`` is
always null (kept for the UI, which reads it). It makes no Databricks call.

``GET /databricks/tables?q=&workspace=`` feeds the table picker: the tables in the
workspace's ``data_allowlist`` (the picker lists what a run may be narrowed to,
never a thread's current selection) that match ``q``, as the app sees them.

``GET /databricks/job-failures/{run,job,explain}?workspace=`` are the Job failures
page's lookups: the chat's job tool functions run directly, with no agent in
between, on a Jobs client acting as the app. ``explain`` adds the fixed status and a
short model-written explanation for a failed run. A run link on another workspace
the caller can see gets ``workspace_hint`` (that alias) instead of a lookup.

``workspace`` is an alias; without it, the caller's legacy or default workspace. An
unknown, hidden or blank alias is a 403 with ``WORKSPACE_UNAVAILABLE_MESSAGE``, never
another workspace.

Every 401/403 these routes return is ``{"detail": <user-safe text>, "code": <code>}``
(:class:`DatabricksError`, through :class:`DatabricksRoute`); the UI branches on ``code``
and shows ``detail``. Codes: ``not_available`` (403, Databricks is off for the caller),
``workspace_unavailable`` (403), ``jobs_disabled`` (403, the chosen workspace has the job
lookup off), ``warehouse_denied`` (403, Databricks refused the app the SQL warehouse or
the workspace), and ``sign_in_required`` (401, the request carries no caller identity).
Other errors (422, 502, 503) are unchanged.

Every Databricks call a route makes runs as the app
(:func:`v1.utils.databricks_client.app_workspace_client`), never as the caller, so the
app's gates decide what they reach. :func:`request_workspace_client` still needs the
caller (the principal LangGraph stamps onto ``request.user``): it writes one audit line
naming them, and table listings carry them in their SQL ``query_tags``. Routes guard
themselves with :func:`require_enabled` / :func:`require_jobs` before any client is built.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from databricks.sdk import WorkspaceClient
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel

from v1.core.config import admin_object_ids, databricks_registered, get_settings
from v1.core.databricks_workspaces import (
    DATABRICKS_SUBAGENT_NAME,
    WORKSPACE_UNAVAILABLE_MESSAGE,
    Workspace,
    WorkspaceUnavailable,
    resolve_workspace,
    visible_workspaces,
)
from v1.core.middlewares import access_control
from v1.core.tools.databricks.jobs.client import JobsClient, failure_fields
from v1.core.tools.databricks.jobs.explain import (
    ExplanationCache,
    ExplanationError,
    cache_key,
    write_explanation,
)
from v1.core.tools.databricks.jobs.fixes import fixed_status
from v1.core.tools.databricks.jobs.refs import link_host, parse_job_id, parse_run_ref, same_host
from v1.core.tools.databricks.jobs.tools import (
    JOBS_DISABLED_MESSAGE,
    explain_run,
    explain_run_detail,
    find_failed_runs,
)
from v1.core.tools.databricks.jobs.traces import where_failed
from v1.core.tools.databricks.sql.statements import caller_tags
from v1.core.tools.databricks.sql.tools import (
    default_statement_client_factory,
    keywords_of,
    query_tables,
)
from v1.core.workspace_registry.snapshot import on_change
from v1.utils.databricks_client import (
    CLIENT_UNAVAILABLE_MESSAGE,
    SIGN_IN_MESSAGE,
    Caller,
    DatabricksAccessError,
    app_workspace_client,
    audit_log,
    request_caller,
)
from v1.utils.group_routing import groups_from_request

# ``databricks_registered``, ``get_settings``, ``app_workspace_client``, ``resolve_workspace``,
# ``default_statement_client_factory``, ``JobsClient``, ``chat_model`` and
# ``EXPLANATIONS`` are looked up on this module at call time so tests can replace them.

logger = logging.getLogger(__name__)

NOT_AVAILABLE_MESSAGE = "Databricks data and job lookup is not available for your access."
TABLES_FAILED_MESSAGE = "Couldn't load tables. Try again."
WAREHOUSE_DENIED_MESSAGE = "This assistant can't use the SQL warehouse. Ask an admin."
OPEN_TABLES_LIMIT = 50
ALLOWLIST_TABLES_LIMIT = 500
MAX_QUERY_CHARS = 200
MAX_ALIAS_CHARS = 100

ENTER_A_REF_MESSAGE = "Enter a job ID, run ID or run link."
JOB_ID_MESSAGE = "A job ID is a number."
RUN_ID_MESSAGE = "A run ID is a number."
MAX_LOOKUP_CHARS = 4000

# The ``code`` beside ``detail`` on every Databricks 401/403 (see the module docstring).
CODE_NOT_AVAILABLE = "not_available"
CODE_WORKSPACE_UNAVAILABLE = "workspace_unavailable"
CODE_JOBS_DISABLED = "jobs_disabled"
CODE_WAREHOUSE_DENIED = "warehouse_denied"
CODE_SIGN_IN_REQUIRED = "sign_in_required"

# Explanations by workspace alias plus ``cache_key`` (the run and exactly what the model was
# given), until restart: run IDs are only unique within a workspace.
EXPLANATIONS = ExplanationCache()
# A workspace that changed or went may be another one behind its alias: its run IDs no
# longer name the runs explained.
on_change(lambda aliases: EXPLANATIONS.drop_workspaces(aliases))


def chat_model() -> BaseChatModel:
    """The backend's chat model, for explanations. Imported here: ``v1.core.agent`` is heavy."""

    from v1.core.agent import get_azure_chat_model

    return get_azure_chat_model()


class DatabricksError(HTTPException):
    """A Databricks 401/403/404/409/422: ``detail`` is user-safe text, ``code`` a stable
    machine code, ``field`` the bad field of a 422.

    :class:`DatabricksRoute` returns it as ``{"detail": ..., "code": ...}`` (plus ``"field"``
    when set); anywhere else it is a plain ``HTTPException``.
    """

    def __init__(self, status_code: int, detail: str, code: str, field: str | None = None) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.code = code
        self.field = field

    def response(self) -> JSONResponse:
        content: dict[str, Any] = {"detail": self.detail, "code": self.code}
        if self.field is not None:
            content["field"] = self.field
        return JSONResponse(status_code=self.status_code, content=content, headers=self.headers)


def sign_in_error() -> DatabricksError:
    """The 401 for a request that carries no caller identity."""

    return DatabricksError(401, SIGN_IN_MESSAGE, CODE_SIGN_IN_REQUIRED)


class DatabricksRoute(APIRoute):
    """The /databricks routes' class: a :class:`DatabricksError` (raised by the route or a
    dependency) becomes ``{"detail", "code"}``. Scoped to these routes; FastAPI's handling
    elsewhere is unchanged."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def route_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except DatabricksError as exc:
                return exc.response()

        return route_handler


@dataclass
class DatabricksAccess:
    """What the caller may do with Databricks; ``reason`` is always None (the UI still
    reads it).

    ``workspaces`` are the ones they see (config order), empty when Databricks is off for
    them (not registered, blocked by the access-control file).
    """

    enabled: bool
    jobs_enabled: bool
    reason: str | None
    workspaces: tuple[Workspace, ...] = ()


class WorkspaceEntry(BaseModel):
    alias: str
    label: str
    jobs_enabled: bool


class CapabilitiesResponse(BaseModel):
    enabled: bool
    jobs_enabled: bool
    reason: str | None
    workspaces: list[WorkspaceEntry]
    default_workspace: str | None
    # The alias a chat with no saved workspace (or "default") resolves to for this caller;
    # null when their default would stand in (its tables dropped), or they're disabled.
    legacy_workspace: str | None
    # Whether the caller may use the workspace admin page (``/databricks/admin``), whatever
    # ``enabled`` says: an admin need not see any workspace.
    is_admin: bool


class TableEntry(BaseModel):
    full_name: str
    type: str | None
    comment: str | None


class TablesResponse(BaseModel):
    mode: Literal["open", "allowlist"]
    tables: list[TableEntry]
    truncated: bool
    needs_query: bool


def _user_field(request: Request, name: str) -> str | None:
    """A non-blank string field of ``request.user``, else ``None`` (also with no user)."""

    try:
        user: Any = request.user
        getter = getattr(user, "get", None)
        value = getter(name) if callable(getter) else getattr(user, name, None)
    except Exception:  # noqa: BLE001 - no auth backend, or a field the principal lacks
        return None
    if isinstance(value, str) and value.strip():
        return value
    return None


def caller_access(request: Request) -> DatabricksAccess:
    """Whether the caller may use Databricks, and the jobs lookup.

    Enabled needs this process to run Databricks (:func:`databricks_registered`),
    the access-control file not blocking the databricks-agent for them, and a workspace their
    groups can see. The jobs lookup is on when any workspace they see has it.
    """

    groups = groups_from_request(request)
    blocked = DATABRICKS_SUBAGENT_NAME in access_control.caller_access(groups).blocked_subagents
    visible = visible_workspaces(groups) if databricks_registered() and not blocked else ()
    enabled = bool(visible)
    return DatabricksAccess(
        enabled=enabled,
        jobs_enabled=enabled and any(w.jobs_enabled for w in visible),
        reason=None,
        workspaces=visible,
    )


def is_admin(request: Request) -> bool:
    """Whether the caller is a workspace admin: this process runs Databricks and one of
    their groups' object ID is in ``DATABRICKS_ADMIN_GROUPS`` (display names there are
    ignored: anyone can create a group with a given name)."""

    if not databricks_registered():
        return False
    admins = admin_object_ids(get_settings().databricks_admin_groups)
    return any(g.strip().lower() in admins for g in groups_from_request(request))


def _not_available() -> DatabricksError:
    return DatabricksError(403, NOT_AVAILABLE_MESSAGE, CODE_NOT_AVAILABLE)


def require_enabled(request: Request) -> DatabricksAccess:
    """Dependency: 403 unless the caller may use Databricks."""

    access = caller_access(request)
    if access.enabled:
        return access
    raise _not_available()


def requested_workspace(request: Request, alias: str | None) -> Workspace:
    """The workspace ``alias`` names for the caller (their legacy or default one when None).

    403 with ``WORKSPACE_UNAVAILABLE_MESSAGE`` when it is unknown or hidden from them, or
    they see none. Unknown and hidden read the same, so aliases can't be probed.
    """

    try:
        workspace, _ = resolve_workspace(groups_from_request(request), alias)
    except WorkspaceUnavailable:
        raise DatabricksError(
            403, WORKSPACE_UNAVAILABLE_MESSAGE, CODE_WORKSPACE_UNAVAILABLE
        ) from None
    return workspace


def legacy_workspace(request: Request) -> str | None:
    """The alias a chat with no saved workspace (or ``default``) resolves to for the
    caller, or None when their default would stand in (tables dropped) or such a chat is
    refused (the legacy workspace is hidden from them or not configured)."""

    try:
        workspace, fallback = resolve_workspace(groups_from_request(request), None)
    except WorkspaceUnavailable:
        return None
    return None if fallback else workspace.alias


def require_jobs(
    request: Request, workspace: str | None = Query(None, max_length=MAX_ALIAS_CHARS)
) -> Workspace:
    """Dependency: the chosen workspace (``?workspace=``), once the caller may use the jobs
    lookup there.

    403 when Databricks is off for them, the workspace is unknown or hidden, or it has the
    jobs lookup off (``JOBS_DISABLED_MESSAGE``); all before the caller check, so they are
    always a 403, never a sign-in 401.
    """

    if not caller_access(request).workspaces:
        raise _not_available()
    chosen = requested_workspace(request, workspace)
    if not chosen.jobs_enabled:
        raise DatabricksError(
            403, JOBS_DISABLED_MESSAGE.format(label=chosen.label), CODE_JOBS_DISABLED
        )
    require_enabled(request)
    return chosen


# A job route's workspace, through :func:`require_jobs`.
JobsWorkspace = Annotated[Workspace, Depends(require_jobs)]


def _request_caller(request: Request) -> Caller:
    """The caller behind ``request``; 401 with the sign-in message when it carries no
    identity (also when there is no auth middleware: ``request.user`` then raises)."""

    try:
        user: Any = request.user
    except Exception:  # noqa: BLE001 - no auth backend: no caller
        user = None
    try:
        return request_caller(user)
    except DatabricksAccessError:
        raise sign_in_error() from None


async def _caller_and_client(
    request: Request, workspace: Workspace, detail: str
) -> tuple[Caller, WorkspaceClient]:
    """The caller and a ``WorkspaceClient`` acting as the app on ``workspace``, after one
    audit line naming the caller, the route and ``detail``.

    401 with the sign-in message when there is no caller (nothing is built), 503 when
    building the client fails (the credential, or the SDK's config resolution).
    """

    caller = _request_caller(request)
    try:
        client = await app_workspace_client(workspace.host)
    except Exception as exc:  # noqa: BLE001 - the credential, or the SDK's config resolution
        # Class only: the message can carry the host URL, request or credential detail.
        logger.warning(
            "databricks_client_unavailable route=%s error=%s", request.url.path, type(exc).__name__
        )
        raise HTTPException(status_code=503, detail=CLIENT_UNAVAILABLE_MESSAGE) from None
    audit_log(caller, workspace=workspace.alias, action=request.url.path, detail=detail)
    return caller, client


async def request_workspace_client(
    request: Request, workspace: Workspace, detail: str = "-"
) -> WorkspaceClient:
    """A ``WorkspaceClient`` acting as the app on ``workspace`` for the request's caller
    (``detail``, e.g. the job or run asked about, goes in the audit line); see
    :func:`_caller_and_client` for the 401 and 503."""

    _, client = await _caller_and_client(request, workspace, detail)
    return client


def _checked(text: str) -> str:
    """The lookup input, trimmed; 422 when it is blank or too long."""

    text = text.strip()
    if not text or len(text) > MAX_LOOKUP_CHARS:
        raise HTTPException(status_code=422, detail=ENTER_A_REF_MESSAGE)
    return text


async def _jobs_client(request: Request, workspace: Workspace, detail: str) -> JobsClient:
    """A Jobs client acting as the app on ``workspace``: Databricks applies the app's job
    permissions. ``detail`` (the job or run asked about) goes in the audit line."""

    workspace_client = await request_workspace_client(request, workspace, detail)
    return JobsClient(workspace_client, timeout_s=get_settings().databricks_jobs_timeout_seconds)


def _log_lookup(kind: str, result: dict, started: float, **extra: object) -> None:
    """One metadata-only line per lookup: IDs, status and latency, never job output."""

    logger.info(
        "databricks_job_lookup kind=%s run_id=%s job_id=%s status=%s%s latency_ms=%d",
        kind,
        result.get("run_id"),
        result.get("job_id"),
        result.get("status"),
        "".join(f" {key}={value}" for key, value in extra.items()),
        int((time.monotonic() - started) * 1000),
    )


def _hint(request: Request, text: str, workspace: Workspace) -> str | None:
    """The alias of another workspace the caller sees that ``text``'s run link is on, if any.

    A link on the chosen workspace, a bare ID, a host no visible workspace has, or one whose
    workspace has the job lookup off (following the hint would only be refused): None.
    """

    host = link_host(text)
    if host is None or same_host(host, workspace.host):
        return None
    for other in visible_workspaces(groups_from_request(request)):
        if other.jobs_enabled and same_host(host, other.host):
            return other.alias
    return None


async def _fixed(client: JobsClient, run_id: int) -> dict | None:
    """The run's fixed status, or None when it can't be read; never blocks the explanation.

    The run is read again for its ``run_type``, which ``explain_run``'s content leaves out.
    """

    try:
        found = await client.get_run(run_id)
        return None if found is None else await fixed_status(client, found)
    except Exception as exc:  # noqa: BLE001 - the fixed check is best effort
        logger.warning("jobs_api_failed run_id=%s %s", run_id, failure_fields(exc))
        return None


def create_databricks_router() -> APIRouter:
    """Build the /databricks router.

    Authentication is enforced by LangGraph's auth middleware. Custom HTTP routes
    are only covered when ``http.enable_custom_route_auth`` is true in
    langgraph.json (it is); that flag is also what populates ``request.user``, so
    without it every Databricks call is a sign-in 401 (no caller to name).
    """

    router = APIRouter(prefix="/databricks", tags=["databricks"], route_class=DatabricksRoute)

    @router.get("/capabilities")
    async def get_capabilities(request: Request) -> CapabilitiesResponse:
        access = caller_access(request)
        logger.debug(
            "databricks capabilities: enabled=%s jobs_enabled=%s reason=%s",
            access.enabled,
            access.jobs_enabled,
            access.reason is not None,
        )
        # Only an enabled caller gets workspaces: the UI shows no Databricks controls otherwise.
        shown = access.workspaces if access.enabled else ()
        return CapabilitiesResponse(
            enabled=access.enabled,
            jobs_enabled=access.jobs_enabled,
            reason=access.reason,
            workspaces=[
                WorkspaceEntry(alias=w.alias, label=w.label, jobs_enabled=w.jobs_enabled)
                for w in shown
            ],
            default_workspace=shown[0].alias if shown else None,
            legacy_workspace=legacy_workspace(request) if access.enabled else None,
            is_admin=is_admin(request),
        )

    @router.get("/tables", dependencies=[Depends(require_enabled)], response_model=None)
    async def get_tables(
        request: Request,
        q: str = Query("", max_length=MAX_QUERY_CHARS),
        workspace: str | None = Query(None, max_length=MAX_ALIAS_CHARS),
    ) -> TablesResponse | JSONResponse:
        """Tables in the workspace's scope matching ``q``, as the app sees them; the
        listing's ``query_tags`` name the caller.

        Open scope needs keywords (it would list the whole metastore) and returns up
        to 50; an allowlist is listed whole (narrowed by ``q`` when given), up to 500.
        One extra row is fetched to tell whether the list was cut.
        """

        chosen = requested_workspace(request, workspace)
        scope = chosen.scope
        mode: Literal["open", "allowlist"] = "open" if scope.is_open else "allowlist"
        words = keywords_of(q)
        if scope.is_open and not words:
            return TablesResponse(mode=mode, tables=[], truncated=False, needs_query=True)

        limit = OPEN_TABLES_LIMIT if scope.is_open else ALLOWLIST_TABLES_LIMIT
        caller, workspace_client = await _caller_and_client(request, chosen, q or "-")
        client = default_statement_client_factory(get_settings())(
            workspace_client,
            chosen.sql_warehouse_id,
            caller_tags(caller, workspace=chosen.alias, source="api"),
        )
        result, tables = await query_tables(client, words, scope=scope, limit=limit + 1)
        if result.status == "permission_denied":
            # Fixed text: Databricks' own message is never shown.
            raise DatabricksError(403, WAREHOUSE_DENIED_MESSAGE, CODE_WAREHOUSE_DENIED)
        if result.status != "completed":
            ref = uuid.uuid4().hex[:8]
            logger.warning(
                "databricks_tables_failed ref=%s status=%s message=%s",
                ref,
                result.status,
                result.message,
            )
            return JSONResponse(
                status_code=502, content={"message": TABLES_FAILED_MESSAGE, "ref": ref}
            )
        return TablesResponse(
            mode=mode,
            tables=[TableEntry(**table) for table in tables[:limit]],
            truncated=len(tables) > limit,
            needs_query=False,
        )

    @router.get("/job-failures/run")
    async def get_job_failure_run(
        request: Request, chosen: JobsWorkspace, ref: str = ""
    ) -> dict[str, Any]:
        """A run's phase and, when it failed, each failed task's detail and where it failed.

        ``ref`` is a run ID or a run link from the chosen workspace. A link on another
        workspace the caller sees returns ``{"result": {"status": "other_workspace"},
        "workspace_hint": <alias>}`` without looking it up there (the page switches and
        asks again); a link on any other host is rejected.
        """

        started = time.monotonic()
        text = _checked(ref)
        hint = _hint(request, text, chosen)
        if hint is not None:
            logger.info("databricks_job_lookup kind=run status=other_workspace")
            return {"result": {"status": "other_workspace"}, "workspace_hint": hint}
        client = await _jobs_client(request, chosen, text)
        content, detail = await explain_run_detail(client, text, workspace_host=chosen.host)
        result = json.loads(content)
        _log_lookup("run", result, started)
        # Read from each task's full output, never from the model.
        where = [where_failed(task) for task in (detail or {}).get("tasks", [])]
        return {"result": result, "detail": detail, "where": where}

    @router.get("/job-failures/job")
    async def get_job_failure_job(
        request: Request, chosen: JobsWorkspace, id: str = ""
    ) -> dict[str, Any]:
        """A job's last failed runs, given its numeric ID."""

        started = time.monotonic()
        # IDs only: a name would make this a name search, which the page doesn't offer.
        text = _checked(id)
        if parse_job_id(text) is None:
            raise HTTPException(status_code=422, detail=JOB_ID_MESSAGE)
        client = await _jobs_client(request, chosen, text)
        result = json.loads(await find_failed_runs(client, text))
        _log_lookup("job", result, started)
        return {"result": result}

    @router.get("/job-failures/explain")
    async def get_job_failure_explain(
        request: Request, chosen: JobsWorkspace, run: str = ""
    ) -> dict[str, Any]:
        """A failed run's fixed status and a short explanation, given its numeric run ID.

        Nothing past the lookup runs unless the run failed (not found, denied and not
        failed all stop there). The explanation is cached per workspace, run and the exact
        content the model was given; the run and its fixed status are read again every time.
        """

        started = time.monotonic()
        run_id = parse_run_ref(_checked(run), None)  # a bare numeric ID only
        if run_id is None:
            raise HTTPException(status_code=422, detail=RUN_ID_MESSAGE)
        client = await _jobs_client(request, chosen, str(run_id))
        content = await explain_run(client, str(run_id), workspace_host=None)
        result = json.loads(content)
        body: dict[str, Any] = {
            "result": result,
            "fixed": None,
            "explanation": None,
            "explanation_error": False,
        }
        if result.get("status") != "failed":
            _log_lookup("explain", result, started)
            return body

        body["fixed"] = await _fixed(client, run_id)
        key = (chosen.alias, *cache_key(run_id, content))
        cached = EXPLANATIONS.get(key)
        if cached is not None:
            body["explanation"] = cached.model_dump()
        else:
            try:
                try:
                    model = chat_model()
                except Exception as exc:  # noqa: BLE001 - keep the run and `fixed`, just no text
                    raise ExplanationError(f"model unavailable ({type(exc).__name__})") from None
                explanation = await write_explanation(
                    model, content, timeout_s=get_settings().databricks_explain_timeout_seconds
                )
            except ExplanationError as exc:
                # The error's text names the failure's type only, never the reply or the run.
                logger.warning("explanation_failed run_id=%s reason=%s", run_id, exc)
                body["explanation_error"] = True
            else:
                EXPLANATIONS.put(key, explanation)
                body["explanation"] = explanation.model_dump()
        _log_lookup(
            "explain",
            result,
            started,
            fixed=(body["fixed"] or {}).get("status"),
            cached=cached is not None,
            explanation_error=body["explanation_error"],
        )
        return body

    return router
