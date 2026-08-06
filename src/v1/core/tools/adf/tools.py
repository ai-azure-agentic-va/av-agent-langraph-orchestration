"""Azure Data Factory tools for the ``adf-agent`` subagent.

Authentication uses the process-wide ``DefaultAzureCredential`` through
:class:`v1.utils.azure_credentials.ThreadOffloadAsyncCredential`, so the same
code runs locally off the developer's ``az login`` session and in Azure off the
resource's managed identity. No keys or secrets are stored — the identity needs
the *Data Factory Reader* role (or higher) on each target factory.

The target factory comes from ``ADF_FACTORY_MAPPING`` (friendly alias →
subscription / resource group / factory name). With a single entry it is used
automatically, so the optional ``factory`` alias each tool accepts stays empty in
normal single-factory use.

All errors (auth, permission, unknown factory, ...) are returned as
``[adf-agent]``-prefixed text so the model can read them and react, matching
the ServiceNow tools' surface-errors-to-the-model convention.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from azure.mgmt.datafactory.aio import DataFactoryManagementClient
from azure.mgmt.datafactory.models import (
    RunFilterParameters,
    RunQueryFilter,
    RunQueryOrderBy,
)
from langchain_core.tools import tool

from v1.core.config import get_settings
from v1.utils.azure_credentials import ThreadOffloadAsyncCredential

logger = logging.getLogger(__name__)
settings = get_settings()

_MAX_MSG = 600  # truncate long ADF error blobs (some are full HTML pages)

_RUN_ID_HELP = "[adf-agent] Please provide a pipeline run_id (get one from list_pipeline_runs)."

# Run-listing pagination bound: 20 pages × 100 runs keeps count questions exact
# up to 2,000 runs per window while capping worst-case latency.
_RUNS_MAX_PAGES = 20

# Rows rendered per run listing; totals are still reported for the whole window.
_MAX_RUN_ROWS = 40

# Statuses ADF reports on a pipeline run. The run-query filter is case-sensitive
# and matches nothing on an unknown value, so a caller's status is checked
# against this set instead of being passed through to a silent empty result.
_RUN_STATUSES = ("Queued", "InProgress", "Succeeded", "Failed", "Cancelling", "Cancelled")


def _truncate(text) -> str:
    """Collapse whitespace and cap length with a visible truncation marker."""
    text = " ".join(str(text).split())
    return text if len(text) <= _MAX_MSG else text[:_MAX_MSG] + " …[truncated]"


def _clean_error(text) -> str:
    """Azure error blobs often embed whole HTML pages — strip tags, then cap."""
    return _truncate(re.sub(r"<[^>]+>", " ", str(text)))


class _InputError(ValueError):
    """Raised for caller input a tool cannot use; the message is model-facing.

    Every tool turns this into its return value, so the model reads what was
    wrong (unknown factory, unusable date window, bad status) and can retry.
    """


def _factory_aliases() -> list[str]:
    return sorted(settings.adf_factory_mapping)


def _default_alias() -> str | None:
    mapping = settings.adf_factory_mapping
    if settings.adf_default_factory and settings.adf_default_factory in mapping:
        return settings.adf_default_factory
    if len(mapping) == 1:
        return next(iter(mapping))
    return None


def _resolve_factory(factory: str) -> tuple[str, str, str, str]:
    """Resolve an alias to ``(alias, subscription_id, resource_group, factory_name)``."""
    mapping = settings.adf_factory_mapping
    if not mapping:
        raise _InputError(
            "[adf-agent] No Data Factory is configured (ADF_FACTORY_MAPPING is empty)."
        )
    alias = (factory or "").strip()
    if not alias:
        alias = _default_alias() or ""
        if not alias:
            raise _InputError(
                "[adf-agent] Several factories are configured and no default is set — "
                "pass factory=<alias>. Available: " + ", ".join(_factory_aliases())
            )
    entry = mapping.get(alias)
    if entry is None:
        raise _InputError(
            f"[adf-agent] Unknown factory '{alias}'. Available: " + ", ".join(_factory_aliases())
        )
    missing = [
        key for key in ("subscription_id", "resource_group", "factory_name") if not entry.get(key)
    ]
    if missing:
        raise _InputError(
            f"[adf-agent] Factory '{alias}' is misconfigured — ADF_FACTORY_MAPPING entry "
            f"is missing: {', '.join(missing)}."
        )
    return alias, entry["subscription_id"], entry["resource_group"], entry["factory_name"]


# One ARM client per subscription (aliases can share a subscription); all share
# the process-wide async credential so token caches are reused.
_clients: dict[str, DataFactoryManagementClient] = {}
_clients_lock = asyncio.Lock()


async def _client(subscription_id: str) -> DataFactoryManagementClient:
    client = _clients.get(subscription_id)
    if client is None:
        async with _clients_lock:
            client = _clients.get(subscription_id)
            if client is None:
                logger.info(
                    "Creating DataFactoryManagementClient for subscription %s", subscription_id
                )
                client = DataFactoryManagementClient(
                    # The native async credential acquires tokens with blocking
                    # work on the event loop, which `langgraph dev`'s
                    # blocking-call detector rejects; this adapter offloads it.
                    credential=ThreadOffloadAsyncCredential(),
                    subscription_id=subscription_id,
                )
                _clients[subscription_id] = client
    return client


async def close_adf_resources() -> None:
    """Close every cached ADF management client (idempotent)."""
    global _clients
    clients, _clients = _clients, {}
    for subscription_id, client in clients.items():
        try:
            await client.close()
        except Exception:  # noqa: BLE001 - best-effort shutdown
            logger.warning(
                "Error closing DataFactoryManagementClient for subscription %s",
                subscription_id,
                exc_info=True,
            )


def _error_text(err) -> str | None:
    """Render an activity error dict as one line, or None if there is none."""
    if isinstance(err, dict) and err.get("message"):
        code = err.get("errorCode", "")
        return f"error{f' {code}' if code else ''}: {_clean_error(err['message'])}"
    return None


def _invoked_text(run) -> str:
    """Render what started a run, including the PARENT RUN ID when a pipeline did.

    ``invoked_by.pipeline_run_id`` is the only link from a child run back up to
    its parent — ADF has no "list my parents" API — so it must be surfaced for
    callers to navigate a hierarchy upward.
    """
    ib = getattr(run, "invoked_by", None)
    if not ib:
        return "?"
    text = f"{ib.name} ({ib.invoked_by_type})"
    if _is_child_run(run):
        text += f" parentRunId={ib.pipeline_run_id}"
    return text


def _is_child_run(run) -> bool:
    """True when this run was started by a parent pipeline's Execute Pipeline."""
    ib = getattr(run, "invoked_by", None)
    return bool(ib and ib.invoked_by_type == "PipelineActivity" and ib.pipeline_run_id)


def _child_run_id(activity) -> str | None:
    """The pipeline run an Execute Pipeline activity started, if it started one."""
    if activity.activity_type != "ExecutePipeline" or not isinstance(activity.output, dict):
        return None
    return activity.output.get("pipelineRunId")


def _parse_date(value: str, label: str) -> datetime:
    """Parse a caller-supplied date, defaulting a bare date to UTC midnight."""
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise _InputError(
            f"[adf-agent] Invalid {label} '{value}' — use YYYY-MM-DD, e.g. "
            "start_date='2026-07-10', end_date='2026-07-12'."
        ) from exc
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _inclusive_end(before: datetime) -> date:
    """The last day a window covers, given its exclusive end."""
    return (before - timedelta(seconds=1)).date()


def _resolve_window(start_date: str, end_date: str, last_n_days: int) -> tuple[datetime, datetime]:
    """The ``(after, before)`` UTC window a run query covers; ``before`` is exclusive.

    ``last_n_days`` is the span and the dates anchor it: with only ``end_date``
    the span is counted BACK from that date, because anchoring the start to
    "now" leaves an empty window whenever end_date is in the past.
    """
    before = datetime.now(timezone.utc)
    if end_date:
        before = _parse_date(end_date, "end_date")
        # A bare date means "through the end of that day".
        if len(end_date.strip()) == 10:
            before += timedelta(days=1)
    if start_date:
        after = _parse_date(start_date, "start_date")
    else:
        after = before - timedelta(days=max(1, last_n_days))
    if after >= before:
        # Phrased from the resolved window, since the end may come from end_date
        # or from "now" (a start_date in the future lands here too).
        raise _InputError(
            f"[adf-agent] Empty date window: it starts {after.date()} and ends "
            f"{_inclusive_end(before)}. Pass a start_date that is earlier than end_date "
            "and not in the future."
        )
    return after, before


def _window_text(after: datetime, before: datetime, dated: bool, last_n_days: int) -> str:
    """Describe the window a query covered, for a no-results message."""
    if not dated:
        return f"in the last {max(1, last_n_days)} day(s)"
    return f"between {after.date()} and {_inclusive_end(before)}"


def _normalize_status(status: str) -> str:
    """Canonical ADF run status for a caller's casing (empty means no filter)."""
    wanted = (status or "").strip()
    if not wanted:
        return ""
    for known in _RUN_STATUSES:
        if known.lower() == wanted.lower():
            return known
    raise _InputError(
        f"[adf-agent] Unknown status '{status}'. Use one of: " + ", ".join(_RUN_STATUSES) + "."
    )


def _run_filters(pipeline_name: str, status: str, trigger_name: str) -> list[RunQueryFilter]:
    """Build a run-query filter for each criterion the caller supplied."""
    criteria = (
        ("PipelineName", pipeline_name),
        ("Status", status),
        # ADF's operand for the trigger (or invoking entity) name on a run is
        # "TriggeredByName", not "TriggerName".
        ("TriggeredByName", trigger_name),
    )
    return [
        RunQueryFilter(operand=operand, operator="Equals", values_property=[value])
        for operand, value in criteria
        if value
    ]


async def _query_runs(client, rg: str, factory_name: str, params) -> tuple[list, bool]:
    """Every run matching ``params``, plus whether the window was fully paged.

    Paging through the window (bounded by ``_RUNS_MAX_PAGES``) is what makes
    reported counts totals for the window rather than one page's worth.
    """
    resp = await client.pipeline_runs.query_by_factory(rg, factory_name, filter_parameters=params)
    runs = list(resp.value or [])
    token = getattr(resp, "continuation_token", None)
    pages = 1
    while token and pages < _RUNS_MAX_PAGES:
        params.continuation_token = token
        resp = await client.pipeline_runs.query_by_factory(
            rg, factory_name, filter_parameters=params
        )
        runs.extend(resp.value or [])
        token = getattr(resp, "continuation_token", None)
        pages += 1
    return runs, token is None


def _render_runs(runs: list, alias: str, pipeline_name: str, complete: bool) -> str:
    """Render a run listing: header, per-pipeline totals when capped, then rows."""
    header = (
        f"[adf-agent] {len(runs)}{'' if complete else '+'} run(s) (newest first) in factory "
        f"'{alias}'" + (f" for '{pipeline_name}'" if pipeline_name else "")
    )
    if not complete:
        header += (
            f" (window has even more runs — counts are lower bounds after "
            f"{_RUNS_MAX_PAGES} pages; narrow the window or filters for exact totals)"
        )
    if len(runs) > _MAX_RUN_ROWS:
        header += "\n  totals by pipeline and status:"
        counts = Counter((r.pipeline_name, r.status) for r in runs)
        for (pipe, run_status), n in sorted(counts.items()):
            header += f"\n    - {pipe} | {run_status}: {n}"
        header += f"\n  showing the newest {_MAX_RUN_ROWS} runs:"
    rows = [
        f"  - runId={r.run_id} | {r.pipeline_name} | {r.status} | "
        f"start={r.run_start} | {r.duration_in_ms or 0} ms | triggeredBy={_invoked_text(r)}"
        for r in runs[:_MAX_RUN_ROWS]
    ]
    return header + "\n" + "\n".join(rows)


@tool
async def list_factories() -> str:
    """List the Azure Data Factories this agent can query, marking the default.

    Use this when the user asks which factories/environments are available, or
    when a factory alias is needed and the user has not named one. Takes no
    arguments. Pass a returned alias as the `factory` argument of other tools.
    """
    mapping = settings.adf_factory_mapping
    if not mapping:
        return "[adf-agent] No Data Factory is configured (ADF_FACTORY_MAPPING is empty)."
    default = _default_alias()
    lines = [
        f"  - {alias}: factory '{mapping[alias].get('factory_name', '?')}'"
        + ("  (default)" if alias == default else "")
        for alias in _factory_aliases()
    ]
    return f"[adf-agent] {len(mapping)} configured factory(ies):\n" + "\n".join(lines)


@tool
async def list_pipelines(factory: str = "") -> str:
    """List every pipeline defined in an Azure Data Factory.

    Args:
        factory: Optional factory alias. Leave empty to
                 use the default factory.

    Use this when the user asks what pipelines exist, or as a first step before
    looking at runs.
    """
    try:
        alias, sub, rg, name = _resolve_factory(factory)
    except _InputError as exc:
        return str(exc)
    try:
        client = await _client(sub)
        names = [p.name async for p in client.pipelines.list_by_factory(rg, name)]
    except Exception as exc:  # surface auth/permission errors to the model as text
        return f"[adf-agent] ERROR listing pipelines in factory '{alias}': {_truncate(exc)}"
    if not names:
        return f"[adf-agent] Factory '{alias}' has no pipelines."
    listing = "\n".join(f"  - {n}" for n in names)
    return f"[adf-agent] Factory '{alias}' has {len(names)} pipeline(s):\n{listing}"


@tool
async def list_pipeline_runs(
    pipeline_name: str = "",
    last_n_days: int = 7,
    status: str = "",
    trigger_name: str = "",
    start_date: str = "",
    end_date: str = "",
    factory: str = "",
) -> str:
    """List recent pipeline runs in an Azure Data Factory, newest first.

    Args:
        pipeline_name: Optional exact pipeline name to filter by (e.g. "pl_orchestrator").
                       Leave empty to list runs across all pipelines.
        last_n_days:   How far back to look (default 7, minimum 1 — ADF always
                       needs a bounded window, so 0 is treated as 1 day). When
                       end_date is given the span is counted back from it.
        status:        Optional status filter, one of "Queued", "InProgress",
                       "Succeeded", "Failed", "Cancelling", "Cancelled" (any
                       casing). Leave empty for all statuses.
        trigger_name:  Optional exact trigger name to filter by (e.g.
                       "tr_orchestrator_every_2_hours") — only runs started by
                       that trigger are returned. Leave empty for all runs.
        start_date:    Optional window start, "YYYY-MM-DD" (UTC, inclusive).
                       Use with end_date for questions about a specific date
                       range (e.g. "between Jul 10 and Jul 12").
        end_date:      Optional window end, "YYYY-MM-DD" (UTC, inclusive —
                       covers that whole day). On its own it means "the
                       last_n_days ending on that date".
        factory:       Optional factory alias. Leave empty
                       to use the default factory.

    Returns each run's runId, pipeline name, status, start time, duration and
    what triggered it (trigger or parent pipeline).
    Use the returned runId with get_pipeline_run_tree (hierarchical pipelines)
    or get_pipeline_run_details (child-free pipelines) to see error logs.
    """
    # Normalized once here, so the filters ADF receives and the names echoed
    # back to the model are the same text.
    pipeline_name = (pipeline_name or "").strip()
    trigger_name = (trigger_name or "").strip()
    try:
        alias, sub, rg, name = _resolve_factory(factory)
        after, before = _resolve_window(start_date, end_date, last_n_days)
        params = RunFilterParameters(
            last_updated_after=after,
            last_updated_before=before,
            filters=_run_filters(pipeline_name, _normalize_status(status), trigger_name),
            # ADF returns pages oldest-first by default, so without this the
            # display cap would show the OLDEST runs as "recent".
            order_by=[RunQueryOrderBy(order_by="RunStart", order="DESC")],
        )
    except _InputError as exc:
        return str(exc)

    try:
        client = await _client(sub)
        runs, complete = await _query_runs(client, rg, name, params)
    except Exception as exc:
        return f"[adf-agent] ERROR querying runs in factory '{alias}': {_truncate(exc)}"

    if not runs:
        scope = f" for pipeline '{pipeline_name}'" if pipeline_name else ""
        window = _window_text(after, before, bool(start_date or end_date), last_n_days)
        return f"[adf-agent] No runs{scope} in factory '{alias}' {window}."
    return _render_runs(runs, alias, pipeline_name, complete)


async def _activity_runs_for(client, rg: str, factory_name: str, run, run_id: str) -> list:
    """Query a run's activity runs using a window around the run itself."""
    now = datetime.now(timezone.utc)
    params = RunFilterParameters(
        last_updated_after=(run.run_start or now) - timedelta(hours=1),
        last_updated_before=now + timedelta(days=1),
        filters=[],
    )
    resp = await client.activity_runs.query_by_pipeline_run(
        rg, factory_name, run_id, filter_parameters=params
    )
    return list(resp.value or [])


@tool
async def get_pipeline_run_details(run_id: str, factory: str = "") -> str:
    """Fetch a single pipeline run's status and its per-activity logs/errors.

    Args:
        run_id:  The pipeline run GUID (from list_pipeline_runs).
        factory: Optional factory alias. Leave empty to
                 use the default factory. Must be the factory the run belongs to.

    Returns the overall run status, what triggered the run, plus for each
    activity in the run its name, type, status, activityRunId, any error
    message, and a short output preview — one level only. Use this to answer
    questions about a specific activity (by name or activityRunId) inside a run.
    For runs of hierarchical pipelines (with Execute Pipeline activities), prefer
    get_pipeline_run_tree, which follows the errors into the child runs.
    """
    if not run_id or not run_id.strip():
        return _RUN_ID_HELP
    run_id = run_id.strip()
    try:
        alias, sub, rg, name = _resolve_factory(factory)
    except _InputError as exc:
        return str(exc)

    try:
        client = await _client(sub)
        run = await client.pipeline_runs.get(rg, name, run_id)
    except Exception as exc:
        return f"[adf-agent] ERROR fetching run '{run_id}' in factory '{alias}': {_truncate(exc)}"

    out = [
        f"[adf-agent] Run {run_id} (factory '{alias}')",
        f"  pipeline    : {run.pipeline_name}",
        f"  status      : {run.status}",
        f"  triggeredBy : {_invoked_text(run)}",
        f"  start       : {run.run_start}",
        f"  end         : {run.run_end}",
        f"  duration    : {run.duration_in_ms or 0} ms",
    ]
    if run.parameters:
        out.append(f"  parameters  : {_truncate(dict(run.parameters))}")
    if run.message:
        out.append(f"  message     : {_clean_error(run.message)}")

    try:
        acts = await _activity_runs_for(client, rg, name, run, run_id)
    except Exception as exc:
        out.append(f"  activities: ERROR querying activity runs: {_truncate(exc)}")
        return "\n".join(out)

    if not acts:
        out.append("  activities: (none reported)")
        return "\n".join(out)

    out.append(f"  activities ({len(acts)}):")
    for a in acts:
        out.append(
            f"    • {a.activity_name} [{a.activity_type}] → {a.status} "
            f"(activityRunId={a.activity_run_id})"
        )
        error_line = _error_text(a.error)
        if error_line:
            out.append(f"        {error_line}")
        if a.status == "Succeeded" and a.output:
            out.append(f"        output: {_truncate(a.output)}")
    return "\n".join(out)


# Recursion guards for run trees: a ForEach over hundreds of pages could fan
# out into hundreds of child runs — walk failures fully, but bound the total.
# Depth is counted in PIPELINE levels; 8 clears the deepest real hierarchy seen
# (5) with headroom. The run budget, not depth, is what bounds a wide ForEach.
_TREE_MAX_DEPTH = 8
_TREE_MAX_RUNS = 25


async def _climb_to_root(client, rg: str, factory_name: str, run_id: str):
    """Follow ``invoked_by.pipeline_run_id`` up to the run a trigger started.

    A failed run found via ``list_pipeline_runs`` is usually a CHILD, so the
    family can only be built after climbing to its root first.
    """
    run = await client.pipeline_runs.get(rg, factory_name, run_id)
    visited = {run_id}
    while _is_child_run(run):
        parent_id = run.invoked_by.pipeline_run_id
        if parent_id in visited:  # defensive: ADF should never cycle
            break
        visited.add(parent_id)
        run = await client.pipeline_runs.get(rg, factory_name, parent_id)
    return run


async def _walk_run_tree(
    client, rg: str, factory_name: str, run_id: str, depth: int, budget: dict, stats: Counter
) -> list[str]:
    # depth counts PIPELINE levels, not indent steps, so _TREE_MAX_DEPTH means
    # what it says.
    indent = "    " * depth
    if depth >= _TREE_MAX_DEPTH:
        return [f"{indent}…[max depth {_TREE_MAX_DEPTH} reached]"]
    if budget["runs"] <= 0:
        budget["truncated"] = True
        return [f"{indent}…[run budget reached — narrow to a specific child run_id]"]
    budget["runs"] -= 1

    try:
        run = await client.pipeline_runs.get(rg, factory_name, run_id)
    except Exception as exc:
        return [f"{indent}✗ run {run_id}: ERROR fetching: {_truncate(exc)}"]

    stats[run.status] += 1
    lines = [f"{indent}{run.pipeline_name} (runId={run_id}) → {run.status}"]

    try:
        acts = await _activity_runs_for(client, rg, factory_name, run, run_id)
    except Exception as exc:
        lines.append(f"{indent}  activities: ERROR querying: {_truncate(exc)}")
        return lines

    # ADF re-wraps a child's error into the parent's message and into the
    # parent's Execute Pipeline activity error, so printing every level repeats
    # one blob and buries the root cause. Print an error only where it
    # ORIGINATED, never on a hop that merely relays a failed child's error up.
    relays = {a.activity_name for a in acts if a.status == "Failed" and _child_run_id(a)}
    if run.message and not relays:
        lines.append(f"{indent}  message: {_clean_error(run.message)}")

    # Every child run is expanded, succeeded ones included: a family's
    # "N failed / M succeeded" count is only true if those branches were
    # actually visited. Activity detail stays failures-only so a wide ForEach
    # does not drown the answer.
    for a in acts:
        if a.status != "Succeeded":
            relayed = a.activity_name in relays
            suffix = " (failed because its child run below failed)" if relayed else ""
            lines.append(
                f"{indent}  • {a.activity_name} [{a.activity_type}] → {a.status}{suffix}"
            )
            error_line = None if relayed else _error_text(a.error)
            if error_line:
                lines.append(f"{indent}      {error_line}")
        child_run_id = _child_run_id(a)
        if child_run_id:
            lines.extend(
                await _walk_run_tree(
                    client, rg, factory_name, child_run_id, depth + 1, budget, stats
                )
            )

    total_failed = sum(1 for a in acts if a.status == "Failed")
    lines.append(f"{indent}  ({len(acts)} activities: {total_failed} failed)")
    return lines


@tool
async def get_pipeline_run_tree(run_id: str, factory: str = "") -> str:
    """Show the WHOLE pipeline family a run belongs to, from ANY run in it.

    Args:
        run_id:  Any pipeline run GUID in the family — parent, child or deep
                 grandchild. It does NOT have to be the top-level run.
        factory: Optional factory alias. Leave empty to
                 use the default factory. Must be the factory the run belongs to.

    This is THE tool for diagnosing hierarchical pipelines (parents that invoke
    children via Execute Pipeline activities, e.g. pl_orchestrator). It first
    climbs UP via each run's parent run id to the run a trigger started, then
    walks the whole tree back down, so passing a failed child still returns the
    entire family — every member's pipeline name, run id and status, plus the
    error messages on failed activities, and a count of how many runs in the
    family failed vs succeeded.

    Note that a child's failure normally fails its parent too, so several failed
    runs in one family are usually ONE root cause echoing upward: the real error
    is the deepest failed activity. Sibling branches can still succeed, so
    report the counts rather than assuming the whole family failed.
    """
    if not run_id or not run_id.strip():
        return _RUN_ID_HELP
    try:
        alias, sub, rg, name = _resolve_factory(factory)
    except _InputError as exc:
        return str(exc)
    requested = run_id.strip()
    client = await _client(sub)

    try:
        root = await _climb_to_root(client, rg, name, requested)
    except Exception as exc:
        return (
            f"[adf-agent] ERROR fetching run '{requested}' in factory "
            f"'{alias}': {_truncate(exc)}"
        )

    budget = {"runs": _TREE_MAX_RUNS, "truncated": False}
    stats: Counter = Counter()
    lines = await _walk_run_tree(client, rg, name, root.run_id, depth=0, budget=budget, stats=stats)

    header = f"[adf-agent] Pipeline family (factory '{alias}')"
    if root.run_id != requested:
        header += (
            f"\n  run {requested} is a CHILD; its family root is "
            f"{root.pipeline_name} (runId={root.run_id}), started by {_invoked_text(root)}"
        )
    total = sum(stats.values())
    counts = ", ".join(f"{n} {status}" for status, n in sorted(stats.items()))
    summary = f"\n  family: {total} pipeline run(s) — {counts or 'none'}"
    if budget["truncated"]:
        summary += " (partial — run budget reached, counts are lower bounds)"
    return header + ":\n" + "\n".join(lines) + summary


def _walk_definition(activities: list, depth: int) -> list[str]:
    # SDK Activity models are mapping-like (as are the raw dicts nested inside
    # ForEach/If containers), so dict-style access covers both.
    lines = []
    for a in activities or []:
        name, a_type = a.get("name", "?"), a.get("type", "?")
        props = a.get("typeProperties") or {}
        pipeline_ref = props.get("pipeline")
        ref = ""
        if isinstance(pipeline_ref, dict) and pipeline_ref.get("referenceName"):
            ref = f" → invokes {pipeline_ref['referenceName']}"
        lines.append(f"{'  ' * depth}- {name} [{a_type}]{ref}")
        for key in ("activities", "ifTrueActivities", "ifFalseActivities"):
            if props.get(key):
                lines.extend(_walk_definition(props[key], depth + 1))
    return lines


@tool
async def get_pipeline_structure(pipeline_name: str, factory: str = "") -> str:
    """Show a pipeline's definition as an activity tree, including which child
    pipelines it invokes (its hierarchy).

    Args:
        pipeline_name: Exact pipeline name, e.g. "pl_orchestrator".
        factory:       Optional factory alias. Leave empty
                       to use the default factory.

    Use this to explain what a pipeline does or whether it is hierarchical —
    Execute Pipeline activities are shown with the child pipeline they invoke,
    and activities nested inside ForEach/If containers are indented under them.
    """
    pipeline_name = (pipeline_name or "").strip()
    if not pipeline_name:
        return "[adf-agent] Please provide a pipeline name (get one from list_pipelines)."
    try:
        alias, sub, rg, name = _resolve_factory(factory)
    except _InputError as exc:
        return str(exc)
    try:
        client = await _client(sub)
        pipeline = await client.pipelines.get(rg, name, pipeline_name)
    except Exception as exc:
        return (
            f"[adf-agent] ERROR fetching pipeline '{pipeline_name}' in factory "
            f"'{alias}': {_truncate(exc)}"
        )
    lines = _walk_definition(pipeline.activities or [], depth=1)
    if not lines:
        return f"[adf-agent] Pipeline '{pipeline_name}' (factory '{alias}') has no activities."
    return f"[adf-agent] Structure of '{pipeline_name}' (factory '{alias}'):\n" + "\n".join(lines)


# The adf-agent's tool set (see v1.core.subagents.adf).
ADF_TOOLS = [
    list_factories,
    list_pipelines,
    list_pipeline_runs,
    get_pipeline_run_details,
    get_pipeline_run_tree,
    get_pipeline_structure,
]


__all__ = ["ADF_TOOLS", "close_adf_resources"]
