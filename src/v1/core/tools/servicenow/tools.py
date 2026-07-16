"""LangChain tools for the ServiceNow incident client.

These tools expose a small, stable surface area for a ServiceNow-focused
subagent:
- get a compact ticket summary
- get full ticket details
- list tickets, optionally filtered by status

The backend is the mode-switchable ``ServiceNowClient``: deterministic local
fixture data by default (mock mode), or the real ServiceNow REST API when
``SERVICENOW_MODE=real`` is configured.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Annotated, Any

from langchain_core.tools import tool
from pydantic import Field

from v1.utils.clients.servicenow import (
    ServiceNowClient,
    ServiceNowConfigurationError,
    ServiceNowError,
    _reference_display,
    _reference_value,
)

SOURCE = "servicenow"

MAX_TICKET_LIMIT = 25

# Page size when the caller omits limit. Env-tunable via SERVICENOW_DEFAULT_LIMIT
# (teammates found 25-row pages too big); clamped to [1, MAX_TICKET_LIMIT] and
# falling back to 10 on an unset or non-numeric value.
try:
    DEFAULT_TICKET_LIMIT = min(
        max(int(os.getenv("SERVICENOW_DEFAULT_LIMIT", "10")), 1), MAX_TICKET_LIMIT
    )
except ValueError:
    DEFAULT_TICKET_LIMIT = 10

# Friendly status -> ServiceNow incident state NUMERIC value. The incident
# contract serves ``state`` as a {value, display_value} reference pair, and the
# real wrapper API filters on the NUMERIC ``state`` value, not the display
# string — the display strings ('In Progress', 'On Hold', ...) do NOT match on the
# live instance, the integer codes do. So we send the integer code on the wire; the
# client's ``state`` filter (passthrough) forwards it verbatim, and the mock matcher
# matches it against the ``value`` side of the {value, display_value} pair.
# The ServiceNow State choice list (verified against the instance / State cheat-sheet) is:
# 1 New, 2 In Progress, 3 On Hold, 6 Resolved, 7 Closed, 8 Canceled (one L —
# 'Canceled' is the instance spelling).
_STATUS_TO_STATE = {
    "new": "1",
    "in_progress": "2",
    "on_hold": "3",
    "resolved": "6",
    "closed": "7",
    "canceled": "8",
}

# Friendly status -> State DISPLAY value, used only to map an incident's state
# display string back to a canonical status on the OUTPUT side (_canonical_status).
# Kept separate from _STATUS_TO_STATE (which now carries the numeric wire value) so
# the input/wire side and the output/display side each have one source of truth.
_STATUS_TO_STATE_DISPLAY = {
    "new": "New",
    "in_progress": "In Progress",
    "on_hold": "On Hold",
    "resolved": "Resolved",
    "closed": "Closed",
    "canceled": "Canceled",
}

# "open" and "closed" are not ServiceNow incident states; they are convenience
# MACROS that expand to an explicit SET of real states. This is the business
# definition of the two buckets (confirmed by the team):
#   open   -> New (1) + In Progress (2) + On Hold (3)        [still being worked]
#   closed -> Resolved (6) + Closed (7) + Cancelled (8)      [no longer being worked]
# A macro is expanded into its member states at normalize time (see
# ``normalize_status_filters``), so the existing per-state fan-out OR's them and
# ``_status_filters`` only ever receives a real numeric state.
#
# Why NOT use active=true/false: on the ServiceNow instance Resolved (state=6) is
# active=TRUE — active only flips false at Closed/Canceled. So active=true would
# pull Resolved into the OPEN bucket, but the business rule puts Resolved in the
# CLOSED bucket. Expanding to explicit states is the only way to honor the rule.
_OPEN_STATUS = "open"
_CLOSED_STATUS = "closed"
_ALL_STATUS = "all"

# Each macro -> the canonical member statuses it expands to. 'all' is every real
# state (both buckets) — the ONLY way to deliberately include closed tickets in a
# query that would otherwise default to open-only. A caller must opt INTO closed
# tickets explicitly; they are never returned by accident.
_STATUS_MACROS = {
    _OPEN_STATUS: ("new", "in_progress", "on_hold"),
    _CLOSED_STATUS: ("resolved", "closed_state", "canceled"),
    _ALL_STATUS: ("new", "in_progress", "on_hold", "resolved", "closed_state", "canceled"),
}

# ``closed_state`` is the single ServiceNow state #7 ("Closed"). It needs its own
# canonical key because the friendly word "closed" is taken by the bucket macro
# above. It maps to the same numeric/display values as state 7.
_STATUS_TO_STATE["closed_state"] = _STATUS_TO_STATE["closed"]
_STATUS_TO_STATE_DISPLAY["closed_state"] = _STATUS_TO_STATE_DISPLAY["closed"]

# Valid INPUT statuses: every real member state plus the two bucket macros. The
# bare friendly word "closed" is a MACRO (the bucket), so the single-state form is
# reachable via the alias table as "closed_state" if ever needed directly.
_REAL_STATUSES = frozenset(_STATUS_TO_STATE) - {"closed"}
VALID_STATUSES = _REAL_STATUSES | {_OPEN_STATUS, _CLOSED_STATUS, _ALL_STATUS}

_STATUS_ALIASES = {
    "cancelled": "canceled",
    "in progress": "in_progress",
    "in-progress": "in_progress",
    "on hold": "on_hold",
    "on-hold": "on_hold",
    "active": _OPEN_STATUS,
    # "closed" the bare word means the whole Closed BUCKET (Resolved+Closed+Cancelled).
    # Use "closed_state"/"closed only" to mean ONLY the single state #7.
    "closed only": "closed_state",
    "closed state": "closed_state",
}

# Inverse of _STATUS_TO_STATE_DISPLAY (state display value, lowercased -> canonical
# status), so the output side (_canonical_status) maps an incident's ``state`` display
# string ('In Progress') back to its canonical status. Built from the DISPLAY map, not
# the numeric wire map, so it round-trips through one source of truth for display names.
# NOTE: ``closed_state`` shares its display ("Closed") and numeric code ("7") with the
# plain ``closed`` key, so we exclude ``closed_state`` from the inverse map — state 7
# round-trips to the simple canonical ``closed`` that users actually see on output.
_STATE_DISPLAY_TO_STATUS = {
    display.lower(): canonical
    for canonical, display in _STATUS_TO_STATE_DISPLAY.items()
    if canonical != "closed_state"
}
# Also accept the raw numeric code -> canonical status, so _canonical_status still
# round-trips when an incident's ``state`` comes back with only its numeric ``value``
# (no display_value) — the same integer codes we now send on the wire.
_STATE_DISPLAY_TO_STATUS.update(
    {code: canonical for canonical, code in _STATUS_TO_STATE.items() if canonical != "closed_state"}
)

# Closed set of "Probable cause" choices on the ServiceNow instance. ``cause`` is an
# EXACT (case-insensitive) match against the FULL stored label — a paraphrase,
# partial word, or off-list value returns ZERO records (see the cause filter rule
# in SERVICENOW_SUBAGENT_PROMPT and SUPPORTED_FILTERS["cause"] in the client). This
# table is the code-side enforcement of that prompt rule: an off-list ``cause`` is
# rejected at the tool boundary instead of being forwarded to silently match nothing.
# Keep it in lock-step with README §3 and the client's cause docstring.
VALID_CAUSES = (
    "Action Request",
    "Code Error",
    "Data Availability",
    "Data Quality",
    "Deployment Issue",
    "Documentation Issues",
    "Education/Training",
    "False Positive",
    "Holiday",
    "Maintenance",
    "Network Cluster Issue",
    "Network or Connectivity Issue",
    "Requirements Issues",
    "Software Upgrade",
    "Subnet Issue",
    "Timing/Scheduling Issue",
)

# Lower-cased label -> canonical label, so a caller that passes 'data quality' or
# 'DATA QUALITY' is canonicalized to the stored 'Data Quality' casing. Matching is
# already case-insensitive on the wire and in mock mode; canonicalizing here keeps
# filters_applied and any logs showing the official label.
_VALID_CAUSE_BY_LOWER = {cause.lower(): cause for cause in VALID_CAUSES}

_TICKET_NUMBER_RE = re.compile(r"^(?:INC|RITM|REQ|CHG|PRB|TASK|CASE)\d{7}$", re.IGNORECASE)

_servicenow_client: ServiceNowClient | None = None
_servicenow_client_lock = asyncio.Lock()


class ServiceNowToolInputError(ValueError):
    """Raised when tool input is invalid before any ServiceNow call is made."""


async def get_servicenow_client() -> ServiceNowClient:
    """Return the shared ServiceNow client for this process.

    Async so the one-time config build resolves Key Vault secrets via the async
    SDK instead of blocking the event loop on first use. The lock makes the
    lazy init safe under concurrent first requests.
    """

    global _servicenow_client

    if _servicenow_client is None:
        async with _servicenow_client_lock:
            if _servicenow_client is None:
                _servicenow_client = await ServiceNowClient.afrom_env()

    return _servicenow_client


def validate_ticket_number(ticket_number: str) -> str:
    """Return a normalized ticket number or raise for invalid input."""

    if not isinstance(ticket_number, str):
        raise ServiceNowToolInputError("ticket_number must be a string")

    normalized = ticket_number.strip().upper()
    if not _TICKET_NUMBER_RE.fullmatch(normalized):
        raise ServiceNowToolInputError(
            "ticket_number must look like INC0001001, RITM0001001, REQ0001001, "
            "CHG0001001, PRB0001001, TASK0001001, or CASE0001001"
        )

    return normalized


def normalize_status(status: str) -> str:
    """Return a canonical status value or raise for unsupported filters."""

    if not isinstance(status, str):
        raise ServiceNowToolInputError("status filters must be strings")

    normalized = status.strip().lower().replace("_", " ")
    normalized = _STATUS_ALIASES.get(normalized, normalized.replace(" ", "_"))
    if normalized not in VALID_STATUSES:
        valid = ", ".join(sorted(VALID_STATUSES))
        raise ServiceNowToolInputError(
            f"unsupported status filter '{status}'. Valid statuses: {valid}"
        )

    return normalized


def normalize_status_filters(statuses: str | Iterable[str] | None) -> tuple[str, ...] | None:
    """Normalize optional status filters while preserving caller order."""

    if statuses is None:
        return None

    if isinstance(statuses, str):
        if not statuses.strip():
            return None
        raw_statuses = [status for status in statuses.split(",") if status.strip()]
    else:
        try:
            raw_statuses = list(statuses)
        except TypeError as exc:
            raise ServiceNowToolInputError(
                "statuses must be a string, iterable of strings, or None"
            ) from exc

    normalized: list[str] = []
    for status in raw_statuses:
        canonical = normalize_status(status)
        # Expand a bucket MACRO ('open'/'closed') into its explicit member states so
        # the per-state fan-out OR's them; a real state passes through unchanged.
        for member in _STATUS_MACROS.get(canonical, (canonical,)):
            if member not in normalized:
                normalized.append(member)

    if not normalized:
        raise ServiceNowToolInputError(
            "at least one status filter is required when statuses is provided"
        )

    return tuple(normalized)


# The closed-bucket member states (numeric-canonical). Used by the OPEN-ONLY
# guard below to detect when an expanded status set contains closed tickets.
_CLOSED_MEMBERS = frozenset(_STATUS_MACROS[_CLOSED_STATUS])

# Raw status words that mean the caller DELIBERATELY wants closed-bucket tickets:
# an explicit state word ('resolved'/'closed'/'cancelled'/'closed_state'), the
# 'closed' bucket macro, or the 'all' macro. Per the team rule, 'all' = EVERY state
# (open + closed), so it opts into the closed bucket. 'open' is the only macro that
# does NOT — omitting statuses or passing 'open' stays open-only.
_EXPLICIT_CLOSED_WORDS = frozenset(
    {"resolved", "closed", "closed_state", "canceled", "cancelled", _CLOSED_STATUS, _ALL_STATUS}
)


def caller_opted_into_closed(statuses: str | Iterable[str] | None) -> bool:
    """True when the RAW caller input names an explicit closed-state word or 'all'.

    This is the single rule that guarantees open-only by default: a query gets
    closed-bucket tickets ONLY when the caller asked for a closed state ('resolved',
    'closed', 'cancelled', the 'closed' bucket) or the 'all' macro, which the team
    defines as EVERY state (open + closed). Omitting statuses or passing 'open' stays
    open-only — 'open' does NOT opt into closed.
    """

    if statuses is None:
        return False
    if isinstance(statuses, str):
        raw = [s for s in statuses.split(",") if s.strip()]
    else:
        try:
            raw = list(statuses)
        except TypeError:
            return False
    for status in raw:
        word = str(status).strip().lower()
        # Resolve through the alias table so 'cancelled'/'closed state' etc. count.
        word = _STATUS_ALIASES.get(word, word)
        if word in _EXPLICIT_CLOSED_WORDS:
            return True
    return False


def normalize_cause(cause: str) -> str:
    """Resolve a (possibly partial) cause term to its canonical ``VALID_CAUSES`` label.

    The ServiceNow instance matches ``cause`` EXACTLY against the FULL stored label — it
    has no substring/``LIKE`` operator for this field, so a partial term sent to the
    wire matches nothing (a false "none found"). End users, however, rarely type the
    full label; they say "subnet" or "network cluster". So we resolve the loose term
    to the full label HERE, against the in-code closed set, and return that exact
    label for the wire. This makes partial input work in BOTH live and mock mode,
    because the API/matcher always receives a complete, valid label.

    Resolution order:
      1. Exact (case-insensitive) label -> canonical casing. ('data quality')
      2. Partial: AND-of-tokens against the closed set — every whitespace-separated
         token of the term must appear as a substring of a label. A UNIQUE match is
         resolved to that full label. ('subnet' -> 'Subnet Issue';
         'network cluster' -> 'Network Cluster Issue').
      3. Ambiguous (a term whose tokens match 2+ labels, e.g. bare 'network' ->
         'Network Cluster Issue' AND 'Network or Connectivity Issue'): raise and list
         the candidates so the caller picks one exactly — never silently query the
         wrong cause.
      4. No match at all ('banana'): raise, listing the full valid set, and point the
         caller at ``close_notes_contains`` / detail-read as the prompt instructs.
    """

    if not isinstance(cause, str):
        raise ServiceNowToolInputError("cause must be a string")

    cleaned = cause.strip()

    # 1. Exact match (case-insensitive) -> canonical casing.
    canonical = _VALID_CAUSE_BY_LOWER.get(cleaned.lower())
    if canonical is not None:
        return canonical

    # 2. Partial: every token the user supplied must appear in the label.
    tokens = cleaned.lower().split()
    if tokens:
        matches = [
            label
            for label in VALID_CAUSES
            if all(token in label.lower() for token in tokens)
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            # 3. Ambiguous — make the caller disambiguate rather than guess.
            raise ServiceNowToolInputError(
                f"ambiguous cause '{cause}' matches multiple labels: "
                f"{', '.join(matches)}. Pass one of these exactly."
            )

    # 4. No exact or partial match anywhere in the closed set.
    valid = ", ".join(VALID_CAUSES)
    raise ServiceNowToolInputError(
        f"unsupported cause '{cause}'. cause is matched against a closed set; pass a "
        f"full or partial form of one of: {valid}. If you only know a loose term that "
        f"isn't in this set, drop the cause filter and use close_notes_contains (or "
        f"read cause back from ticket detail)."
    )


def validate_ticket_limit(limit: int | None) -> int:
    """Validate a ticket list count limit; values above the default are CLAMPED.

    HARD ENFORCEMENT: page size is deployment-controlled, never model-controlled.
    The model kept passing limit=25 on default-shaped queries despite the prompt's
    "OMIT limit" instruction, so a prompt-level gate is not enough — any requested
    limit above the env-configured default (SERVICENOW_DEFAULT_LIMIT, normally 10)
    is silently clamped down to it. Raise the env var to widen pages; page with
    offset (single-state queries) to see more. The only path allowed to exceed the
    default is the explicit ticket_numbers batch, which sizes itself to the count
    (capped at MAX_TICKET_LIMIT) AFTER this clamp.
    """

    if limit is None:
        return DEFAULT_TICKET_LIMIT

    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ServiceNowToolInputError("limit must be an integer")

    if limit < 1:
        raise ServiceNowToolInputError("limit must be at least 1")

    return min(limit, DEFAULT_TICKET_LIMIT)


def resolve_ticket_limit(*, limit: int | None = None, count: int | None = None) -> int:
    """Resolve supported limit/count aliases into one validated value."""

    if limit is not None and count is not None and limit != count:
        raise ServiceNowToolInputError("limit and count cannot disagree")

    return validate_ticket_limit(count if limit is None else limit)


def validate_offset(offset: int | None) -> int:
    """Validate a pagination offset; default 0."""

    if offset is None:
        return 0

    if isinstance(offset, bool) or not isinstance(offset, int):
        raise ServiceNowToolInputError("offset must be an integer")

    if offset < 0:
        raise ServiceNowToolInputError("offset must be 0 or greater")

    return offset


def _canonical_status(state_display: str) -> str:
    """Map a state display value like 'In Progress' to its canonical 'in_progress'.

    Driven by the inverse of ``_STATUS_TO_STATE`` so it stays in lock-step with
    ``normalize_status``; alias resolution and a slug fallback keep it from
    diverging (or raising) on server states outside the known set.
    """

    text = state_display.strip().lower()
    canonical = _STATE_DISPLAY_TO_STATUS.get(text)
    if canonical is not None:
        return canonical
    slug = text.replace("-", " ")
    return _STATUS_ALIASES.get(slug, slug.replace(" ", "_"))


def _utc_timestamp(raw: Any) -> str | None:
    """Render a ServiceNow incident timestamp with an explicit ``UTC`` marker.

    ServiceNow serves incident timestamps (opened_at, closed_at, resolved_at,
    sys_updated_on, ...) in UTC, but the bare ``YYYY-MM-DD HH:MM:SS`` string
    carries no zone — so neither the model nor the user can tell which timezone
    it is. We append a ``UTC`` suffix on the OUTPUT side so the timezone travels
    with the value into the answer (e.g. ``'2026-05-10 17:00:00 UTC'``). The
    suffix is display-only: filtering/date math reads the raw incident fields,
    never this normalized value. Returns ``None`` for an empty/missing timestamp
    (rendered 'Not available' downstream) and never double-appends when a ``UTC``
    marker is already present.
    """

    text = _reference_value(raw).strip()
    if not text:
        return None
    if text.upper().endswith("UTC"):
        return text
    return f"{text} UTC"


def _error_payload(exc: Exception, *, kind: str = "servicenow_error") -> dict[str, Any]:
    return {
        "ok": False,
        "source": SOURCE,
        "kind": kind,
        "error": str(exc),
    }


def _not_found_payload(ticket_number: str, *, degraded: bool = False) -> dict[str, Any]:
    error = f"ticket {ticket_number} was not found"
    if degraded:
        error += (
            " (live ServiceNow lookup failed; only the local fallback dataset "
            "was searched, so the ticket may still exist)"
        )
    return {
        "ok": False,
        "source": SOURCE,
        "kind": "ticket_not_found",
        "error": error,
        "degraded": degraded,
    }


async def _fetch_incident(
    client: ServiceNowClient, ticket_number: str
) -> tuple[Mapping[str, Any] | None, dict[str, Any]]:
    """Fetch one incident plus its envelope so mode/degraded stay visible.

    We list with a ``number`` filter rather than collapsing to a single incident
    so the envelope's mode/degraded flags survive — otherwise real-mode fallback
    data could masquerade as live data.
    """

    envelope = await client.list_incidents(
        filters={"number": ticket_number}, limit=1
    )
    incidents = envelope.get("incidents") or []
    return (incidents[0] if incidents else None), envelope


def _ticket_base(incident: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ticket_number": _reference_value(incident.get("number")).upper(),
        "short_description": _reference_value(incident.get("short_description")),
        "status": _canonical_status(_reference_value(incident.get("state"))),
        "priority": _reference_value(incident.get("priority")),
        "category": _reference_value(incident.get("category")),
        # assignment_group is a reference field whose raw value is a sys_id — show
        # the DISPLAY name only, never the sys_id (empty -> 'Not available' on render).
        "assignment_group": _reference_display(incident.get("assignment_group")),
        "configuration_item": _reference_value(incident.get("configuration_item")),
        # Surface the root-cause keyword on every list/summary row (not just on
        # the detail fetch) so the subagent can post-filter a list by cause type
        # — pipeline-infra vs PII/config error, "timeout" vs "vendor outage" —
        # without a per-ticket detail call. The field is a short keyword
        # ('Source timeout', 'Authentication issue', ...), so this is cheap.
        "cause": _reference_value(incident.get("cause")),
        # Engineer who worked the ticket: prefer resolved_by, fall back to
        # assigned_to (README §6 #3) so the list path can answer "who worked X"
        # without a per-result detail fetch. DISPLAY name only — these are people
        # reference fields whose raw value is a sys_id, which must never leak to the
        # user; an empty display falls through to '' (rendered 'Not available').
        "engineer": (
            _reference_display(incident.get("resolved_by"))
            or _reference_display(incident.get("assigned_to"))
        ),
        # UTC-labeled: ServiceNow serves these in UTC; mark them so the user sees the
        # zone. opened_at rides the compact row too (it is on every record anyway) so
        # "when was it opened" never renders 'Not available' off a detail=False row.
        # Live QA records can OMIT opened_at while carrying sys_created_on (same
        # instant — record creation IS the open time), so fall back to it.
        "opened_at": (
            _utc_timestamp(incident.get("opened_at"))
            or _utc_timestamp(incident.get("sys_created_on"))
        ),
        "updated_at": _utc_timestamp(incident.get("sys_updated_on")),
        # Deep link to the incident, constructed sys_id-based by the client from the
        # instance origin (the API returns no usable link). The sys_id lives ONLY
        # inside this URL — it is never surfaced as its own field. Present on every
        # summary/list/detail row so the agent can always hand back a clickable link.
        "ticket_url": _reference_value(incident.get("ticket_url")) or None,
    }


def _ticket_detail_fields(incident: Mapping[str, Any]) -> dict[str, Any]:
    """The card fields ticket DETAIL adds on top of the compact ``_ticket_base`` row.

    Factored out of ``normalize_ticket_detail`` so ``servicenow_list_tickets`` can
    return the SAME complete card on EVERY row in a SINGLE call (``detail=True``).
    This is the fix for the per-ticket fan-out latency: ``servicenow_get_ticket_detail``
    is itself just a number-filtered ``list_incidents`` call, so the list endpoint
    already returns every field below on each record — the compact list normalizer
    merely discarded them, forcing the agent to re-fetch each ticket one by one
    (N+1 network round-trips for what one list call already had). Surfacing them
    here costs ZERO extra ServiceNow requests.
    """

    return {
        "description": _reference_value(incident.get("description")),
        # People reference fields: DISPLAY name only — never the raw sys_id
        # value (empty -> '' so the output layer renders 'Not available').
        "assigned_to": _reference_display(incident.get("assigned_to")),
        "resolved_by": _reference_display(incident.get("resolved_by")),
        # UTC-labeled: ServiceNow serves these in UTC; mark the zone on output.
        # (opened_at already rides the compact _ticket_base row.)
        "resolved_at": _utc_timestamp(incident.get("resolved_at")),
        "closed_at": _utc_timestamp(incident.get("closed_at")),
        "cause": _reference_value(incident.get("cause")),
        # Resolution text: read close_notes, FALLING BACK to the record's
        # ``resolution_notes`` key. The live wrapper serves the field as
        # ``close_notes``, but the bundled mock fixture stored it as
        # ``resolution_notes`` — the fallback keeps the resolution populated in BOTH
        # modes (without it, mock-mode cards and missing-data/cluster classification
        # evidence would come back blank).
        "close_notes": (
            _reference_value(incident.get("close_notes"))
            or _reference_value(incident.get("resolution_notes"))
        ),
        "close_code": _reference_value(incident.get("close_code")),
        # ticket_url is already set by _ticket_base (sys_id-based deep link).
    }


def normalize_ticket_detail(
    incident: Mapping[str, Any],
    *,
    mode: str | None = None,
    degraded: bool = False,
) -> dict[str, Any]:
    """Normalize a raw incident payload for full ticket details."""

    ticket = _ticket_base(incident)
    ticket.update(_ticket_detail_fields(incident))

    return {
        "ok": True,
        "source": SOURCE,
        "kind": "ticket_detail",
        "ticket": ticket,
        "mode": mode,
        "degraded": degraded,
    }


def normalize_ticket_list(
    incidents: Iterable[Mapping[str, Any]],
    *,
    statuses: tuple[str, ...] | None,
    limit: int,
    offset: int = 0,
    mode: str | None = None,
    degraded: bool = False,
    has_more: bool = False,
    filters: Mapping[str, Any] | None = None,
    detail: bool = True,
) -> dict[str, Any]:
    """Normalize merged incident results with validated filter metadata.

    When ``detail`` is True (the default) every row carries the COMPLETE incident
    card — the same fields ``servicenow_get_ticket_detail`` returns (long
    description, resolution/close notes, opened_at, closed_at, resolved_by,
    assigned_to, close_code) — so the agent can render cards and classify the whole
    result set from this ONE call, with NO per-ticket detail fetch. ``detail=False``
    keeps the compact row (number/status/priority/...) for lightweight scans.
    """

    if detail:
        tickets = [
            {**_ticket_base(incident), **_ticket_detail_fields(incident)}
            for incident in incidents
        ]
    else:
        tickets = [_ticket_base(incident) for incident in incidents]

    # A multi-status query fans out one API call per state and merges them; a single
    # shared offset can't honestly index those independent result sets (and is rejected
    # upstream), so there is NO usable next-page cursor. Emit next_offset only for a
    # SINGLE state — null tells the agent "not pageable; narrow to one state to page".
    # (A bare call defaults to the OPEN bucket = 3 states, so it is multi-status too;
    # statuses=None here is only the direct ticket_numbers fetch, where paging is moot.)
    pageable = statuses is not None and len(statuses) == 1
    return {
        "ok": True,
        "source": SOURCE,
        "kind": "ticket_list",
        "count": len(tickets),
        "limit": limit,
        "offset": offset,
        "next_offset": offset + len(tickets) if pageable else None,
        "status_filter": list(statuses or []),
        "filters_applied": dict(filters or {}),
        "detail": detail,
        "tickets": tickets,
        "mode": mode,
        "degraded": degraded,
        "has_more": has_more,
    }


def _status_filters(status: str) -> dict[str, Any]:
    # Macros ('open'/'closed') are expanded to member states in
    # normalize_status_filters, so by here ``status`` is always a real state whose
    # NUMERIC code goes on the wire. (We no longer use active=true for 'open' — it
    # wrongly includes Resolved; see the _STATUS_MACROS note.)
    return {"state": _STATUS_TO_STATE[status]}


_DATE_BOUND_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d")


def _validate_date_bound(name: str, value: str) -> str:
    normalized = value.strip()
    for fmt in _DATE_BOUND_FORMATS:
        try:
            datetime.strptime(normalized, fmt)
        except ValueError:
            continue
        return normalized
    # Reject impossible calendar dates / times (e.g. 2026-13-45, 99:99:99) that a
    # digit-shape check would wave through into the query.
    raise ServiceNowToolInputError(
        f"{name} must be a valid 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS', got '{value}'"
    )


def _build_field_filters(
    *,
    description_contains: str | None,
    short_description_contains: str | None,
    close_notes_contains: str | None,
    cause: str | None,
    assigned_to: str | None,
    resolved_by: str | None,
    assigned_to_name: str | None,
    resolved_by_name: str | None,
    assignment_group: str | None,
    priority: str | None,
    created_after: str | None,
    created_before: str | None,
    updated_after: str | None,
    updated_before: str | None,
    ticket_numbers: str | None,
) -> dict[str, Any]:
    """Map tool arguments onto the filter keys the ServiceNow client supports.

    Only the README §3.2 supported set is mapped here. ``category`` and
    ``opened_*`` are intentionally NOT filters (category is an output field used
    for agent-side classification; date windows use ``created_*`` / ``updated_*``).
    """

    # The wrapper API ANDs filters, so assigned_to + resolved_by in ONE call means
    # "assigned to X AND resolved by Y" — for a single engineer that is almost always
    # empty (the assignee is rarely also the resolver). To find every ticket a person
    # worked, the agent must query assigned_to and resolved_by in SEPARATE calls and
    # union. Reject the combined form rather than silently returning zero.
    def _present(value: str | None) -> bool:
        return value is not None and bool(value.strip())

    if (_present(assigned_to) or _present(assigned_to_name)) and (
        _present(resolved_by) or _present(resolved_by_name)
    ):
        raise ServiceNowToolInputError(
            "assigned_to and resolved_by cannot be combined in one query (the API ANDs "
            "them, so it returns ~0 records). To find every ticket a person worked, run "
            "TWO separate searches — one with assigned_to, one with resolved_by — and "
            "union the results (dedupe by number)."
        )

    filters: dict[str, Any] = {}

    substring_filters = {
        "description_contains": description_contains,
        "short_description_contains": short_description_contains,
        "close_notes_contains": close_notes_contains,
        "assigned_to": assigned_to,
        "resolved_by": resolved_by,
        "assigned_to_name": assigned_to_name,
        "resolved_by_name": resolved_by_name,
        "assignment_group": assignment_group,
        "priority": priority,
    }
    for key, value in substring_filters.items():
        if value is not None and value.strip():
            filters[key] = value.strip()

    # cause is exact-match against the closed VALID_CAUSES set: canonicalize the
    # casing and reject an off-list value here rather than forwarding it to match
    # nothing on the wire (a false "none found").
    if cause is not None and cause.strip():
        filters["cause"] = normalize_cause(cause)

    date_bounds = {
        "created_after": created_after,
        "created_before": created_before,
        "updated_after": updated_after,
        "updated_before": updated_before,
    }
    for key, value in date_bounds.items():
        if value is not None and value.strip():
            filters[key] = _validate_date_bound(key, value)

    if ticket_numbers is not None and ticket_numbers.strip():
        numbers = [
            validate_ticket_number(part)
            for part in ticket_numbers.split(",")
            if part.strip()
        ]
        if numbers:
            filters["number"] = ",".join(numbers)

    return filters


@tool
async def servicenow_get_ticket_detail(
    ticket_number: Annotated[
        str,
        Field(
            description=(
                "ServiceNow incident number in the form INC followed by seven "
                "digits, e.g. INC3011201."
            )
        ),
    ],
) -> dict[str, Any]:
    """Get full normalized details for one ServiceNow ticket. If you have enough information from the list endpoint for the user query, you should not use this tool to save a network round-trip."""

    try:
        normalized_number = validate_ticket_number(ticket_number)
        incident, envelope = await _fetch_incident(
            await get_servicenow_client(), normalized_number
        )
        if incident is None:
            return _not_found_payload(
                normalized_number, degraded=bool(envelope.get("degraded"))
            )
        return normalize_ticket_detail(
            incident,
            mode=envelope.get("mode"),
            degraded=bool(envelope.get("degraded")),
        )
    except ServiceNowToolInputError as exc:
        return _error_payload(exc, kind="invalid_input")
    except (ServiceNowError, ServiceNowConfigurationError) as exc:
        return _error_payload(exc)


@tool
async def servicenow_list_tickets(
    statuses: Annotated[
        str | None,
        Field(
            description=(
                "Optional comma-separated status filters. Supported values: "
                "new, in_progress, on_hold, resolved, canceled, plus three BUCKET "
                "macros: 'open', 'closed', and 'all'. Aliases like 'in progress' and "
                "'on hold' are accepted. "
                "'open' expands to New + In Progress + On Hold (tickets still being "
                "worked). 'closed' expands to Resolved + Closed + Cancelled (tickets "
                "no longer being worked). Note Resolved is in the CLOSED bucket, not "
                "open. "
                "'all' expands to EVERY state (open + closed). GATE: pass 'all' (or any "
                "closed word) ONLY when the user's own words ask for it — 'all'/'every' "
                "incident, closed/resolved/cancelled, history, or a past time window. A "
                "topical ask ('incidents related to / for <X>') is NOT such a signal: "
                "OMIT this argument. "
                "SAFE DEFAULT: if you OMIT this argument the tool returns OPEN tickets "
                "only — closed/resolved/cancelled tickets are NEVER returned unless you "
                "ask for them with an explicit closed word: 'all', 'closed', "
                "'open,closed', 'resolved', or 'canceled'. To include resolved/closed "
                "tickets (historical or engineer-worked-on questions), pass 'all' (every "
                "state), 'open,closed', or 'closed' for history only. So for 'what's "
                "broken now' / related-incident questions you can omit it (or pass "
                "'open'). Pass individual states (e.g. 'new,in_progress') for finer "
                "control; use 'closed only' for just the single Closed state without "
                "Resolved/Cancelled. A user who names ONE specific state ('resolved "
                "incidents', 'cancelled tickets') gets EXACTLY that state — pass "
                "statuses='resolved' alone, NOT the 'closed' bucket; single-state "
                "queries are also the only ones that paginate."
            )
        ),
    ] = None,
    description_contains: Annotated[
        str | None,
        Field(
            description=(
                "Case-insensitive substring matched against the ticket description "
                "(the long description names the data source / business segment, "
                "e.g. 'transaction ledger' or 'Core Banking'). Use this for general "
                "free-text and data-source searches. Pass a plain keyword or key "
                "nouns — NO % wildcards or quotes (matching is automatic; multi-word "
                "values match as AND-of-words, not an exact phrase)."
            )
        ),
    ] = None,
    short_description_contains: Annotated[
        str | None,
        Field(
            description=(
                "Case-insensitive substring matched against the ticket TITLE only "
                "(short_description). Titles are TERSE — data source names and detail "
                "live in the long description, so prefer description_contains as the "
                "primary content filter. When the ask names TWO different keywords (a "
                "system/pipeline/tool term AND a data source), SPLIT them: put the "
                "system/tool term HERE and the data source in description_contains — "
                "never cram both into description_contains as one AND-of-words value. "
                "Plain keyword, no % wildcards; multi-word matches AND-of-words."
            )
        ),
    ] = None,
    close_notes_contains: Annotated[
        str | None,
        Field(
            description=(
                "Case-insensitive substring matched against the close notes — the "
                "record of how a (closed) incident was resolved. Use this to find "
                "how a similar issue was fixed and for cluster evidence. Plain "
                "keyword, no % wildcards."
            )
        ),
    ] = None,
    cause: Annotated[
        str | None,
        Field(
            description=(
                "Match on the cause field against this CLOSED set (the 'Probable cause' "
                "choices): 'Action Request', 'Code Error', 'Data Availability', 'Data "
                "Quality', 'Deployment Issue', 'Documentation Issues', 'Education/Training', "
                "'False Positive', 'Holiday', 'Maintenance', 'Network Cluster Issue', "
                "'Network or Connectivity Issue', 'Requirements Issues', 'Software Upgrade', "
                "'Subnet Issue', 'Timing/Scheduling Issue'. You may pass a FULL label or a "
                "PARTIAL form of one — a partial term is resolved to the full label by "
                "AND-of-tokens (every word you give must appear in the label), so 'subnet' "
                "-> 'Subnet Issue' and 'network cluster' -> 'Network Cluster Issue'. A term "
                "that matches MULTIPLE labels (e.g. bare 'network') is rejected as ambiguous "
                "with the candidates listed — narrow it. A term matching NONE (e.g. "
                "'timeout', 'banana') is rejected; for a loose term not in this set, drop "
                "cause and use close_notes_contains instead (the cause is usually echoed in "
                "the close notes). Plain keyword, no % wildcards."
            )
        ),
    ] = None,
    assigned_to: Annotated[
        str | None,
        Field(
            description=(
                "ServiceNow user CODE of the assignee, e.g. 'D7834' — NOT a sys_id "
                "and NOT the display name (a sys_id returns 0 records). PREFERRED over "
                "assigned_to_name whenever you have the code (extract it from a "
                "'Name (CODE)' string). Do NOT also set resolved_by in the same call — "
                "the API ANDs them and returns ~0; to find everything a person worked, "
                "query assigned_to and resolved_by in SEPARATE calls and union."
            )
        ),
    ] = None,
    resolved_by: Annotated[
        str | None,
        Field(
            description=(
                "ServiceNow user CODE of the resolver, e.g. 'D7834' (same rules as "
                "assigned_to: code only, never a sys_id; preferred over "
                "resolved_by_name). Do NOT also set assigned_to in the same call — the "
                "API ANDs them and returns ~0; query the two in SEPARATE calls and union."
            )
        ),
    ] = None,
    assigned_to_name: Annotated[
        str | None,
        Field(
            description=(
                "EXACT full name of the assignee INCLUDING the user-ID code in "
                "parentheses, e.g. 'Dhanalakshmi Sundharam (D7834)'. The code is "
                "REQUIRED: a bare name without the parenthesized code returns ZERO on "
                "the live instance, and partial names never match. If you do not have "
                "the user's code, do NOT guess it or pass a bare name — ask the user "
                "for their user ID, or read it from a ticket they worked via "
                "servicenow_get_ticket_detail, and reuse the full 'Name (CODE)' "
                "verbatim. Note: the assigned_to_name field comes back empty in the "
                "response body even when this filter matches, so read the assigned_to "
                "display value to confirm."
            )
        ),
    ] = None,
    resolved_by_name: Annotated[
        str | None,
        Field(
            description=(
                "EXACT full name of the resolver INCLUDING the user-ID code in "
                "parentheses, e.g. 'Dhanalakshmi Sundharam (D7834)'. The code is "
                "REQUIRED and partial/bare names return ZERO (same rule as "
                "assigned_to_name) — if you lack the code, ask the user for their user "
                "ID or read it from a ticket they worked, do not guess."
            )
        ),
    ] = None,
    assignment_group: Annotated[
        str | None,
        Field(
            description=(
                "Assignment group name (substring) or sys_id; comma-separated to "
                "match any of several groups."
            )
        ),
    ] = None,
    priority: Annotated[
        str | None,
        Field(
            description=(
                "Priority as a bare integer 1-4 (1 = highest). A display form like "
                "'1 - Critical' is reduced to its leading integer."
            )
        ),
    ] = None,
    created_after: Annotated[
        str | None,
        Field(description="Only tickets created on/after this moment: 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS'."),
    ] = None,
    created_before: Annotated[
        str | None,
        Field(description="Only tickets created on/before this moment: 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS'."),
    ] = None,
    updated_after: Annotated[
        str | None,
        Field(description="Only tickets updated on/after this moment: 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS'."),
    ] = None,
    updated_before: Annotated[
        str | None,
        Field(description="Only tickets updated on/before this moment: 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS'."),
    ] = None,
    ticket_numbers: Annotated[
        str | None,
        Field(
            description=(
                "Comma-separated list of specific ticket numbers to fetch in ONE "
                "call, e.g. 'INC3011201,INC3185010'. ALWAYS use this for two or more "
                "numbers instead of calling servicenow_get_ticket_detail per ticket. It "
                "returns every named incident regardless of status (no status filter is "
                "applied), so closed/resolved ones come back too; the limit is sized to "
                "the count automatically (up to the backend max of 25 per call)."
            )
        ),
    ] = None,
    limit: Annotated[
        int | None,
        Field(
            description=(
                "Maximum tickets to return. OMIT it for a normal list (the backend "
                "default applies). Values ABOVE the backend default are CLAMPED down "
                "to it — passing a big limit does nothing; to see more results, page "
                "with offset (single-state queries only) or narrow the filters. Pass "
                "a value only to request FEWER rows than the default."
            )
        ),
    ] = None,
    count: Annotated[
        int | None,
        Field(description="Alias for limit. Do not pass both unless they match. Values above the backend default are clamped to it."),
    ] = None,
    offset: Annotated[
        int | None,
        Field(
            description=(
                "Pagination start, 0-based (defaults to 0). To page, advance by "
                "limit and read next_offset from the result. Not supported together "
                "with multiple statuses — paginate one status at a time."
            )
        ),
    ] = None,
    detail: Annotated[
        bool,
        Field(
            description=(
                "Whether each matched row carries the COMPLETE incident card (long "
                "description, resolution/close notes, opened_at, closed_at, "
                "resolved_by/assigned_to, close_code — everything "
                "servicenow_get_ticket_detail returns) in this ONE call. Defaults to "
                "true. Set it FALSE for a plain multi-incident list/display: the compact "
                "row (number, short description, state, priority, engineer, ticket_url) "
                "is all a one-line list row needs and is far lighter on tokens. Set it "
                "TRUE only when you must READ cause/description/close_notes to CLASSIFY "
                "rows (pipeline vs missing-data vs cluster) or will render FULL cards (a "
                "single incident or a full-details request) — then the single list "
                "result is self-sufficient and you need NO per-ticket "
                "servicenow_get_ticket_detail calls."
            )
        ),
    ] = True,
) -> dict[str, Any]:
    """List normalized ServiceNow tickets filtered by status, any content field,
    people, dates, priority, or specific ticket numbers. All filters combine
    with AND semantics (statuses combine with OR among themselves).

    With detail=True (default) each row is the FULL incident card, so a single
    call answers classify/summarize/full-detail questions without any per-ticket
    servicenow_get_ticket_detail fan-out. For a plain multi-incident list/display,
    pass detail=False for a lighter compact row (one concise line per incident)."""

    try:
        normalized_statuses = normalize_status_filters(statuses)
        # SAFE DEFAULT: when the caller passes NO status, default to the OPEN bucket
        # (New + In Progress + On Hold) rather than every status.
        if normalized_statuses is None:
            normalized_statuses = _STATUS_MACROS[_OPEN_STATUS]
        # OPEN-ONLY GUARD: closed-bucket tickets are returned ONLY when the caller named
        # an explicit closed-state word ('resolved'/'closed'/'cancelled'/the 'closed'
        # bucket) or the 'all' macro (team rule: 'all' = every state). Otherwise strip
        # the closed members so the query stays open-only. ponytail: with every route to
        # a closed member now triggering caller_opted_into_closed, this is a defensive
        # backstop — it only bites a future status set that carries closed members
        # without a recognized closed/all word.
        elif not caller_opted_into_closed(statuses):
            open_only = tuple(s for s in normalized_statuses if s not in _CLOSED_MEMBERS)
            # Only narrow if something open remains; a deliberate single closed-state
            # query (which would set caller_opted_into_closed True) never reaches here.
            if open_only:
                normalized_statuses = open_only
        normalized_limit = resolve_ticket_limit(limit=limit, count=count)
        normalized_offset = validate_offset(offset)
        # Explicit ticket numbers are a DIRECT fetch by name: status is irrelevant (the
        # caller wants each named incident regardless of its state, exactly like
        # servicenow_get_ticket_detail and the raw API), so bypass the status fan-out —
        # ONE call returns them all, closed ones included, instead of N per-ticket
        # lookups. Size the limit to the count (capped at the backend max) so a batch is
        # never truncated by the default limit.
        requested_numbers = (
            [p.strip() for p in ticket_numbers.split(",") if p.strip()]
            if ticket_numbers
            else []
        )
        if requested_numbers:
            normalized_statuses = None
            normalized_limit = min(
                max(normalized_limit, len(requested_numbers)), MAX_TICKET_LIMIT
            )
        # A single shared offset cannot be fanned across per-status queries
        # honestly (each status has its own result set), so reject it rather than
        # silently re-returning page 1.
        if normalized_offset and normalized_statuses is not None and len(normalized_statuses) > 1:
            raise ServiceNowToolInputError(
                "offset is not supported with multiple statuses; paginate one "
                "status at a time"
            )
        field_filters = _build_field_filters(
            description_contains=description_contains,
            short_description_contains=short_description_contains,
            close_notes_contains=close_notes_contains,
            cause=cause,
            assigned_to=assigned_to,
            resolved_by=resolved_by,
            assigned_to_name=assigned_to_name,
            resolved_by_name=resolved_by_name,
            assignment_group=assignment_group,
            priority=priority,
            created_after=created_after,
            created_before=created_before,
            updated_after=updated_after,
            updated_before=updated_before,
            ticket_numbers=ticket_numbers,
        )
        client = await get_servicenow_client()

        # The wrapper API (incident_list_api_prefix) accepts ONE state value per
        # call — it is not ServiceNow's native sysparm_query, so there is no
        # multi-value state / stateIN form to collapse this into. Hence we query
        # each requested status and merge, de-duplicating by ticket number. The
        # per-status fan-out visible in LangSmith is therefore expected, NOT a
        # bug: the gather() below makes wall-clock = the slowest single call, not
        # the sum. Field filters apply to every per-status query (AND semantics).
        if normalized_statuses is None:
            envelopes = [
                await client.list_incidents(
                    filters=field_filters or None,
                    limit=normalized_limit,
                    offset=normalized_offset,
                )
            ]
        elif len(normalized_statuses) == 1:
            envelopes = [
                await client.list_incidents(
                    filters={**field_filters, **_status_filters(normalized_statuses[0])},
                    limit=normalized_limit,
                    offset=normalized_offset,
                )
            ]
        else:
            # Fan the per-status queries out concurrently — a shared OAuth token
            # and one AsyncClient make this safe, and asyncio.gather preserves
            # caller order, so latency is the slowest call instead of their sum.
            envelopes = list(
                await asyncio.gather(
                    *(
                        client.list_incidents(
                            filters={**field_filters, **_status_filters(status)},
                            limit=normalized_limit,
                        )
                        for status in normalized_statuses
                    )
                )
            )

        degraded = False
        mode: str | None = None
        has_more = False
        groups: list[list[Mapping[str, Any]]] = []
        for envelope in envelopes:
            degraded = degraded or bool(envelope.get("degraded"))
            mode = mode or envelope.get("mode")
            has_more = has_more or bool(envelope.get("has_more"))
            groups.append(list(envelope.get("incidents", [])))

        # Interleave round-robin across the per-status results so one populous
        # status cannot starve the others out of the shared limit.
        merged: list[Mapping[str, Any]] = []
        seen: set[str] = set()
        for rank in range(max((len(group) for group in groups), default=0)):
            for group in groups:
                if rank >= len(group):
                    continue
                incident = group[rank]
                number = _reference_value(incident.get("number")).upper()
                if number in seen:
                    continue
                seen.add(number)
                merged.append(incident)

        # STATE BACKSTOP: drop any row whose state was not requested. The per-status
        # fan-out already sends a state filter per call, but if the live wrapper ever
        # ignores/mishandles it, closed rows would silently ride an open-only query.
        # Enforce the contract locally so that can never reach the user. (None =
        # direct ticket_numbers fetch — status is deliberately irrelevant there.)
        if normalized_statuses is not None:
            allowed = {
                "closed" if s == "closed_state" else s for s in normalized_statuses
            }
            merged = [
                i
                for i in merged
                if _canonical_status(_reference_value(i.get("state"))) in allowed
            ]

        return normalize_ticket_list(
            merged[:normalized_limit],
            statuses=normalized_statuses,
            limit=normalized_limit,
            offset=normalized_offset,
            mode=mode,
            degraded=degraded,
            has_more=has_more or len(merged) > normalized_limit,
            filters=field_filters,
            detail=detail,
        )
    except ServiceNowToolInputError as exc:
        return _error_payload(exc, kind="invalid_input")
    except (ServiceNowError, ServiceNowConfigurationError) as exc:
        return _error_payload(exc)


SERVICENOW_TOOLS = [
    servicenow_get_ticket_detail,
    servicenow_list_tickets,
]


async def close_servicenow_resources() -> None:
    global _servicenow_client

    if _servicenow_client is not None:
        await _servicenow_client.aclose()
        _servicenow_client = None


__all__ = [
    "SERVICENOW_TOOLS",
    "VALID_CAUSES",
    "ServiceNowToolInputError",
    "close_servicenow_resources",
    "get_servicenow_client",
    "normalize_cause",
    "normalize_status",
    "normalize_status_filters",
    "normalize_ticket_detail",
    "normalize_ticket_list",
    "resolve_ticket_limit",
    "servicenow_get_ticket_detail",
    "servicenow_list_tickets",
    "validate_ticket_number",
]
