"""The SQL tools: find tables, describe one, run a read-only query, offer a table choice.

Every tool runs as the app (its managed identity) and tags each statement with the caller
(``query_tags``), so Databricks shows who asked. Each tool wraps a plain function (`search`,
`describe`, `run_query`, `choose`), so the logic is testable without a LangGraph runtime.
"""

import json
import logging
import re
from collections.abc import Callable, Mapping
from typing import Any

from databricks.sdk.service.sql import StatementState
from langchain_core.tools import BaseTool, tool

from v1.core.config import Settings
from v1.core.databricks_workspaces import Workspace, WorkspaceUnavailable
from v1.core.tools.databricks.channel import emit_status, record_choices, record_result
from v1.core.tools.databricks.labels import with_workspace
from v1.core.tools.databricks.run_workspace import run_workspace, workspace_unavailable_payload
from v1.core.tools.databricks.sql.guard import SqlRejected, check_sql, checked_sql
from v1.core.tools.databricks.sql.scope import (
    OUTSIDE_SCOPE,
    DataScope,
    format_name,
    table_parts,
)
from v1.core.tools.databricks.sql.selection import (
    SELECTION_REJECTED,
    SelectionInvalid,
    run_scope,
)
from v1.core.tools.databricks.sql.statements import (
    StatementClient,
    StatementResult,
    caller_tags,
)
from v1.utils.databricks_client import (
    DatabricksAccessError,
    access_error_payload,
    app_workspace_client,
    audit_log,
    client_error_payload,
    run_caller,
)

# ``app_workspace_client``, ``run_caller`` and ``run_workspace`` are looked up on this module
# at call time so tests can replace them.

logger = logging.getLogger(__name__)

# (WorkspaceClient, the workspace's SQL warehouse id, query_tags or None) -> StatementClient
StatementClientFactory = Callable[[Any, str, Mapping[str, str] | None], StatementClient]
StatusCallback = Callable[[str], None]
ResultCallback = Callable[[dict], None]

RUN_SQL = "run_sql"
CHOOSE_TABLES = "choose_tables"

SEARCHING = "Searching tables…"
DESCRIBING = "Reading table schema…"
WAITING = "Waiting for the SQL warehouse to start…"
RUNNING = "Running query…"

MAX_KEYWORDS = 8
MAX_SEARCH_RESULTS = 50
SAMPLE_ROWS = 3
MAX_COLUMNS = 500

SEARCH_DESCRIPTION = (
    "Find tables, views and metric views by keywords, matched against catalog, schema and "
    "table names and table comments. Returns full names to pass to describe_table and run_sql. "
    "Metric views are listed first: prefer them for business metrics. A listed table can still "
    "turn out to be unreadable."
)
DESCRIBE_DESCRIPTION = (
    "Get the columns (name, type, comment) of one table, view or metric view, plus "
    f"{SAMPLE_ROWS} sample rows for tables and views. Pass the full name exactly as "
    "search_tables returned it."
)
RUN_SQL_DESCRIPTION = (
    "Run one read-only Databricks SQL SELECT query. Use fully qualified catalog.schema.table "
    "names, backticking names with special characters. "
    "Aggregate in SQL; never fetch raw rows to count or sum them. You see the first rows only "
    "(check `truncated`)."
)
MIN_CHOICES = 2
MAX_CHOICES = 6
DEFAULT_CHOICE_QUESTION = "Which table should I use?"
MAX_CHOICE_QUESTION_CHARS = 200  # the model writes the question; keep it a short heading
CHOOSE_DESCRIPTION = (
    f"Show the user {MIN_CHOICES}-{MAX_CHOICES} candidate tables as buttons. Call it only when "
    "the tables are truly ambiguous: they would give different answers and nothing in the "
    "question tells them apart. Never call it when the user has already selected tables. Pass "
    "full names exactly as search_tables returned them, and one short question. After calling "
    "it, stop: end your turn with one short sentence; do not answer the question yet."
)
WAITING_FOR_USER = (
    "The user will pick a table. End your turn with one short sentence; do not answer the "
    "question yet."
)
SELECTION_ACTIVE = (
    "The user already selected tables for this chat, so don't offer a choice. Use the selected "
    "tables, picking the best fit."
)
METRIC_VIEW_HINT = (
    "This is a metric view. Select dimension columns and wrap measure columns in MEASURE(), "
    "then GROUP BY the dimensions, e.g. SELECT `Region`, MEASURE(`Total Sales`) FROM <name> "
    "GROUP BY ALL. Columns whose comment describes a count, sum, average or rate are measures. "
    "SELECT * does not work on metric views."
)

ERROR_MESSAGES = {
    "permission_or_not_found": (
        "The table doesn't exist or this assistant doesn't have access to it. Tell the user "
        "an admin can grant access; do not try other tables. Details: {detail}"
    ),
    "permission_denied": (
        "This assistant doesn't have access to Databricks here (the SQL warehouse or the "
        "workspace). Tell the user to ask a workspace admin to grant the app access; do not "
        "retry."
    ),
    "timeout": (
        "The query took too long and was cancelled. Suggest a narrower question (shorter date "
        "range, fewer breakdowns)."
    ),
    "failed": "The query failed. Fix it and try again if you can. Details: {detail}",
}

_HAYSTACK = "lower(concat_ws(' ', table_catalog, table_schema, table_name, coalesce(comment, '')))"
_TABLES_SQL = """\
SELECT table_catalog, table_schema, table_name, table_type, comment
FROM system.information_schema.tables
WHERE table_schema <> 'information_schema'
  AND {scope}{match}
ORDER BY (table_type = 'METRIC_VIEW') DESC, {rank}table_catalog, table_schema, table_name
LIMIT {limit}"""
# Exact matches on system.information_schema take ~2 s; lower() on both sides took ~48 s.
_DESCRIBE_SQL = """\
SELECT t.table_type, t.comment, c.column_name, c.full_data_type, c.comment
FROM system.information_schema.tables t
LEFT JOIN system.information_schema.columns c
  ON c.table_catalog = t.table_catalog
  AND c.table_schema = t.table_schema
  AND c.table_name = t.table_name
WHERE t.table_catalog = :catalog AND t.table_schema = :schema AND t.table_name = :table
ORDER BY c.ordinal_position"""


def _dump(payload: dict) -> str:
    return json.dumps(payload, default=str)


def _rejected(message: str) -> str:
    return _dump({"status": "rejected", "message": message})


def _error(status: str, detail: str | None) -> str:
    return _dump({"status": status, "message": ERROR_MESSAGES[status].format(detail=detail)})


def _reporter(
    on_status: StatusCallback | None, running_text: str | None = None
) -> Callable[[StatementState], None]:
    texts = {StatementState.PENDING: WAITING, StatementState.RUNNING: running_text}

    def report(state: StatementState) -> None:
        if on_status and (text := texts.get(state)):
            on_status(text)

    return report


def keywords_of(text: str) -> list[str]:
    words = (word.lower().replace("%", "") for word in re.split(r"[\s,]+", text))
    return [word for word in words if len(word) >= 2][:MAX_KEYWORDS]


def _like_literal(word: str) -> str:
    """``word`` with LIKE's ``_`` wildcard and its default backslash escape matched literally."""
    return word.replace("\\", "\\\\").replace("_", "\\_")


def _tables_query(words: list[str], scope: DataScope, limit: int) -> tuple[str, dict[str, str]]:
    """Tables in scope, optionally ranked by keyword hits. No keywords = list everything."""
    scope_sql, params = scope.search_filter()
    match = rank = ""
    if words:
        hits = " + ".join(
            f"CASE WHEN {_HAYSTACK} LIKE :k{i} THEN 1 ELSE 0 END" for i in range(len(words))
        )
        match, rank = f"\n  AND ({hits}) > 0", f"({hits}) DESC, "
        patterns = {f"k{i}": f"%{_like_literal(word)}%" for i, word in enumerate(words)}
        params = {**params, **patterns}
    return _TABLES_SQL.format(scope=scope_sql, match=match, rank=rank, limit=limit), params


async def query_tables(
    client: StatementClient,
    words: list[str],
    *,
    scope: DataScope,
    limit: int,
    on_state: Callable[[StatementState], None] | None = None,
) -> tuple[StatementResult, list[dict]]:
    sql, params = _tables_query(words, scope, limit)
    result = await client.execute(sql, parameters=params, max_rows=limit, on_state=on_state)
    tables = [
        {"full_name": format_name(catalog, schema, table), "type": kind, "comment": comment}
        for catalog, schema, table, kind, comment in result.rows
    ]
    return result, tables


async def search(
    client: StatementClient,
    keywords: str,
    *,
    scope: DataScope,
    limit: int = 20,
    on_status: StatusCallback | None = None,
) -> str:
    words = keywords_of(keywords)
    if not words:
        return _rejected("give one or more keywords of at least 2 characters")
    if on_status:
        on_status(SEARCHING)
    limit = min(max(limit, 1), MAX_SEARCH_RESULTS)
    result, tables = await query_tables(
        client, words, scope=scope, limit=limit, on_state=_reporter(on_status)
    )
    if result.status != "completed":
        return _error(result.status, result.message)
    return _dump({"status": "completed", "tables": tables})


async def describe(
    client: StatementClient,
    full_name: str,
    *,
    scope: DataScope,
    on_status: StatusCallback | None = None,
) -> str:
    parts = table_parts(full_name)
    if parts is None:
        return _rejected("use the fully qualified catalog.schema.table name from search_tables")
    name = format_name(*parts)
    if not scope.covers(*parts):
        return _rejected(OUTSIDE_SCOPE.format(name))
    if on_status:
        on_status(DESCRIBING)
    report = _reporter(on_status)
    info = await client.execute(
        _DESCRIBE_SQL,
        parameters=dict(zip(("catalog", "schema", "table"), parts, strict=True)),
        max_rows=MAX_COLUMNS,
        on_state=report,
    )
    if info.status != "completed":
        return _error(info.status, info.message)
    if not info.rows:
        return _error("permission_or_not_found", f"{name} was not found")
    table_type, comment = info.rows[0][:2]
    payload: dict[str, Any] = {
        "status": "completed",
        "full_name": name,
        "type": table_type,
        "comment": comment,
        "columns": [
            {"name": row[2], "type": row[3], "comment": row[4]}
            for row in info.rows
            if row[2] is not None
        ],
    }
    if table_type == "METRIC_VIEW":
        payload["how_to_query"] = METRIC_VIEW_HINT
        return _dump(payload)
    sample_sql = f"SELECT * FROM {name} LIMIT {SAMPLE_ROWS}"
    if reason := check_sql(sample_sql, scope):
        return _rejected(reason)
    sample = await client.execute(sample_sql, max_rows=SAMPLE_ROWS, on_state=report)
    if sample.status != "completed":
        # information_schema can list tables the user cannot actually read.
        return _error(sample.status, sample.message)
    payload["sample_rows"] = sample.rows
    return _dump(payload)


async def run_query(
    client: StatementClient,
    query: str,
    *,
    scope: DataScope,
    max_rows_to_model: int,
    max_rows_to_ui: int,
    on_status: StatusCallback | None = None,
    on_result: ResultCallback | None = None,
) -> str:
    """Run a checked query. The model gets the first ``max_rows_to_model`` rows as JSON;
    ``on_result`` gets up to ``max_rows_to_ui`` for the UI, only when the query completed."""
    try:
        query = checked_sql(query, scope)
    except SqlRejected as rejected:
        return _rejected(str(rejected))
    # row_limit makes the warehouse flag `truncated` when more rows exist.
    result = await client.execute(
        query,
        max_rows=max(max_rows_to_model, max_rows_to_ui),
        on_state=_reporter(on_status, RUNNING),
    )
    if result.status != "completed":
        return _error(result.status, result.message)
    if on_result is not None:
        on_result(_rows_payload(query, result, max_rows_to_ui))
    return _dump({"status": "completed", **_rows_payload(query, result, max_rows_to_model)})


def _rows_payload(sql: str, result: StatementResult, max_rows: int) -> dict:
    rows = result.rows[:max_rows]
    return {
        "sql": sql,
        "columns": result.columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": result.truncated or len(rows) < len(result.rows),
    }


def choose(
    candidates: list[str], question: str, *, scope: DataScope, selection_active: bool
) -> tuple[str, dict | None]:
    """Validate candidate tables; return the model JSON and the choices for the UI (or None)."""
    if selection_active:
        return _rejected(SELECTION_ACTIVE), None
    names: list[str] = []
    for candidate in candidates:
        parts = table_parts(candidate)
        if parts is None:
            return _rejected(f"use full catalog.schema.table names, not {candidate}"), None
        name = format_name(*parts)
        if not scope.covers(*parts):
            return _rejected(OUTSIDE_SCOPE.format(name)), None
        if name not in names:
            names.append(name)
    if not MIN_CHOICES <= len(names) <= MAX_CHOICES:
        return _rejected(f"offer between {MIN_CHOICES} and {MAX_CHOICES} different tables"), None
    choices = {"question": _choice_question(question), "tables": names}
    return _dump({"status": "waiting_for_user", "message": WAITING_FOR_USER}), choices


def _choice_question(question: str) -> str:
    """The model's question, stripped and cut to ``MAX_CHOICE_QUESTION_CHARS`` (with "…")."""
    question = question.strip()
    if not question:
        return DEFAULT_CHOICE_QUESTION
    if len(question) <= MAX_CHOICE_QUESTION_CHARS:
        return question
    return question[: MAX_CHOICE_QUESTION_CHARS - 1].rstrip() + "…"


def default_statement_client_factory(settings: Settings) -> StatementClientFactory:
    """StatementClients for a WorkspaceClient on the given warehouse (the run workspace's),
    with the global timeouts and the caller's ``query_tags``."""

    def make(
        workspace_client: Any, warehouse_id: str, query_tags: Mapping[str, str] | None = None
    ) -> StatementClient:
        return StatementClient(
            workspace_client,
            warehouse_id,
            timeout_s=settings.databricks_sql_timeout_seconds,
            poll_interval_s=settings.databricks_sql_poll_interval_seconds,
            query_tags=query_tags,
        )

    return make


def build_sql_tools(
    settings: Settings, client_factory: StatementClientFactory | None = None
) -> list[BaseTool]:
    """``[search_tables, describe_table, run_sql, choose_tables]``, each running as the app
    with the caller in its ``query_tags``. Every answer given once the chat's workspace is
    known names it (``workspace``: its label)."""

    factory = client_factory or default_statement_client_factory(settings)

    async def resolved(
        tool_name: str, detail: str = "", *, needs_client: bool = True
    ) -> tuple[str, list[str], DataScope, StatementClient | None] | str:
        """This run's workspace label, raw selection, scope (the selection narrows the
        workspace's) and a client acting as the app, tagged with the caller; or the payload
        the tool returns instead (labelled once the workspace is known). Reads the selection
        once, so an ignored one is logged once per call. With a client, writes the call's
        audit line (``detail`` is what was asked)."""
        try:
            workspace, legacy_fallback = run_workspace()
        except WorkspaceUnavailable:
            logger.warning("databricks_workspace_unavailable")
            return workspace_unavailable_payload()
        run = await resolved_in(workspace, legacy_fallback, tool_name, detail, needs_client)
        if isinstance(run, str):
            return with_workspace(run, workspace.label)
        return (workspace.label, *run)

    async def resolved_in(
        workspace: Workspace,
        legacy_fallback: bool,
        tool_name: str,
        detail: str,
        needs_client: bool,
    ) -> tuple[list[str], DataScope, StatementClient | None] | str:
        """``resolved`` once the workspace is known: its result without the label."""
        try:
            selected, scope = run_scope(workspace, legacy_fallback)
        except SelectionInvalid as exc:
            return _rejected(SELECTION_REJECTED.format(exc))
        if not needs_client:
            return selected, scope, None
        try:
            caller = run_caller()
        except DatabricksAccessError:
            logger.warning("databricks_caller_unknown tool=%s", tool_name)
            return access_error_payload()
        try:
            workspace_client = await app_workspace_client(workspace.host)
        except Exception as exc:  # noqa: BLE001 - the credential, or the SDK's config resolution
            # Class only: the message can carry the host URL, request or credential detail.
            logger.warning(
                "databricks_client_unavailable tool=%s error=%s", tool_name, type(exc).__name__
            )
            return client_error_payload()
        audit_log(caller, workspace=workspace.alias, action=tool_name, detail=detail)
        tags = caller_tags(caller, workspace=workspace.alias, source="agent")
        return selected, scope, factory(workspace_client, workspace.sql_warehouse_id, tags)

    @tool("search_tables", description=SEARCH_DESCRIPTION)
    async def search_tables(keywords: str, limit: int = 20) -> str:
        if isinstance(run := await resolved("search_tables", keywords), str):
            return run
        label, _, scope, client = run
        content = await search(client, keywords, scope=scope, limit=limit, on_status=emit_status)
        return with_workspace(content, label)

    @tool("describe_table", description=DESCRIBE_DESCRIPTION)
    async def describe_table(full_name: str) -> str:
        if isinstance(run := await resolved("describe_table", full_name), str):
            return run
        label, _, scope, client = run
        content = await describe(client, full_name, scope=scope, on_status=emit_status)
        return with_workspace(content, label)

    @tool(RUN_SQL, description=RUN_SQL_DESCRIPTION)
    async def run_sql(query: str) -> str:
        if isinstance(run := await resolved(RUN_SQL, query), str):
            return run
        label, _, scope, client = run
        content = await run_query(
            client,
            query,
            scope=scope,
            max_rows_to_model=settings.databricks_sql_max_rows_to_model,
            max_rows_to_ui=settings.databricks_sql_max_rows_to_ui,
            on_status=emit_status,
            on_result=record_result,
        )
        return with_workspace(content, label)

    @tool(CHOOSE_TABLES, description=CHOOSE_DESCRIPTION)
    async def choose_tables(candidates: list[str], question: str = "") -> str:
        if isinstance(run := await resolved(CHOOSE_TABLES, needs_client=False), str):
            return run
        label, selected, scope, _ = run
        # An invalid selection was rejected above, so a non-empty one is in use.
        content, choices = choose(
            candidates, question, scope=scope, selection_active=bool(selected)
        )
        if choices is not None:
            record_choices(choices)
        return with_workspace(content, label)

    return [search_tables, describe_table, run_sql, choose_tables]
