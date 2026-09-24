"""Azure Data Factory tools for the ``adf-agent`` subagent.

Authentication uses the process-wide ``DefaultAzureCredential`` through
:class:`v1.utils.azure_credentials.ThreadOffloadAsyncCredential`, so the same
code runs locally off the developer's ``az login`` session and in Azure off the
resource's managed identity. No keys or secrets are stored. Read-only tools
require a role that can read the target factory; mutation tools additionally
require write permission and are
disabled unless both ``ADF_WRITE_ENABLED`` and an explicit factory allowlist permit them.

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
from urllib.parse import quote

from azure.core.exceptions import ResourceNotFoundError
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

# Analytics/recovery page the WHOLE window (their counts/averages must be exact,
# not one page's worth), so they use a much higher cap. This is only a runaway-
# loop guard: 200 pages × 100 runs ≈ 20k runs, well past any single pipeline's
# history in ADF's ~45-day retention. When it IS hit the "complete" flag is
# False and the caller must label the result incomplete rather than exact.
_ANALYTICS_MAX_PAGES = 200

# ADF retains pipeline-run history for roughly this many days. Analysis requests
# longer than this cannot be fully answered from ADF alone; the tools say so
# rather than implying the whole period was covered.
_ADF_HISTORY_DAYS = 45

# Rows rendered per run listing; totals are still reported for the whole window.
_MAX_RUN_ROWS = 40

# Rows rendered per source-system discovery group. Lower than _MAX_RUN_ROWS
# because discovery SWEEPS every configured factory by default, so its row count
# is the sum across the estate rather than one factory's newest runs — and each
# row now carries a ~250-character run link. Without a cap a broad needle across
# three factories renders tens of kilobytes of URL into a tool result the
# subagent must reproduce into an answer capped at AI_LLM_DEFAULT_MAX_TOKENS,
# and the orchestrator must reproduce again. The candidate COUNT in the header is
# computed before this cap and stays exact.
_DISCOVERY_MAX_ROWS = 25

# What "active" means for a pipeline, everywhere: it has at least one run in the
# trailing window. ONE definition on purpose — inventory and source-system
# discovery disagreeing about which pipelines count would put two different
# answers to "what pipelines are there" in front of the same user.
_ACTIVE_RUN_DAYS = 30
_INVENTORY_ACTIVE_DAYS = _ACTIVE_RUN_DAYS

# Hidden pipelines are named while the list stays readable, counted beyond that.
_INACTIVE_NAMES_MAX = 15

# Statuses ADF reports on a pipeline run. The run-query filter is case-sensitive
# and matches nothing on an unknown value, so a caller's status is checked
# against this set instead of being passed through to a silent empty result.
# ADF spells the in-progress cancel state "Canceling" (one 'l'); the terminal
# state is "Cancelled" (two 'l's).
_RUN_STATUSES = ("Queued", "InProgress", "Succeeded", "Failed", "Canceling", "Cancelled")

# Terminal states carry a final runtime; active ones do not yet. "Completed"
# below means terminal (Succeeded/Failed/Cancelled) — deliberately NOT
# "successful", per the runtime use cases.
_TERMINAL_STATUSES = frozenset({"Succeeded", "Failed", "Cancelled"})
_ACTIVE_STATUSES = frozenset({"Queued", "InProgress", "Canceling"})


# ARM error blobs embed full resource paths; the subscription GUID in them is
# not something a chat answer should relay verbatim.
_SUB_ID_RE = re.compile(r"(/subscriptions/)[0-9a-fA-F-]{36}", re.IGNORECASE)


def _truncate(text) -> str:
    """Collapse whitespace, redact subscription ids, and cap length."""
    text = _SUB_ID_RE.sub(r"\1<redacted>", " ".join(str(text).split()))
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


def _factory_label(alias: str) -> str:
    """Render a factory by its ADF resource name — the name the Azure UI shows.

    The alias is only THIS deployment's shorthand for a config key; it is not
    what the factory is called anywhere the user can look. Aliases are routinely
    an abbreviation of the resource name, so a portal search for one finds
    nothing — and an answer built on the alias cannot be verified. Naming BOTH
    halves fixed the verifiability but made every sentence carry a name that
    does not exist in Azure, which read as two different factories. Model-facing
    text therefore names the resource alone.

    The alias keeps working as the ``factory`` ARGUMENT (see _resolve_factory),
    and operator-facing messages about ADF_FACTORY_MAPPING still name it,
    because there it identifies the config key that needs fixing. Falls back to
    the alias when no resource name is configured, so a half-filled mapping
    still renders something a human can act on.
    """
    return f"'{_factory_name(alias)}'"


def _factory_name(alias: str) -> str:
    """The bare ADF resource name — for table cells, where quotes are noise."""
    name = str((settings.adf_factory_mapping.get(alias) or {}).get("factory_name", "")).strip()
    return name or alias


def _alias_for_resource(mapping: dict, wanted: str) -> str | None:
    """The alias owning the ADF resource named ``wanted``, if any.

    Tool output names a factory by its RESOURCE name, so that is what comes back
    as the ``factory`` argument — from the user reading it, or from the model
    echoing it. ARM resource names are case-insensitive, so compare that way.
    """
    wanted = wanted.casefold()
    for alias, entry in mapping.items():
        if str((entry or {}).get("factory_name", "")).strip().casefold() == wanted:
            return alias
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
            # Named by resource, like every other list: this one TELLS the
            # caller to type a value back, so an alias here would re-teach the
            # vocabulary the rest of the output no longer uses.
            raise _InputError(
                "[adf-agent] Several factories are configured and no default is set — "
                "pass factory=<name>. Available: "
                + ", ".join(_factory_label(a) for a in _factory_aliases())
            )
    entry = mapping.get(alias)
    if entry is None:
        # Tool output names factories by their ADF RESOURCE name, so that is what
        # comes back here — quoting the tool's own words must not be an error,
        # and a user who only knows the Azure name has nothing else to type.
        resolved = _alias_for_resource(mapping, alias)
        if resolved is not None:
            alias, entry = resolved, mapping[resolved]
    if entry is None:
        raise _InputError(
            f"[adf-agent] Unknown factory '{alias}'. Available: "
            + ", ".join(_factory_label(a) for a in _factory_aliases())
        )
    missing = [
        key for key in ("subscription_id", "resource_group", "factory_name") if not entry.get(key)
    ]
    if missing:
        # Deliberately the ALIAS and not the resource name: this is the one
        # message aimed at whoever edits the config, and the alias IS the broken
        # key. Naming the resource here would point at a value that may be the
        # very field that is missing.
        raise _InputError(
            f"[adf-agent] Factory '{alias}' is misconfigured — ADF_FACTORY_MAPPING entry "
            f"is missing: {', '.join(missing)}."
        )
    return alias, entry["subscription_id"], entry["resource_group"], entry["factory_name"]


def _resolve_factory_targets(
    factory: str = "", *, all_when_empty: bool = False
) -> list[tuple[str, str, str, str]]:
    """Resolve one factory, or all configured factories for cross-factory discovery.

    Most tools intentionally keep the configured default behavior. Discovery is
    different: the acceptance criteria require searching every configured ADF
    instance unless the caller explicitly narrows the request to one alias.
    """
    explicit = (factory or "").strip()
    if explicit or not all_when_empty:
        return [_resolve_factory(explicit)]
    mapping = settings.adf_factory_mapping
    if not mapping:
        raise _InputError(
            "[adf-agent] No Data Factory is configured (ADF_FACTORY_MAPPING is empty)."
        )
    return [_resolve_factory(alias) for alias in _factory_aliases()]


# ADF Studio's own monitoring UI. "en" is the locale segment Studio itself
# emits; it redirects to the browser's locale, so it is safe to hard-code.
_ADF_STUDIO = "https://adf.azure.com/en/monitoring"

# What Studio shows when it opens cold. Repeated in model-facing text because it
# is the single most common reason a tester reports "the run isn't there".
_MONITOR_WINDOW_HINT = (
    "ADF Studio's Monitor tab opens on the last 24 hours — widen the time range "
    "to see older runs"
)


def _factory_arm_path(sub: str, rg: str, factory_name: str) -> str:
    """The factory's ARM resource id — what ADF Studio needs to open a factory."""
    return (
        f"/subscriptions/{sub}/resourceGroups/{rg}"
        f"/providers/Microsoft.DataFactory/factories/{factory_name}"
    )


def _run_url(sub: str, rg: str, factory_name: str, run_id: str) -> str:
    """A deep link that opens ONE pipeline run in ADF Studio's Monitor tab.

    Without it a run id is, in practice, unverifiable. Monitor has no find-by-
    run-id box and opens on the last 24 hours, so any older run renders as an
    empty grid until the tester guesses the right range — which is exactly the
    "I can't see the runs in ADF to validate the run id" report. This link opens
    that one run whatever the filter says.

    It embeds the factory's ARM path because Studio has no other way to know
    which factory to open. That is a deliberate exception to the routing rule
    against printing ARM paths: here the path IS the link, and it names only the
    factory the user already asked about.
    """
    arm = _factory_arm_path(sub, rg, factory_name)
    return f"{_ADF_STUDIO}/pipelineruns/{quote(str(run_id), safe='')}?factory={arm}"


def _factory_monitor_url(sub: str, rg: str, factory_name: str, pipeline_name: str = "") -> str:
    """A deep link to a factory's Monitor tab — the run list, not a single run.

    ``pipeline_name`` pre-fills Monitor's own pipeline filter. Without it a line
    that names ONE pipeline opens a grid reading "Pipeline name: All", so the
    reader has to re-find the pipeline by hand and routinely concludes the link
    is broken. The parameter is undocumented and Studio ignores query keys it
    does not know, so the worst case is exactly the unfiltered tab this link
    opened before. There is no equivalent for the time range, which is why the
    window caveat has to travel with every one of these links.
    """
    url = f"{_ADF_STUDIO}/pipelineruns?factory={_factory_arm_path(sub, rg, factory_name)}"
    if pipeline_name:
        url += f"&filter.pipelinename={quote(pipeline_name, safe='')}"
    return url


# --- how a link is rendered, everywhere ---------------------------------------
# ONE shape per kind of link, so the model has one thing to pass through and a
# reader one thing to recognise. Two rules govern every site below:
#
#   * a run id is only checkable if the line that PRINTS it also links it, built
#     from the factory THAT run lives in. A link to the wrong factory opens a
#     real Studio page showing nothing, which reads as "the run does not exist" —
#     worse than no link at all. So a run id goes unlinked only when the factory
#     is genuinely unknown (a lookup that failed), or when the same run is
#     already linked a line or two away in the same block;
#   * the factory's Monitor tab is NOT a substitute for a run link (it opens on
#     the last 24 hours and cannot search by run id). It appears at most once per
#     answer, on outputs that claim something about runs but have no run id to
#     link — an empty listing, or rows beyond a display cap.
#
# Both are rendered as markdown rather than bare URLs. A factory's ARM path makes
# a run URL ~250 characters, which swamps the pipe-delimited row or aligned field
# it belongs to, and forty of them turn a listing into a wall of subscription
# paths. The destination is wrapped in angle brackets because the ARM path
# carries the resource group verbatim and Azure permits parentheses in a resource
# group name: an unwrapped CommonMark destination ends at the first unbalanced
# ')', which would truncate the link silently in one deployment and nowhere else.
# Labels are always WORDS. A bracketed number would collide with the [n]
# knowledge-base citation markers the UI turns into "Referenced Sources" entries.


def _run_link(sub: str, rg: str, factory_name: str, run_id: str) -> str:
    """The link that opens ONE run — appended to the line that prints its id."""
    return f"[open run](<{_run_url(sub, rg, factory_name, run_id)}>)"


def _capped_rows(rows: list[str], cap: int = _DISCOVERY_MAX_ROWS) -> list[str]:
    """Trim a discovery row group, saying so rather than truncating silently.

    The overflow line carries no link: the rows past the cap are not named, so
    there is no run to open — and a factory Monitor link here would answer a
    question about specific pipelines with a 24-hour grid.
    """
    if len(rows) <= cap:
        return rows
    return rows[:cap] + [
        f"  … and {len(rows) - cap} more not shown (showing the first {cap}); "
        "narrow the source system or name a single factory to see them"
    ]


def _monitor_footer(
    sub: str, rg: str, factory_name: str, indent: str = "  ", pipeline_name: str = ""
) -> list[str]:
    """The once-per-answer Monitor line, with the caveat that bounds what it shows.

    The caveat travels WITH the link rather than living in the prompt: a reader
    who opens Monitor cold sees the last 24 hours, finds an empty grid, and
    concludes the tool invented the runs it just listed.

    ``pipeline_name`` is passed only where ONE pipeline is genuinely in scope,
    and the label then names it: a filtered tab and a factory-wide one look
    identical until the grid loads, so a reader told only "open Monitor" reads
    the missing rows as the pipeline having no runs anywhere.
    """
    url = _factory_monitor_url(sub, rg, factory_name, pipeline_name)
    label = f"open Monitor filtered to '{pipeline_name}'" if pipeline_name else "open Monitor"
    return [f"{indent}in ADF Studio: [{label}](<{url}>)", f"{indent}({_MONITOR_WINDOW_HINT})"]


async def _locate_run(run_id: str, factory: str):
    """Find which configured factory a run id lives in.

    A run id is globally unique and carries no factory in it, so a caller who
    has one often does NOT know where it lives — incident correlation routinely
    hands back runs from several factories at once. Defaulting an un-named
    factory to ADF_DEFAULT_FACTORY and stopping at the first 404 therefore fails
    exactly the case cross-factory work creates, and fails it noisily: the ARM
    error carries a request id and an RFC link that end up in the user's answer.

    An EXPLICIT alias is honoured and a miss there stays a miss — quietly
    answering about a different factory than the one asked for would be worse
    than the error. Only an empty ``factory`` sweeps, default first.

    Returns ``(alias, sub, rg, name, client, run)`` — the run itself comes back
    because locating it already fetched it — or a model-facing string naming
    every factory searched.

    That returned triple is what every caller must build a run link from: it
    names the factory the run was FOUND in, which for a sweep is routinely not
    the default one. Neither string return carries a link, and neither can: one
    reports a factory that errored mid-sweep (the run may be in the next one),
    the other that no configured factory holds the id at all.
    """
    explicit = (factory or "").strip()
    try:
        targets = _resolve_factory_targets(explicit, all_when_empty=True)
    except _InputError as exc:
        return str(exc)

    if not explicit:
        default = _default_alias()
        targets = sorted(targets, key=lambda target: target[0] != default)

    missed: list[str] = []
    for alias, sub, rg, name in targets:
        try:
            client = await _client(sub)
            run = await client.pipeline_runs.get(rg, name, run_id)
        except ResourceNotFoundError:
            missed.append(alias)
            continue
        except Exception as exc:  # noqa: BLE001 - surfaced to the model as text
            return (
                f"[adf-agent] ERROR fetching run '{run_id}' in factory "
                f"{_factory_label(alias)}: {_truncate(exc)}"
            )
        return alias, sub, rg, name, client, run

    searched = ", ".join(_factory_label(alias) for alias in missed)
    return (
        f"[adf-agent] Run '{run_id}' was not found in "
        + (f"factory {searched}." if len(missed) == 1 else f"any configured factory: {searched}.")
        + " Check the run id, or name the factory explicitly if it is one this"
        " deployment does not read."
    )


def _write_guard(alias: str) -> str | None:
    """Return a model-facing denial when ADF writes are not explicitly enabled.

    This is enforced inside each mutating tool rather than only in the prompt,
    so a model mistake or direct tool invocation cannot bypass it.
    """
    if not bool(getattr(settings, "adf_write_enabled", False)):
        return (
            "[adf-agent] WRITE BLOCKED: ADF mutations are disabled. Set "
            "ADF_WRITE_ENABLED=true and explicitly allow the factory alias in "
            "ADF_WRITE_FACTORY_ALLOWLIST after granting the runtime identity the "
            "required Data Factory write role."
        )
    allowed = {
        str(value).strip()
        for value in getattr(settings, "adf_write_factory_allowlist", [])
        if str(value).strip()
    }
    if alias not in allowed:
        allowed_text = ", ".join(sorted(allowed)) or "(none)"
        return (
            f"[adf-agent] WRITE BLOCKED: factory '{alias}' is not in "
            f"ADF_WRITE_FACTORY_ALLOWLIST. Allowed aliases: {allowed_text}."
        )
    return None


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
                    # NOT azure.identity.aio: its AzureCliCredential leg still
                    # blocks on os.stat, which `langgraph dev`'s detector rejects
                    # (see ThreadOffloadAsyncCredential for the full reason).
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

    The parent id is NOT deep-linked. This helper is a fragment embedded in rows
    across half the tools and is handed a run, not a factory, so it has nothing
    to build a URL from; threading a factory triple through every caller to link
    a second id on a line that already links its own would cost more than it
    pays. get_pipeline_run_tree is the intended way to walk to the parent, and
    it links every member of the family.
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


def _as_utc(moment: datetime) -> datetime:
    """A timestamp made tz-aware (UTC) so it can be compared against ``now``.

    msrest deserializes ADF timestamps as aware UTC; this only guards the
    arithmetic against a naive value, which would raise on the subtraction.
    """
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _parse_date(value: str, label: str) -> datetime:
    """Parse a caller-supplied date, defaulting a bare date to UTC midnight."""
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise _InputError(
            f"[adf-agent] Invalid {label} '{value}' — use YYYY-MM-DD, e.g. "
            "start_date='2026-07-10', end_date='2026-07-12'."
        ) from exc
    return _as_utc(moment)


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
    # UTC day boundaries, and named as such: a run late on a local evening falls
    # into the NEXT UTC day, so an unlabelled range invites an off-by-one read.
    return f"between {after.date()} and {_inclusive_end(before)} (UTC)"


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


def _run_filters(pipeline_name: str, status: str) -> list[RunQueryFilter]:
    """Build a run-query filter for each server-supported criterion supplied.

    Only fields ADF's Pipeline Runs Query API actually filters on are sent:
    PipelineName and Status. Trigger/invoker is NOT a supported run-query filter
    operand (the old "TriggeredByName" matched nothing), so trigger matching is
    done client-side from each run's invocation info — see ``_extract_trigger_name``.

    ``values`` is the kwarg name on azure-mgmt-datafactory 9.2.0 (the pinned
    version); 10.x renamed it to ``values_property`` and rejects this one, so
    every run query here fails loudly on an unreviewed SDK bump — by design.
    """
    criteria = (
        ("PipelineName", pipeline_name),
        ("Status", status),
    )
    return [
        RunQueryFilter(operand=operand, operator="Equals", values=[value])
        for operand, value in criteria
        if value
    ]


def _runs_params(
    pipeline_name: str, after: datetime, before: datetime, status: str = ""
) -> RunFilterParameters:
    """A RunFilterParameters for one pipeline/status over [after, before), newest first."""
    return RunFilterParameters(
        last_updated_after=after,
        last_updated_before=before,
        filters=_run_filters(pipeline_name, status),
        order_by=[RunQueryOrderBy(order_by="RunStart", order="DESC")],
    )


async def _page_runs(client, rg: str, factory_name: str, params, max_pages: int) -> tuple[list, bool]:
    """Page a run query up to ``max_pages``; returns (runs, fully_paged).

    ``fully_paged`` is True only when ADF stopped handing back a continuation
    token before the cap — the signal callers use to decide whether a count is a
    total or a lower bound.
    """
    resp = await client.pipeline_runs.query_by_factory(rg, factory_name, filter_parameters=params)
    runs = list(resp.value or [])
    token = getattr(resp, "continuation_token", None)
    pages = 1
    while token and pages < max_pages:
        params.continuation_token = token
        resp = await client.pipeline_runs.query_by_factory(
            rg, factory_name, filter_parameters=params
        )
        runs.extend(resp.value or [])
        token = getattr(resp, "continuation_token", None)
        pages += 1
    return runs, token is None


async def _query_runs(client, rg: str, factory_name: str, params) -> tuple[list, bool]:
    """Runs matching ``params`` for a DISPLAY listing, bounded by ``_RUNS_MAX_PAGES``.

    The bound caps latency for conversational listings; the returned flag tells
    the caller when the window ran past it so counts can be marked lower bounds.
    """
    return await _page_runs(client, rg, factory_name, params, _RUNS_MAX_PAGES)


async def _query_all_runs(client, rg: str, factory_name: str, params) -> tuple[list, bool]:
    """Runs matching ``params`` for ANALYTICS — pages to the end of the window.

    The discovery/recovery/runtime tools must not compute an "exact" result from
    a truncated window, so this pages until ADF stops returning a continuation
    token (bounded only by the runaway-loop guard ``_ANALYTICS_MAX_PAGES``). If
    that guard is hit the returned flag is False and the caller MUST report the
    result as incomplete rather than exact.
    """
    return await _page_runs(client, rg, factory_name, params, _ANALYTICS_MAX_PAGES)


# --- shared analytics helpers -------------------------------------------------
# Deterministic building blocks for the discovery / recovery / runtime tools.
# The business rules (trigger match, exact-parameter match, runtime maths) live
# HERE, in code, not in the model — the tools compute; the model presents.


def _extract_trigger_name(run) -> str:
    """The invoking entity's name from a run's invocation info ('' if unknown).

    ADF records what started a run on ``invoked_by`` — a manual start, a trigger,
    or a parent pipeline's Execute Pipeline activity. Trigger comparisons read
    this CLIENT-SIDE because the run-query API has no trigger filter operand.
    """
    ib = getattr(run, "invoked_by", None)
    return (getattr(ib, "name", "") or "") if ib else ""


def _normalized_parameters(run) -> dict:
    """A run's parameter dict, treating a missing/None set as empty.

    Values are returned EXACTLY as ADF gave them — no case-folding, type
    coercion, or key dropping — so a later exact comparison stays faithful.
    """
    return dict(getattr(run, "parameters", None) or {})


def _exact_parameter_match(left, right) -> bool:
    """True only if two parameter sets are identical.

    ``dict ==`` is order-independent (different key order still matches) yet key-
    and value-exact (a changed value, or an added/removed parameter, does NOT
    match). A missing set is treated as empty, never as a wildcard.
    """
    return (left or {}) == (right or {})


def _runtime_ms(run):
    """A run's duration in ms, or None when it has no final runtime yet."""
    return getattr(run, "duration_in_ms", None)


def _completed_runs(runs: list) -> list:
    """Terminal runs (Succeeded/Failed/Cancelled) that carry a real duration.

    Active runs are excluded because they have no final runtime. Per the runtime
    use cases this is "completed", NOT "successful": a Failed or Cancelled run
    still completed and its runtime counts toward the history.
    """
    return [r for r in runs if _is_terminal_run(r) and _runtime_ms(r) is not None]


def _is_terminal_run(run) -> bool:
    """True when a run has reached a final state (Succeeded/Failed/Cancelled)."""
    return getattr(run, "status", None) in _TERMINAL_STATUSES


def _runtime_stats(runs: list) -> dict | None:
    """count / avg / min / max duration (ms) over the given completed runs.

    Returns None when there is nothing to aggregate, so callers say "no runtime
    history" rather than dividing by zero.
    """
    durations = [_runtime_ms(r) for r in runs]
    durations = [d for d in durations if d is not None]
    if not durations:
        return None
    return {
        "count": len(durations),
        "avg_ms": sum(durations) / len(durations),
        "min_ms": min(durations),
        "max_ms": max(durations),
    }


def _fmt_duration(ms) -> str:
    """Render a millisecond duration as e.g. '2h 48m 03s' ('—' when unknown)."""
    if ms is None:
        return "—"
    total_seconds = int(round(ms / 1000))
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes or hours:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds:02d}s" if (hours or minutes) else f"{seconds}s")
    return " ".join(parts)


def _fmt_ts(value) -> str:
    """Render an ADF timestamp as unambiguous UTC: '2026-09-16 00:43:21 UTC'.

    Every time ADF returns is UTC, but ``str(datetime)`` spells that zone as a
    bare '+00:00' offset — and the ADF Portal shows LOCAL time by default, so a
    run at 00:43 UTC is displayed there on the PREVIOUS evening. Read side by
    side, the unlabelled form looks like the agent invented a date a day out.
    Labelling the zone HERE rather than in the prompt means every tool line
    carries it whether or not the model remembers to — it does not always: it
    has relayed a start time with the offset stripped off entirely. Sub-second
    precision is dropped as operational noise; run IDs, not microseconds, are
    what tell two runs apart. A naive datetime is rendered as-is and still
    labelled (ADF's wire format is Z-suffixed, so naive can only mean UTC),
    while a value that is not a datetime is passed through untouched rather
    than labelled on an assumption.
    """
    if value is None:
        return "(unknown)"
    if not isinstance(value, datetime):
        return str(value)
    moment = value.astimezone(timezone.utc) if value.utcoffset() is not None else value
    return moment.strftime("%Y-%m-%d %H:%M:%S UTC")


def _sort_by_start(runs: list, newest_first: bool = True) -> list:
    """Runs ordered by start time; runs without a start sink to the end.

    Client-side ordering the analytics tools rely on — ADF's DESC order applies
    to a single query, but recovery/estimate merge and re-slice runs after
    filtering, so they re-sort explicitly rather than trust arrival order.
    """
    dated = [r for r in runs if getattr(r, "run_start", None) is not None]
    undated = [r for r in runs if getattr(r, "run_start", None) is None]
    dated.sort(key=lambda r: r.run_start, reverse=newest_first)
    return dated + undated


# --- pipeline-definition search (used by discover_pipelines_by_source_system) --
# The named sections of a pipeline definition a source-system reference can hide
# in. azure-mgmt-datafactory 9.2.0's as_dict() puts these at the TOP level;
# the attribute fallback below (and the offline fake) nest everything but the
# name under 'properties'. _matched_sections reads either shape.
_DEFINITION_SECTIONS = ("name", "description", "parameters", "variables", "annotations", "activities")


def _definition_wire(pipeline) -> dict:
    """A pipeline definition as a plain nested dict (wire shape when available)."""
    if isinstance(pipeline, dict):
        # Raw ARM JSON from _keep_wire: already the nested {name, properties}
        # shape _matched_sections prefers, and the only shape that still carries
        # the 'type' discriminator of an activity 9.2.0 cannot model. Rebuild it
        # rather than returning it whole so the ARM 'id' — which spells out the
        # subscription, resource group and factory — cannot manufacture a
        # source-system "match" out of infrastructure names. Mirrors the
        # attribute branch below, including omitting an absent name.
        out: dict = {}
        name = pipeline.get("name")
        if name is not None:
            out["name"] = name
        props = pipeline.get("properties")
        out["properties"] = props if isinstance(props, dict) else {}
        return out
    as_dict = getattr(pipeline, "as_dict", None)
    if callable(as_dict):
        try:
            return as_dict()
        except Exception:  # noqa: BLE001 - fall back to attribute reading
            pass
    props = getattr(pipeline, "properties", None)
    source = props if props is not None else pipeline
    out: dict = {}
    name = getattr(pipeline, "name", None)
    if name is not None:
        out["name"] = name
    section: dict = {}
    for key in _DEFINITION_SECTIONS:
        if key == "name":
            continue
        value = getattr(source, key, None)
        if value is None and source is not pipeline:
            value = getattr(pipeline, key, None)
        if value is not None:
            section[key] = value
    out["properties"] = section
    return out


def _deep_text(obj) -> str:
    """A recursive text rendering of a nested structure for substring search.

    Keys AND values are included, so a token that appears only as a parameter
    name, an annotation, a dataset/linked-service referenceName, or a deeply
    nested activity property is still found. Azure SDK models are unwrapped via
    as_dict; everything else is walked structurally.
    """
    as_dict = getattr(obj, "as_dict", None)
    if callable(as_dict) and not isinstance(obj, dict):
        try:
            obj = as_dict()
        except Exception:  # noqa: BLE001
            pass
    if isinstance(obj, dict):
        return " ".join(f"{k} {_deep_text(v)}" for k, v in obj.items())
    if isinstance(obj, (list, tuple, set)):
        return " ".join(_deep_text(v) for v in obj)
    return str(obj)


def _serialize_pipeline_definition(pipeline) -> str:
    """The whole pipeline definition as one lowercased searchable string."""
    return _deep_text(_definition_wire(pipeline)).lower()


def _definition_props(wire: dict) -> dict:
    """The section-bearing half of a wire definition, whichever shape it is in.

    9.2.0's as_dict() has no 'properties' wrapper — the sections sit at the top
    level. Without this the evidence silently collapses to 'name'.
    """
    props = wire.get("properties")
    return props if isinstance(props, dict) else wire


def _matched_sections(pipeline, needle: str) -> list[str]:
    """Which named definition sections contain ``needle`` (already lowercased)."""
    wire = _definition_wire(pipeline)
    props = _definition_props(wire)
    hits = []
    if needle in _deep_text(wire.get("name", "")).lower():
        hits.append("name")
    for key in _DEFINITION_SECTIONS:
        if key == "name":
            continue
        value = props.get(key)
        if value is not None and needle in _deep_text(value).lower():
            hits.append(key)
    return hits


# Evidence is quoted from the definition, so it is bounded twice: at most this
# many "field=value" specifics per matched section, each value windowed to this
# many characters. A discovery row has to JUSTIFY its match on one line, not
# reprint the pipeline.
_EVIDENCE_PER_SECTION = 2
_EVIDENCE_SNIPPET = 60


def _evidence_leaf(text: str, needle: str) -> str:
    """The matching value, windowed around ``needle`` when it is long."""
    text = " ".join(str(text).split())
    if len(text) <= _EVIDENCE_SNIPPET:
        return text
    # Keep the match itself inside the window — a head-only clip of a long
    # description usually cuts off the very word that justified the row.
    start = max(0, max(text.lower().find(needle), 0) - _EVIDENCE_SNIPPET // 3)
    return ("…" if start else "") + text[start : start + _EVIDENCE_SNIPPET] + "…"


def _evidence_paths(value, needle: str, path: str) -> list[str]:
    """``field="value"`` specifics naming exactly where ``needle`` sits in a section.

    Walks the same structure _deep_text flattens, but KEEPS THE KEY PATH, so a
    hit can be quoted as the smallest self-justifying fact: 'matched: parameters'
    never told anyone the pipeline carries SourceSystem="LOANSYS", and a match
    nobody can check reads as an arbitrary one. Dict KEYS are matched too (a
    parameter *named* LOANSYS_ID is a real hit with no matching value); list
    indices are dropped, because an annotation's position is noise.
    """
    as_dict = getattr(value, "as_dict", None)
    if callable(as_dict) and not isinstance(value, dict):
        try:
            value = as_dict()
        except Exception:  # noqa: BLE001 - fall back to the structural walk
            pass
    if isinstance(value, dict):
        hits: list[str] = []
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if needle in str(key).lower():
                hits.append(child_path)
            else:
                hits.extend(_evidence_paths(child, needle, child_path))
        return hits
    if isinstance(value, (list, tuple, set)):
        return [hit for child in value for hit in _evidence_paths(child, needle, path)]
    text = str(value)
    if needle not in text.lower():
        return []
    return [f'{path}="{_evidence_leaf(text, needle)}"']


def _match_evidence(pipeline, needle: str) -> list[str]:
    """Per-section evidence, each entry naming the field that carried ``needle``.

    Same sections as _matched_sections — which stays the section-level contract
    the SDK-shape tests pin — drilled down one level further. A section falls
    back to its bare name only when the hit spans a key/value boundary _deep_text
    joined and no single field contains the needle.
    """
    props = _definition_props(_definition_wire(pipeline))
    evidence = []
    for section in _matched_sections(pipeline, needle):
        if section == "name":
            evidence.append("name")  # already the row's second column
            continue
        paths = _evidence_paths(props.get(section), needle, section)
        if not paths:
            evidence.append(section)
            continue
        # How MANY fields carried the needle is the strength of the match, and a
        # row that shows two of five understates it — which is how a candidate
        # gets judged weak and dropped. No '|' and no bracketed number: this text
        # lands in a pipe-delimited row that also carries [n] citation markers.
        hidden = len(paths) - _EVIDENCE_PER_SECTION
        shown = ", ".join(paths[:_EVIDENCE_PER_SECTION])
        evidence.append(f"{shown} (… and {hidden} more in {section})" if hidden > 0 else shown)
    return evidence


def _render_runs(
    runs: list, alias: str, pipeline_name: str, complete: bool, sub: str, rg: str, name: str
) -> str:
    """Render a run listing: header, per-pipeline totals when capped, then rows.

    Every row carries a deep link to its own run — this tool is the single
    biggest producer of run ids in the file, and its docstring tells the model to
    hand those ids onward, so an unopenable id here propagates everywhere. The
    Monitor footer is NOT what makes them checkable: it opens on the last 24
    hours while this tool's default window is 7 days, so most rows it prints are
    invisible behind it. It stays because it is the only affordance for the runs
    BEYOND the row cap, and for a reader who wants to browse rather than open one
    run. The factory triple is taken rather than a finished URL so the rows and
    the footer cannot come to name two different factories.
    """
    header = (
        f"[adf-agent] {len(runs)}{'' if complete else '+'} run(s) (newest first) in factory "
        f"{_factory_label(alias)}" + (f" for '{pipeline_name}'" if pipeline_name else "")
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
    # The link is the LAST column on purpose: 'runId={id} ' keeps its trailing
    # space, so anything parsing the id out of a row still reads a clean value.
    rows = [
        f"  - runId={r.run_id} | {r.pipeline_name} | {r.status} | "
        f"start={_fmt_ts(r.run_start)} | {r.duration_in_ms or 0} ms | "
        f"triggeredBy={_invoked_text(r)} | {_run_link(sub, rg, name, r.run_id)}"
        for r in runs[:_MAX_RUN_ROWS]
    ]
    footer = "\n" + "\n".join(_monitor_footer(sub, rg, name, pipeline_name=pipeline_name))
    return header + "\n" + "\n".join(rows) + footer


@tool
async def list_factories() -> str:
    """List the Azure Data Factories this agent can query, marking the default.

    Use this when the user asks which factories/environments are available, or
    when a factory is needed and the user has not named one. Takes no arguments.
    Returns the factories' Azure resource names — the names the ADF UI shows.
    Pass a returned name straight back as the `factory` argument of other tools.
    """
    mapping = settings.adf_factory_mapping
    if not mapping:
        return "[adf-agent] No Data Factory is configured (ADF_FACTORY_MAPPING is empty)."
    default = _default_alias()
    lines = [
        f"  - {_factory_label(alias)}" + ("  (default)" if alias == default else "")
        for alias in _factory_aliases()
    ]
    return f"[adf-agent] {len(mapping)} configured factory(ies):\n" + "\n".join(lines)


def _pipeline_row(name: str, description, run=None, link: str = "") -> str:
    """One inventory row: the factory's own description, plus the last run if known."""
    # The description is the factory's own metadata; pipelines without one get
    # "(no description set)" so the model reports that honestly instead of
    # inventing a gloss from the pipeline name.
    text = _truncate(description) if description else "(no description set)"
    if run is None:
        return f"  - {name} — {text}"
    row = (
        f"  - {name} — {text} | last run {_fmt_ts(getattr(run, 'run_start', None))} "
        f"({getattr(run, 'status', 'unknown')})"
    )
    return f"{row} | {link}" if link else row


def _inactive_names(names: list[str]) -> str:
    """The hidden pipelines by name while that stays readable, else nothing.

    A bare count reads as "trust me"; hundreds of names drown the answer. Naming
    a short tail keeps the default filter auditable without either failure.
    """
    if not names or len(names) > _INACTIVE_NAMES_MAX:
        return "."
    return ": " + ", ".join(sorted(names)) + "."


async def _latest_run_by_pipeline(
    client, rg: str, factory_name: str, after: datetime, before: datetime
) -> tuple[dict, bool]:
    """Map pipeline name -> its most recent run in the window, plus a complete flag.

    ONE factory-wide run query, never one per definition: a per-pipeline fan-out
    is the N+1 that made the ServiceNow list tool slow, and inventory runs on
    every "what pipelines are there" question.
    """
    runs, complete = await _query_all_runs(
        client, rg, factory_name, _runs_params("", after, before)
    )
    latest: dict = {}
    for run in _sort_by_start(runs):
        key = getattr(run, "pipeline_name", None)
        if key and key not in latest:
            latest[key] = run
    return latest, complete


@tool
async def list_pipelines(
    factory: str = "",
    include_inactive: bool = False,
    active_days: int = _INVENTORY_ACTIVE_DAYS,
) -> str:
    """List the pipelines that are actually in use in an Azure Data Factory.

    By DEFAULT this returns only ACTIVE pipelines — those with at least one run
    in the last 30 days. A factory keeps every definition ever deployed, so the
    unfiltered inventory buries the handful anyone operates under retired ones.
    Same activity rule as discover_pipelines_by_source_system, so "active" means
    one thing across every ADF answer.

    Args:
        factory: Optional factory alias. Leave empty to
                 use the default factory.
        include_inactive: Set true ONLY when the user explicitly asks for every
                 pipeline, for retired/archived/all pipelines, or for a pipeline
                 the default window hid. Never set it to pad a thin answer.
        active_days: Size of the activity window in days (default 30). Set it
                 only when the user names a different period. Ignored entirely
                 when include_inactive is true.

    Use this when the user asks what pipelines exist, or as a first step before
    looking at runs.
    """
    try:
        alias, sub, rg, name = _resolve_factory(factory)
    except _InputError as exc:
        return str(exc)
    try:
        client = await _client(sub)
        pipes = [
            (p.name, getattr(p, "description", None))
            async for p in client.pipelines.list_by_factory(rg, name)
        ]
    except Exception as exc:  # surface auth/permission errors to the model as text
        return (
            f"[adf-agent] ERROR listing pipelines in factory {_factory_label(alias)}: "
            f"{_truncate(exc)}"
        )
    # No link on either branch below, and the rule is the same one: link only
    # where the output asserts something about RUNS. "No pipelines" and the full
    # definition inventory make no run claim at all, so a Monitor link would
    # answer a question nobody asked — on the longest output this tool produces.
    if not pipes:
        return f"[adf-agent] Factory {_factory_label(alias)} has no pipelines."

    if include_inactive:
        listing = "\n".join(_pipeline_row(n, d) for n, d in pipes)
        return (
            f"[adf-agent] Factory {_factory_label(alias)} has {len(pipes)} pipeline(s) "
            f"— FULL inventory, active and inactive:\n{listing}"
        )

    days = max(1, int(active_days or _INVENTORY_ACTIVE_DAYS))
    now = datetime.now(timezone.utc)
    try:
        latest, complete = await _latest_run_by_pipeline(
            client, rg, name, now - timedelta(days=days), now
        )
    except Exception as exc:
        # Fail OPEN. A run-query outage must not silently shrink the inventory:
        # hiding pipelines we merely could not check reads as "these do not
        # exist", which is the worse error. Show everything and say why.
        listing = "\n".join(_pipeline_row(n, d) for n, d in pipes)
        # This branch's whole message is "I could not check which of these
        # actually run", and Monitor is exactly where the reader checks that.
        return (
            f"[adf-agent] Factory {_factory_label(alias)} has {len(pipes)} pipeline(s). "
            f"The {days}-day activity filter could NOT be applied ({_truncate(exc)}), so "
            f"this is the full inventory, active and inactive:\n{listing}\n"
            + "\n".join(_monitor_footer(sub, rg, name))
        )

    # Newest run first, which is what the header promises: _latest_run_by_pipeline
    # fills the map in newest-first order, so its key order IS the ranking and
    # re-sorting on run_start here would only re-litigate the missing-timestamp
    # case it already settled. ADF hands back pipelines alphabetically, and
    # alphabetical order tells nobody which pipeline is running today.
    rank = {n: i for i, n in enumerate(latest)}
    active = sorted(((n, d) for n, d in pipes if n in latest), key=lambda row: rank[row[0]])
    inactive = [n for n, _ in pipes if n not in latest]
    window = f"the last {days} day(s)"
    if not active:
        # The strongest negative claim this tool can make about a whole factory,
        # and the one most often disbelieved (usually it means the wrong factory,
        # a permission problem, or too narrow a window). The link lets the reader
        # disprove it in one click — with the caveat, or the 24-hour grid it
        # opens on "confirms" the claim for entirely the wrong reason.
        return (
            f"[adf-agent] Factory {_factory_label(alias)} has {len(pipes)} pipeline(s), but "
            f"NONE has run in {window} — the factory is not idle-free, it is entirely idle "
            "by this measure. Call list_pipelines(include_inactive=true) for the full "
            "inventory.\n" + "\n".join(_monitor_footer(sub, rg, name))
        )
    # Each active row quotes a last-run time and status, so it makes a claim
    # about one specific run — and _latest_run_by_pipeline already holds that
    # whole run object, run id included, so linking it costs no extra call.
    def _row(n: str, d) -> str:
        run = latest.get(n)
        run_id = getattr(run, "run_id", None) if run is not None else None
        link = _run_link(sub, rg, name, run_id) if run_id else ""
        return _pipeline_row(n, d, run, link)

    listing = "\n".join(_row(n, d) for n, d in active)
    lines = [
        f"[adf-agent] Factory {_factory_label(alias)}: {len(active)} ACTIVE pipeline(s) "
        f"(ran in {window}), newest run first:",
        listing,
    ]
    if inactive:
        lines.append(
            f"  Hidden: {len(inactive)} pipeline(s) defined but with no run in {window}"
            f"{_inactive_names(inactive)} Call list_pipelines(include_inactive=true) to "
            "list them."
        )
    if not complete:
        lines.append(
            "  NOTE: the run history for this window was truncated, so a pipeline listed "
            "as inactive may have run early in the window."
        )
    # Monitor IN ADDITION to the per-row links above, not instead of them: the
    # rows cover the active pipelines, while the Hidden and truncated-history
    # notes make claims about runs this listing does NOT name — which is exactly
    # the "no id to link" case the footer exists for.
    lines.extend(_monitor_footer(sub, rg, name))
    return "\n".join(lines)


async def _pipeline_missing_hint(
    client, rg: str, factory_name: str, alias: str, pipeline_name: str
) -> str | None:
    """A "pipeline does not exist" message when an empty run list is really a
    missing pipeline, else None.

    ADF's run query returns an empty page for a pipeline that does not exist,
    which reads as "exists but idle" — the source of a whole family of
    misleading "no runs" answers. This tells the two cases apart so every tool
    that queries runs by pipeline can lead with "missing", not "idle".
    """
    if not pipeline_name:
        return None
    try:
        await client.pipelines.get(rg, factory_name, pipeline_name)
    except ResourceNotFoundError:
        others = [a for a in _factory_aliases() if a != alias]
        # Name the ADF resource, never the alias: a "not in <alias>" hint sends
        # the reader looking for a factory that is not called that in Azure, and
        # they conclude the pipeline exists nowhere.
        hint = (
            f" Only factory {_factory_label(alias)} was searched; also configured: "
            + ", ".join(_factory_label(a) for a in others)
            + " (re-run with factory=<name> to check them)."
            if others
            else ""
        )
        return (
            f"[adf-agent] Pipeline '{pipeline_name}' DOES NOT EXIST in factory "
            f"{_factory_label(alias)} — the empty run list means the pipeline is "
            f"missing, not idle.{hint} Use list_pipelines to see the actual "
            "pipeline names."
        )
    except Exception:  # noqa: BLE001 - existence unknown; keep the plain answer
        return None
    return None


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
                       "Succeeded", "Failed", "Canceling", "Cancelled" (any
                       casing). Leave empty for all statuses.
        trigger_name:  Optional exact trigger name to filter by (e.g.
                       "tr_every_2_hours") — only runs started by
                       that trigger are returned. Matched against each run's
                       invocation info AFTER the query (ADF's run-query API has
                       no trigger filter). Leave empty for all runs.
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
        # ADF returns pages oldest-first by default, so RunStart DESC (inside
        # _runs_params) is what makes the display cap show the NEWEST runs.
        params = _runs_params(pipeline_name, after, before, _normalize_status(status))
    except _InputError as exc:
        return str(exc)

    try:
        client = await _client(sub)
        runs, complete = await _query_runs(client, rg, name, params)
    except Exception as exc:
        return (
            f"[adf-agent] ERROR querying runs in factory {_factory_label(alias)}: "
            f"{_truncate(exc)}"
        )

    window = _window_text(after, before, bool(start_date or end_date), last_n_days)
    # pipeline_name is "" on an all-pipelines listing, which leaves the footer
    # factory-wide; both branches that use this string name one pipeline when it
    # is set, and an unfiltered tab there is the "Pipeline name: All" report.
    monitor = "\n" + "\n".join(_monitor_footer(sub, rg, name, pipeline_name=pipeline_name))
    if not runs:
        missing = await _pipeline_missing_hint(client, rg, name, alias, pipeline_name)
        if missing:
            return missing
        scope = f" for pipeline '{pipeline_name}'" if pipeline_name else ""
        return (
            f"[adf-agent] No runs{scope} in factory {_factory_label(alias)} {window}."
            f"{monitor}"
        )

    # Trigger filtering is client-side: ADF's run-query API cannot filter on the
    # trigger, so the window is queried without it and each run's invocation
    # info is compared here. Counts/rows below reflect the trigger-matched set.
    if trigger_name:
        matched = [r for r in runs if _extract_trigger_name(r) == trigger_name]
        if not matched:
            scope = f" for pipeline '{pipeline_name}'" if pipeline_name else ""
            # Monitor, not a run link: there is no run to open, and this is the
            # branch most likely to be a typo'd trigger name (the match is exact
            # and client-side), so the reader's next move is to browse the
            # factory and find what the trigger is actually called.
            return (
                f"[adf-agent] No runs{scope} started by trigger '{trigger_name}' in factory "
                f"{_factory_label(alias)} {window} ({len(runs)} run(s) matched the "
                f"window under other "
                f"triggers/invokers).{monitor}"
            )
        runs = matched
    return _render_runs(runs, alias, pipeline_name, complete, sub, rg, name)


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


def _run_fields(run, indent: str = "  ") -> list[str]:
    """The field block of a run's detail card, indented for its context.

    Shared so a run reads IDENTICALLY wherever it is shown — the run-detail tool
    and the incident correlation both render this block, and a reader comparing
    the two against the ADF Portal should never have to reconcile two layouts.
    """
    out = [
        f"{indent}pipeline    : {run.pipeline_name}",
        f"{indent}status      : {run.status}",
        f"{indent}triggeredBy : {_invoked_text(run)}",
        f"{indent}start       : {_fmt_ts(run.run_start)}",
        f"{indent}end         : {_fmt_ts(run.run_end)}",
        f"{indent}duration    : {run.duration_in_ms or 0} ms",
    ]
    if run.parameters:
        out.append(f"{indent}parameters  : {_truncate(dict(run.parameters))}")
    if run.message:
        out.append(f"{indent}message     : {_clean_error(run.message)}")
    return out


def _activity_lines(acts: list, indent: str = "  ") -> list[str]:
    """One line per activity run, plus its error and (on success) its output."""
    out = [f"{indent}activities ({len(acts)}):"]
    for a in acts:
        out.append(
            f"{indent}  • {a.activity_name} [{a.activity_type}] → {a.status} "
            f"(activityRunId={a.activity_run_id})"
        )
        error_line = _error_text(a.error)
        if error_line:
            out.append(f"{indent}      {error_line}")
        if a.status == "Succeeded" and a.output:
            out.append(f"{indent}      output: {_truncate(a.output)}")
    return out


async def _activity_block(sub: str, rg: str, factory_name: str, run, indent: str) -> list[str]:
    """A run's activity lines, or a line saying why they could not be read.

    The activity query is a SECOND call that can fail on its own after the run
    itself was read fine; that is reported in place rather than discarding the
    run, because the run's own status and message are still the answer.
    """
    try:
        client = await _client(sub)
        acts = await _activity_runs_for(client, rg, factory_name, run, run.run_id)
    except Exception as exc:  # noqa: BLE001 - surfaced to the model as text
        return [f"{indent}activities: ERROR querying activity runs: {_truncate(exc)}"]
    if not acts:
        return [f"{indent}activities: (none reported)"]
    return _activity_lines(acts, indent)


@tool
async def get_pipeline_run_details(run_id: str, factory: str = "") -> str:
    """Fetch a single pipeline run's status and its per-activity logs/errors.

    Args:
        run_id:  The pipeline run GUID (from list_pipeline_runs).
        factory: Optional factory alias. LEAVE IT EMPTY unless the user named a
                 factory: a run id carries no factory, so an empty value searches
                 every configured one and reports where the run actually lives.
                 Naming an alias restricts the search to it, and a run that lives
                 elsewhere then reports as not found.

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
    located = await _locate_run(run_id, factory)
    if isinstance(located, str):
        return located
    alias, sub, rg, name, client, run = located

    # On the header, beside the id it opens, instead of as a separate field
    # below: one convention for a run link across every tool, and the id and its
    # link cannot drift apart when the field block grows. The factory triple is
    # whichever one _locate_run actually found the run in, which for a
    # factory-less call is often not the default.
    out = [
        f"[adf-agent] Run {run_id} (factory {_factory_label(alias)}) "
        f"{_run_link(sub, rg, name, run_id)}"
    ]
    out.extend(_run_fields(run))
    out.extend(await _activity_block(sub, rg, name, run, "  "))
    return "\n".join(out)


# Recursion guards for run trees: a ForEach over hundreds of pages could fan
# out into hundreds of child runs — walk failures fully, but bound the total.
# Depth is counted in PIPELINE levels; 8 clears the deepest real hierarchy seen
# (5) with headroom. The run budget, not depth, is what bounds a wide ForEach.
_TREE_MAX_DEPTH = 8
_TREE_MAX_RUNS = 25


async def _climb_to_root(client, rg: str, factory_name: str, run_id: str, run=None):
    """Follow ``invoked_by.pipeline_run_id`` up to the run a trigger started.

    A failed run found via ``list_pipeline_runs`` is usually a CHILD, so the
    family can only be built after climbing to its root first. ``run`` lets a
    caller that already fetched the starting run skip re-fetching it.
    """
    if run is None:
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
    client,
    sub: str,
    rg: str,
    factory_name: str,
    run_id: str,
    depth: int,
    budget: dict,
    stats: Counter,
) -> list[str]:
    # ``sub`` is carried purely to build each node's link. A whole family lives
    # in ONE factory (Execute Pipeline cannot cross factories), so it is the same
    # triple at every depth — passed down rather than closed over so the walker
    # stays a plain function of its arguments.
    #
    # EVERY node is linked, succeeded ones included. The tree's job is to show
    # which branch failed, and a reader who can only open the failures cannot
    # check the branch that supposedly worked — which is exactly what gets
    # disputed when a family is "partially" fine.
    indent = "    " * depth
    if depth >= _TREE_MAX_DEPTH:
        # Truncation lines name and link the run the walk stopped AT, so the
        # reader can continue in Studio (or pass that id back) instead of being
        # told only that something was cut off.
        return [
            f"{indent}…[max depth {_TREE_MAX_DEPTH} reached at runId={run_id}] "
            f"{_run_link(sub, rg, factory_name, run_id)}"
        ]
    if budget["runs"] <= 0:
        # Deliberately NOT linked, unlike every other line in this walk. This
        # branch returns without spending budget, so it fires once per remaining
        # sibling rather than once per tree: a ForEach that fanned out to 200
        # child runs emits ~175 of these. At ~250 characters each a link here
        # would add ~45 KB to one tool message — on the only lines in the answer
        # describing runs the walker deliberately never fetched. The id is kept
        # because it is what the reader passes back to narrow the walk; the URL
        # is what they can build from any linked node once they do.
        budget["truncated"] = True
        return [
            f"{indent}…[run budget reached at runId={run_id} — narrow to a specific "
            f"child run_id]"
        ]
    budget["runs"] -= 1

    try:
        run = await client.pipeline_runs.get(rg, factory_name, run_id)
    except Exception as exc:
        # Linked despite the failure: the id came from the PARENT's activity, so
        # the run is known to exist in this factory — the fetch is what broke,
        # and Studio is where the reader confirms which of the two it was.
        return [
            f"{indent}✗ runId={run_id}: ERROR fetching: {_truncate(exc)} "
            f"{_run_link(sub, rg, factory_name, run_id)}"
        ]

    stats[run.status] += 1
    lines = [
        f"{indent}{run.pipeline_name} (runId={run_id}) → {run.status} "
        f"{_run_link(sub, rg, factory_name, run_id)}"
    ]

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
                    client, sub, rg, factory_name, child_run_id, depth + 1, budget, stats
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
        factory: Optional factory alias. LEAVE IT EMPTY unless the user named a
                 factory: a run id carries no factory, so an empty value searches
                 every configured one and reports where the run actually lives.
                 Naming an alias restricts the search to it, and a run that lives
                 elsewhere then reports as not found.

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
    requested = run_id.strip()
    located = await _locate_run(requested, factory)
    if isinstance(located, str):
        return located
    alias, sub, rg, name, client, run = located

    try:
        root = await _climb_to_root(client, rg, name, requested, run)
    except Exception as exc:
        # Linked, unlike the fetch errors elsewhere: _locate_run already FOUND
        # this run in this factory, so the failure is somewhere up the parent
        # chain and the requested run itself is known to be openable here.
        return (
            f"[adf-agent] ERROR fetching run '{requested}' in factory "
            f"{_factory_label(alias)}: {_truncate(exc)} "
            f"{_run_link(sub, rg, name, requested)}"
        )

    budget = {"runs": _TREE_MAX_RUNS, "truncated": False}
    stats: Counter = Counter()
    lines = await _walk_run_tree(
        client, sub, rg, name, root.run_id, depth=0, budget=budget, stats=stats
    )

    header = f"[adf-agent] Pipeline family (factory {_factory_label(alias)})"
    if root.run_id != requested:
        # Normally neither id is linked here: both are linked a few lines below
        # on their own nodes, and repeating a ~250-character URL twice in one
        # answer costs more than the shorter path to it saves.
        #
        # But "a few lines below" is not guaranteed. The walk starts at the ROOT
        # and can exhaust its run budget on earlier branches, so a requested run
        # sitting under a later branch never gets a node — leaving this header as
        # its only mention, and the run the caller actually asked about as the one
        # run in the answer with no way to open it. Test for the link rather than
        # for the id: the budget-truncation line above prints an id WITHOUT a
        # link, so matching on the id alone would read as covered when it is not.
        linked = f"/pipelineruns/{quote(requested, safe='')}?"
        reached = any(linked in line for line in lines)
        header += (
            f"\n  run {requested} is a CHILD; its family root is "
            f"{root.pipeline_name} (runId={root.run_id}), started by {_invoked_text(root)}"
        )
        if not reached:
            header += f" — {_run_link(sub, rg, name, requested)}"
    total = sum(stats.values())
    counts = ", ".join(f"{n} {status}" for status, n in sorted(stats.items()))
    summary = f"\n  family: {total} pipeline run(s) — {counts or 'none'}"
    if budget["truncated"]:
        summary += " (partial — run budget reached, counts are lower bounds)"
    # No link on the summary: the root run is the tree's first line and is
    # already linked there, so a second copy of the same URL would only add
    # length. Every other member is linked on its own node.
    return header + ":\n" + "\n".join(lines) + summary


_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def _model_field(obj, wire_key: str):
    """Read ``wire_key`` off an SDK model or a wire dict, whichever ``obj`` is.

    azure-mgmt-datafactory 9.2.0 deserializes activities into TYPED msrest models
    (ExecutePipelineActivity, ForEachActivity, ...) which have no dict access,
    expose snake_case attributes, and flatten typeProperties onto the activity
    itself. Raw wire dicts — the offline fake, and anything read back through
    as_dict — keep the camelCase spelling instead. Accept either.
    """
    if obj is None:
        return None
    snake = _CAMEL_BOUNDARY.sub("_", wire_key).lower()
    if hasattr(obj, "get"):
        for key in (wire_key, snake):
            value = obj.get(key)
            if value is not None:
                return value
        return None
    value = getattr(obj, snake, None)
    if value is not None:
        return value
    # 9.2.0 deserializes any activity whose 'type' is outside its discriminator
    # map into the BASE Activity class — DatabricksJob today, plus whatever ADF
    # ships after this SDK generation. Those models have none of the typed
    # attributes; the whole typeProperties blob is parked in
    # additional_properties instead, so without this fallback a container-shaped
    # unknown activity renders as a childless leaf and its invoked pipeline
    # disappears. The discriminator value itself is consumed and discarded by the
    # deserializer (absent from additional_properties AND from as_dict), so off a
    # model such an activity can only render as '[?]' — which is precisely why
    # get_pipeline_structure reads the raw wire via _keep_wire instead. This
    # branch is the fallback for when that wire read is unavailable.
    extra = getattr(obj, "additional_properties", None)
    if isinstance(extra, dict):
        for key in (wire_key, snake):
            if extra.get(key) is not None:
                return extra[key]
    return None


def _keep_wire(response, deserialized, _headers):
    """``cls`` hook for pipelines.get that returns the untouched ARM JSON.

    Worth the indirection because 9.2.0 DISCARDS the 'type' discriminator of any
    activity it does not model (DatabricksJob today, and more as ADF ships types
    faster than this deliberately-pinned SDK learns them). That value survives
    nowhere on the model — not as an attribute, not in additional_properties,
    not in as_dict() — so the wire is the only place it can be read. Falls back
    to the model if the body will not parse as JSON.
    """
    try:
        return response.http_response.json()
    except Exception:  # noqa: BLE001 - any unreadable body falls back to the model
        return deserialized


def _wire_activities(pipeline) -> list:
    """A pipeline's activity list, accepting the wire dict or the typed model."""
    if isinstance(pipeline, dict):
        return (pipeline.get("properties") or {}).get("activities") or []
    return getattr(pipeline, "activities", None) or []


def _walk_definition(activities: list, depth: int) -> list[str]:
    """Render an activity tree from typed SDK models or raw wire dicts.

    On 9.2.0 a container's children hang off the activity itself (``.activities``,
    ``.if_true_activities``); in the wire shape they are nested under
    'typeProperties'. Look under typeProperties first, then on the activity.
    """
    lines = []
    for a in activities or []:
        name = _model_field(a, "name") or "?"
        a_type = _model_field(a, "type") or "?"
        props = _model_field(a, "typeProperties") or {}
        pipeline_ref = _model_field(props, "pipeline") or _model_field(a, "pipeline")
        ref_name = _model_field(pipeline_ref, "referenceName")
        ref = f" → invokes {ref_name}" if ref_name else ""
        lines.append(f"{'  ' * depth}- {name} [{a_type}]{ref}")
        for key in ("activities", "ifTrueActivities", "ifFalseActivities"):
            children = _model_field(props, key) or _model_field(a, key)
            if children:
                lines.extend(_walk_definition(children, depth + 1))
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
        pipeline = await client.pipelines.get(rg, name, pipeline_name, cls=_keep_wire)
    except ResourceNotFoundError:
        # A missing pipeline is an ANSWER, not a failure, and it must not read as
        # one: rendered through the generic branch below it became "ERROR fetching
        # pipeline ...", which the model treated as a dead end and stopped at —
        # skipping the documentation that may well describe this name.
        others = [a for a in _factory_aliases() if a != alias]
        hint = (
            " Also configured: " + ", ".join(_factory_label(a) for a in others) + "."
            if others
            else ""
        )
        return (
            f"[adf-agent] Pipeline '{pipeline_name}' DOES NOT EXIST in factory "
            f"{_factory_label(alias)}, so it has no structure to show. This is a "
            f"definitive answer, not a lookup failure.{hint} The knowledge base may "
            "still document this name (retired, not yet built, or in an unconfigured "
            "factory) — search it and report the two findings separately."
        )
    except Exception as exc:
        return (
            f"[adf-agent] ERROR fetching pipeline '{pipeline_name}' in factory "
            f"{_factory_label(alias)}: {_truncate(exc)}"
        )
    lines = _walk_definition(_wire_activities(pipeline), depth=1)
    if not lines:
        return (
            f"[adf-agent] Pipeline '{pipeline_name}' (factory "
            f"{_factory_label(alias)}) has no activities."
        )
    header = f"[adf-agent] Structure of '{pipeline_name}' (factory {_factory_label(alias)}):"
    return header + "\n" + "\n".join(lines)


# ============================================================================
# Business tools (read-only): discovery, recovery, runtime/ETA, SLA, triggers.
# Each computes its rule deterministically here and returns finished text — the
# model chooses the tool and presents the result, it does not do the maths.
# ============================================================================

# A matched pipeline counts as a candidate only if it ran within this window —
# the same window list_pipelines uses, so "active" cannot mean two things.
_DISCOVERY_ACTIVE_DAYS = _ACTIVE_RUN_DAYS

# Past this many candidates the answer requirement states the obligation but
# stops naming them: a roster longer than this is a second copy of the table,
# and the line it lives on has to stay readable to be obeyed.
_DISCOVERY_ROSTER_MAX = 25


@tool
async def discover_pipelines_by_source_system(
    source_system_name: str,
    view_name: str = "",
    factory: str = "",
) -> str:
    """Find every active pipeline that references a source system.

    Unless ``factory`` is explicitly supplied, this searches ALL configured ADF
    factories. Definition matching is case-insensitive and deterministic; only
    pipelines with a run in the last 30 days are returned as candidates. Matching
    but inactive definitions are counted and excluded, per the use-case rule.

    Args:
        source_system_name: Source-system text such as ``LOANSYS``.
        view_name: Optional view-name context. A matching view groups those
                   candidates first; candidates that match the source system but
                   not the view are still returned, under their own heading.
                   Never a definitive lineage mapping.
        factory: Optional alias to deliberately narrow the search to one factory.
    """
    needle = (source_system_name or "").strip()
    if not needle:
        return "[adf-agent] Please provide a source system name to search for (e.g. 'LOANSYS')."
    needle_lower = needle.lower()
    view = (view_name or "").strip()
    view_lower = view.lower()
    try:
        targets = _resolve_factory_targets(factory, all_when_empty=True)
    except _InputError as exc:
        return str(exc)

    now = datetime.now(timezone.utc)
    after = now - timedelta(days=_DISCOVERY_ACTIVE_DAYS)
    active: list[str] = []
    off_view: list[str] = []
    # The rendered rows above are pipe-delimited prose. The roster the answer
    # requirement below has to quote needs the NAMES alone, so they are kept as
    # they are found rather than parsed back out of the table.
    active_names: list[str] = []
    off_view_names: list[str] = []
    matched_definitions = 0
    inactive_count = 0
    inactive_rows: list[str] = []
    searched_definitions = 0
    incomplete = False
    errors: list[str] = []

    for alias, sub, rg, factory_name in targets:
        try:
            client = await _client(sub)
            pipeline_names = [
                p.name async for p in client.pipelines.list_by_factory(rg, factory_name)
            ]
        except Exception as exc:
            errors.append(f"{_factory_name(alias)}: could not list pipelines: {_truncate(exc)}")
            continue

        searched_definitions += len(pipeline_names)
        for pipeline_name in pipeline_names:
            try:
                definition = await client.pipelines.get(
                    rg, factory_name, pipeline_name, cls=_keep_wire
                )
            except Exception as exc:
                errors.append(
                    f"definition lookup failed for {_factory_name(alias)}/{pipeline_name}: "
                    f"{_truncate(exc)}"
                )
                continue
            blob = _serialize_pipeline_definition(definition)
            name_hit = needle_lower in pipeline_name.lower()
            if needle_lower not in blob and not name_hit:
                continue
            matched_definitions += 1
            sections = _match_evidence(definition, needle_lower)
            if name_hit and "name" not in sections:
                sections = ["name"] + sections
            sections = sections or ["definition"]
            view_hit = bool(view) and (view_lower in blob or view_lower in pipeline_name.lower())
            if view_hit:
                sections.append(f"view '{view}'")

            try:
                runs, complete = await _query_all_runs(
                    client,
                    rg,
                    factory_name,
                    _runs_params(pipeline_name, after, now),
                )
            except Exception as exc:
                errors.append(
                    f"{_factory_name(alias)}/{pipeline_name}: run-history lookup failed: "
                    f"{_truncate(exc)}"
                )
                continue
            incomplete = incomplete or not complete
            if not runs:
                inactive_count += 1
                # Deliberately unlinked: the whole point of this row is that the
                # pipeline has NO run in the window, so there is no run id to
                # open, and a factory link here would offer a 24-hour grid as
                # evidence about a 30-day absence.
                inactive_rows.append(
                    f"  - {_factory_name(alias)} | {pipeline_name} "
                    f"(matched: {', '.join(sections)})"
                )
                continue
            latest = _sort_by_start(runs)[0]
            # Built from THIS row's factory triple, never the default and never
            # targets[0]: this loop sweeps every configured factory, so the row
            # being printed is routinely from a different subscription than the
            # one a factory-less call would have resolved to. A link to the wrong
            # factory opens a real Studio page with nothing in it, which reads as
            # "this run does not exist" — the exact doubt the link exists to end.
            row = (
                f"  {_factory_name(alias)} | {pipeline_name} | {latest.status} | "
                f"{_fmt_ts(latest.run_start)} | runId={latest.run_id} | "
                f"matched: {', '.join(sections)} | "
                f"{_run_link(sub, rg, factory_name, latest.run_id)}"
            )
            # A view narrows the answer, so the source-system matches it does NOT
            # cover are split out HERE rather than left for the model to drop
            # silently: they are still active candidates for the source system,
            # and a reader who never sees them cannot tell a deliberate narrowing
            # from a missed pipeline.
            if view and not view_hit:
                off_view.append(row)
                off_view_names.append(pipeline_name)
            else:
                active.append(row)
                active_names.append(pipeline_name)

    # Name the ADF RESOURCE, never the alias: a discovery answer is only
    # checkable if the reader can find the factory in the portal, and the alias
    # is this deployment's shorthand, which matches nothing there.
    scope = (
        f"factory {_factory_label(targets[0][0])}"
        if len(targets) == 1
        else f"all {len(targets)} configured factories"
    )
    candidates = len(active) + len(off_view)
    monitor_tail: list[str] = []
    if not candidates:
        lines = [
            f"[adf-agent] No active pipeline candidate for source system '{needle}' in {scope} "
            f"(searched {searched_definitions} definition(s); active means a run in the last "
            f"{_DISCOVERY_ACTIVE_DAYS} days)."
        ]
        # Monitor only when ONE factory was searched. It is the same negative
        # claim list_pipelines makes when nothing has run, and a reader checks it
        # the same way — but a sweep has no single factory to point at, and one
        # Monitor link per configured factory is a link list, not an answer.
        # Held back to the very end: it is a footer, and the inactive rows and
        # lookup errors below it are findings, which belong above it.
        if len(targets) == 1:
            _, only_sub, only_rg, only_name = targets[0]
            monitor_tail = _monitor_footer(only_sub, only_rg, only_name)
    else:
        scope_prefix = "in" if len(targets) == 1 else "across"
        # Said on the header line, which is the one line a reader is certain to
        # take the answer's size from: a group count read as the total is how a
        # six-candidate result gets reported as two.
        split_note = ""
        if active and off_view:
            split_note = (
                f", of which {len(active)} also reference view '{view}' and "
                f"{len(off_view)} do not name it — the groups below are a "
                "SORT ORDER, not a filter, and every row in both is a candidate"
            )
        lines = [
            f"[adf-agent] Source system '{needle}' — {candidates} active candidate pipeline(s) "
            f"{scope_prefix} {scope} (last {_DISCOVERY_ACTIVE_DAYS} days){split_note}:",
            # The header now names every field the rows carry, run id included:
            # it was already one short of them, and a legend that stops before the
            # last column invites the reader to read the link as part of Evidence.
            "  Factory | Pipeline | Status | Last Run | Run Id | Evidence | Open",
        ]
        # Both headings state what the group IS. The old pair opened by negating
        # the caller's own filter ("BUT NOT VIEW ...", "set aside by the view
        # only"), which reads as a disposition already taken on the reader's
        # behalf — an invitation to drop the group rather than report it.
        if active and off_view:
            lines.append(
                f"  GROUP A — candidates that also reference view '{view}' ({len(active)}):"
            )
        lines.extend(_capped_rows(active))
        if off_view:
            lines.append(
                # Membership here is decided by one substring test: the caller's
                # view string is ABSENT from the definition. The tool never
                # extracts a view name, so a heading claiming these "populate a
                # different view" would order the model to name something the
                # search never determined.
                f"  GROUP B — candidates that reference '{needle}' without naming view "
                f"'{view}' ({len(off_view)}); report each one with the evidence its row carries:"
            )
            lines.extend(_capped_rows(off_view))
    if len(targets) > 1:
        # One legend line rather than repeating the scope per row: the reader
        # sees up front every factory the answer covers, so an absent pipeline
        # is distinguishable from an unsearched factory. Names only, no links —
        # this line answers "where did you look", not "show me a run", and every
        # factory that produced a row is already reachable from that row.
        lines.insert(
            1, "  Factories searched: " + ", ".join(_factory_label(t[0]) for t in targets)
        )
    if inactive_count:
        # Excluded from the candidate list, but still NAMED: "which LOANSYS pipelines
        # exist but are dormant?" is a real question, and a bare count cannot answer it.
        lines.append(
            f"  Excluded {inactive_count} matching definition(s) with no run in the last "
            f"{_DISCOVERY_ACTIVE_DAYS} days, as required by the active-pipeline rule."
        )
        lines.append("  MATCHED BUT INACTIVE (not candidates):")
        lines.extend(_capped_rows(inactive_rows))
    if matched_definitions == 0 and not errors:
        lines.append(
            f"  No pipeline definition referenced '{needle}' (view context: "
            f"{view or '(not supplied)'})."
        )
    if incomplete:
        lines.append(
            "  NOTE: run history could not be fully paged for at least one candidate; "
            "the active set may be incomplete."
        )
    if errors:
        lines.append("  Partial lookup errors: " + "; ".join(errors))
    lines.extend(monitor_tail)
    if candidates and (off_view or inactive_rows):
        # A split result is where rows go missing: every reader downstream, model
        # or human, sees a grouped table and reports the first group as the
        # answer. The obligation is stated HERE, last, so it is the most recent
        # thing read — a prompt rule thousands of tokens away loses to the shape
        # of the table. Only NAMES are interpolated: descriptions and annotations
        # come from pipeline definitions, and this line reads as an instruction.
        roster = ", ".join(active_names + off_view_names)
        named = f" — {roster} —" if candidates <= _DISCOVERY_ROSTER_MAX else " — every row above —"
        directive = (
            f"  ANSWER REQUIREMENT: this result is split across groups. Your answer must "
            f"name all {candidates} active candidate(s){named} each with the one-line reason "
            "it is in its group."
        )
        if inactive_count:
            directive += (
                f" Name the {inactive_count} matched-but-inactive definition(s) too, "
                "labelled NOT candidates."
            )
        lines.append(directive)
    return "\n".join(lines)


# How far back to look for the failed run (scenario B) and its reruns.
_RECOVERY_LOOKBACK_DAYS = 30


def _describe_near_misses(subsequent: list, trigger: str, params: dict) -> str:
    """Why later runs of the pipeline did not count as a recovery (for the reason line)."""
    if not subsequent:
        return " No later run of this pipeline was found at all."
    reasons = set()
    for r in subsequent:
        if _extract_trigger_name(r) != trigger:
            reasons.add("different trigger")
        elif not _exact_parameter_match(_normalized_parameters(r), params):
            reasons.add("different parameters")
    if reasons:
        return (
            f" {len(subsequent)} later run(s) exist but were excluded ("
            + ", ".join(sorted(reasons)) + ")."
        )
    return ""


# Rejected near-misses rendered in full. The aggregate reason line above gives the
# count; these rows exist so the user can SEE the later success that was turned
# down, so a handful is enough — 50 rows would bury the decision itself.
_MAX_REJECTED_ROWS = 5


def _parameter_diffs(failed_params: dict, other: dict) -> list[str]:
    """Per-key parameter differences, rendered 'Key (failed -> candidate)'.

    A changed value, a parameter the candidate DROPPED and one it ADDED all break
    the exact-match rule, so all three are named and the missing side renders as
    '(absent)'. Keys are sorted so the same mismatch always reads the same way.
    """
    diffs = []
    for key in sorted(set(failed_params) | set(other)):
        in_failed, in_other = key in failed_params, key in other
        if in_failed and in_other and failed_params[key] == other[key]:
            continue
        before = failed_params[key] if in_failed else "(absent)"
        after = other[key] if in_other else "(absent)"
        diffs.append(f"{key} ({before} -> {after})")
    return diffs


def _identity_difference(run, trigger: str, params: dict) -> str:
    """How ONE later run differs from the failed run's recovery identity.

    Names the trigger change and/or the exact parameter(s) that diverged. The
    values shown are the ones actually COMPARED — the bare invoker name from
    _extract_trigger_name and the verbatim parameter dict — so the evidence
    cannot drift from the decision.
    """
    parts = []
    other_trigger = _extract_trigger_name(run)
    if other_trigger != trigger:
        parts.append(
            f"trigger ({trigger or '(unknown / manual)'} -> "
            f"{other_trigger or '(unknown / manual)'})"
        )
    parts += _parameter_diffs(params, _normalized_parameters(run))
    return _truncate("; ".join(parts)) if parts else "(no identity difference)"


def _rejected_success_rows(
    subsequent: list, trigger: str, params: dict, sub: str, rg: str, factory_name: str
) -> list[str]:
    """Later runs that SUCCEEDED but failed the identity rule, each with its diff.

    Only successes are itemized: a run that did not succeed could never have been
    the recovery, so the aggregate near-miss count above covers it. "Different
    parameters" without the value that moved is not evidence a user can act on,
    so each rejected success is shown with its own runId, invoker and parameters
    next to the exact field that diverged. Evidence only — the Recovered / Not
    Recovered decision is made before this is called and is never changed by it.

    The factory triple is threaded in so those rows can be linked. This block is
    where a user most often disagrees with the tool ("that rerun DID fix it"),
    and the parameter diff that settles it is far easier to read in Studio than
    in a truncated one-line preview.
    """
    rejected = _sort_by_start(
        [
            r
            for r in subsequent
            if r.status == "Succeeded"
            and not (
                _extract_trigger_name(r) == trigger
                and _exact_parameter_match(_normalized_parameters(r), params)
            )
        ],
        newest_first=False,  # chronological: the first later success reads first
    )
    if not rejected:
        return []
    rows = [
        f"  REJECTED LATER SUCCESS(ES): {len(rejected)} run(s) Succeeded after the failure "
        "but did NOT meet the recovery identity rule (evidence only; the decision above "
        "stands):"
    ]
    for r in rejected[:_MAX_REJECTED_ROWS]:
        other_params = _normalized_parameters(r)
        rows.append(
            f"    - runId={r.run_id} | {_fmt_ts(r.run_start)} | "
            f"triggeredBy={_invoked_text(r)} | "
            f"parameters={_truncate(other_params) if other_params else '(none)'} | "
            f"{_run_link(sub, rg, factory_name, r.run_id)}"
        )
        rows.append(f"      differs on: {_identity_difference(r, trigger, params)}")
    if len(rejected) > _MAX_REJECTED_ROWS:
        # No link: the runs past the cap are not named, so there is no id to
        # open, and Monitor cannot search by run id — it would not lead the
        # reader to them either. Raising _MAX_REJECTED_ROWS is the real lever.
        rows.append(
            f"    … and {len(rejected) - _MAX_REJECTED_ROWS} more rejected successful "
            "run(s) not shown."
        )
    return rows


@tool
async def validate_pipeline_recovery(
    pipeline_name: str = "",
    failed_run_id: str = "",
    factory: str = "",
) -> str:
    """Decide whether a FAILED pipeline run was recovered by a later rerun.

    Recovery identity is Pipeline + Trigger + exact Parameters: a later run only
    counts if it re-ran the SAME pipeline, was started by the SAME trigger /
    invoker, used the EXACT same parameter set (no fuzzy/lowercase/added/removed
    parameters), and Succeeded. A different trigger, any changed parameter, or a
    rerun that failed again all mean NOT recovered.

    Args:
        pipeline_name: The pipeline to check. Optional if failed_run_id is given
                       (it is read from the run).
        failed_run_id: The specific failed run to validate. If omitted, the
                       latest failed run of pipeline_name in the last 30 days is
                       selected and reported.
        factory:       Optional factory alias. Leave empty for the default.
    """
    pipeline_name = (pipeline_name or "").strip()
    requested_pipeline_name = pipeline_name
    failed_run_id = (failed_run_id or "").strip()
    if not pipeline_name and not failed_run_id:
        return (
            "[adf-agent] Provide a failed_run_id, or a pipeline_name to find its latest "
            "failed run."
        )
    try:
        alias, sub, rg, name = _resolve_factory(factory)
    except _InputError as exc:
        return str(exc)
    client = await _client(sub)
    now = datetime.now(timezone.utc)

    # --- Step 1: identify the failed run and capture its recovery identity ----
    selected_note = ""
    if failed_run_id:
        try:
            failed = await client.pipeline_runs.get(rg, name, failed_run_id)
        except Exception as exc:
            # The ONE place in this tool that must not link. Unlike the tools
            # that call _locate_run, this one takes whatever factory was asked
            # for (the default, when none was), so the commonest cause of this
            # error is a run that lives in a DIFFERENT factory. A link built
            # from this factory would open a page that cannot contain the run,
            # turning "I could not fetch it" into "it does not exist".
            return (
                f"[adf-agent] ERROR fetching run '{failed_run_id}' in factory "
                f"{_factory_label(alias)}: {_truncate(exc)}"
            )
        # Past the fetch, the run is known to live HERE, so every line below
        # that names a run id links it against this factory.
        if failed.status != "Failed":
            return (
                f"[adf-agent] Cannot perform failed-run recovery validation because run "
                f"'{failed_run_id}' is {failed.status}, not Failed. "
                f"{_run_link(sub, rg, name, failed_run_id)}"
            )
        run_pipeline_name = (failed.pipeline_name or "").strip()
        if (
            requested_pipeline_name
            and run_pipeline_name
            and requested_pipeline_name != run_pipeline_name
        ):
            return (
                f"[adf-agent] INPUT MISMATCH: run '{failed_run_id}' belongs to pipeline "
                f"'{run_pipeline_name}', not the supplied pipeline "
                f"'{requested_pipeline_name}'. No recovery decision was made. "
                f"{_run_link(sub, rg, name, failed_run_id)}"
            )
        pipeline_name = run_pipeline_name or requested_pipeline_name
    else:
        after = now - timedelta(days=_RECOVERY_LOOKBACK_DAYS)
        try:
            runs, _ = await _query_all_runs(
                client, rg, name, _runs_params(pipeline_name, after, now, status="Failed")
            )
        except Exception as exc:
            return (
                f"[adf-agent] ERROR querying runs in factory {_factory_label(alias)}: "
                f"{_truncate(exc)}"
            )
        failures = _sort_by_start([r for r in runs if r.status == "Failed"])
        if not failures:
            missing = await _pipeline_missing_hint(client, rg, name, alias, pipeline_name)
            if missing:
                return missing
            return (
                f"[adf-agent] No failed run found for pipeline '{pipeline_name}' in factory "
                f"{_factory_label(alias)} in the last {_RECOVERY_LOOKBACK_DAYS} days — "
                "nothing to validate."
            )
        failed = failures[0]
        failed_run_id = failed.run_id
        if len(failures) > 1:
            selected_note = (
                f" — selected the latest of {len(failures)} failed runs in the last "
                f"{_RECOVERY_LOOKBACK_DAYS} days (run started {_fmt_ts(failed.run_start)})"
            )

    failed_trigger = _extract_trigger_name(failed)
    failed_params = _normalized_parameters(failed)
    failed_start = getattr(failed, "run_start", None)
    if failed_start is None:
        # A refusal about a run whose own timestamp is missing is the one most
        # likely to be read as a tool bug; the link is how the reader sees the
        # same gap in ADF's own UI.
        return (
            f"[adf-agent] Cannot safely validate recovery for run '{failed_run_id}' because "
            "ADF returned no run_start timestamp; subsequent runs cannot be ordered. "
            f"{_run_link(sub, rg, name, failed_run_id)}"
        )

    # --- Step 2: subsequent runs of the SAME pipeline, after the failed run ---
    step2_after = failed_start
    try:
        later_runs, complete = await _query_all_runs(
            client, rg, name, _runs_params(pipeline_name, step2_after, now)
        )
    except Exception as exc:
        return (
            f"[adf-agent] ERROR querying runs in factory {_factory_label(alias)}: "
            f"{_truncate(exc)}"
        )

    def _is_after(r) -> bool:
        rs = getattr(r, "run_start", None)
        return rs is not None and rs > failed_start

    subsequent = [
        r
        for r in later_runs
        if r.run_id != failed.run_id and r.pipeline_name == pipeline_name and _is_after(r)
    ]

    # --- Steps 3-4: same trigger AND exact parameters (both client-side) ------
    identity_matches = _sort_by_start(
        [
            r
            for r in subsequent
            if _extract_trigger_name(r) == failed_trigger
            and _exact_parameter_match(_normalized_parameters(r), failed_params)
        ],
        newest_first=False,  # chronological, so [0] is the earliest matching rerun
    )

    # --- Step 5: decide ------------------------------------------------------
    lines = [
        f"[adf-agent] Recovery validation in factory {_factory_label(alias)}{selected_note}",
        f"  Pipeline Name : {pipeline_name}",
        f"  Failed Run ID : {failed_run_id} {_run_link(sub, rg, name, failed_run_id)}",
        f"  Trigger Name  : {failed_trigger or '(unknown / manual)'}",
        f"  Parameters    : {_truncate(failed_params) if failed_params else '(none)'}",
    ]
    if getattr(failed, "run_group_id", None):
        # Never linked: a run GROUP id is not a run id. Studio's run URL takes
        # the latter, so the same-looking GUID would 404 or, worse, open some
        # unrelated run — and this line is already labelled supporting evidence.
        lines.append(f"  Run Group ID  : {failed.run_group_id}  (supporting evidence only)")

    recovered = next((r for r in identity_matches if r.status == "Succeeded"), None)
    if recovered is not None:
        lines += [
            f"  Recovery Run ID  : {recovered.run_id} "
            f"{_run_link(sub, rg, name, recovered.run_id)}",
            "  Recovery Status  : Succeeded",
            "  Recovery Decision: Recovered",
            "  Reason: a subsequent execution used the same pipeline, trigger and exact "
            "parameter set and completed successfully.",
        ]
    elif identity_matches:
        latest = identity_matches[-1]
        lines += [
            f"  Recovery Run ID  : {latest.run_id} {_run_link(sub, rg, name, latest.run_id)}",
            f"  Recovery Status  : {latest.status}",
            "  Recovery Decision: Not Recovered",
            f"  Reason: {len(identity_matches)} subsequent execution(s) matched the pipeline, "
            f"trigger and exact parameters, but none Succeeded (latest was {latest.status}).",
        ]
    else:
        # "(none)" is the literal absence of a recovery run, so there is nothing
        # to link and nothing a link could prove. Keep it bare — a link here
        # would suggest a run id that the decision says does not exist.
        lines += [
            "  Recovery Run ID  : (none)",
            "  Recovery Status  : (none)",
            "  Recovery Decision: Not Recovered",
            "  Reason: no subsequent execution matched the pipeline, trigger and exact "
            "parameter set." + _describe_near_misses(subsequent, failed_trigger, failed_params),
        ]
    if recovered is None:
        # Both Not-Recovered branches need the same evidence: the branch above
        # says WHY in aggregate, these rows say which run and which field.
        lines += _rejected_success_rows(
            subsequent, failed_trigger, failed_params, sub, rg, name
        )
    if not complete:
        lines.append(
            "  NOTE: run history could not be fully paged; a matching rerun may exist beyond "
            "the retrieved window."
        )
    return "\n".join(lines)


# How many completed executions the ETA average uses at most.
_ESTIMATE_HISTORY_RUNS = 30

# Below this many completed executions the "average" is one or two observations
# and cannot be called representative; the estimate is still shown, but labelled.
_ESTIMATE_MIN_HISTORY_RUNS = 3

# In-flight runs given their own ETA row. Usually one or two, but a pipeline with
# a stuck trigger backlog can have dozens Queued at once and every row carries a
# ~250-character run link. The "N active run(s)" count above the rows is taken
# before this cap, so the ceiling hides rows, never the number of them.
_ESTIMATE_MAX_ACTIVE_ROWS = 10

# How a finished run reads in the "nothing is running" status line. Each terminal
# state gets its own verb: "completed" must not be allowed to imply "succeeded"
# (see _TERMINAL_STATUSES), and a cancellation is neither.
_FINISHED_OUTCOMES = {
    "Succeeded": "COMPLETED successfully (Succeeded)",
    "Failed": "FINISHED with a failure (Failed)",
    "Cancelled": "was CANCELLED (Cancelled)",
}


def _finished_status(latest) -> str:
    """The idle status line, led by what the most recent run actually DID.

    "Is pipeline X running?" was answered with the absence alone — 'not currently
    running' — leaving the outcome on the Latest Run line below, where the model
    flattened it to a bare status token. Stating the negative AND the completion,
    with its end time and runtime, in ONE line makes the answer lead with the
    event that happened. Clauses the run cannot support are dropped, never
    invented, and a status outside _TERMINAL_STATUSES is reported verbatim.
    """
    status = getattr(latest, "status", None)
    outcome = _FINISHED_OUTCOMES.get(status, f"ended with ADF status {status}")
    end = getattr(latest, "run_end", None)
    took = _runtime_ms(latest)
    return (
        f"not currently running — its most recent run {outcome}"
        + (f" at {_fmt_ts(end)}" if end is not None else "")
        + (f" after {_fmt_duration(took)}" if took is not None else "")
    )


def _eta_clause(start: datetime, elapsed_ms: float, stats: dict) -> str:
    """The ETA clause for one active run — a timestamp only while it can still happen.

    ``start + average`` is a PROJECTION, and once the run has been going longer
    than the average that projection already lies in the past. Printing a past
    timestamp as "Expected End Time" asserts a finish that demonstrably did not
    happen — the run is still active — which is what makes a 33s-average estimate
    on a 4-hour run wrong rather than merely rough. Past the average the
    arithmetic stays visible (the derivation is the evidence) but is labelled
    exceeded; past the longest completed run the history is exhausted outright,
    so say that rather than imply the average still predicts anything.
    """
    avg_ms = stats["avg_ms"]
    projected = start + timedelta(milliseconds=avg_ms)
    if elapsed_ms < avg_ms:
        return f"Expected End Time ≈ {_fmt_ts(projected)}"
    beyond = (
        f"longer than every completed run in history (longest {_fmt_duration(stats['max_ms'])})"
        if elapsed_ms >= stats["max_ms"]
        else f"longer than the historical average of {_fmt_duration(avg_ms)}"
    )
    return (
        f"Expected End Time: none — ALREADY EXCEEDED: start + average projected "
        f"{_fmt_ts(projected)}, {_fmt_duration(elapsed_ms - avg_ms)} ago. The run is still "
        f"active and has been running {beyond}, so this history does not predict when it "
        "will finish: report it as running longer than usual, with no reliable ETA."
    )


@tool
async def get_pipeline_runtime_estimate(pipeline_name: str, factory: str = "") -> str:
    """Report a pipeline's current state and, if running, estimate completion.

    Finds the pipeline's latest execution(s). If one or more is currently
    running, estimates completion from the average runtime of up to the last 30
    COMPLETED executions (Succeeded/Failed/Cancelled — active runs are excluded,
    they have no final runtime yet). If a run has already been going longer than
    that average the projected end time lies in the past; it is reported as
    ALREADY EXCEEDED next to the elapsed time, never as the expected finish. If
    nothing is running, reports the latest run's outcome and that no completion
    estimate applies. A pipeline may have several concurrent active runs; each
    gets its own estimate.

    Args:
        pipeline_name: Exact pipeline name, e.g. "pl_orchestrator".
        factory:       Optional factory alias. Leave empty for the default.
    """
    pipeline_name = (pipeline_name or "").strip()
    if not pipeline_name:
        return "[adf-agent] Please provide a pipeline name (get one from list_pipelines)."
    try:
        alias, sub, rg, name = _resolve_factory(factory)
    except _InputError as exc:
        return str(exc)
    client = await _client(sub)
    now = datetime.now(timezone.utc)
    after = now - timedelta(days=_ADF_HISTORY_DAYS)
    try:
        runs, complete = await _query_all_runs(client, rg, name, _runs_params(pipeline_name, after, now))
    except Exception as exc:
        return (
            f"[adf-agent] ERROR querying runs in factory {_factory_label(alias)}: "
            f"{_truncate(exc)}"
        )
    if not runs:
        missing = await _pipeline_missing_hint(client, rg, name, alias, pipeline_name)
        if missing:
            return missing
        # Existing pipeline, no runs in the window: answer the status question
        # outright, but never let "not running" be read as "a run finished" —
        # there is no outcome to report.
        return (
            f"[adf-agent] Pipeline '{pipeline_name}' in factory {_factory_label(alias)} is not "
            f"currently running, and has no runs at all in the last {_ADF_HISTORY_DAYS} days — "
            "so there is no completed run to report and no runtime history to estimate from.\n"
            # No run id exists to link, and this is the branch a reader is most
            # likely to want to check against ADF itself, so Monitor earns its
            # place here — with the window caveat, since the claim covers 45 days
            # and the tab it opens shows one.
            + "\n".join(_monitor_footer(sub, rg, name, pipeline_name=pipeline_name))
        )

    ordered = _sort_by_start(runs)  # newest first
    active = [r for r in ordered if r.status in _ACTIVE_STATUSES]
    history = _completed_runs(ordered)[:_ESTIMATE_HISTORY_RUNS]
    stats = _runtime_stats(history)

    lines = [
        f"[adf-agent] Runtime estimate for '{pipeline_name}' in factory {_factory_label(alias)}"
    ]
    if not active:
        latest = ordered[0]
        lines += [
            f"  Current Status: {_finished_status(latest)}",
            f"  Latest Run    : runId={latest.run_id} | start={_fmt_ts(latest.run_start)} | "
            f"end={_fmt_ts(latest.run_end)} | runtime={_fmt_duration(_runtime_ms(latest))} | "
            f"{_run_link(sub, rg, name, latest.run_id)}",
            "  Completion estimate: Not applicable because the pipeline is not currently running.",
        ]
        return "\n".join(lines)

    lines.append(
        f"  Current Status: {len(active)} active run(s) — "
        + ", ".join(sorted({r.status for r in active}))
    )
    if stats is None:
        lines.append(
            f"  Historical Runs Used: 0 — no completed executions in the last "
            f"{_ADF_HISTORY_DAYS} days, so no completion estimate can be made."
        )
    else:
        shortfall = "" if stats["count"] >= _ESTIMATE_HISTORY_RUNS else (
            f" (fewer than {_ESTIMATE_HISTORY_RUNS} available — estimate is rough)"
        )
        lines += [
            f"  Historical Runs Used: {stats['count']}{shortfall}",
            f"  Historical Average Runtime: {_fmt_duration(stats['avg_ms'])}",
        ]
        if stats["count"] < _ESTIMATE_MIN_HISTORY_RUNS:
            lines.append(
                f"  NOTE: {stats['count']} completed run(s) is too small a sample to be "
                "representative — treat any estimate below as indicative, not a commitment."
            )
    for r in active[:_ESTIMATE_MAX_ACTIVE_ROWS]:
        start = getattr(r, "run_start", None)
        # Elapsed is what turns "start + average" from a prediction into a falsified
        # one, and it is the evidence for "running longer than usual", so compute it
        # once and show it whether or not there is history to compare it against.
        elapsed_ms = None if start is None else (now - _as_utc(start)).total_seconds() * 1000
        if stats is not None and elapsed_ms is not None:
            eta_txt = _eta_clause(start, elapsed_ms, stats)
        elif stats is None:
            eta_txt = "Expected End Time: unknown (no completed history)"
        else:
            eta_txt = "Expected End Time: unknown (run has no start time)"
        elapsed_txt = "" if elapsed_ms is None else f" | Elapsed={_fmt_duration(elapsed_ms)}"
        # An in-flight run is the one case where the link beats the text it
        # follows: the ETA is a projection from history, while Studio shows the
        # run's live progress — and "is it stuck?" is what this row gets asked.
        lines.append(
            f"    • Current Run ID={r.run_id} | {r.status} | "
            f"Current Start Time={_fmt_ts(start)}{elapsed_txt} | {eta_txt} | "
            f"{_run_link(sub, rg, name, r.run_id)}"
        )
    if len(active) > _ESTIMATE_MAX_ACTIVE_ROWS:
        # No link: the runs past the cap are not named, so there is no run to
        # open. The count above is the exact one.
        lines.append(
            f"    … and {len(active) - _ESTIMATE_MAX_ACTIVE_ROWS} more active run(s) not "
            f"shown (showing the first {_ESTIMATE_MAX_ACTIVE_ROWS}); this many concurrent "
            "runs usually means a trigger backlog rather than one slow execution"
        )
    if not complete:
        lines.append(
            "  NOTE: run history could not be fully paged; the average uses only the runs retrieved."
        )
    return "\n".join(lines)


def _history_limit_note(analysis_days: int) -> str:
    """The ~45-day ADF-retention caveat, when a request reaches past it."""
    return (
        f"  NOTE: ADF retains pipeline-run history for about {_ADF_HISTORY_DAYS} days, so a "
        f"{analysis_days}-day request cannot be fully covered from ADF alone — the figures "
        f"above reflect only the runs ADF still retains, not the whole requested period."
    )


def _trim_number(x) -> str:
    """'180.0' -> '180', '2.5' -> '2.5' for a tidy SLA echo."""
    return str(int(x)) if float(x).is_integer() else str(x)


def _exceedance_rate(n_exceed: int, total: int) -> str:
    """The exceedance percentage, never collapsing a nonzero count to 0%/100%.

    Rounding ``100 * n_exceed / total`` alone would print e.g. '0%' next to
    '1 / 360' (0.28% rounds to 0) or '100%' next to '359 / 360' — a rate that
    contradicts the exact count on the line above. Clamp those two boundaries to
    '<1%' / '>99%' so the rate and the count never disagree.
    """
    pct = round(100 * n_exceed / total)
    if n_exceed and pct == 0:
        return "<1%"
    if n_exceed < total and pct == 100:
        return ">99%"
    return f"{pct}%"


def _sla_assessment(avg_ms: float, sla_ms: float, n_exceed: int, total: int) -> str:
    """An evidence-based SLA verdict from the actual exceedance count, not the average alone."""
    if n_exceed == 0:
        return "no run exceeded the SLA; average runtime is within the SLA."
    pct = 100 * n_exceed / total
    frequency = "in most executions" if pct > 50 else "in a minority of executions"
    position = "below" if avg_ms <= sla_ms else "above"
    return (
        f"the pipeline exceeded the SLA {frequency} ({n_exceed} of {total}); average runtime "
        f"is {position} the SLA."
    )


@tool
async def analyze_pipeline_runtime(
    pipeline_name: str,
    analysis_days: int = 30,
    sla_minutes: float | None = None,
    factory: str = "",
) -> str:
    """Analyse a pipeline's historical runtimes and, if an SLA is given, assess it.

    Computes run count, average, minimum and maximum runtime over the last
    ``analysis_days`` of COMPLETED executions. When ``sla_minutes`` is supplied,
    also reports how many and what percentage of runs exceeded the SLA — an
    evidence-based assessment, NOT a guess from the average. This tool does NOT
    look up the SLA: the caller resolves it (from the request, or from the
    knowledge base with ai_search_tool) and passes a number here. If no SLA is
    available it reports the statistics and says SLA assessment could not be
    completed — it never invents one.

    Args:
        pipeline_name: Exact pipeline name.
        analysis_days: How many days back to analyse (default 30).
        sla_minutes:   The SLA in minutes, if known. Omit when none is available.
        factory:       Optional factory alias. Leave empty for the default.
    """
    pipeline_name = (pipeline_name or "").strip()
    if not pipeline_name:
        return "[adf-agent] Please provide a pipeline name (get one from list_pipelines)."
    analysis_days = max(1, analysis_days)
    try:
        alias, sub, rg, name = _resolve_factory(factory)
    except _InputError as exc:
        return str(exc)
    client = await _client(sub)
    now = datetime.now(timezone.utc)
    after = now - timedelta(days=analysis_days)
    try:
        runs, complete = await _query_all_runs(client, rg, name, _runs_params(pipeline_name, after, now))
    except Exception as exc:
        return (
            f"[adf-agent] ERROR querying runs in factory {_factory_label(alias)}: "
            f"{_truncate(exc)}"
        )

    # Nothing in this tool is linked, and that is deliberate: every line it
    # returns is an AGGREGATE over many runs (count, average, min, max, SLA
    # exceedances) and it never names a run id. There is no single run a link
    # could open, and Monitor's 24-hour grid is not where anyone re-derives a
    # 30-day average. get_pipeline_runtime_estimate is the tool that names runs.
    lines = [
        f"[adf-agent] Runtime analysis for '{pipeline_name}' (factory {_factory_label(alias)})",
        f"  Analysis Period: last {analysis_days} day(s)",
    ]
    over_retention = analysis_days > _ADF_HISTORY_DAYS

    if not runs:
        missing = await _pipeline_missing_hint(client, rg, name, alias, pipeline_name)
        if missing:
            return missing
        lines.append("  Runs Analysed: 0 — no runs in this period; nothing to analyse.")
        if over_retention:
            lines.append(_history_limit_note(analysis_days))
        return "\n".join(lines)

    completed = _completed_runs(runs)
    stats = _runtime_stats(completed)
    if stats is None:
        lines.append(
            f"  Runs Analysed: 0 completed ({len(runs)} run(s) found but none have finished, "
            "so there is no runtime to analyse yet)."
        )
        if over_retention:
            lines.append(_history_limit_note(analysis_days))
        return "\n".join(lines)

    lines += [
        f"  Runs Analysed: {stats['count']}",
        f"  Average Runtime: {_fmt_duration(stats['avg_ms'])}",
        f"  Minimum Runtime: {_fmt_duration(stats['min_ms'])}",
        f"  Maximum Runtime: {_fmt_duration(stats['max_ms'])}",
    ]
    if sla_minutes is None or sla_minutes <= 0:
        lines.append(
            "  SLA: not provided — SLA assessment could not be completed. Supply an SLA (from "
            "the SLA document or the user) to compare."
        )
    else:
        sla_ms = sla_minutes * 60_000
        n_exceed = sum(1 for r in completed if (_runtime_ms(r) or 0) > sla_ms)
        lines += [
            f"  SLA: {_fmt_duration(sla_ms)} ({_trim_number(sla_minutes)} min)",
            f"  Runs Exceeding SLA: {n_exceed} / {stats['count']}",
            f"  SLA Exceedance Rate: {_exceedance_rate(n_exceed, stats['count'])}",
            "  Assessment: " + _sla_assessment(stats["avg_ms"], sla_ms, n_exceed, stats["count"]),
        ]

    if over_retention:
        lines.append(_history_limit_note(analysis_days))
    if not complete:
        lines.append(
            "  NOTE: run history could not be fully paged; these statistics are based on a "
            "truncated window and are NOT exact."
        )
    return "\n".join(lines)


# --- trigger deployment validation -------------------------------------------
# Business terminology (enabled/disabled) mapped to ADF runtime states in code,
# never left to the model. ADF states: Started, Stopped, Disabled.
_TRIGGER_STATES = ("Started", "Stopped", "Disabled")
_ENABLED_SYNONYMS = frozenset({"enabled", "enable", "on", "started", "start", "active"})
_DISABLED_SYNONYMS = frozenset({"disabled", "disable", "off", "stopped", "stop", "inactive"})
# Expected enabled -> Started PASS; Stopped/Disabled FAIL.
# Expected disabled -> Stopped/Disabled PASS; Started FAIL.
_PASS_STATES = {"enabled": {"Started"}, "disabled": {"Stopped", "Disabled"}}


def _normalize_expected_state(value: str) -> str | None:
    """Map a caller's word to 'enabled'/'disabled', or None if unrecognised."""
    v = (value or "").strip().lower()
    if v in _ENABLED_SYNONYMS:
        return "enabled"
    if v in _DISABLED_SYNONYMS:
        return "disabled"
    return None


def _trigger_runtime_state(resource) -> str:
    """The ADF runtime state string from a TriggerResource ('UNKNOWN' if absent)."""
    props = getattr(resource, "properties", None)
    state = getattr(props, "runtime_state", None) if props is not None else None
    if state is None:
        state = getattr(resource, "runtime_state", None)
    if state is None:
        return "UNKNOWN"
    # SDK enums render as 'TriggerRuntimeState.STARTED' via str(); .value is 'Started'.
    return getattr(state, "value", None) or str(state)


def _trigger_result(expected: str, actual: str) -> str:
    """PASS/FAIL for an actual state against the expected business state."""
    if actual not in _TRIGGER_STATES:
        return "UNKNOWN"
    return "PASS" if actual in _PASS_STATES[expected] else "FAIL"


@tool
async def get_trigger_states(
    trigger_names: list[str],
    factory: str = "",
) -> str:
    """Read current ADF trigger states without requiring an expected state.

    Use this for questions such as "are these triggers enabled or disabled?"
    where the user has not supplied a deployment expectation. Missing triggers
    are reported individually and do not suppress the rest of the batch.
    """
    names = list(
        dict.fromkeys(
            name.strip()
            for name in (trigger_names or [])
            if name and name.strip()
        )
    )
    if not names:
        return "[adf-agent] Please provide at least one trigger name to inspect."
    try:
        alias, sub, rg, factory_name = _resolve_factory(factory)
    except _InputError as exc:
        return str(exc)
    client = await _client(sub)

    rows: list[tuple[str, str, str]] = []
    for trigger_name in names:
        try:
            resource = await client.triggers.get(rg, factory_name, trigger_name)
        except ResourceNotFoundError:
            rows.append((trigger_name, "NOT FOUND", "NOT FOUND"))
            continue
        except Exception as exc:  # noqa: BLE001 - preserve partial batch results
            rows.append((trigger_name, f"ERROR: {_truncate(exc)}", "ERROR"))
            continue
        if resource is None:
            rows.append((trigger_name, "NOT FOUND", "NOT FOUND"))
            continue
        actual = _trigger_runtime_state(resource)
        if actual == "Started":
            business = "Enabled"
        elif actual in {"Stopped", "Disabled"}:
            business = "Disabled"
        else:
            business = "Unknown"
        rows.append((trigger_name, actual, business))

    found = sum(
        1
        for _, actual, _ in rows
        if actual != "NOT FOUND" and not actual.startswith("ERROR:")
    )
    lines = [
        f"[adf-agent] Current trigger states (factory {_factory_label(alias)}) - "
        f"{found}/{len(rows)} found",
        "  Trigger | ADF State | Business State",
        *[f"  {trigger} | {actual} | {business}" for trigger, actual, business in rows],
    ]
    return "\n".join(lines)


@tool
async def validate_trigger_states(
    trigger_names: list[str],
    expected_state: str,
    factory: str = "",
) -> str:
    """Validate that ADF triggers are in an expected state (read-only).

    For each trigger, reads its ADF runtime state and checks it against the
    expected business state. This is validation ONLY — it never starts, stops or
    otherwise changes a trigger.

    Args:
        trigger_names:  The trigger names to check, e.g.
                        ["TR_CUSTOMER_LOAD", "TR_FINANCE_LOAD"].
        expected_state: "enabled" or "disabled" (deployment intent). ADF maps
                        enabled -> Started; disabled -> Stopped or Disabled.
        factory:        Optional factory alias. Leave empty for the default.

    Returns a Trigger | Expected State | Actual State | Result table. A missing
    trigger is reported on its own row without failing the others.
    """
    names = [n.strip() for n in (trigger_names or []) if n and n.strip()]
    if not names:
        return "[adf-agent] Please provide at least one trigger name to validate."
    expected = _normalize_expected_state(expected_state)
    if expected is None:
        return (
            f"[adf-agent] Unknown expected_state '{expected_state}'. Use 'enabled' (ADF "
            "Started) or 'disabled' (ADF Stopped/Disabled)."
        )
    try:
        alias, sub, rg, name = _resolve_factory(factory)
    except _InputError as exc:
        return str(exc)
    client = await _client(sub)

    rows: list[tuple[str, str, str, str]] = []
    for trig in names:
        try:
            resource = await client.triggers.get(rg, name, trig)
        except ResourceNotFoundError:
            rows.append((trig, expected, "NOT FOUND", "FAIL"))
            continue
        except Exception as exc:  # noqa: BLE001 - per-trigger error, keep going
            rows.append((trig, expected, f"ERROR: {_truncate(exc)}", "ERROR"))
            continue
        if resource is None:
            rows.append((trig, expected, "NOT FOUND", "FAIL"))
            continue
        actual = _trigger_runtime_state(resource)
        rows.append((trig, expected, actual, _trigger_result(expected, actual)))

    passed = sum(1 for *_, res in rows if res == "PASS")
    header = (
        f"[adf-agent] Trigger deployment validation (factory {_factory_label(alias)}) — expected "
        f"'{expected}': {passed}/{len(rows)} PASS"
    )
    body = ["  Trigger | Expected State | Actual State | Result"]
    body += [f"  {t} | {e} | {a} | {r}" for t, e, a, r in rows]
    return header + "\n" + "\n".join(body)


@tool
async def manage_trigger_states(
    trigger_names: list[str],
    action: str,
    execute: bool = False,
    factory: str = "",
) -> str:
    """Idempotently enable or disable ADF triggers, then verify each result.

    ``execute=False`` returns a dry-run plan. A real mutation requires an explicit
    ``execute=True`` plus both ADF write gates. Every trigger is processed and
    verified independently, so one missing or failed trigger does not hide the
    outcome of the others.
    """
    names = list(
        dict.fromkeys(
            name.strip()
            for name in (trigger_names or [])
            if name and name.strip()
        )
    )
    if not names:
        return "[adf-agent] Please provide at least one trigger name to manage."
    desired = _normalize_expected_state(action)
    if desired is None:
        return (
            f"[adf-agent] Unknown trigger action '{action}'. Use 'enable' or 'disable'."
        )
    try:
        alias, sub, rg, factory_name = _resolve_factory(factory)
    except _InputError as exc:
        return str(exc)
    if execute:
        denied = _write_guard(alias)
        if denied:
            return denied
    client = await _client(sub)
    target_adf_state = "Started" if desired == "enabled" else "Stopped"
    rows: list[tuple[str, str, str, str]] = []

    for trigger_name in names:
        try:
            resource = await client.triggers.get(rg, factory_name, trigger_name)
        except ResourceNotFoundError:
            rows.append((trigger_name, "NOT FOUND", "NOT FOUND", "NOT EXECUTED"))
            continue
        except Exception as exc:  # noqa: BLE001 - preserve per-trigger outcomes
            rows.append((trigger_name, "ERROR", "ERROR", f"ERROR: {_truncate(exc)}"))
            continue
        if resource is None:
            rows.append((trigger_name, "NOT FOUND", "NOT FOUND", "NOT EXECUTED"))
            continue

        previous = _trigger_runtime_state(resource)
        if previous in _PASS_STATES[desired]:
            rows.append((trigger_name, previous, previous, "NO ACTION REQUIRED"))
            continue
        if not execute:
            rows.append((trigger_name, previous, target_adf_state, "WOULD CHANGE"))
            continue

        try:
            if desired == "enabled":
                poller = await client.triggers.begin_start(rg, factory_name, trigger_name)
            else:
                poller = await client.triggers.begin_stop(rg, factory_name, trigger_name)
            await poller.result()
            verified = await client.triggers.get(rg, factory_name, trigger_name)
            current = _trigger_runtime_state(verified)
            result = "SUCCESS" if current in _PASS_STATES[desired] else "VERIFY FAILED"
            rows.append((trigger_name, previous, current, result))
        except Exception as exc:  # noqa: BLE001 - one trigger must not abort the batch
            try:
                verified = await client.triggers.get(rg, factory_name, trigger_name)
                current = _trigger_runtime_state(verified)
            except Exception:  # noqa: BLE001 - verification itself failed
                current = "UNKNOWN"
            rows.append(
                (trigger_name, previous, current, f"ERROR: {_truncate(exc)}")
            )

    successes = sum(
        1
        for *_, result in rows
        if result in {"SUCCESS", "NO ACTION REQUIRED"}
    )
    mode = "EXECUTED" if execute else "DRY RUN"
    lines = [
        f"[adf-agent] Trigger management {mode} (factory {_factory_label(alias)}) - requested "
        f"'{desired}'; {successes}/{len(rows)} already-correct or successful",
        "  Trigger | Previous State | Current/Planned State | Result",
        *[
            f"  {trigger} | {previous} | {current} | {result}"
            for trigger, previous, current, result in rows
        ],
    ]
    return "\n".join(lines)


_RERUN_MODES = {
    "from_failure": True,
    "from-failure": True,
    "failed_activities": True,
    "full": False,
    "all": False,
}


@tool
async def rerun_failed_pipeline(
    failed_run_id: str,
    mode: str = "from_failure",
    execute: bool = False,
    factory: str = "",
) -> str:
    """Safely create a recovery run from one exact failed pipeline run.

    The tool requires a failed run ID (not only a pipeline name), reuses that
    run's parameter set, blocks duplicate active/successful runs with the same
    pipeline and exact parameters, and defaults to dry-run. Real execution also
    requires the configured ADF write gates.
    """
    failed_run_id = (failed_run_id or "").strip()
    if not failed_run_id:
        return _RUN_ID_HELP
    normalized_mode = (mode or "from_failure").strip().lower()
    if normalized_mode not in _RERUN_MODES:
        return (
            f"[adf-agent] Unknown rerun mode '{mode}'. Use 'from_failure' or 'full'."
        )
    start_from_failure = _RERUN_MODES[normalized_mode]
    try:
        alias, sub, rg, factory_name = _resolve_factory(factory)
    except _InputError as exc:
        return str(exc)
    if execute:
        denied = _write_guard(alias)
        if denied:
            return denied
    client = await _client(sub)

    try:
        failed = await client.pipeline_runs.get(rg, factory_name, failed_run_id)
    except Exception as exc:
        # Unlinked for the same reason as in validate_pipeline_recovery: this
        # tool takes the factory it was given (the default, when none), so a
        # failed fetch usually means the run lives in ANOTHER factory, and a
        # link built here would open a page that cannot contain it.
        return (
            f"[adf-agent] ERROR fetching run '{failed_run_id}' in factory "
            f"{_factory_label(alias)}: {_truncate(exc)}"
        )
    # The fetch succeeded, so the run is in THIS factory: everything below links.
    # This tool's refusals are the ones users argue with ("it IS failed, rerun
    # it"), and the link is what settles the argument in one click.
    if failed.status != "Failed":
        return (
            f"[adf-agent] RERUN NOT STARTED: run '{failed_run_id}' is "
            f"{failed.status}, not Failed. {_run_link(sub, rg, factory_name, failed_run_id)}"
        )
    pipeline_name = (failed.pipeline_name or "").strip()
    failed_start = getattr(failed, "run_start", None)
    if not pipeline_name or failed_start is None:
        return (
            f"[adf-agent] RERUN NOT STARTED: failed run '{failed_run_id}' is missing "
            "its pipeline name or start timestamp, so it cannot be validated safely. "
            f"{_run_link(sub, rg, factory_name, failed_run_id)}"
        )
    parameters = _normalized_parameters(failed)

    now = datetime.now(timezone.utc)
    try:
        later_runs, complete = await _query_all_runs(
            client,
            rg,
            factory_name,
            _runs_params(pipeline_name, failed_start, now),
        )
    except Exception as exc:
        return f"[adf-agent] ERROR checking duplicate runs: {_truncate(exc)}"
    duplicate = next(
        (
            run
            for run in _sort_by_start(later_runs)
            if run.run_id != failed_run_id
            and getattr(run, "run_start", None) is not None
            and run.run_start > failed_start
            and _exact_parameter_match(_normalized_parameters(run), parameters)
            and run.status in (_ACTIVE_STATUSES | {"Succeeded"})
        ),
        None,
    )
    if duplicate is not None:
        # The blocking run is linked, not the failed one: the reader's question
        # here is "what is this other run and is it really the same work?", and
        # that is answered by opening IT.
        return (
            f"[adf-agent] RERUN BLOCKED: pipeline '{pipeline_name}' already has a later "
            f"{duplicate.status} run with the exact same parameters "
            f"(runId={duplicate.run_id}). "
            f"{_run_link(sub, rg, factory_name, duplicate.run_id)}"
        )
    if not complete:
        return (
            "[adf-agent] RERUN NOT STARTED: duplicate-run history could not be fully "
            "paged, so safe duplicate protection could not be completed."
        )

    mode_text = "restart from failed activities" if start_from_failure else "rerun full pipeline"
    preview = [
        f"[adf-agent] Pipeline rerun {'EXECUTION' if execute else 'DRY RUN'} "
        f"(factory {_factory_label(alias)})",
        f"  Pipeline Name : {pipeline_name}",
        f"  Failed Run ID : {failed_run_id} {_run_link(sub, rg, factory_name, failed_run_id)}",
        f"  Parameters    : {_truncate(parameters) if parameters else '(none)'}",
        f"  Mode          : {mode_text}",
    ]
    if not execute:
        preview.append(
            "  Result        : NOT EXECUTED - call again with execute=true after explicit "
            "user authorization."
        )
        return "\n".join(preview)

    # ADF reuses the referenced run's parameters when reference_pipeline_run_id
    # is supplied; the API ignores a separate parameters payload in that case.
    kwargs = {
        "reference_pipeline_run_id": failed_run_id,
        "is_recovery": True,
        "start_from_failure": start_from_failure,
    }
    try:
        response = await client.pipelines.create_run(
            rg, factory_name, pipeline_name, **kwargs
        )
    except Exception as exc:
        return "\n".join(preview + [f"  Result        : ERROR - {_truncate(exc)}"])
    new_run_id = getattr(response, "run_id", None)
    if not new_run_id:
        # No run link is possible — ADF may well have STARTED a run and merely
        # failed to return its id, and a link needs an id. This is the one
        # branch where Monitor is genuinely the right answer AND its 24-hour
        # window is no handicap: the run, if it exists, was created seconds ago.
        return "\n".join(
            preview
            + ["  Result        : ERROR - ADF returned no new run ID."]
            + _monitor_footer(sub, rg, factory_name, pipeline_name=pipeline_name)
        )
    try:
        created = await client.pipeline_runs.get(rg, factory_name, new_run_id)
        created_status = getattr(created, "status", "UNKNOWN")
    except Exception:
        created_status = "SUBMITTED (status not yet readable)"
    return "\n".join(
        preview
        + [
            f"  New Run ID    : {new_run_id} {_run_link(sub, rg, factory_name, new_run_id)}",
            f"  Initial Status: {created_status}",
            "  Result        : SUBMITTED",
            "  Follow-up     : validate recovery after the new run completes; the strict "
            "recovery rule still compares pipeline, invoker/trigger, and exact parameters.",
        ]
    )


# --- ServiceNow incident ↔ Data Factory correlation ---------------------------
# The hybrid use case: an incident says a pipeline failed and carries the moment
# it was raised, and an engineer's first ten minutes go into finding the run
# behind it BY HAND — read the ticket, pick out the name, open ADF, guess which
# factory, scroll the run list to the right hour. This does that in one call.
# Two rules are what make the result safe to hand to a reader:
#   * every pipeline name is checked against the factory's REAL pipeline list
#     before a single run is fetched. A name lifted out of prose is a CLAIM, and
#     one the factory does not have is reported as exactly that — never quietly
#     dropped, and never rendered as though the pipeline existed;
#   * runs are ranked by how close their FAILURE sits to the moment the ticket
#     was opened, and every row spells out that gap AND its direction, so the
#     reader judges the link instead of inheriting one the tool asserted.

_INCIDENT_BEFORE_HOURS = 24  # the failure almost always precedes the ticket
_INCIDENT_AFTER_HOURS = 4  # ... but a ticket can be raised mid-failure
_INCIDENT_MAX_SPAN_HOURS = 24 * _ADF_HISTORY_DAYS  # nothing older is in ADF anyway
_INCIDENT_MAX_ROWS = 8  # failed-run rows listed; the count is still exact
_INCIDENT_MAX_CARDS = 3  # full activity-level cards, closest failures first
_INCIDENT_MAX_TEXT = 20_000  # guard against a pasted attachment dump

# A pipeline run id as ADF spells it. A GUID pasted into a ticket is the
# strongest correlation signal there is — an exact run, not a time guess.
_GUID_RE = re.compile(r"\b[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\b", re.IGNORECASE)

# A token in the ticket that CLAIMS to be a pipeline. Deliberately narrow — the
# 'pl_' naming convention only — because this feeds the "named but not in any
# factory" line, and a looser pattern turns ordinary prose ('the pipeline_name
# field') into a phantom pipeline. Hyphens are allowed INSIDE the name because
# real tickets spell pipelines both ways ('pl_UC2_TriggerMismatch' and
# 'PL-EX-04-SAMPLE-LEDGER-DAILY-INGEST'), and clipping the second form at
# its first hyphen would report a pipeline nobody named. Names NOT matching the
# pattern are still found when they are real: those are matched against the
# factory's own list instead.
_CLAIMED_PIPELINE_RE = re.compile(r"\bpl[_-][A-Za-z0-9][A-Za-z0-9_-]*\b", re.IGNORECASE)


def _parse_moment(value: str, label: str) -> datetime:
    """Parse a ticket timestamp: 'YYYY-MM-DD HH:MM:SS', ISO-8601, or a bare date.

    ServiceNow hands back ``opened_at`` as "2026-09-12 14:03:22" in UTC, which
    ``fromisoformat`` reads directly; a trailing 'Z' is normalized first because
    ADF and the knowledge base both spell UTC that way. A value with no zone is
    taken as UTC — that is what ServiceNow stores, and assuming local time would
    silently shift the whole correlation window by hours.
    """
    raw = (value or "").strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        return _as_utc(datetime.fromisoformat(raw))
    except ValueError as exc:
        raise _InputError(
            f"[adf-agent] Could not read {label} '{value}'. Pass the ticket's "
            "opened/created timestamp in UTC as 'YYYY-MM-DD HH:MM:SS', e.g. "
            f"{label}='2026-09-12 14:03:22'."
        ) from exc


def _incident_span(hours_before, hours_after) -> tuple[int, int]:
    """Clamp the caller's correlation window to something ADF can answer."""

    def _hours(value, default: int, floor: int) -> int:
        try:
            hours = int(value)
        except (TypeError, ValueError):
            return default
        return max(floor, min(hours, _INCIDENT_MAX_SPAN_HOURS))

    return (
        _hours(hours_before, _INCIDENT_BEFORE_HOURS, 1),
        _hours(hours_after, _INCIDENT_AFTER_HOURS, 0),
    )


def _mentions(text: str, name: str) -> bool:
    """True when ``name`` appears in ``text`` as a whole token, any casing.

    Token boundaries are checked against ``[A-Za-z0-9_-]`` rather than ``\\b`` so
    a shorter pipeline name cannot match inside a longer one: 'pl_UC2_Trigger'
    must NOT be reported as mentioned by a ticket that says
    'pl_UC2_TriggerMismatch', and 'PL-EX-04' must not match inside
    'PL-EX-04-SAMPLE-LEDGER'.
    """
    pattern = rf"(?<![A-Za-z0-9_-]){re.escape(name)}(?![A-Za-z0-9_-])"
    return re.search(pattern, text, re.IGNORECASE) is not None


def _failure_moment(run):
    """When a run's failure actually landed: its end, or its start if it never ended."""
    return getattr(run, "run_end", None) or getattr(run, "run_start", None)


def _gap_text(moment, anchor: datetime) -> str:
    """The gap between a run and the ticket, WITH its direction.

    Direction is the whole point: a run that ended twelve minutes BEFORE the
    ticket is a candidate cause, while one that started an hour AFTER it cannot
    be — and a bare "12m" reads the same either way.
    """
    if moment is None:
        return "an unknown distance from the ticket (ADF reported no timestamp)"
    seconds = (_as_utc(moment) - anchor).total_seconds()
    if abs(seconds) < 1:
        return "at the moment the ticket was opened"
    side = "BEFORE" if seconds < 0 else "AFTER"
    return f"{_fmt_duration(abs(seconds) * 1000)} {side} the ticket was opened"


def _by_proximity(pairs: list, anchor: datetime) -> list:
    """``(alias, run)`` pairs ordered by how close each failure is to ``anchor``.

    Distinct from ``_sort_by_start``: this ranks by DISTANCE from a moment, in
    either direction, which is the question an incident asks ("what failed
    around then?") rather than "what happened most recently".
    """
    dated = [p for p in pairs if _failure_moment(p[1]) is not None]
    undated = [p for p in pairs if _failure_moment(p[1]) is None]
    dated.sort(key=lambda p: abs((_as_utc(_failure_moment(p[1])) - anchor).total_seconds()))
    return dated + undated


@tool
async def correlate_incident_with_pipeline_runs(
    incident_text: str = "",
    opened_at: str = "",
    incident_number: str = "",
    pipeline_names: str = "",
    hours_before: int = 24,
    hours_after: int = 4,
    factory: str = "",
) -> str:
    """Find the Data Factory runs behind a ServiceNow incident, ranked by time.

    The ticket→pipeline investigation in one call. Given an incident's TEXT and
    the moment it was OPENED, this (1) matches every pipeline the ticket mentions
    against the factories' real pipeline lists, (2) lists the FAILED runs of
    those pipelines around the ticket's creation time — closest failure first,
    with the gap and its direction spelled out — and (3) returns full
    activity-level detail for the closest ones, so the likely root cause is in
    the first answer instead of three tool calls later. Any run GUID pasted into
    the ticket is looked up directly as well. When the ticket names no pipeline
    this deployment has, it falls back to every failed run in the window.

    Args:
        incident_text:   The ticket's text — short description, description and
                         work notes, pasted together. Pipeline names and run
                         GUIDs are read out of it, so pass it VERBATIM; a
                         summary drops exactly the names this needs.
        opened_at:       When the ticket was opened/created, UTC, as
                         "YYYY-MM-DD HH:MM:SS" (ISO-8601 also accepted). Every
                         run is ranked against this moment — take it from the
                         ticket, never estimate it.
        incident_number: Optional ticket number (e.g. "INC0002278"), echoed back
                         so the answer names what was correlated.
        pipeline_names:  Optional comma-separated pipeline names, for when the
                         caller already knows which ones the ticket means. They
                         are validated against the factory exactly like names
                         found in the text.
        hours_before:    How far BEFORE the ticket to look (default 24).
        hours_after:     How far AFTER the ticket to look (default 4).
        factory:         Optional factory alias. Leave EMPTY to search every
                         configured factory — a ticket rarely says which one.
    """
    raw_text = incident_text or ""
    text = raw_text[:_INCIDENT_MAX_TEXT]
    incident_number = (incident_number or "").strip()
    explicit = [n.strip() for n in (pipeline_names or "").replace(";", ",").split(",") if n.strip()]
    if not text and not explicit:
        return (
            "[adf-agent] Nothing to correlate: pass the incident's text as incident_text "
            "(short description, description and work notes), or the pipeline name(s) it "
            "names as pipeline_names."
        )

    anchor = None
    if (opened_at or "").strip():
        try:
            anchor = _parse_moment(opened_at, "opened_at")
        except _InputError as exc:
            return str(exc)
    ticket_run_ids = list(dict.fromkeys(_GUID_RE.findall(text)))
    if anchor is None and not ticket_run_ids:
        return (
            "[adf-agent] Missing the ticket's opened/created timestamp. Pass it as "
            "opened_at (e.g. opened_at='2026-09-12 14:03:22', UTC) — without it there is "
            "no moment to rank runs against, and this ticket's text carries no run id to "
            "fall back on. Read it off the incident rather than estimating one."
        )

    try:
        targets = _resolve_factory_targets(factory, all_when_empty=True)
    except _InputError as exc:
        return str(exc)
    before_h, after_h = _incident_span(hours_before, hours_after)
    by_alias = {alias: (sub, rg, name) for alias, sub, rg, name in targets}

    # --- Step 1: which of the ticket's names are REAL pipelines, and where ----
    known: dict[str, list[str]] = {}
    real_names: set[str] = set()
    problems: list[str] = []
    wanted = {n.casefold() for n in explicit}
    for alias, sub, rg, name in targets:
        try:
            client = await _client(sub)
            names = [p.name async for p in client.pipelines.list_by_factory(rg, name) if p.name]
        except Exception as exc:  # noqa: BLE001 - surfaced to the model as text
            problems.append(
                f"    - could not list pipelines in factory {_factory_label(alias)}: "
                f"{_truncate(exc)} — nothing was correlated there."
            )
            continue
        real_names.update(names)
        known[alias] = sorted(n for n in names if n.casefold() in wanted or _mentions(text, n))

    real_folded = {n.casefold() for n in real_names}
    claimed = list(dict.fromkeys(_CLAIMED_PIPELINE_RE.findall(text) + explicit))
    absent = [c for c in claimed if c.casefold() not in real_folded]
    matched = [(alias, n) for alias, names in sorted(known.items()) for n in names]
    sweep = not matched

    # --- Step 2: failed runs in the window around the ticket ------------------
    after = before = None
    hits: list[tuple[str, object]] = []
    truncated = False
    if anchor is not None:
        after = anchor - timedelta(hours=before_h)
        before = anchor + timedelta(hours=after_h)
        for alias, sub, rg, name in targets:
            if alias not in known:  # its pipeline list failed above
                continue
            # A factory-wide sweep only when the ticket named nothing real
            # ANYWHERE; otherwise a factory that matched no name stays quiet
            # instead of burying the match in unrelated failures.
            queries = known[alias] or ([""] if sweep else [])
            for pipe in queries:
                try:
                    client = await _client(sub)
                    runs, complete = await _query_all_runs(
                        client, rg, name, _runs_params(pipe, after, before, "Failed")
                    )
                except Exception as exc:  # noqa: BLE001 - surfaced to the model as text
                    scope = f" for '{pipe}'" if pipe else ""
                    problems.append(
                        f"    - could not query runs in factory {_factory_label(alias)}"
                        f"{scope}: {_truncate(exc)}"
                    )
                    continue
                truncated = truncated or not complete
                hits.extend((alias, r) for r in runs if r.status == "Failed")

    ranked: list[tuple[str, object]] = []
    seen_ids: set = set()
    if anchor is not None:
        for pair in _by_proximity(hits, anchor):
            run_id = getattr(pair[1], "run_id", None)
            if run_id in seen_ids:  # two aliases can point at one ADF resource
                continue
            seen_ids.add(run_id)
            ranked.append(pair)

    # --- Step 3: render -------------------------------------------------------
    label = f"Incident {incident_number}" if incident_number else "Incident"
    out = [f"[adf-agent] {label} ↔ Data Factory correlation"]
    if anchor is not None:
        out.append(f"  ticket opened : {_fmt_ts(anchor)}")
        out.append(
            f"  search window : {_fmt_ts(after)} → {_fmt_ts(before)} "
            f"({before_h}h before / {after_h}h after the ticket)"
        )
    out.append("  factories     : " + ", ".join(_factory_label(a) for a, *_ in targets))
    if len(raw_text) > _INCIDENT_MAX_TEXT:
        # This clip moves the tool's INPUTS, not just its output: run ids and
        # pipeline names live in work notes appended at the END of a long ticket,
        # so a silent clip turns "named in the ticket" into NONE and diverts the
        # whole correlation onto the factory-wide sweep — a different answer, with
        # nothing anywhere saying the ticket was only partly read.
        out.append(
            f"  ticket text   : CLIPPED at {_INCIDENT_MAX_TEXT:,} of {len(raw_text):,} "
            "characters — pipeline names and run ids past that point were NOT read, so "
            "this correlation may be missing both. Pass the tail as incident_text, or the "
            "names directly as pipeline_names."
        )

    if matched:
        out.append("  pipelines named in the ticket AND present in Data Factory:")
        out.extend(f"    - {n} — factory {_factory_label(a)}" for a, n in matched)
    else:
        out.append(
            "  pipelines named in the ticket AND present in Data Factory: NONE — no "
            "configured factory holds a pipeline whose name appears in this ticket."
        )
    if absent:
        out.append(
            "  named in the ticket but NOT in any searched factory — NO runs were "
            "fetched for these, and none may be reported against them:"
        )
        out.extend(f"    - {c}" for c in absent[:_INCIDENT_MAX_ROWS])
        if len(absent) > _INCIDENT_MAX_ROWS:
            # The heading's whole promise is completeness, so a clipped list is
            # worse than a short one: a name that WAS checked and found absent
            # becomes indistinguishable from a name the ticket never mentioned,
            # and no count elsewhere in this answer recovers the difference.
            out.append(
                f"    … and {len(absent) - _INCIDENT_MAX_ROWS} more name(s) in the ticket "
                "that no searched factory has — the same rule covers them: NO runs were "
                "fetched, and none may be reported against them."
            )

    if anchor is None:
        out.append(
            "  time correlation: SKIPPED — no opened_at was supplied, so only the run "
            "id(s) found in the ticket text were looked up."
        )
    elif not ranked:
        scope = (
            "the pipeline(s) listed above"
            if matched
            else "ANY pipeline in any configured factory (swept, because the ticket "
            "named none this deployment has)"
        )
        out.append(
            f"  failed runs   : NONE. No run of {scope} failed in that window, so the "
            "failure this ticket describes is not in the window searched — widen it via "
            f"hours_before, or it may be outside ADF's ~{_ADF_HISTORY_DAYS}-day history."
        )
    else:
        lead = f"  failed runs   : {len(ranked)} in the window, closest failure first"
        if sweep:
            lead += " (factory-wide sweep — the ticket named no pipeline this deployment has)"
        if truncated:
            lead += " (paging guard hit — this count is a lower bound)"
        out.append(lead + ":")
        for alias, run in ranked[:_INCIDENT_MAX_ROWS]:
            # Per-row factory, looked up from the row's OWN alias. These rows are
            # merged from every configured factory and re-sorted by proximity, so
            # consecutive rows routinely come from different subscriptions — the
            # module default is wrong here by construction.
            row_sub, row_rg, row_name = by_alias[alias]
            out.append(
                f"    - runId={run.run_id} | {run.pipeline_name} | {run.status} | failed "
                f"{_gap_text(_failure_moment(run), anchor)}"
            )
            # The link rides the continuation line, which already names the
            # factory: the id and the resource it belongs to stay together, and
            # the ranked first line stays short enough to scan down.
            out.append(
                f"        start={_fmt_ts(run.run_start)} | end={_fmt_ts(run.run_end)} | "
                f"{_fmt_duration(run.duration_in_ms)} | triggeredBy={_invoked_text(run)} | "
                f"factory {_factory_label(alias)} | "
                f"{_run_link(row_sub, row_rg, row_name, run.run_id)}"
            )
        if len(ranked) > _INCIDENT_MAX_ROWS:
            # Unlinked: these runs are counted, not named. A Monitor link cannot
            # reach them either (no search by run id, 24-hour window), and there
            # is no one factory to point at in a multi-factory correlation.
            out.append(
                f"    … and {len(ranked) - _INCIDENT_MAX_ROWS} more failed run(s) in the "
                "window, further from the ticket time."
            )

    carded_ids = set()
    for alias, run in ranked[:_INCIDENT_MAX_CARDS]:
        sub, rg, name = by_alias[alias]
        carded_ids.add(getattr(run, "run_id", None))
        out.append("")
        # Linked again even though the same run appears in the ranked rows above:
        # a card is a page of activity detail, and by the time a reader is
        # reading one, the row it came from has scrolled away.
        out.append(
            f"  FULL DETAIL — run {run.run_id} ({run.pipeline_name}, factory "
            f"{_factory_label(alias)}, failed {_gap_text(_failure_moment(run), anchor)}) "
            f"{_run_link(sub, rg, name, run.run_id)}"
        )
        out.extend(_run_fields(run, "    "))
        out.extend(await _activity_block(sub, rg, name, run, "    "))
    if len(ranked) > _INCIDENT_MAX_CARDS:
        # Says un-EXPANDED, never un-examined: these runs were fetched, ranked and
        # (up to the row cap) printed above. Without the line the asymmetry between
        # three cards and eight rows reads as the rest having no error detail.
        out.append("")
        out.append(
            f"  FULL DETAIL covers only the {_INCIDENT_MAX_CARDS} failure(s) closest to the "
            f"ticket. The other {len(ranked) - _INCIDENT_MAX_CARDS} failed run(s) were "
            "ranked but NOT expanded to activity level — call get_pipeline_run_details on a "
            "run id above to see its errors."
        )

    for run_id in ticket_run_ids[:_INCIDENT_MAX_CARDS]:
        out.append("")
        if run_id in seen_ids:
            # "already listed above" was the entire message, which sent the
            # reader scrolling for the pipeline, the factory and now the link.
            # seen_ids is filled only from `ranked`, so the pair is always there.
            seen_alias, seen_run = next(
                pair for pair in ranked if getattr(pair[1], "run_id", None) == run_id
            )
            seen_sub, seen_rg, seen_name = by_alias[seen_alias]
            # Linked only if this run did NOT get a FULL DETAIL card. With a card
            # its link sits a handful of lines up, and the commonest correlation
            # of all — a ticket quoting one run id that also correlates — would
            # otherwise carry the same ~250-character URL three times: ranked row,
            # card header, and here. Past _INCIDENT_MAX_CARDS there IS no card,
            # so the only other copy is a ranked row far above and the link earns
            # its place again.
            here = (
                ""
                if run_id in carded_ids
                else f" {_run_link(seen_sub, seen_rg, seen_name, run_id)}"
            )
            out.append(
                f"  RUN ID IN THE TICKET — run {run_id} ({seen_run.pipeline_name}, factory "
                f"{_factory_label(seen_alias)}): correlated above.{here}"
            )
            continue
        for alias, sub, rg, name in targets:
            try:
                client = await _client(sub)
                run = await client.pipeline_runs.get(rg, name, run_id)
            except ResourceNotFoundError:
                continue  # not this factory's run; try the next one
            except Exception as exc:  # noqa: BLE001 - surfaced to the model as text
                # No link: this is a per-factory probe in a search that is still
                # running, and the next factory may well hold the run. A link to
                # the factory that just errored would name the wrong one.
                problems.append(
                    f"    - could not fetch run '{run_id}' in factory "
                    f"{_factory_label(alias)}: {_truncate(exc)}"
                )
                continue
            gap = f", failed {_gap_text(_failure_moment(run), anchor)}" if anchor else ""
            # Built inside the loop, from the factory the search actually landed
            # in — this is the branch that RESOLVES which factory owns a GUID
            # pasted into a ticket, so any other triple would undo that work.
            out.append(
                f"  RUN ID IN THE TICKET — run {run_id} ({run.pipeline_name}, factory "
                f"{_factory_label(alias)}, {run.status}{gap if run.status == 'Failed' else ''}) "
                f"{_run_link(sub, rg, name, run_id)}"
            )
            out.extend(_run_fields(run, "    "))
            out.extend(await _activity_block(sub, rg, name, run, "    "))
            break
        else:
            # Deliberately unlinked: every configured factory was asked and none
            # owns this id, so there is no factory a link could be built from.
            # Guessing one would manufacture the "the run does not exist" page
            # that this message is carefully NOT claiming.
            out.append(
                f"  RUN ID IN THE TICKET — {run_id}: NOT FOUND in any searched factory. "
                "Report it as a run id this deployment cannot read, not as a missing run."
            )
    if len(ticket_run_ids) > _INCIDENT_MAX_CARDS:
        # The worst of the caps: a run id the ticket itself pasted appears nowhere
        # else in this answer, so a reader cannot tell it was even seen.
        overflow = ticket_run_ids[_INCIDENT_MAX_CARDS:]
        # Past the card cap a pasted id may still have been resolved: the window
        # sweep ranks Failed runs by proximity to the ticket, not by what the
        # ticket quoted, so one of these can already be printed and linked in the
        # ranked list above. Calling that one un-checked would tell the reader to
        # downgrade a run this same answer correlated.
        correlated = [r for r in overflow if r in seen_ids]
        skipped = [r for r in overflow if r not in seen_ids]
        if correlated:
            out.append("")
            out.append(
                f"  {len(correlated)} further run id(s) pasted in the ticket are among the "
                f"ranked failures above — correlated, NOT expanded here: "
                f"{', '.join(correlated)}."
            )
        if skipped:
            # Named but deliberately unlinked — which factory owns each is
            # precisely what was not resolved, and a guessed one manufactures
            # the "run does not exist" page.
            out.append("")
            out.append(
                f"  … and {len(skipped)} more run id(s) pasted in the ticket, NOT looked up "
                f"(only the first {_INCIDENT_MAX_CARDS} are resolved by id): "
                f"{', '.join(skipped)}. Report these as un-checked, never as missing."
            )

    if problems:
        out.append("")
        out.append("  lookups that did not complete (results above are partial):")
        out.extend(problems)

    out.append("")
    out.append(
        "  NOTE: closeness in time is a LEAD, not proof. Say these runs failed near the "
        "moment the ticket was raised; never state that one CAUSED the incident unless "
        "the ticket itself names that run or pipeline."
    )
    return "\n".join(out)


# The adf-agent's tool set (see v1.core.subagents.adf).
ADF_TOOLS = [
    list_factories,
    list_pipelines,
    list_pipeline_runs,
    get_pipeline_run_details,
    get_pipeline_run_tree,
    get_pipeline_structure,
    correlate_incident_with_pipeline_runs,
    discover_pipelines_by_source_system,
    validate_pipeline_recovery,
    get_pipeline_runtime_estimate,
    analyze_pipeline_runtime,
    get_trigger_states,
    validate_trigger_states,
    manage_trigger_states,
    rerun_failed_pipeline,
]


__all__ = ["ADF_TOOLS", "close_adf_resources"]
