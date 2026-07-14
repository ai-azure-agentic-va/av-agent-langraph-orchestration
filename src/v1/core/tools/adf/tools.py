"""Azure Data Factory tools for the ``adf-agent`` subagent.

Authentication uses the process-wide async ``DefaultAzureCredential``
(:func:`v1.utils.azure_credentials.get_async_azure_credential`), so the SAME
code works everywhere:

- **Locally** it picks up the developer's ``az login`` session.
- **Deployed in Azure** it uses the resource's **managed identity**.

No keys or secrets are stored — the identity just needs the *Data Factory
Reader* role (or higher) on each target factory.

Multi-factory: the reachable factories are configured through
``ADF_FACTORY_MAPPING`` (friendly alias → subscription / resource group /
factory name) with ``ADF_DEFAULT_FACTORY`` naming the alias used when the
caller does not pick one. Every tool takes an optional ``factory`` alias.

The tools:

1. ``list_factories``           — which factories the agent can query
2. ``list_pipelines``           — what pipelines exist in a factory
3. ``list_pipeline_runs``       — recent runs (optionally filtered by pipeline/status)
4. ``get_pipeline_run_details`` — one run's status + per-activity errors (flat)
5. ``get_pipeline_run_tree``    — a run and all child pipeline runs, recursively
6. ``get_pipeline_structure``   — a pipeline's activity tree and child references

All errors (auth, permission, unknown factory, ...) are returned as
``[adf-agent]``-prefixed text so the model can read them and react, matching
the ServiceNow tools' surface-errors-to-the-model convention.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from azure.mgmt.datafactory.aio import DataFactoryManagementClient
from azure.mgmt.datafactory.models import RunFilterParameters, RunQueryFilter
from langchain_core.tools import tool

from v1.core.config import get_settings
from v1.utils.azure_credentials import get_async_azure_credential

logger = logging.getLogger(__name__)
settings = get_settings()

SOURCE = "adf"

_MAX_MSG = 600  # truncate long ADF error blobs (some are full HTML pages)

_RUN_ID_HELP = "[adf-agent] Please provide a pipeline run_id (get one from list_pipeline_runs)."


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
                    credential=get_async_azure_credential(),
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
    lines = []
    for alias in _factory_aliases():
        entry = mapping[alias]
        marker = "  (default)" if alias == default else ""
        lines.append(f"  - {alias}: factory '{entry.get('factory_name', '?')}'{marker}")
    return f"[adf-agent] {len(mapping)} configured factory(ies):\n" + "\n".join(lines)


@tool
async def list_pipelines(factory: str = "") -> str:
    """List every pipeline defined in an Azure Data Factory.

    Args:
        factory: Optional factory alias (from list_factories). Leave empty to
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
    factory: str = "",
) -> str:
    """List recent pipeline runs in an Azure Data Factory.

    Args:
        pipeline_name: Optional exact pipeline name to filter by (e.g. "pl_orchestrator").
                       Leave empty to list runs across all pipelines.
        last_n_days:   How far back to look (default 7).
        status:        Optional status filter, one of "Succeeded", "Failed",
                       "InProgress", "Queued", "Cancelled". Leave empty for all.
        factory:       Optional factory alias (from list_factories). Leave empty
                       to use the default factory.

    Returns each run's runId, pipeline name, status, start time and duration.
    Use the returned runId with get_pipeline_run_tree (hierarchical pipelines)
    or get_pipeline_run_details (child-free pipelines) to see error logs.
    """
    try:
        alias, sub, rg, name = _resolve_factory(factory)
    except _FactoryError as exc:
        return str(exc)
    now = datetime.now(timezone.utc)
    filters = []
    if pipeline_name:
        filters.append(
            RunQueryFilter(operand="PipelineName", operator="Equals", values_property=[pipeline_name])
        )
    if status:
        filters.append(RunQueryFilter(operand="Status", operator="Equals", values_property=[status]))
    params = RunFilterParameters(
        last_updated_after=now - timedelta(days=max(1, last_n_days)),
        last_updated_before=now,
        filters=filters,
    )
    try:
        client = await _client(sub)
        resp = await client.pipeline_runs.query_by_factory(rg, name, filter_parameters=params)
    except Exception as exc:
        return f"[adf-agent] ERROR querying runs in factory '{alias}': {_truncate(exc)}"

    runs = list(resp.value or [])
    if not runs:
        scope = f" for pipeline '{pipeline_name}'" if pipeline_name else ""
        return (
            f"[adf-agent] No runs{scope} in factory '{alias}' in the last {last_n_days} day(s)."
        )

    lines = []
    for r in runs[:40]:  # cap output; user can narrow with filters
        lines.append(
            f"  - runId={r.run_id} | {r.pipeline_name} | {r.status} | "
            f"start={r.run_start} | {r.duration_in_ms or 0} ms"
        )
    header = f"[adf-agent] {len(runs)} run(s) in factory '{alias}'" + (
        f" for '{pipeline_name}'" if pipeline_name else ""
    )
    if len(runs) > 40:
        header += " (showing first 40 — filter by pipeline_name or status to narrow)"
    return header + ":\n" + "\n".join(lines)


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
        factory: Optional factory alias (from list_factories). Leave empty to
                 use the default factory. Must be the factory the run belongs to.

    Returns the overall run status plus, for each activity in the run, its name,
    type, status, any error message, and a short output preview — one level only.
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
        f"  pipeline : {run.pipeline_name}",
        f"  status   : {run.status}",
        f"  start    : {run.run_start}",
        f"  end      : {run.run_end}",
        f"  duration : {run.duration_in_ms or 0} ms",
    ]
    if run.message:
        out.append(f"  message  : {_clean_error(run.message)}")

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
        out.append(f"    • {a.activity_name} [{a.activity_type}] → {a.status}")
        error_line = _error_text(a.error)
        if error_line:
            out.append(f"        {error_line}")
        if a.status == "Succeeded" and a.output:
            out.append(f"        output: {_truncate(a.output)}")
    return "\n".join(out)


# Recursion guards for run trees: a ForEach over hundreds of pages could fan
# out into hundreds of child runs — walk failures fully, but bound the total.
_TREE_MAX_DEPTH = 5
_TREE_MAX_RUNS = 25


async def _walk_run_tree(
    client, rg: str, factory_name: str, run_id: str, depth: int, budget: dict
) -> list[str]:
    indent = "  " * depth
    if depth > _TREE_MAX_DEPTH:
        return [f"{indent}…[max depth {_TREE_MAX_DEPTH} reached]"]
    if budget["runs"] <= 0:
        return [f"{indent}…[run budget reached — narrow to a specific child run_id]"]
    budget["runs"] -= 1

    try:
        run = await client.pipeline_runs.get(rg, factory_name, run_id)
    except Exception as exc:
        return [f"{indent}✗ run {run_id}: ERROR fetching: {_truncate(exc)}"]

    lines = [f"{indent}{run.pipeline_name} (runId={run_id}) → {run.status}"]
    if run.message:
        lines.append(f"{indent}  message: {_clean_error(run.message)}")

    try:
        acts = await _activity_runs_for(client, rg, factory_name, run, run_id)
    except Exception as exc:
        lines.append(f"{indent}  activities: ERROR querying: {_truncate(exc)}")
        return lines

    # Show failures in full and recurse into their children; summarize the rest
    # so a wide ForEach doesn't drown the answer.
    succeeded_children = 0
    for a in acts:
        child_run_id = (a.output or {}).get("pipelineRunId") if isinstance(a.output, dict) else None
        is_execute = a.activity_type == "ExecutePipeline"
        if a.status == "Succeeded" and is_execute and child_run_id:
            succeeded_children += 1
            continue
        if a.status == "Succeeded" and not is_execute:
            continue

        lines.append(f"{indent}  • {a.activity_name} [{a.activity_type}] → {a.status}")
        error_line = _error_text(a.error)
        if error_line:
            lines.append(f"{indent}      {error_line}")
        if is_execute and child_run_id:
            lines.extend(
                await _walk_run_tree(client, rg, factory_name, child_run_id, depth + 2, budget)
            )

    total_failed = sum(1 for a in acts if a.status == "Failed")
    summary = f"{indent}  ({len(acts)} activities: {total_failed} failed"
    if succeeded_children:
        summary += f"; {succeeded_children} succeeded child pipeline run(s) not expanded"
    lines.append(summary + ")")
    return lines


@tool
async def get_pipeline_run_tree(run_id: str, factory: str = "") -> str:
    """Walk a pipeline run AND all its child pipeline runs, recursively.

    Args:
        run_id:  The pipeline run GUID of the top (parent) run.
        factory: Optional factory alias (from list_factories). Leave empty to
                 use the default factory. Must be the factory the run belongs to.

    This is THE tool for diagnosing hierarchical pipelines (parents that invoke
    children via Execute Pipeline activities, e.g. pl_orchestrator). It follows
    each Execute Pipeline activity into its child run — and grandchildren —
    returning the whole tree with statuses and error messages, so the root
    cause is visible however deep it is. Failed branches are expanded fully;
    succeeded child runs are counted but not expanded.
    """
    if not run_id or not run_id.strip():
        return _RUN_ID_HELP
    try:
        alias, sub, rg, name = _resolve_factory(factory)
    except _FactoryError as exc:
        return str(exc)
    client = await _client(sub)
    budget = {"runs": _TREE_MAX_RUNS}
    lines = await _walk_run_tree(client, rg, name, run_id.strip(), depth=0, budget=budget)
    return f"[adf-agent] Run tree (factory '{alias}'):\n" + "\n".join(lines)


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
        factory:       Optional factory alias (from list_factories). Leave empty
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
    list_factories,
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
    "list_factories",
    "list_pipeline_runs",
    "list_pipelines",
]
