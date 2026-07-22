"""Azure Data Factory tools for the ``adf-agent`` subagent.

Authentication uses the process-wide ``DefaultAzureCredential`` via a
thread-offloaded async adapter
(:class:`v1.utils.azure_credentials.ThreadOffloadAsyncCredential`), so the SAME
code works everywhere:

- **Locally** it picks up the developer's ``az login`` session.
- **Deployed in Azure** it uses the resource's **managed identity**.

No keys or secrets are stored — the identity just needs the *Data Factory
Reader* role (or higher) on each target factory.

Factory config: the target factory is configured through ``ADF_FACTORY_MAPPING``
(friendly alias → subscription / resource group / factory name); with a single
entry it is used automatically, so callers never pass ``factory``. The optional
``factory`` alias on each tool remains for a possible future multi-factory
deployment but is left empty in normal single-factory use.

The tools:

1. ``list_pipelines``           — what pipelines exist in the factory
2. ``list_pipeline_runs``       — recent runs (optionally filtered by pipeline/status)
3. ``get_pipeline_run_details`` — one run's status + per-activity errors (flat)
4. ``get_pipeline_run_tree``    — the whole pipeline family a run belongs to,
   reached from ANY run in it (climbs to the root, then walks back down)
5. ``get_pipeline_structure``   — a pipeline's activity tree and child references

All errors (auth, permission, unknown factory, ...) are returned as
``[adf-agent]``-prefixed text so the model can read them and react, matching
the ServiceNow tools' surface-errors-to-the-model convention.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

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

SOURCE = "adf"

_MAX_MSG = 600  # truncate long ADF error blobs (some are full HTML pages)

_RUN_ID_HELP = "[adf-agent] Please provide a pipeline run_id (get one from list_pipeline_runs)."

# Run-listing pagination bound: 20 pages × 100 runs keeps count questions exact
# up to 2,000 runs per window while capping worst-case latency.
_RUNS_MAX_PAGES = 20


def _truncate(text) -> str:
    """Collapse whitespace and cap length with a visible truncation marker."""
    text = " ".join(str(text).split())
    return text if len(text) <= _MAX_MSG else text[:_MAX_MSG] + " …[truncated]"


def _clean_error(text) -> str:
    """Azure error blobs often embed whole HTML pages — strip tags, then cap."""
    return _truncate(re.sub(r"<[^>]+>", " ", str(text)))


class _FactoryError(ValueError):
    """Raised when a factory alias cannot be resolved; message is model-facing."""


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
        raise _FactoryError(
            "[adf-agent] No Data Factory is configured (ADF_FACTORY_MAPPING is empty)."
        )
    alias = (factory or "").strip()
    if not alias:
        alias = _default_alias() or ""
        if not alias:
            raise _FactoryError(
                "[adf-agent] Several factories are configured and no default is set — "
                "pass factory=<alias>. Available: " + ", ".join(_factory_aliases())
            )
    entry = mapping.get(alias)
    if entry is None:
        raise _FactoryError(
            f"[adf-agent] Unknown factory '{alias}'. Available: " + ", ".join(_factory_aliases())
        )
    missing = [
        key for key in ("subscription_id", "resource_group", "factory_name") if not entry.get(key)
    ]
    if missing:
        raise _FactoryError(
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
                    # Thread-offloaded adapter over the sync credential: the
                    # native async credential does blocking work on the event
                    # loop during acquisition, which `langgraph dev`'s
                    # blocking-call detector rejects (breaking ADF auth in
                    # local dev). See ThreadOffloadAsyncCredential.
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
    except _FactoryError as exc:
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
        last_n_days:   How far back to look (default 7). Ignored when
                       start_date/end_date are given.
        status:        Optional status filter, one of "Succeeded", "Failed",
                       "InProgress", "Queued", "Cancelled". Leave empty for all.
        trigger_name:  Optional exact trigger name to filter by (e.g.
                       "tr_orchestrator_every_2_hours") — only runs started by
                       that trigger are returned. Leave empty for all runs.
        start_date:    Optional window start, "YYYY-MM-DD" (UTC, inclusive).
                       Use with end_date for questions about a specific date
                       range (e.g. "between Jul 10 and Jul 12").
        end_date:      Optional window end, "YYYY-MM-DD" (UTC, inclusive —
                       covers that whole day).
        factory:       Optional factory alias. Leave empty
                       to use the default factory.

    Returns each run's runId, pipeline name, status, start time, duration and
    what triggered it (trigger or parent pipeline).
    Use the returned runId with get_pipeline_run_tree (hierarchical pipelines)
    or get_pipeline_run_details (child-free pipelines) to see error logs.
    """
    try:
        alias, sub, rg, name = _resolve_factory(factory)
    except _FactoryError as exc:
        return str(exc)
    now = datetime.now(timezone.utc)
    window_after = now - timedelta(days=max(1, last_n_days))
    window_before = now
    if start_date or end_date:
        try:
            if start_date:
                window_after = datetime.fromisoformat(start_date.strip())
                if window_after.tzinfo is None:
                    window_after = window_after.replace(tzinfo=timezone.utc)
            if end_date:
                window_before = datetime.fromisoformat(end_date.strip())
                if window_before.tzinfo is None:
                    window_before = window_before.replace(tzinfo=timezone.utc)
                # A bare date means "through the end of that day".
                if len(end_date.strip()) == 10:
                    window_before += timedelta(days=1)
        except ValueError:
            return (
                "[adf-agent] Invalid start_date/end_date — use YYYY-MM-DD, e.g. "
                "start_date='2026-07-10', end_date='2026-07-12'."
            )
    filters = []
    if pipeline_name:
        filters.append(
            RunQueryFilter(operand="PipelineName", operator="Equals", values_property=[pipeline_name])
        )
    if status:
        filters.append(RunQueryFilter(operand="Status", operator="Equals", values_property=[status]))
    if trigger_name:
        # ADF's run-query operand is "TriggeredByName" (not "TriggerName");
        # it matches the trigger or invoking entity name on each run.
        filters.append(
            RunQueryFilter(
                operand="TriggeredByName", operator="Equals", values_property=[trigger_name]
            )
        )
    params = RunFilterParameters(
        last_updated_after=window_after,
        last_updated_before=window_before,
        filters=filters,
        # ADF returns pages oldest-first by default, so an unordered query plus
        # our display cap silently showed the OLDEST runs as "recent".
        order_by=[RunQueryOrderBy(order_by="RunStart", order="DESC")],
    )
    try:
        client = await _client(sub)
        resp = await client.pipeline_runs.query_by_factory(rg, name, filter_parameters=params)
        runs = list(resp.value or [])
        # Page through the full window (bounded) so counts are totals, not the
        # first-page slice — count questions were silently under-reporting.
        token = getattr(resp, "continuation_token", None)
        pages = 1
        while token and pages < _RUNS_MAX_PAGES:
            params.continuation_token = token
            resp = await client.pipeline_runs.query_by_factory(
                rg, name, filter_parameters=params
            )
            runs.extend(resp.value or [])
            token = getattr(resp, "continuation_token", None)
            pages += 1
    except Exception as exc:
        return f"[adf-agent] ERROR querying runs in factory '{alias}': {_truncate(exc)}"
    if not runs:
        scope = f" for pipeline '{pipeline_name}'" if pipeline_name else ""
        window = (
            f"between {window_after.date()} and {window_before.date()}"
            if (start_date or end_date)
            else f"in the last {last_n_days} day(s)"
        )
        return f"[adf-agent] No runs{scope} in factory '{alias}' {window}."

    lines = []
    for r in runs[:40]:  # cap output; user can narrow with filters
        invoked = _invoked_text(r)
        lines.append(
            f"  - runId={r.run_id} | {r.pipeline_name} | {r.status} | "
            f"start={r.run_start} | {r.duration_in_ms or 0} ms | triggeredBy={invoked}"
        )
    exact = token is None
    header = (
        f"[adf-agent] {len(runs)}{'' if exact else '+'} run(s) (newest first) in factory "
        f"'{alias}'" + (f" for '{pipeline_name}'" if pipeline_name else "")
    )
    if not exact:
        header += f" (window has even more runs — counts are lower bounds after {_RUNS_MAX_PAGES} pages; narrow the window or filters for exact totals)"
    if len(runs) > 40:
        counts = Counter((r.pipeline_name, r.status) for r in runs)
        header += "\n  totals by pipeline and status:"
        for (pipe, run_status), n in sorted(counts.items()):
            header += f"\n    - {pipe} | {run_status}: {n}"
        header += "\n  showing the newest 40 runs:"
    return header + "\n" + "\n".join(lines)


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
    except _FactoryError as exc:
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
    # depth counts PIPELINE levels (not indent steps) so _TREE_MAX_DEPTH means
    # what it says — indenting by 2 per level here silently capped it at ~2.
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

    # ADF re-wraps a child's error into the parent's message and into the parent's
    # Execute Pipeline activity error, so a 5-level failure prints the same blob 5
    # times and buries the root cause. Print an error only where it ORIGINATED —
    # i.e. not on the hop that merely relays a failed child's error upward.
    relays = {
        a.activity_name
        for a in acts
        if a.activity_type == "ExecutePipeline"
        and a.status == "Failed"
        and isinstance(a.output, dict)
        and a.output.get("pipelineRunId")
    }
    if run.message and not relays:
        lines.append(f"{indent}  message: {_clean_error(run.message)}")

    # Every child run is expanded — a family's "N failed / M succeeded" count is
    # only true if the succeeded branches were actually visited. Activity detail
    # is still failures-only, so a wide ForEach doesn't drown the answer.
    for a in acts:
        child_run_id = (a.output or {}).get("pipelineRunId") if isinstance(a.output, dict) else None
        is_execute = a.activity_type == "ExecutePipeline"
        if a.status != "Succeeded":
            suffix = " (failed because its child run below failed)" if a.activity_name in relays else ""
            lines.append(
                f"{indent}  • {a.activity_name} [{a.activity_type}] → {a.status}{suffix}"
            )
            error_line = None if a.activity_name in relays else _error_text(a.error)
            if error_line:
                lines.append(f"{indent}      {error_line}")
        if is_execute and child_run_id:
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
    except _FactoryError as exc:
        return str(exc)
    requested = run_id.strip()
    client = await _client(sub)

    try:
        root = await _climb_to_root(client, rg, name, requested)
    except Exception as exc:
        return f"[adf-agent] ERROR fetching run '{requested}' in factory '{alias}': {_truncate(exc)}"

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
    if not pipeline_name or not pipeline_name.strip():
        return "[adf-agent] Please provide a pipeline name (get one from list_pipelines)."
    try:
        alias, sub, rg, name = _resolve_factory(factory)
    except _FactoryError as exc:
        return str(exc)
    try:
        client = await _client(sub)
        pipeline = await client.pipelines.get(rg, name, pipeline_name.strip())
    except Exception as exc:
        return (
            f"[adf-agent] ERROR fetching pipeline '{pipeline_name}' in factory "
            f"'{alias}': {_truncate(exc)}"
        )
    lines = _walk_definition(pipeline.activities or [], depth=1)
    if not lines:
        return f"[adf-agent] Pipeline '{pipeline_name}' (factory '{alias}') has no activities."
    return f"[adf-agent] Structure of '{pipeline_name}' (factory '{alias}'):\n" + "\n".join(lines)


ADF_TOOLS = [
    list_pipelines,
    list_pipeline_runs,
    get_pipeline_run_details,
    get_pipeline_run_tree,
    get_pipeline_structure,
]


__all__ = [
    "ADF_TOOLS",
    "close_adf_resources",
    "get_pipeline_run_details",
    "get_pipeline_run_tree",
    "get_pipeline_structure",
    "list_pipeline_runs",
    "list_pipelines",
]
