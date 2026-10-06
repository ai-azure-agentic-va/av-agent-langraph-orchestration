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
import functools
import os
import re
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Annotated, Any

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from pydantic import Field

from v1.utils.clients.servicenow import (
    SUPPORTED_FILTERS,
    ServiceNowClient,
    ServiceNowConfigurationError,
    ServiceNowError,
    _reference_display,
    _reference_value,
)

SOURCE = "servicenow"

MAX_TICKET_LIMIT = 25

# Rows per call when a mode reads EVERY page (classified lists, newest_first, the
# similar-incident search). Never shown to the user: a bigger page only means fewer
# round trips. The live wrapper accepts up to 100; 50 is the team's setting for now.
_EXHAUST_PAGE_SIZE = 50

# A one-subject search also reads titles (a subject named only in the title never
# matches the description) while its description and title matches together stay
# under this many rows. Both sets are re-read in full on every page.
_TITLE_UNION_MAX_ROWS = 300

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
#   closed -> Resolved (6) + Closed (7)                      [no longer being worked]
# Cancelled (8) is in NEITHER bucket and in no macro: a search never returns it.
# A macro is expanded into its member states at normalize time (see
# ``normalize_status_filters``), so ``_status_filters`` only ever receives real
# states, which go out as ONE comma list of numeric codes.
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
# Stakeholder rule: Cancelled (state 8) is NEVER surfaced by a search. End users do
# not act on cancelled tickets, so 'closed' means Resolved + Closed and 'all' means
# the FIVE live states (1,2,3,6,7) — there is no status word that puts state 8 in a
# list. (A Cancelled ticket the user names by NUMBER still reads back fine; this rule
# is about what a SEARCH returns, which is why the display map below keeps state 8.)
_STATUS_MACROS = {
    _OPEN_STATUS: ("new", "in_progress", "on_hold"),
    _CLOSED_STATUS: ("resolved", "closed_state"),
    _ALL_STATUS: ("new", "in_progress", "on_hold", "resolved", "closed_state"),
}

# ``closed_state`` is the single ServiceNow state #7 ("Closed"). It needs its own
# canonical key because the friendly word "closed" is taken by the bucket macro
# above. It maps to the same numeric/display values as state 7.
_STATUS_TO_STATE["closed_state"] = _STATUS_TO_STATE["closed"]
_STATUS_TO_STATE_DISPLAY["closed_state"] = _STATUS_TO_STATE_DISPLAY["closed"]

# Valid INPUT statuses: every real member state plus the two bucket macros. The
# bare friendly word "closed" is a MACRO (the bucket), so the single-state form is
# reachable via the alias table as "closed_state" if ever needed directly.
# "canceled" is deliberately NOT an input status — rejecting it here is what makes
# state 8 unreachable from a search, whatever a prompt or a future caller asks for.
_REAL_STATUSES = frozenset(_STATUS_TO_STATE) - {"closed", "canceled"}
VALID_STATUSES = _REAL_STATUSES | {_OPEN_STATUS, _CLOSED_STATUS, _ALL_STATUS}

_STATUS_ALIASES = {
    "cancelled": "canceled",
    "in progress": "in_progress",
    "in-progress": "in_progress",
    "on hold": "on_hold",
    "on-hold": "on_hold",
    "active": _OPEN_STATUS,
    # "closed" the bare word means the whole Closed BUCKET (Resolved+Closed only —
    # Cancelled is never included). Use "closed_state"/"closed only" to mean ONLY
    # the single state #7.
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
    if normalized == "canceled":
        # Named explicitly rather than falling into the generic "unsupported" message:
        # without this the agent sees a validation error, retries with another status
        # and presents live tickets as if they were the cancelled ones.
        raise ServiceNowToolInputError(
            "cancelled (state 8) incidents are not available from search. Tell the user "
            "cancelled tickets are not surfaced; never substitute another status for them."
        )
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
        # the comma-list state filter OR's them; a real state passes through unchanged.
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
    offset to see more. The only path allowed to exceed the
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


# Separator between a cursor's OFFSET part and the ticket numbers already shown.
# The suffix exists because the wrapper only offers offset/limit paging over a LIVE
# result set: when an incident is created (or re-ordered) between two page fetches,
# every later record shifts down one slot, so the next offset re-serves a row the
# previous page already displayed. Carrying the last page's numbers lets the merge
# skip them. ponytail: only the PREVIOUS page is carried, so a shift larger than one
# page can still duplicate — the real fix is keyset paging (order by a stable key and
# page from the last one seen), which needs a sort/after param the wrapper lacks.
_CURSOR_SEEN_SEP = "|"


def split_cursor(offset: int | str | None) -> tuple[int | str | None, frozenset[str]]:
    """Split a cursor into its offset part and the ticket numbers already shown.

    ``'10|INC1,INC2'`` -> ``('10', {'INC1', 'INC2'})``; the offset text is returned
    untouched so :func:`parse_offset` keeps its contract.
    """

    if not isinstance(offset, str) or _CURSOR_SEEN_SEP not in offset:
        return offset, frozenset()
    head, _, tail = offset.partition(_CURSOR_SEEN_SEP)
    seen = {part.strip().upper() for part in tail.split(",") if part.strip()}
    return head.strip(), frozenset(seen)


def parse_offset(offset: int | str | None) -> int:
    """Validate a pagination offset: an int, or its digit string (the cursor's head).

    Every query is ONE result set now (several states go out as one comma list), so
    one record offset indexes it honestly.
    """

    if isinstance(offset, str):
        text = offset.strip()
        if not text:
            return 0
        if not text.isdigit():
            raise ServiceNowToolInputError(
                "offset must be an integer or the exact next_offset value from the "
                "previous result"
            )
        return validate_offset(int(text))

    return validate_offset(offset)


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


def _incident_timestamp(raw: Any) -> str | None:
    """Render a ServiceNow incident timestamp as a BARE ``YYYY-MM-DD HH:MM:SS``.

    ServiceNow serves incident timestamps (opened_at, closed_at, resolved_at,
    sys_updated_on, ...) in UTC. We deliberately emit NO timezone label: the UI
    converts the value to the viewer's own local zone, so a 'UTC' suffix riding
    the string is simply WRONG next to a converted clock value — and it survives
    conversion, because it is literal text inside the model's prose rather than a
    parsed field. This function is the single place that decides, so any marker a
    record happens to carry is stripped here rather than in three prompt layers.
    Filtering/date math reads the raw incident fields, never this display value.
    Returns ``None`` for an empty/missing timestamp ('Not available' downstream).
    """

    text = _reference_value(raw).strip()
    if not text:
        return None
    if text.upper().endswith("UTC"):
        text = text[:-3].strip()
    return text or None


def _created_sort_key(incident: Mapping[str, Any]) -> str:
    """The incident's creation instant, as a key that sorts chronologically.

    The wrapper exposes no sort/order param, so "most recent first" can only be
    honoured on our side — which is why ``newest_first`` exhausts every page
    before sorting: ordering one arbitrary page would answer "newest of these 10"
    while claiming to answer "newest overall".

    Both stored forms are zero-padded ``YYYY-MM-DD HH:MM:SS``, so a plain string
    compare IS the chronological one and no parsing (or timezone guessing) is
    needed. Same field pair as the row's ``opened_at``: live QA records can omit
    ``opened_at`` while carrying ``sys_created_on``. A record with neither sorts
    LAST under reverse=True rather than posing as the newest.
    """

    return (
        _incident_timestamp(incident.get("opened_at"))
        or _incident_timestamp(incident.get("sys_created_on"))
        or ""
    )


# A scheme-prefixed URI, up to the first whitespace. Backticks are excluded from
# both ends so an already-fenced URI is left alone rather than double-wrapped.
_URI_RE = re.compile(r"(?<!`)\b[a-z][a-z0-9+.\-]*://[^\s`]+", re.IGNORECASE)


def _shield_uris(text: str) -> str:
    """Fence a raw URI in backticks so the renderer cannot HALF-linkify it.

    Incident text carries storage URIs like
    ``abfss://container@host.dfs.core.windows.net/Path/``. Markdown
    auto-linkers match the email-shaped middle (``container@host…net``) and wrap
    only THAT fragment in an anchor, leaving the scheme and the path outside it —
    the user sees one URI broken into a blue piece and two black pieces, and the
    link goes nowhere. A code span suppresses auto-linking entirely, which is the
    right default here: these schemes (abfss/wasbs/adl/sftp) are not browser
    -openable, so a whole-URI hyperlink would be just as dead. Whole link or no
    link — never a partial one.
    """

    def fence(match: re.Match[str]) -> str:
        # Sentence punctuation trailing the URI belongs to the prose, not the URI.
        uri = match.group(0).rstrip(".,;:!?)")
        return f"`{uri}`{match.group(0)[len(uri):]}"

    return _URI_RE.sub(fence, text)


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


def _list_row(
    ticket: Mapping[str, Any], state_display: str, assignee: str, resolver: str
) -> str:
    """Render the one-line LIST ROW the agent prints verbatim.

    Built here rather than described to the model in prose. The shape used to live
    as an English recipe in the subagent prompt AND in the message-formatting skill,
    and the two drifted: the CI segment was added to one copy while the other still
    declared the field set closed at State/Priority/Assigned to, so CI rendered
    intermittently depending on which instruction the model weighted. One writer,
    one shape, no re-derivation per answer.
    """

    number = ticket["ticket_number"]
    url = ticket["ticket_url"]
    priority = ticket["priority"]
    # ServiceNow serves priority as '1 - Critical'; the row prefixes the rank with P.
    if priority[:1].isdigit():
        priority = f"P{priority}"
    segments = [f"[{number}]({url})" if url else number]
    if ticket["short_description"]:
        segments.append(ticket["short_description"])
    for label, value in (
        ("State", state_display),
        ("Priority", priority),
        ("Assigned to", assignee),
        # The resolver only when it is someone else. This slot used to print the
        # resolver-first ``engineer`` under "Assigned to", so a ticket assigned to a
        # person and closed by an alert bot listed the BOT as its assignee.
        ("Resolved by", resolver if resolver != assignee else ""),
        ("CI", ticket["configuration_item"]),
    ):
        # An empty segment is DROPPED, never padded: 'Not available' on a list row
        # is exactly what the closed-field-set rule forbids.
        if value:
            segments.append(f"**{label}:** {value}")
    return " — ".join(segments)


def _ticket_base(incident: Mapping[str, Any]) -> dict[str, Any]:
    assignee = _reference_display(incident.get("assigned_to"))
    resolver = _reference_display(incident.get("resolved_by"))
    ticket = {
        "ticket_number": _reference_value(incident.get("number")).upper(),
        "short_description": _shield_uris(
            _reference_value(incident.get("short_description"))
        ),
        "status": _canonical_status(_reference_value(incident.get("state"))),
        "priority": _reference_value(incident.get("priority")),
        "category": _reference_value(incident.get("category")),
        # assignment_group is a reference field whose raw value is a sys_id — show
        # the DISPLAY name only, never the sys_id (empty -> 'Not available' on render).
        "assignment_group": _reference_display(incident.get("assignment_group")),
        # CI — the live wrapper serves it as ``cmdb_ci`` (the bundled mock fixture
        # uses ``configuration_item``), so read both or real mode renders it blank.
        # Its display value carries the FULL pipeline/application name (e.g.
        # 'PL-100-EXAMPLE_COPY', 'Databricks', 'ASL'), which is how a
        # "pipeline incidents" ask is classified agent-side. It is OUTPUT ONLY: the
        # instance's ``ci`` param is exact-match on the whole name, so a partial
        # value returns zero — never filter on it.
        "configuration_item": (
            _reference_value(incident.get("cmdb_ci"))
            or _reference_value(incident.get("configuration_item"))
        ),
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
        "engineer": resolver or assignee,
        # Bare date+time, NO zone label — the UI converts to the viewer's local zone,
        # so a 'UTC' suffix would survive the conversion and mislabel the result.
        # opened_at rides the compact row too (it is on every record anyway) so
        # "when was it opened" never renders 'Not available' off a detail=False row.
        # Live QA records can OMIT opened_at while carrying sys_created_on (same
        # instant — record creation IS the open time), so fall back to it.
        "opened_at": (
            _incident_timestamp(incident.get("opened_at"))
            or _incident_timestamp(incident.get("sys_created_on"))
        ),
        "updated_at": _incident_timestamp(incident.get("sys_updated_on")),
        # Deep link to the incident, constructed sys_id-based by the client from the
        # instance origin (the API returns no usable link). The sys_id lives ONLY
        # inside this URL — it is never surfaced as its own field. Present on every
        # summary/list/detail row so the agent can always hand back a clickable link.
        "ticket_url": _reference_value(incident.get("ticket_url")) or None,
    }
    # The finished LIST ROW rides every row so the agent prints instead of composing.
    # ``status`` above is the canonical slug ('in_progress'); the row wants the
    # server's own display value ('In Progress'), so read it straight off the record.
    ticket["row"] = _list_row(
        ticket, _reference_value(incident.get("state")), assignee, resolver
    )
    return ticket


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
        # Free text — fence any raw URI so the renderer cannot half-linkify it.
        "description": _shield_uris(_reference_value(incident.get("description"))),
        # People reference fields: DISPLAY name only — never the raw sys_id
        # value (empty -> '' so the output layer renders 'Not available').
        # caller = who REPORTED the ticket. It was filterable but never returned, so
        # "who opened INC…?" had to be answered from assigned_to — the wrong person.
        "caller": _reference_display(incident.get("caller_id")),
        "assigned_to": _reference_display(incident.get("assigned_to")),
        "resolved_by": _reference_display(incident.get("resolved_by")),
        # Bare date+time, NO zone label (see _incident_timestamp — the UI localizes).
        # (opened_at already rides the compact _ticket_base row.)
        "resolved_at": _incident_timestamp(incident.get("resolved_at")),
        "closed_at": _incident_timestamp(incident.get("closed_at")),
        "cause": _reference_value(incident.get("cause")),
        # Resolution text: read close_notes, FALLING BACK to the record's
        # ``resolution_notes`` key. The live wrapper serves the field as
        # ``close_notes``, but the bundled mock fixture stored it as
        # ``resolution_notes`` — the fallback keeps the resolution populated in BOTH
        # modes (without it, mock-mode cards and missing-data/cluster classification
        # evidence would come back blank).
        "close_notes": _shield_uris(
            _reference_value(incident.get("close_notes"))
            or _reference_value(incident.get("resolution_notes"))
        ),
        "close_code": _reference_value(incident.get("close_code")),
        # ticket_url is already set by _ticket_base (sys_id-based deep link).
    }


def _summary_card(ticket: Mapping[str, Any], state_display: str) -> str:
    """Render the SUMMARY view a single-incident answer prints verbatim.

    Same reasoning as ``_list_row``, one layer up. This shape was an English recipe
    in THREE places — the subagent prompt, the message-formatting skill and the
    orchestrator — and the skill copy sits behind an OPTIONAL ``read_file``, so
    whether the model ever saw the spec varied per run. Four runs of one
    "summarize INC…" produced four different layouts: differing field sets, a raw
    ``in_progress`` slug in one, and in another the incident number as BOLD TEXT
    with no link at all. A recipe re-derived per answer drifts; a rendered string
    cannot.
    """

    number = ticket["ticket_number"]
    url = ticket["ticket_url"]
    head = f"[{number}]({url})" if url else number
    if ticket["short_description"]:
        head = f"{head} — {ticket['short_description']}"
    # Owner for open work, resolver once it is resolved/closed.
    if ticket.get("resolved_by"):
        owner = ("Resolved by", ticket["resolved_by"])
    else:
        owner = ("Assigned to", ticket.get("assigned_to") or ticket["engineer"])
    # The FULL CARD's field order, minus Description / Resolution notes (the
    # model's plain-language paragraph replaces both) and minus Close code.
    fields = (
        ("Priority", ticket["priority"]),
        ("State", state_display),
        ("Category", ticket["category"]),
        ("Assignment group", ticket["assignment_group"]),
        ("Reported by", ticket.get("caller")),
        owner,
        ("Cause", ticket["cause"]),
        ("Opened at", ticket["opened_at"]),
        ("Resolved at", ticket.get("resolved_at")),
        ("Closed at", ticket.get("closed_at")),
        ("Configuration item", ticket["configuration_item"]),
    )
    # Empty fields are DROPPED, never padded — 'Not available' rows belong only to
    # the FULL CARD view. An open incident simply has no Resolved at / Closed at.
    return "\n".join(
        [head, *(f"- **{label}:** {value}" for label, value in fields if value)]
    )


def normalize_ticket_detail(
    incident: Mapping[str, Any],
    *,
    mode: str | None = None,
    degraded: bool = False,
) -> dict[str, Any]:
    """Normalize a raw incident payload for full ticket details."""

    ticket = _ticket_base(incident)
    ticket.update(_ticket_detail_fields(incident))
    # The finished SUMMARY rides the card so the agent prints instead of composing.
    # ``status`` is the canonical slug ('in_progress'); the card wants the server's
    # own display value ('In Progress'), so read it straight off the record.
    ticket["summary"] = _summary_card(ticket, _reference_value(incident.get("state")))

    return {
        "ok": True,
        "source": SOURCE,
        "kind": "ticket_detail",
        "ticket": ticket,
        "mode": mode,
        "degraded": degraded,
    }


_SYS_ID_RE = re.compile(r"[0-9a-fA-F]{32}")


def _list_header(
    total_count: int | None,
    shown: int,
    has_more: bool,
    shown_before: int = 0,
    group: str | None = None,
) -> str:
    """Render the count line the agent prints verbatim above a list.

    Third and last of the backend-rendered shapes, for the same reason as
    ``_list_row`` and ``_summary_card``: it lived as an English recipe in the
    subagent prompt and drifted exactly like the other two did. The subagent
    composed "Found 28 open incidents mentioning TSYS; showing the first 10."
    from the raw total, then the parent re-synthesized it into a number-free
    "Here are open incidents currently related to TSYS:" — hiding a figure we
    were holding. One writer, one shape, no re-derivation per answer.

    Carries NO subject clause on purpose. The subject never drifted (the parent
    kept "related to TSYS" and dropped the numbers), and the backend does not
    know how the user phrased the ask. This owns the numbers; the model's own
    sentence underneath still names the subject. The one exception is an
    assignment ``group``. It is the exact filter, not the user's phrasing, and a
    team's list printed without it looked like unrelated incidents to testers.
    """

    subject = f" for {group}" if group else ""
    # No total from the source is NOT the same as "the page is everything" — say
    # the count is unknown rather than letting ``shown`` pose as the total.
    if total_count is None:
        noun = "incident" if shown == 1 else "incidents"
        return (
            f"Showing {shown} {noun}{subject}; more are available (exact count unavailable)."
            if has_more
            else f"Showing {shown} {noun}{subject}."
        )
    noun = "incident" if total_count == 1 else "incidents"
    # A one-page result needs no positional range. Once paging is involved, give an
    # INCLUSIVE range instead of a bare page size: "showing 10" twice running tells
    # the reader nothing about where they are, while 1-10 / 11-20 / 21-28 does.
    if shown_before == 0 and total_count <= shown and not has_more:
        return f"Found {total_count} {noun}{subject}."
    # An empty page has no positions to name. Without this, range_start runs past
    # range_end and the line reads "showing 29-28" — which happens for real when a
    # user asks for more after the last page and lost-cursor recovery returns
    # nothing, since shown_before is then the full total.
    if shown == 0:
        return f"Found {total_count} {noun}{subject}."
    range_start = shown_before + 1
    range_end = min(shown_before + shown, total_count)
    return f"Found {total_count} {noun}{subject}; showing {range_start}-{range_end}."


def normalize_ticket_list(
    incidents: Iterable[Mapping[str, Any]],
    *,
    statuses: tuple[str, ...] | None,
    limit: int,
    offset: int | str = 0,
    mode: str | None = None,
    degraded: bool = False,
    has_more: bool = False,
    next_cursor: str | None = None,
    filters: Mapping[str, Any] | None = None,
    detail: bool = True,
    total_count: int | None = None,
    consumed: int | None = None,
    shown_before: int = 0,
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

    # ``next_cursor`` is the caller's 'offset|INC…' form; without one, next_offset is
    # the plain integer. (statuses=None is the direct ticket_numbers fetch — paging is
    # moot.) Advance by rows CONSUMED, not rows shown. The merge passes over duplicates
    # and wrong-state rows, so len(tickets) under-counts what was read — advancing by
    # it rewinds the offset into the previous page and re-serves its tail.
    pageable = statuses is not None and isinstance(offset, int)
    next_offset: int | str | None = next_cursor
    if next_cursor is None and pageable:
        next_offset = offset + (len(tickets) if consumed is None else consumed)
    # A sys_id or a comma list of groups is no name to print.
    group = str((filters or {}).get("assignment_group") or "")
    if "," in group or _SYS_ID_RE.fullmatch(group):
        group = ""
    return {
        "ok": True,
        "source": SOURCE,
        "kind": "ticket_list",
        "count": len(tickets),
        # How many incidents match the query in TOTAL, across every page (the API's
        # ``total_count``), vs ``count`` = rows on THIS page. Users ask "how many
        # incidents are there for X" — answer from this, never from count. null means
        # the source reported no total: the agent must SAY the count is unavailable,
        # never substitute count (which would claim the page IS the whole result set).
        "total_count": total_count,
        # The finished COUNT LINE, same contract as each row's ``row``: the agent
        # prints it instead of composing one from the two integers above.
        "header": _list_header(total_count, len(tickets), has_more, shown_before, group or None),
        "limit": limit,
        "offset": offset,
        "next_offset": next_offset,
        "status_filter": list(statuses or []),
        "filters_applied": dict(filters or {}),
        "detail": detail,
        "tickets": tickets,
        "mode": mode,
        "degraded": degraded,
        "has_more": has_more,
    }


def _status_filters(statuses: Iterable[str]) -> dict[str, Any]:
    # Macros ('open'/'closed') are expanded to member states in
    # normalize_status_filters, so by here every entry is a real state whose NUMERIC
    # code goes on the wire. Several states are ONE comma list (live-verified:
    # state=6,7 returns one total and one next_offset). (We no longer use active=true
    # for 'open' — it wrongly includes Resolved; see the _STATUS_MACROS note.)
    return {"state": ",".join(_STATUS_TO_STATE[status] for status in statuses)}


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


# Generic words that describe the QUERY rather than its subject. The wrapper ANDs
# every word of a contains value, so one stray meta-word ("crm data source" instead
# of "crm") requires the description to contain it too and silently returns zero —
# a false "none found". The prompt tells the agent to strip these, but a paraphrase
# re-adds them; stripping here makes it deterministic.
# Deliberately NARROWER than the prompt's list: 'issue(s)', 'records', 'cases',
# 'source' (bare), 'open' and 'active' are REAL content words here — 'cluster issue'
# is a documented close-notes search and 'Open Banking' is a plausible subject — so
# a regex must not touch them. Multi-word entries come first (longest match wins).
_META_WORD_RE = re.compile(
    r"\b(?:data\s+sources?|datasources?|related\s+to|incidents?|tickets?|related|about|for)\b",
    re.IGNORECASE,
)


def strip_meta_words(value: str) -> str:
    """Drop query-type meta-words from a free-text filter value.

    ``'crm data source'`` -> ``'crm'``; ``'incidents related to core banking'`` ->
    ``'core banking'``. Returns the ORIGINAL value when stripping would empty it,
    so a subject that IS a meta-word (a dataset literally named 'Incidents') still
    searches for itself rather than for everything.
    """

    stripped = " ".join(_META_WORD_RE.sub(" ", value).split())
    return stripped or value


# ---------------------------------------------------------------------------
# Missing-data retrieval prefilter (missing_data=True on servicenow_list_tickets)
# ---------------------------------------------------------------------------
# These are a RETRIEVAL prefilter, not the final semantic verdict — the calling
# agent still applies the stricter expectation-plus-absence rubric from its own
# prompt to every candidate this backend returns. Their job is narrower: make
# sure the agent never has to trust page 1 alone, and never has to guess
# whether a data-source token appeared inside an unrelated identifier.

# A proximity window that stays inside ONE sentence of ONE field. A dot ends it only
# when it ends a sentence, so a dotted identifier ('sales.orders') or a version
# ('2.0') no longer cuts a match short, and the newline joining title and
# description is never crossed (the title's last word cannot pair with the
# description's first).
_SENTENCE = r"(?:[^.\n]|\.(?=\S))"

# A pipeline/job/notebook execution FAILURE is an infrastructure incident, not
# proof that expected business data is absent — exclude it before it is ever
# offered as a missing-data candidate.
_PIPELINE_EXECUTION_ERROR_RE = re.compile(
    r"\b(?:azure\s+)?databricks\s+notebook\s+error\s+logging\b"
    r"|\bnotebook\s+(?:execution\s+)?(?:error|failure|aborted)\b"
    r"|\bcritical\s+no\s+files\s+found\s+to\s+process\b"
    r"|\b(?:job|pipeline)\s+(?:execution\s+)?(?:failure|failed|aborted|abort)\b",
    re.IGNORECASE,
)

# The same stakeholder rule — "did the pipeline JOB fail to RUN?" — in the wording a
# missing-data search meets: a NAMED job failing ('pipeline PL-X-LOAD failed',
# 'JOB-NAME-01 timeout'), a job that failed to ingest/run/connect, an exception
# class, out of memory. A source-side cause alone (an HTTP error, a source returning
# nothing) is NOT this: the job ran, the data is absent. (Timeouts and vendor outages
# carry no label at all — see below.)
# Kept apart from the regex above, which the similar-resolution families share.
_JOB_RUN_FAILURE_RE = re.compile(
    r"\b(?:job|pipeline|notebook|workflow|activity)\s+(?:[\w.-]+\s+){0,2}"
    r"(?:execution\s+)?(?:failed|failure|aborted|abort)\b"
    r"|(?-i:\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+){2,})\s+(?:\w+\s+){0,2}"
    r"(?:failed|failure|aborted|abort|timeout|throttled)\b"
    r"|\bfailed\s+to\s+(?:ingest|run|execute|start|complete|connect|authenticate|"
    r"retrieve|pull|extract)\b"
    r"|(?-i:\b[A-Z][a-z]+(?:[A-Z][a-z]*)+(?:Exception|Error|Failure)\b)"
    r"|\bout\s+of\s+memory\b",
    re.IGNORECASE,
)

# A stale fileDate/lookup-table marker caused by a broken refresh/extract
# dependency is a processing incident, not evidence that expected business
# data is absent.
_REFRESH_OR_EXTRACT_PROCESS_ERROR_RE = re.compile(
    r"\b(?:broken|unable\s+to\s+(?:run|execute))\s+(?:our\s+)?(?:refresh|extract)\s*"
    r"(?:process|delta|update)?\b"
    r"|\b(?:refresh|extract)\s+(?:\w+\s+)?(?:is\s+|has\s+been\s+)?"
    r"(?:failing|failed|failure|fails)\b"
    r"|\bdata\s+is\s+coming\s+up\s+as\s+an?\s+older\s+date\b",
    re.IGNORECASE,
)

# Stakeholder rule (for now): a timeout (API, network, source, job) or a vendor outage
# carries NO label — neither missing data nor a pipeline incident, even when rows are
# absent or a job aborted because of it. A user who asks for these tickets gets them
# from a plain search. ('timeout' as a substring also covers SocketTimeoutException.)
_TIMEOUT_OR_VENDOR_OUTAGE_RE = re.compile(
    r"timeout|\btime[\s-]outs?\b|\btim(?:ed|ing)[\s-]out\b"
    rf"|\bvendors?\b{_SENTENCE}{{0,60}}\boutages?\b|\boutages?\b{_SENTENCE}{{0,60}}\bvendors?\b",
    re.IGNORECASE,
)
# A content term that is only the word timeout, in any of its spellings.
_TIMEOUT_TERM_RE = re.compile(
    r"(?:time[\s-]?outs?|timed[\s-]?out)(?:\s+(?:errors?|issues?|failures?|alerts?))?",
    re.IGNORECASE,
)
# A generic word for the kind of thing a name is; group 1 is its singular.
_GENERIC_THING_RE = re.compile(r"(feed|job|pipeline|report)s?", re.IGNORECASE)

# Missing/absent FIELD or COLUMN — a record exists but one expected attribute
# on it does not. Stakeholder rule: schema/mapping defect, RELATED DATA
# QUALITY, not a missing-data incident.
_FIELD = r"(?:field|column|attribute|parameter|data\s+element)s?"
_FIELD_ABSENCE = r"(?:missing|absent|uncaptured|not\s+captured|not\s+migrated|unavailable|null)"
_MISSING_FIELD_OR_COLUMN_RE = re.compile(
    rf"\b{_FIELD}\b{_SENTENCE}{{0,80}}\b{_FIELD_ABSENCE}\b"
    rf"|\b{_FIELD_ABSENCE}\b{_SENTENCE}{{0,80}}\b{_FIELD}\b",
    re.IGNORECASE,
)

# Missing/absent ROW, RECORD, FILE, PERIOD, or other business-data unit — the
# core missing-data signal: an expected unit of business data did not arrive.
# ('members' is not a unit: "the members report e-mail was not delivered" is an
# e-mail/report incident.)
_UNIT = r"(?:rows?|records?|entities?|files?|feeds?|partitions?|snapshots?|periods?|months?|dates?|batch(?:es)?)"
_ABSENCE = (
    r"(?:missing|absent|uncaptured|dropped|dropping|undelivered|empty|stale|older|truncated|"
    r"(?:not|never|\w+n't)\s+(?:yet\s+)?(?:been\s+)?"
    r"(?:captured|loaded|landed|delivered|arrived|received|"
    r"capture|load|land|deliver|arrive|receive)|"
    r"(?:not|stopped)\s+(?:loading|landing))"
)
_MISSING_BUSINESS_DATA_UNIT_RE = re.compile(
    rf"\b{_UNIT}\b{_SENTENCE}{{0,100}}\b{_ABSENCE}\b"
    rf"|\b{_ABSENCE}\b{_SENTENCE}{{0,100}}\b{_UNIT}\b"
    # A count that falls short of what was due: 'zero new records', 'short by 500',
    # 'only 3 of the expected 10', 'a partial load'.
    r"|\bzero\s+(?:\w+\s+){0,3}?(?:rows|records|files)\b"
    r"|\bno\s+(?:new\s+|fresh\s+|updated\s+|or\s+)*(?:rows|records|files)\b"
    r"|\bshort\s+by\b|\bof\s+the\s+expected\b|\bpartial\s+(?:load|delivery|file)\b",
    re.IGNORECASE,
)

# Stakeholder rule: an identifier ending in '_VW' names a database VIEW, and the WHOLE
# view being missing/unavailable is a DEPLOYMENT issue — unless the text also proves
# rows inside it are absent (the unit evidence above, which is checked first).
_VIEW_ABSENCE = (
    r"(?:missing|absent|unavailable|inaccessible|not\s+(?:available|accessible|found)|"
    r"does\s+not\s+exist)"
)
_WHOLE_VIEW_MISSING_RE = re.compile(
    rf"\b\w+_VW\b{_SENTENCE}{{0,80}}\b{_VIEW_ABSENCE}\b"
    rf"|\b{_VIEW_ABSENCE}\b{_SENTENCE}{{0,80}}\b\w+_VW\b",
    re.IGNORECASE,
)

# The plainer wording raisers use for absent data when no unit is named: "Missing
# data: …", "data has not arrived", "no data", "stale", "has not been refreshed",
# "empty responses". Read only AFTER both precedence rules, so a missing field or a
# missing view never qualifies through it.
_MISSING_DATA_WORDING_RE = re.compile(
    rf"\bdata\b{_SENTENCE}{{0,60}}\b{_ABSENCE}\b|\b{_ABSENCE}\s+data\b"
    r"|\bno\s+(?:new\s+|fresh\s+)?data\b|\bstale\b"
    r"|(?:\bnot|n't|\bnever)\s+(?:yet\s+)?(?:been\s+)?refreshed\b"
    r"|\bempty\s+(?:responses?|payloads?)\b",
    re.IGNORECASE,
)


def _incident_search_text(incident: Mapping[str, Any]) -> str:
    """Return the incident text the missing-data prefilter reads."""

    return "\n".join(
        (
            _reference_value(incident.get("short_description")),
            _reference_value(incident.get("description")),
        )
    )


# Split a word into identifier atoms: non-alphanumerics, camelCase humps, and
# letter/digit transitions are all boundaries. 'CaseManagementSystem' becomes
# Case/Management/System, so 'tsys' can never leak across the hump; 'webCRM2parquet'
# becomes web/CRM/2/parquet, so its 'CRM' becomes visible.
_IDENTIFIER_ATOM_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+|[0-9]+")
# A word carrying an underscore, hyphen, digit, or internal capital is a technical
# identifier (00_webcrm_raw_int, Pre_WEBCRM_DW_MD_R) whose atoms may in turn
# embed the subject. Plain prose words never qualify, so 'crm' still cannot match
# inside 'xacrmstore'.
_TECHNICAL_WORD_RE = re.compile(r"[_\-0-9]|(?<=[a-z])[A-Z]")
# The wire's substring match already returns 'duplicates'/'duplicated' for 'duplicate';
# accept those regular endings on words of 4+ letters so 'adf' still never matches 'ADFS'.
# ponytail: regular endings only; 'stopped'/'failure' need a stemmer or the classifier.
_WORD_ENDINGS = ("s", "es", "d", "ed", "ing")


def _subject_atom_sets(text: str) -> tuple[set[str], set[str]]:
    """Return (exact-match forms, technical-identifier atoms), lowercased.

    The exact set also keeps each undivided word ('LexisNexis'), since a run-together
    product name is a legitimate subject; only the technical set is searched by
    substring, so the concatenation can never reintroduce a cross-boundary match.
    """

    atoms: set[str] = set()
    technical: set[str] = set()
    for word in text.split():
        is_technical = bool(_TECHNICAL_WORD_RE.search(word))
        for chunk in re.findall(r"[A-Za-z0-9]+", word):
            atoms.add(chunk.lower())
            chunk_atoms = {atom.lower() for atom in _IDENTIFIER_ATOM_RE.findall(chunk)}
            atoms |= chunk_atoms
            if is_technical:
                technical |= chunk_atoms
    return atoms, technical


def incident_matches_subject(incident: Mapping[str, Any], subject: str | None) -> bool:
    """Locally verify the content filter in case the live wrapper leaks unrelated rows.

    The wrapper's description_contains can match across a word boundary inside an
    unrelated identifier: "CaseManagementSystem" contains the letters "tsys" and
    leaked a non-TSYS fraud incident into a TSYS query. Split the text into
    identifier atoms so a prose word never matches inside itself, then allow the
    subject INSIDE an atom only when that atom came from a technical identifier —
    'crm' is real evidence in webCRM2parquet / Pre_WEBCRM_DW_MD_R /
    00_webcrm_raw_int, and still not evidence in 'xacrmstore'.
    """

    if subject is None or not subject.strip():
        return True
    tokens = re.findall(r"[a-z0-9]+", subject.lower())
    if not tokens:
        return True
    atoms, technical = _subject_atom_sets(_incident_search_text(incident))
    return all(
        token in atoms
        or (len(token) >= 4 and any(token + ending in atoms for ending in _WORD_ENDINGS))
        or any(token in atom for atom in technical)
        for token in tokens
    )


def _incident_contains_phrase(incident: Mapping[str, Any], phrase: str) -> bool:
    """Whether the incident text holds ``phrase`` as ONE run, as the live wrapper matches.

    The mock splits a value into words, so 'subscription email' also matched alerts
    carrying both words far apart (13 rows where live finds the 3 real ones).
    """

    text = " ".join(_LITERAL_ESCAPE_RE.sub(" ", _incident_search_text(incident)).split())
    return " ".join(phrase.split()).lower() in text.lower()


def has_missing_data_evidence(incident: Mapping[str, Any]) -> bool:
    """Return whether incident text contains concrete expected-data absence evidence.

    This is a retrieval prefilter, not the final semantic verdict: it keeps every
    row that reads as missing data (records absent, no data, stale/not refreshed,
    empty or undelivered file, rows dropped or short) and drops pipeline execution
    failures, timeouts and vendor outages, missing fields/columns and whole missing
    views before the calling agent applies its own rubric.
    """

    text = _incident_search_text(incident)
    # A pipeline/notebook execution error is an infrastructure incident even when its
    # log says "NO FILES FOUND TO PROCESS" or the table it feeds is now stale — the
    # job failed to RUN. Likewise a stale fileDate/lookup-table marker caused by a
    # broken refresh or extract process. Both belong to the pipeline use case. A
    # timeout or vendor outage belongs to neither (no label).
    if (
        _PIPELINE_EXECUTION_ERROR_RE.search(text)
        or _JOB_RUN_FAILURE_RE.search(text)
        or _REFRESH_OR_EXTRACT_PROCESS_ERROR_RE.search(text)
        or _TIMEOUT_OR_VENDOR_OUTAGE_RE.search(text)
    ):
        return False
    if _MISSING_BUSINESS_DATA_UNIT_RE.search(text):
        return True
    # Stakeholder rule: a missing/uncaptured field or column on an otherwise-present
    # record is schema/DQ, not missing business data (a row/file/period deficit,
    # checked above, still wins when both are mentioned in the same incident).
    if _MISSING_FIELD_OR_COLUMN_RE.search(text):
        return False
    # Stakeholder rule: a whole '_VW' view missing is a deployment issue (rows proven
    # absent inside it, checked above, still win).
    if _WHOLE_VIEW_MISSING_RE.search(text):
        return False
    # Lag ("consumer lag", "feed delayed") is latest data older than expected — the
    # same wording the similar-resolution 'lag' family reads.
    return bool(_MISSING_DATA_WORDING_RE.search(text) or _SIMILAR_LAG_RE.search(text))


def _build_field_filters(
    *,
    description_contains: str | None,
    close_notes_contains: str | None,
    ci_contains: str | None = None,
    cause: str | None,
    assigned_to: str | None,
    resolved_by: str | None,
    assigned_to_contains: str | None,
    resolved_by_contains: str | None,
    assigned_to_name: str | None,
    resolved_by_name: str | None,
    caller_id: str | None,
    caller_id_name_contains: str | None,
    assignment_group: str | None,
    priority: str | None,
    created_after: str | None,
    created_before: str | None,
    updated_after: str | None,
    updated_before: str | None,
    ticket_numbers: str | None,
    description_contains_and: str | None = None,
    description_contains_or: str | None = None,
) -> dict[str, Any]:
    """Map tool arguments onto the filter keys the ServiceNow client supports.

    Only the README §3.2 supported set is mapped here. ``category`` and
    ``opened_*`` are intentionally NOT filters (category is an output field used
    for agent-side classification; date windows use ``created_*`` / ``updated_*``).
    """

    # The wrapper API ANDs filters, so assigned_to + resolved_by in ONE call means
    # "assigned to X AND resolved by Y" — for a single engineer that is almost always
    # empty (the assignee is rarely also the resolver). A person's incidents are ONE
    # assigned-to search. Reject the combined form rather than silently returning zero.
    def _present(value: str | None) -> bool:
        return value is not None and bool(value.strip())

    # Any assigned-to form vs any resolved-by form — all three per role are the same
    # underlying field, so the AND-returns-zero trap applies across every combination.
    if any(map(_present, (assigned_to, assigned_to_contains, assigned_to_name))) and any(
        map(_present, (resolved_by, resolved_by_contains, resolved_by_name))
    ):
        raise ServiceNowToolInputError(
            "assigned_to and resolved_by cannot be combined in one query (the API ANDs "
            "them, so it returns ~0 records). Search ONE role: assigned_to for a "
            "person's incidents, resolved_by only when the user asked what they resolved."
        )

    filters: dict[str, Any] = {}

    substring_filters = {
        # NOTE: short_description_contains is deliberately NOT exposed. The client
        # still supports it, but the title is terse and adds little weight, and
        # because the API ANDs filters, splitting one ask across title + description
        # silently narrowed results. ONE content filter, on the long description.
        "description_contains": description_contains,
        # Several phrases in ONE call (comma-separated): every one / any one of them.
        "description_contains_and": description_contains_and,
        "description_contains_or": description_contains_or,
        "close_notes_contains": close_notes_contains,
        # CI substring. Populated only on (some) closed incidents, so it narrows a
        # targeted history query and would empty an open-incident list.
        "ci_contains": ci_contains,
        "assigned_to": assigned_to,
        "resolved_by": resolved_by,
        "assigned_to_contains": assigned_to_contains,
        "resolved_by_contains": resolved_by_contains,
        "assigned_to_name": assigned_to_name,
        "resolved_by_name": resolved_by_name,
        # Caller is a DIFFERENT field from assigned_to/resolved_by, so ANDing it with
        # either is legitimate ("reported by X, assigned to Y") — no combine guard.
        "caller_id": caller_id,
        "caller_id_name_contains": caller_id_name_contains,
        "assignment_group": assignment_group,
        "priority": priority,
    }
    # Meta-words come off CONTENT filters only: a person or assignment-group name is
    # taken verbatim ('Service Desk' is a group name, 'for' can sit in a name).
    content_keys = {
        "description_contains",
        "description_contains_and",
        "description_contains_or",
        "close_notes_contains",
        "ci_contains",
    }
    for key, value in substring_filters.items():
        if value is not None and value.strip():
            cleaned = value.strip()
            filters[key] = strip_meta_words(cleaned) if key in content_keys else cleaned

    def _terms(key: str) -> list[str]:
        return [term.strip() for term in filters.get(key, "").split(",") if term.strip()]

    # A symptom glued onto a name ('<product> <part> not starting') matches only a
    # ticket worded exactly so — almost none live, since tickets say what went wrong
    # their own way. Search the names; the results say what broke. A one-word name
    # ('feed not loading') is too vague on its own, so it is let through.
    for key in ("description_contains", "description_contains_and", "description_contains_or"):
        for term in _terms(key):
            glued = _SUBJECT_NOT_VERB_RE.match(term)
            if glued and len(glued["name"].split()) >= 2:
                raise ServiceNowToolInputError(
                    f"{key} '{term}' keeps the symptom '{term[len(glued['name']):].strip()}', "
                    "which tickets word their own way, so it finds almost nothing. Retry ONCE "
                    f"with only the names in '{glued['name']}': two things (a product with "
                    "its version is ONE term; the part of it that broke is another) go in "
                    "description_contains_and, one name in description_contains."
                )

    # A generic word glued to a name ('<source> feed', 'feed <product>') is one
    # contiguous phrase live, so it misses '<source> data feed' and 'feed from
    # <source>'. The word becomes its own AND term, which still matches every ticket
    # the phrase did. A symptom the check above let through ('feed not loading') stays
    # as is. ponytail: a one-word name ('<x> feed') stays glued, as in that check, and
    # so do OR terms ('<a> feed' OR '<b> feed' has no one-call form); widen if either
    # shows up.
    def _unglue(term: str) -> list[str]:
        words = term.split()
        if len(words) >= 3 and not _SUBJECT_NOT_VERB_RE.match(term):
            if last := _GENERIC_THING_RE.fullmatch(words[-1]):
                return [" ".join(words[:-1]), last[1].lower()]
            if first := _GENERIC_THING_RE.fullmatch(words[0]):
                return [first[1].lower(), " ".join(words[1:])]
        return [term]

    bare = filters.get("description_contains", "")
    moved = [bare] if len(_unglue(bare)) > 1 else []
    and_terms = [*moved, *_terms("description_contains_and")]
    unglued = [part for term in and_terms for part in _unglue(term)]
    if unglued != and_terms:
        if moved:
            del filters["description_contains"]
        filters["description_contains_and"] = ",".join(dict.fromkeys(unglued))

    # A timeout is worded 'timeout' or 'timed out', so a bare timeout TERM ANDed with
    # the subject ('<source>,timeout') misses every ticket using the other spelling.
    # Any bare timeout term becomes the either-spelling OR filter of the prompt's
    # timeout recipe, and what is left is the subject. Left alone when the OR slot
    # already holds other subjects.
    or_terms = _terms("description_contains_or")
    if all(map(_TIMEOUT_TERM_RE.fullmatch, or_terms)):
        and_terms = _terms("description_contains_and")
        timeouts = [term for term in and_terms if _TIMEOUT_TERM_RE.fullmatch(term)]
        rest = [term for term in and_terms if term not in timeouts]
        bare = filters.get("description_contains", "")
        bare = bare if _TIMEOUT_TERM_RE.fullmatch(bare) else ""
        if or_terms or timeouts or bare:
            spellings = [*or_terms, *timeouts, bare]
            if bare:
                del filters["description_contains"]
            filters.pop("description_contains_and", None)
            if len(rest) == 1 and "description_contains" not in filters:
                filters["description_contains"] = rest[0]
            elif rest:
                filters["description_contains_and"] = ",".join(rest)
            filters["description_contains_or"] = ",".join(
                dict.fromkeys(t.lower() for t in ("timeout", "timed out", *spellings) if t)
            )

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

    # FAIL LOUD on a filter the client cannot wire. The client DROPS an unknown key
    # with only a log line and runs the query anyway, so the caller gets UNFILTERED
    # results presented as filtered — a silent wrong answer with no exception. That
    # happens whenever this module knows a filter the deployed client does not (e.g.
    # assigned_to_contains against an older client), which is exactly the state a
    # partial merge leaves behind. Raise instead: a visible error beats a plausible
    # lie, and it is why the prompt no longer needs a "never send these" list.
    unsupported = sorted(set(filters) - set(SUPPORTED_FILTERS))
    if unsupported:
        raise ServiceNowToolInputError(
            f"filters not supported by the configured ServiceNow client: "
            f"{', '.join(unsupported)}. Supported: {', '.join(sorted(SUPPORTED_FILTERS))}."
        )

    return filters


async def _exhaust_incidents(
    client: ServiceNowClient, filters: Mapping[str, Any] | None
) -> tuple[list[Mapping[str, Any]], str | None, bool]:
    """Page through EVERY row matching filters, ignoring the normal per-call page cap.

    Missing-data mode has already paid the cost to exhaust the broad query — it must
    not stop at the first page (the default-sized page) and silently miss a
    later-page match.
    """

    rows: list[Mapping[str, Any]] = []
    mode: str | None = None
    degraded = False
    offset = 0
    while True:
        envelope = await client.list_incidents(
            filters=filters, limit=_EXHAUST_PAGE_SIZE, offset=offset
        )
        mode = mode or envelope.get("mode")
        degraded = degraded or bool(envelope.get("degraded"))
        page = list(envelope.get("incidents", []))
        rows.extend(page)
        nxt = envelope.get("next_offset")
        if not envelope.get("has_more") or not isinstance(nxt, int) or nxt <= offset:
            break
        offset = nxt
    return rows, mode, degraded


def _title_twins(filter_sets: list[Mapping[str, Any] | None]) -> list[dict[str, Any]]:
    """The same queries run against the TITLE instead of the long description.

    ``description_contains`` searches the long description only, and a name that
    sits only in the title (alert-raised tickets often read "<app>: queue not
    refreshing") never matches it. Both fields in ONE call are AND-ed, which
    narrows, so the title search is a separate call per set, unioned by the
    caller's de-dup.

    Every ``description_contains_and`` term gets a twin too: that term in the
    title, the rest still in the description. A product name often sits only in
    the title ("<product>: <part> not starting" over a description that names
    just the part), and ANDing it against the description alone missed the ticket.
    Each ``description_contains_or`` term gets one too, the term alone in the title,
    so 'A or B' finds what 'A' alone finds.
    """

    twins: list[dict[str, Any]] = []
    for filters in filter_sets:
        if not filters:
            continue
        if "description_contains" in filters:
            twins.append(
                {
                    **{key: value for key, value in filters.items() if key != "description_contains"},
                    "short_description_contains": filters["description_contains"],
                }
            )
        terms = [t.strip() for t in filters.get("description_contains_and", "").split(",") if t.strip()]
        base = {key: value for key, value in filters.items() if key != "description_contains_and"}
        for term in terms:
            others = [other for other in terms if other != term]
            twins.append(
                {
                    **base,
                    **({"description_contains_and": ",".join(others)} if others else {}),
                    "short_description_contains": term,
                }
            )
        base = {key: value for key, value in filters.items() if key != "description_contains_or"}
        for term in [t.strip() for t in filters.get("description_contains_or", "").split(",") if t.strip()]:
            twins.append({**base, "short_description_contains": term})
    return twins


async def _list_classified_candidates(
    client: ServiceNowClient,
    *,
    statuses: tuple[str, ...] | None,
    field_filters: Mapping[str, Any],
    subject: str | None,
    detail: bool,
    keep: Callable[[Mapping[str, Any]], bool],
    suppress_header: bool,
    newest_first: bool = False,
    cap: int | None = None,
    search_titles: bool = False,
) -> dict[str, Any]:
    """Exhaust every source/status page and keep only the rows ``keep`` accepts.

    Both classified modes need the same retrieval: one page cap cannot be trusted to
    hold the matches, so every page is exhausted and no further paging is offered.
    They differ only in the verdict's finality. ``missing_data`` is a PREFILTER — the
    calling agent still applies its own rubric — so the "Found N; showing 1-10"
    header (which describes retrieval, not the answer) is suppressed. The
    pipeline-related filter IS the answer, so its "Found N incidents." header is the
    exact classified count the user asked for.

    ``newest_first`` reuses the same exhaust-then-decide retrieval for a different
    reason: the wrapper has no sort param, so the only honest "most recent" is one
    taken over EVERY matching row. ``cap`` then trims the sorted list to the page
    the caller asked for, while ``total_count`` keeps reporting the full match count
    — so "Found 48 incidents; showing 1-10" stays true of the newest 10.
    ``search_titles`` adds the :func:`_title_twins` of every query.
    """

    filter_sets = [
        (field_filters or None)
        if statuses is None
        else {**field_filters, **_status_filters(statuses)}
    ]
    if search_titles:
        filter_sets += _title_twins(filter_sets)

    pages = await asyncio.gather(
        *(_exhaust_incidents(client, filters) for filters in filter_sets)
    )

    mode: str | None = None
    degraded = False
    seen: set[str] = set()
    candidates: list[Mapping[str, Any]] = []
    for rows, page_mode, page_degraded in pages:
        mode = mode or page_mode
        degraded = degraded or page_degraded
        for incident in rows:
            number = _reference_value(incident.get("number")).upper()
            if number in seen:
                continue
            seen.add(number)
            if not incident_matches_subject(incident, subject):
                continue
            if not keep(incident):
                continue
            candidates.append(incident)

    # total_count describes the MATCH SET, so it is taken before the cap — otherwise
    # a capped result would claim the page size was the whole answer.
    total_count = len(candidates)
    if newest_first:
        candidates.sort(key=_created_sort_key, reverse=True)
        if cap is not None:
            candidates = candidates[:cap]

    result = normalize_ticket_list(
        candidates,
        statuses=statuses,
        limit=max(len(candidates), 1),
        offset=0,
        mode=mode,
        degraded=degraded,
        has_more=False,
        filters=field_filters,
        detail=detail,
        total_count=total_count,
    )
    if suppress_header:
        result["header"] = ""
    return result


# ---------------------------------------------------------------------------
# Similar-resolution retrieval (servicenow_find_similar_resolutions)
# ---------------------------------------------------------------------------
# A model-controlled two-call recipe (fetch parent, then a broad closed-history
# search) drifted badly: fabricated filters, wrong source matched (substring
# leaks), pipeline/table incidents mixed in as "similar", inconsistent result
# counts across runs, and a known-good historical match buried past the first
# broad page never being reached. This dedicated tool owns the whole workflow
# so retrieval and local classification are deterministic instead of re-derived
# per answer.

_SIMILAR_RESOLUTION_FAMILIES = frozenset(
    {
        "pipeline",
        "pipeline_api",
        "notebook",
        "data_reconciliation",
        "file_delivery",
        "vendor",
        "lag",
        "connectivity",
        # Everything the eight data-pipeline patterns above do not describe (server
        # and disk alerts, app or config problems): no failure-pattern gate, so the
        # symptom phrase in source_subject plus the owning team decide the match.
        "general",
    }
)

_SIMILAR_PIPELINE_FAMILY_RE = re.compile(
    r"\b(?:azure\s+)?databricks\s+notebook\s+error\s+logging\b"
    r"|\bnotebook\s+(?:execution\s+)?(?:error|failure|aborted)\b"
    r"|\b(?:pipeline|job|workflow)\s+(?:failure|failed|aborted|abort)\b"
    r"|\b(?:api|fetch)\s+(?:failed|failure|timeout)\b"
    r"|\b(?:timeout|timed\s+out|connection|network)\b",
    re.IGNORECASE,
)
_SIMILAR_NON_PIPELINE_RE = re.compile(
    r"\b(?:table|view|column|field)s?\b[^.]{0,80}\b(?:missing|not\s+showing|incorrect)\b"
    # The job RAN and its output is wrong: a data-quality incident, not a failure of
    # the pipeline's execution. Keeps report/value complaints about a source out of
    # "which pipeline broke" answers. A NEGATED mention ("not a data quality issue")
    # says the opposite and hid a real job failure.
    r"|(?<!\bnot\s)(?<!\bnot\sa\s)\bdata[\s-]quality\b",
    re.IGNORECASE,
)
_SIMILAR_RECONCILIATION_RE = re.compile(
    r"\brow[\s-]?(?:counts?|presence)\b"
    r"|\b(?:discrepanc(?:y|ies)|mismatch(?:es)?)\b"
    r"|\b(?:uncaptured|not\s+captured|missing|dropped|extra)\s+rows?\b"
    r"|\bsource\b[^.]{0,120}\btarget\b[^.]{0,120}\b(?:row|record|count|reconcil)\b",
    re.IGNORECASE | re.DOTALL,
)
_SIMILAR_NON_RECONCILIATION_RE = re.compile(
    r"\b(?:transaction|dollar|monetary|balance)\s+amount\b"
    r"|\b(?:enhancement|PII|masking|encryption|CRM|UI)\b",
    re.IGNORECASE,
)
_SIMILAR_FILE_DELIVERY_RE = re.compile(
    r"\b(?:missing|absent|undelivered|not\s+delivered|not\s+landed|truncated|"
    r"incomplete|empty|late|delayed)\b[^.]{0,120}\b(?:files?|feeds?|extracts?)\b"
    r"|\b(?:files?|feeds?|extracts?)\b[^.]{0,120}\b(?:missing|absent|undelivered|"
    r"not\s+delivered|not\s+landed|truncated|incomplete|empty|late|delayed)\b",
    re.IGNORECASE | re.DOTALL,
)
_SIMILAR_VENDOR_RE = re.compile(
    r"\bvendor\b[^.]{0,120}\b(?:outage|maintenance|delay|unavailable|failure|recovered)\b",
    re.IGNORECASE | re.DOTALL,
)
_SIMILAR_LAG_RE = re.compile(
    r"\b(?:consumer|kafka|feed|data)\b[^.]{0,120}\b(?:lag|stale|delayed|behind)\b"
    r"|\b(?:offset|backlog)\b[^.]{0,120}\b(?:growing|cleared|lag)\b",
    re.IGNORECASE | re.DOTALL,
)
_SIMILAR_CONNECTIVITY_RE = re.compile(
    r"\b(?:connection|connectivity|network)\b[^.]{0,80}\b(?:timeout|timed\s+out|"
    r"unreachable|refused|failure)\b"
    r"|\bconnection\s+refused\b",
    re.IGNORECASE | re.DOTALL,
)


def normalize_similar_resolution_family(failure_family: str) -> str:
    """Return a canonical family key or raise for an unsupported family."""

    if not isinstance(failure_family, str):
        raise ServiceNowToolInputError("failure_family must be a string")
    normalized = failure_family.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized not in _SIMILAR_RESOLUTION_FAMILIES:
        raise ServiceNowToolInputError(
            "failure_family must be one of: "
            + ", ".join(sorted(_SIMILAR_RESOLUTION_FAMILIES))
        )
    return normalized


def matches_similar_resolution_family(incident: Mapping[str, Any], failure_family: str) -> bool:
    """Apply a high-precision prefilter before LLM similar-incident classification."""

    normalized = normalize_similar_resolution_family(failure_family)
    if normalized == "general":
        return True
    text = _incident_search_text(incident)
    if normalized in {"pipeline", "pipeline_api", "notebook"}:
        return not _SIMILAR_NON_PIPELINE_RE.search(text) and bool(
            _SIMILAR_PIPELINE_FAMILY_RE.search(text)
        )
    if normalized == "data_reconciliation":
        return not _SIMILAR_NON_RECONCILIATION_RE.search(text) and bool(
            _SIMILAR_RECONCILIATION_RE.search(text)
        )
    if normalized == "file_delivery":
        return not _PIPELINE_EXECUTION_ERROR_RE.search(text) and bool(
            _SIMILAR_FILE_DELIVERY_RE.search(text)
        )
    if normalized == "vendor":
        return bool(_SIMILAR_VENDOR_RE.search(text))
    if normalized == "lag":
        return bool(_SIMILAR_LAG_RE.search(text))
    return bool(_SIMILAR_CONNECTIVITY_RE.search(text))


def is_pipeline_related_incident(incident: Mapping[str, Any]) -> bool:
    """Return whether the incident is a pipeline/ingest EXECUTION failure.

    Same deterministic gate the similar-resolution workflow uses, reused so "list the
    pipeline incidents for <source>" and "find similar pipeline incidents" agree about
    what counts as a pipeline incident — except a timeout or vendor outage, which gets
    no pipeline label here (stakeholder rule, for now) yet still pairs with its own
    kind when finding similar incidents.
    """

    if _TIMEOUT_OR_VENDOR_OUTAGE_RE.search(_incident_search_text(incident)):
        return False
    return matches_similar_resolution_family(incident, "pipeline")


# A snake_case technical identifier (feed/table/column name) — dataset-shaped
# tokens like 'svc_orders_daily' are far more selective retrieval evidence than
# the bare business-segment word, and let a targeted historical query find an
# old match a broad segment page of 800+ rows would bury.
_TECHNICAL_IDENTIFIER_RE = re.compile(r"(?<![a-z0-9])([a-z][a-z0-9]*(?:_[a-z0-9]+)+)(?![a-z0-9])", re.IGNORECASE)
_GENERIC_FINGERPRINT_TOKENS = frozenset(
    {"missing_data", "source_system", "cloud_service", "business_impact"}
)
# The notebook/job name a pipeline incident is raised for ('webCRM2parquet'). It is
# the single most selective retrieval token available for these families — the bare
# source word returns ~500 rows where the notebook name returns tens — and the
# instance's contains match is case-insensitive, so the literal is kept verbatim.
_PIPELINE_NOTEBOOK_IDENTIFIER_RE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9_-]*parquet[A-Za-z0-9_-]*)\b", re.IGNORECASE
)


def derive_similarity_fingerprint(
    incident: Mapping[str, Any], failure_family: str | None = None
) -> tuple[str, ...]:
    """Extract stable parent evidence that can narrow historical similarity retrieval.

    Two dataset/feed identifiers are usually enough to collapse a source-wide pool;
    a third column/date marker further disambiguates recurring automated incidents.
    For a pipeline/notebook parent the notebook name alone is stronger than either,
    so it wins outright when present.
    """

    if failure_family in {"pipeline", "pipeline_api", "notebook"}:
        notebook = _PIPELINE_NOTEBOOK_IDENTIFIER_RE.search(_incident_search_text(incident))
        if notebook:
            return (notebook.group(1),)

    # The AFFECTED HOST must not pin the similarity search. A server-scoped parent
    # names its box in the CI, and that name also appears in the incident text — so
    # without this the fingerprint becomes the hostname and the "similar incidents"
    # history collapses to prior tickets on that ONE server, which is the opposite of
    # matching on the symptom. The CI display value is the host verbatim, so excluding
    # tokens it already contains needs no hostname pattern to guess at. Pipeline
    # families are exempt: a parent with no notebook name still reaches here, and its
    # CI is the PL-* pipeline, which names the very dataset those families match on.
    ci_text = (
        ""
        if failure_family in {"pipeline", "pipeline_api", "notebook"}
        else (
            _reference_value(incident.get("cmdb_ci"))
            or _reference_value(incident.get("configuration_item"))
        ).lower()
    )
    identifiers: list[str] = []
    for match in _TECHNICAL_IDENTIFIER_RE.finditer(_incident_search_text(incident)):
        token = match.group(1).lower()
        if token in _GENERIC_FINGERPRINT_TOKENS or token in identifiers:
            continue
        if ci_text and token in ci_text:
            continue
        identifiers.append(token)
    identifiers.sort(
        key=lambda token: (
            0
            if token.startswith(("svc_", "feed_", "file_", "extract_"))
            else 1
            if token.startswith(("md_", "src_", "source_"))
            else 2
        )
    )
    return tuple(identifiers[:3])


# A model-literalized recipe placeholder ("segment-placeholder", "<subject>", ...)
# must never reach the ServiceNow client as a real filter value.
_PLACEHOLDER_FILTER_RE = re.compile(
    r"[<\[][^<>\[\]]*[>\]]|(?:^|[\s_-])placeholder(?:$|[\s_-])", re.IGNORECASE
)


def validate_content_filter(name: str, value: str) -> str:
    """Reject a template placeholder before it can become a ServiceNow query param."""

    if not isinstance(value, str):
        raise ServiceNowToolInputError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or _PLACEHOLDER_FILTER_RE.search(cleaned):
        raise ServiceNowToolInputError(
            f"{name} contains a template placeholder, not an evidenced incident value. "
            "Read the source incident's short description or description, and pass a "
            "literal subject that actually occurs in it — never a filter name or values "
            "such as 'segment-placeholder' or '<subject>'."
        )
    return cleaned


# Alert descriptions arrive with LITERAL escapes ('\n', even '\\\n') that printed as
# text. A lowercase letter after one means a Windows path segment ('D:\nightly'): kept.
_LITERAL_ESCAPE_RE = re.compile(r"\\+[nrt](?![a-z])")
_SUBJECT_NOT_VERB_RE = re.compile(
    r"^(?P<name>.+?)\s+(?:(?:is|are|was|were)\s+)?not\s+[a-z]+ing$", re.IGNORECASE
)
_STALL_VERB = (
    r"(?:stall(?:s|ed|ing)?|stuck|hang(?:s|ing)?|hung"
    r"|freez(?:e|es|ing)|froze(?:n)?|crash(?:es|ed|ing)?|fail(?:s|ed|ing)?"
    r"|stop(?:s|ped|ping)?)"
)
_SUBJECT_LEAD_VERB_RE = re.compile(
    rf"^(?:(?:is|are|was|were)\s+)?{_STALL_VERB}\s+(?:at|in|on|during)\s+"
    r"(?:(?:the|a|an)\s+)?(?P<name>.+)$",
    re.IGNORECASE,
)
_SUBJECT_TRAIL_VERB_RE = re.compile(
    rf"^(?P<name>.+?)\s+(?:(?:is|are|was|were)\s+)?{_STALL_VERB}$", re.IGNORECASE
)
_SUBJECT_HIT_THING_RE = re.compile(
    r"\S\s+(?P<thing>package|dataset|queue|job|request|report|workbook|host|node)s?$",
    re.IGNORECASE,
)


def _concise_incident_description(raw: Any, title: str = "", *, max_chars: int = 220) -> str:
    """Return a stable short description without model-authored summarization.

    Empty when there is none or it only repeats the title — alert tickets paste the
    title into the description, so that line just printed the title a second time.
    """

    text = " ".join(_LITERAL_ESCAPE_RE.sub(" ", _reference_value(raw)).split())
    title = " ".join(title.split()).casefold()
    if not text or (title and title in text.casefold()):
        return ""
    first_sentence = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0]
    summary = first_sentence if len(first_sentence) >= 40 else text
    if len(summary) <= max_chars:
        return summary
    clipped = summary[: max_chars - 1].rsplit(" ", 1)[0].rstrip(" ,;:")
    return f"{clipped}…"


def _similar_resolution_header(count: int, team: str) -> str:
    """The count line, in plain words. It used to explain the retrieval ("candidate
    pool … this is NOT the number of similar incidents …"), which users could not read."""

    scope = f"from the same team ({team})" if team else "across all teams"
    if not count:
        return f"No similar closed incidents found {scope}."
    return f"Found {count} similar closed {'incident' if count == 1 else 'incidents'} {scope}."


def _similar_resolution_candidate_block(ticket: Mapping[str, Any]) -> str:
    """One match: its list row, then what caused it and how it was fixed."""

    fields = [ticket["row"]]
    summary = _concise_incident_description(ticket.get("description"), ticket["short_description"])
    if summary:
        fields.append(f"- **Summary:** {summary}")
    if ticket.get("cause"):
        fields.append(f"- **Cause:** {ticket['cause']}")
    fields.append(f"- **Resolution notes:** {ticket.get('close_notes') or 'Not available'}")
    return "\n".join(fields)


_PIPELINE_CI_RE = re.compile(r"^PL[-_]", re.IGNORECASE)


def _historical_pipeline_ci_summary(
    tickets: Iterable[Mapping[str, Any]], family: str
) -> str | None:
    """Answer "which pipeline executes this notebook?" from the historical CI fields.

    The CI is only populated on closed incidents (and not all of them), which is
    exactly why this is read off the matched HISTORY rather than the open parent.
    """

    if family not in {"pipeline", "pipeline_api", "notebook"}:
        return None
    pipeline_cis = sorted(
        {
            ci
            for ci in (
                str(ticket.get("configuration_item") or "").strip() for ticket in tickets
            )
            if _PIPELINE_CI_RE.match(ci)
        },
        key=str.casefold,
    )
    if len(pipeline_cis) == 1:
        return (
            "Pipeline identified from historical configuration item (CI): "
            f"**{pipeline_cis[0]}**."
        )
    if pipeline_cis:
        return "Historical pipeline configuration items (CIs): " + ", ".join(
            f"**{ci}**" for ci in pipeline_cis
        )
    return None


def _render_similar_resolution_answer(
    source_ticket: Mapping[str, Any],
    header: str,
    candidate_blocks: Iterable[str],
    pipeline_ci_summary: str | None = None,
) -> str:
    """Render the only user-facing answer: the parent BRIEFLY, then its closed matches.

    The parent is its one list row (+ a summary line when the description adds to the
    title), not the full card — users asked for the matches, not every parent field.
    """

    parent = [source_ticket["row"]]
    summary = _concise_incident_description(
        source_ticket.get("description"), source_ticket["short_description"]
    )
    if summary:
        parent.append(f"- **Summary:** {summary}")
    parts = ["## Incident summary", "\n".join(parent), "## Similar closed incidents", header]
    if pipeline_ci_summary:
        parts.append(pipeline_ci_summary)
    # Detail lines are indented to the item's text so they nest under its number.
    parts.extend(
        f"{index}. " + block.replace("\n", "\n" + " " * len(f"{index}. "))
        for index, block in enumerate(candidate_blocks, 1)
    )
    return "\n\n".join(parts)


@tool
async def servicenow_get_ticket_detail(
    ticket_number: Annotated[
        str,
        Field(
            description=(
                "ServiceNow incident number in the form INC followed by seven "
                "digits, e.g. INC0001001."
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


def _group_spellings(name: str) -> list[str]:
    """``name`` first, then the spellings of it that differ only in ' - ' and spacing.

    Group names match EXACTLY on the API, punctuation included, and users drop the
    hyphen: 'ACME OPS SUPPORT TEAM L2' finds nothing when the group is 'ACME OPS -
    SUPPORT TEAM L2'. ponytail: one ' - ' per spelling, so a two-hyphen name
    typed without them stays unmatched; add pairs if one turns up.
    """

    words = name.replace("-", " ").split()
    hyphened = [f"{' '.join(words[:i])} - {' '.join(words[i:])}" for i in range(1, len(words))]
    return list(dict.fromkeys([name, " ".join(words), *hyphened]))


def _retry_group_spelling(list_tickets: Callable[..., Any]) -> Callable[..., Any]:
    """Re-run an EMPTY assignment-group search under the group's real spelling.

    Without this, a mistyped group read as "this team has no open incidents".
    Only an empty single-name search pays for the lookups: one row per spelling,
    in any state. A name that already names a group keeps its genuine zero, and
    a name that matches no group says so instead of reporting an empty team.
    """

    @functools.wraps(list_tickets)
    async def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = await list_tickets(*args, **kwargs)
        group = (kwargs.get("assignment_group") or "").strip()
        if (
            not result.get("ok")
            or result.get("count")
            or result.get("total_count")
            or not group
            or "," in group
            or _SYS_ID_RE.fullmatch(group)
        ):
            return result
        spellings = _group_spellings(group)
        try:
            client = await get_servicenow_client()
            probes = await asyncio.gather(
                *(
                    client.list_incidents(filters={"assignment_group": s}, limit=1, offset=0)
                    for s in spellings
                )
            )
        except (ServiceNowError, ServiceNowConfigurationError):
            return result
        hit = next(((s, probe) for s, probe in zip(spellings, probes) if probe.get("incidents")), None)
        if hit is None:
            return {
                **result,
                "header": (
                    f"No assignment group is named '{group}'. Group names must match "
                    "exactly, including any ' - ', so please check the group's exact name."
                ),
            }
        spelling, probe = hit
        if spelling == group:
            return result
        # The group's own casing for the header; the match itself ignores case.
        real = _reference_display(probe["incidents"][0].get("assignment_group"))
        found = real if real.lower() == spelling.lower() else spelling
        return await list_tickets(*args, **{**kwargs, "assignment_group": found})

    return wrapper


@tool
@_retry_group_spelling
async def servicenow_list_tickets(
    statuses: Annotated[
        str | None,
        Field(
            description=(
                "Optional comma-separated status filters. Supported values: "
                "new, in_progress, on_hold, resolved, plus three BUCKET "
                "macros: 'open', 'closed', and 'all'. Aliases like 'in progress' and "
                "'on hold' are accepted. "
                "'open' expands to New + In Progress + On Hold (tickets still being "
                "worked). 'closed' expands to Resolved + Closed (tickets no longer "
                "being worked). Note Resolved is in "
                "the CLOSED bucket, not open. Cancelled (state 8) is NEVER returned by a "
                "search and is not a value here — a 'canceled' request is rejected, so "
                "tell the user cancelled tickets are not surfaced rather than "
                "substituting another status. "
                "'all' expands to the five live states (open + closed). GATE: pass 'all' (or any "
                "closed word) ONLY when the user's own words ask for it — 'all'/'every' "
                "incident, closed/resolved, history, or a past time window. A "
                "topical ask ('incidents related to / for <X>') is NOT such a signal: "
                "OMIT this argument. Handling instructions wrapped around the user's "
                "question ('return each matching incident', 'not just a count', 'as a "
                "list') are NOT status words either — they say how to present the rows, "
                "never how wide to search. "
                "SAFE DEFAULT: if you OMIT this argument the tool returns OPEN tickets "
                "only — closed/resolved tickets are NEVER returned unless you "
                "ask for them with an explicit closed word: 'all', 'closed', "
                "'open,closed', or 'resolved'. To include resolved/closed "
                "tickets (historical or engineer-worked-on questions), pass 'all' (every "
                "live state), 'open,closed', or 'closed' for history only. So for 'what's "
                "broken now' / related-incident questions you can omit it (or pass "
                "'open'). Pass individual states (e.g. 'new,in_progress') for finer "
                "control; use 'closed only' for just the single Closed state without "
                "Resolved. A user who names ONE specific state ('resolved "
                "incidents', 'on hold tickets') gets EXACTLY that state — pass "
                "statuses='resolved' alone, NOT the 'closed' bucket."
            )
        ),
    ] = None,
    description_contains: Annotated[
        str | None,
        Field(
            description=(
                "THE content filter for ONE subject (several subjects: "
                "description_contains_or / description_contains_and; an assignment-"
                "group name goes in assignment_group, never here). "
                "Case-insensitive substring matched against the long description "
                "and the title; the description names the data source / business segment / system (e.g. "
                "'transaction ledger', 'Core Banking', 'databricks'). Pass ONE plain "
                "subject, a product NAME always in whole (a version or qualifier is "
                "part of it) — no % wildcards or quotes. The value must appear "
                "word-for-word as ONE piece of text, so every extra word only narrows: "
                "two subjects NEVER share this field, glued or reordered — they go in "
                "description_contains_and / _or."
            )
        ),
    ] = None,
    description_contains_or: Annotated[
        str | None,
        Field(
            description=(
                "Comma-separated subjects, e.g. 'vpn,printer': matches incidents "
                "whose long description contains ANY of them, in ONE call. Use it when "
                "the user names alternatives or a list — 'A or B', 'either A or B', "
                "'incidents for A and B' (each one's incidents). Each subject follows the "
                "description_contains rules."
            )
        ),
    ] = None,
    description_contains_and: Annotated[
        str | None,
        Field(
            description=(
                "Comma-separated subjects, e.g. 'contoso,databricks': matches only "
                "incidents whose long description contains EVERY one, in any order. Use "
                "it when both must be in the SAME incident — 'databricks incidents for "
                "contoso', 'A incidents involving B'. Each subject follows the "
                "description_contains rules."
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
    ci_contains: Annotated[
        str | None,
        Field(
            description=(
                "Case-insensitive substring matched against the CONFIGURATION ITEM "
                "(the pipeline/application the incident belongs to). The CI is "
                "populated on CLOSED incidents and not on all of them, so this "
                "filter EMPTIES a normal open-incident list — never set it there. "
                "Use it ONLY on a closed/resolved history search that is already "
                "narrowed to a concrete notebook or job name, e.g. "
                "description_contains='<notebook>' plus ci_contains='pl' to keep "
                "only the PL-* pipeline records that name the executing pipeline."
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
                "ServiceNow user CODE of the assignee, e.g. 'E1042' — NOT a sys_id "
                "and NOT the display name (a sys_id returns 0 records). PREFERRED over "
                "assigned_to_contains whenever you have the code (extract it from a "
                "'Name (CODE)' string). Do NOT also set resolved_by in the same call — "
                "the API ANDs them and returns ~0."
            )
        ),
    ] = None,
    resolved_by: Annotated[
        str | None,
        Field(
            description=(
                "ServiceNow user CODE of the resolver, e.g. 'E1042' (same rules as "
                "assigned_to: code only, never a sys_id; preferred over "
                "resolved_by_contains). Do NOT also set assigned_to in the same call — the "
                "API ANDs them and returns ~0."
            )
        ),
    ] = None,
    assigned_to_contains: Annotated[
        str | None,
        Field(
            description=(
                "Case-insensitive SUBSTRING of the assignee's name — a FIRST name or a "
                "LAST name alone is enough ('Alvarez', 'Chen'). Use this whenever "
                "the user names a person but you do NOT have their user code: it needs no "
                "code and no exact full name. Do NOT also set resolved_by/"
                "resolved_by_contains in the same call — the API ANDs them and returns ~0."
            )
        ),
    ] = None,
    resolved_by_contains: Annotated[
        str | None,
        Field(
            description=(
                "Case-insensitive SUBSTRING of the resolver's name — a FIRST or LAST name "
                "alone is enough (same rules as assigned_to_contains). Do NOT also set "
                "assigned_to/assigned_to_contains in the same call."
            )
        ),
    ] = None,
    assigned_to_name: Annotated[
        str | None,
        Field(
            description=(
                "EXACT-match fallback: the assignee's full name INCLUDING the user-ID code "
                "in parentheses, e.g. 'Rosa Alvarez (E1042)'. A bare name without "
                "the code returns ZERO, and partial names never match — so prefer "
                "assigned_to_contains (first OR last name alone) unless you specifically "
                "want an exact whole-name match. Never guess a code to satisfy this filter; "
                "use assigned_to_contains instead. Note: the assigned_to_name field comes "
                "back empty in the response body even when this filter matches, so read the "
                "assigned_to display value to confirm."
            )
        ),
    ] = None,
    resolved_by_name: Annotated[
        str | None,
        Field(
            description=(
                "EXACT-match fallback for the resolver: full name INCLUDING the "
                "parenthesized user-ID code (same rules as assigned_to_name — bare or "
                "partial names return ZERO). Prefer resolved_by_contains."
            )
        ),
    ] = None,
    caller_id: Annotated[
        str | None,
        Field(
            description=(
                "ServiceNow user CODE(s) of the CALLER — the person who REPORTED the "
                "ticket, which is NOT the assignee or resolver. Comma-separated to match "
                "any of several people, e.g. 'E0001,E0002'. Codes only, never a sys_id or "
                "a display name. Prefer caller_id_name_contains when you only have a name."
            )
        ),
    ] = None,
    caller_id_name_contains: Annotated[
        str | None,
        Field(
            description=(
                "Case-insensitive SUBSTRING of the CALLER's (reporter's) name — a FIRST or "
                "LAST name alone is enough ('Smith'). Use this whenever the user says a "
                "ticket was raised/reported/opened/submitted BY someone and you do not have "
                "their code. Unlike assigned_to/resolved_by, a caller filter CAN be combined "
                "with them ('reported by X and assigned to Y') — different fields."
            )
        ),
    ] = None,
    assignment_group: Annotated[
        str | None,
        Field(
            description=(
                "Optional. EXACT assignment-group name (or sys_id), comma-separated "
                "to match any of several. The match is on the FULL name — a partial "
                "one like 'Prod Support' returns nothing, so pass the whole name or "
                "omit this. Set it whenever the user gives a group's FULL name — one "
                "ending in SUPPORT, SUPPORT TEAM or ENGINEERING (optionally a tier like "
                "L2), e.g. '<APP> PROD SUPPORT' — and keep that name OUT of every content "
                "filter. Unset searches every group."
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
                "call, e.g. 'INC0001001,INC0001002'. ALWAYS use this for two or more "
                "numbers instead of calling servicenow_get_ticket_detail per ticket. It "
                "returns every named incident regardless of status (no status filter is "
                "applied), so closed/resolved ones come back too; the limit is sized to "
                "the count automatically (up to the backend max of 25 per call)."
            )
        ),
    ] = None,
    missing_data: Annotated[
        bool,
        Field(
            description=(
                "Set TRUE only for a missing-data incident search. The backend exhausts "
                "all pages matching the unchanged source/status filters and keeps only "
                "rows with concrete absence evidence (missing/uncaptured/dropped rows, "
                "missing files or periods, no data, stale or not-refreshed data, and "
                "equivalent signals); timeout and vendor-outage tickets carry no label "
                "and are never returned. This is a "
                "classification prefilter, not the final semantic verdict; still apply "
                "the missing-data rubric to every returned row before presenting it."
            )
        ),
    ] = False,
    pipeline_related: Annotated[
        bool,
        Field(
            description=(
                "Set TRUE for 'the pipeline / pipeline-related incidents for <source>'. "
                "The backend exhausts EVERY page matching the unchanged filters and "
                "keeps only genuine pipeline/ingest EXECUTION failures (notebook and "
                "job run errors, pipeline aborts, ingest/file-processing failures), "
                "dropping data-quality and application/web-workflow tickets that merely "
                "mention the source, and every timeout or vendor-outage ticket (those "
                "carry no label). OMIT statuses so open incidents in every open "
                "state are covered in ONE call. The returned count IS the answer: "
                "report it and list every returned incident — do not re-filter and do "
                "not paginate."
            )
        ),
    ] = False,
    newest_first: Annotated[
        bool,
        Field(
            description=(
                "Set TRUE whenever the user asks for RECENT / the MOST RECENT / LATEST / "
                "NEWEST incident(s), or for 'the last N' of something — e.g. 'the most "
                "recent incident for <app>', 'recent <app> incidents', 'the last 5 "
                "incidents for <app>'. It keeps the OPEN default (pass closed statuses "
                "only when the user asks for them) and searches titles as well as "
                "descriptions for the description_contains value. The backend exhausts "
                "EVERY page matching the filters, "
                "sorts by creation time NEWEST FIRST, and returns the top `limit` rows; "
                "the header still reports the FULL match count, so 'Found 48 incidents; "
                "showing 1-10' means those 10 ARE the newest 10 of 48. Pair it with "
                "limit=1 for a single 'the most recent incident for <X>' answer. "
                "The ServiceNow wrapper exposes NO sort parameter, so WITHOUT this flag "
                "results come back in the instance's own arbitrary order — never claim "
                "a row is the latest, and never eyeball opened_at across one page to "
                "pick it, unless you set this. Results are NOT pageable (the whole "
                "ordered set was computed in one pass): do not send an offset with it."
            )
        ),
    ] = False,
    limit: Annotated[
        int | None,
        Field(
            description=(
                "Maximum tickets to return. OMIT it for a normal list (the backend "
                "default applies). Values ABOVE the backend default are CLAMPED down "
                "to it — passing a big limit does nothing; to see more results, page "
                "with offset=<next_offset from the previous result> or narrow the "
                "filters. Pass a value only to request FEWER rows than the default."
            )
        ),
    ] = None,
    count: Annotated[
        int | None,
        Field(description="Alias for limit. Do not pass both unless they match. Values above the backend default are clamped to it."),
    ] = None,
    offset: Annotated[
        int | str | None,
        Field(
            description=(
                "Pagination cursor; omit for the first page. NEVER compute one — to "
                "page, re-issue the SAME query with offset set to the previous "
                "result's next_offset VERBATIM. It is an OPAQUE token: a plain "
                "integer, or one with a trailing '|INC…,INC…' segment naming the rows "
                "already shown. Pass the WHOLE value back — dropping the part after "
                "'|' makes incidents repeat on the next page. LOST IT? Do NOT omit this "
                "argument (that restarts at page 1 and repeats rows the user has already "
                "seen) — send '0|' followed by the incident numbers you already showed, "
                "e.g. '0|INC001,INC002'; those rows are skipped and paging continues."
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
    # Injected by LangGraph, never a model argument; absent on a direct call.
    state: Annotated[dict | None, InjectedState] = None,
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
        # (New + In Progress + On Hold) rather than every status. newest_first keeps it
        # too: it reads every matching page, and closed history runs to thousands.
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
        # Strip the "already shown" suffix before parsing: the offset half is a plain
        # int, the numbers half feeds the merge's skip set.
        offset, carried_seen = split_cursor(offset)
        int_offset = parse_offset(offset)
        # Checked BEFORE the paging rules below: an ordered result is computed in ONE
        # pass over the whole match set, so no offset can resume it. Falling through
        # would answer with a paging complaint and point the model at a next_offset
        # this mode never issues. Silently ignoring it is worse still — the newest
        # rows would come back a second time wearing "page 2", which no downstream
        # check can catch.
        if newest_first and int_offset:
            raise ServiceNowToolInputError(
                "newest_first results are not pageable — the whole match set is "
                "ordered in one pass. Re-issue the query without an offset, raising "
                "limit if you need more of the newest rows."
            )
        # Explicit ticket numbers are a DIRECT fetch by name: status is irrelevant (the
        # caller wants each named incident regardless of its state, exactly like
        # servicenow_get_ticket_detail and the raw API), so bypass the status filter —
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
        # PAGE-ALIGNED OFFSETS ONLY: every sanctioned next_offset is offset+len(rows),
        # and pages are full until the final one (has_more=false ends paging), so a
        # legitimate offset is always a multiple of the page size. A model that
        # invents an offset (e.g. 25 from the pre-clamp era while pages are now 10)
        # would silently skip records; reject it with the corrective instruction so
        # the retry reads next_offset instead. An offset carrying the '|INC…' suffix
        # is one this tool issued: lost-cursor recovery below reads several windows
        # and can stop mid-window ('19|INC…'), so it is exempt. ponytail: if the
        # wrapper ever serves a short page with has_more=true, relax this too.
        if int_offset % normalized_limit and not carried_seen:
            raise ServiceNowToolInputError(
                f"offset {int_offset} is not a multiple of the page size "
                f"{normalized_limit}. Never compute offsets — re-issue the SAME query "
                "with offset set to the exact next_offset value from the previous "
                "result (pages advance by the page size)."
            )
        field_filters = _build_field_filters(
            description_contains=description_contains,
            close_notes_contains=close_notes_contains,
            ci_contains=ci_contains,
            cause=cause,
            assigned_to=assigned_to,
            resolved_by=resolved_by,
            assigned_to_contains=assigned_to_contains,
            resolved_by_contains=resolved_by_contains,
            assigned_to_name=assigned_to_name,
            resolved_by_name=resolved_by_name,
            caller_id=caller_id,
            caller_id_name_contains=caller_id_name_contains,
            assignment_group=assignment_group,
            priority=priority,
            created_after=created_after,
            created_before=created_before,
            updated_after=updated_after,
            updated_before=updated_before,
            ticket_numbers=ticket_numbers,
            description_contains_and=description_contains_and,
            description_contains_or=description_contains_or,
        )
        # 'Show timeout incidents' went out with NO filter about 1 run in 4 (rewording
        # the prompt didn't move it) and every open incident came back as a timeout.
        # A subagent's task text is the user's ask: when it names a timeout or a vendor
        # outage, the search must carry one. Exempt: a fetch by number, and the
        # missing-data/pipeline modes, which drop those tickets by rule ('missing data,
        # not timeouts'). ponytail: a plain 'open incidents that aren't timeouts' is
        # still pushed onto the timeout filter; add a negation check if that shows up.
        ask = next(
            (m.text for m in reversed((state or {}).get("messages", [])) if m.type == "human"),
            "",
        )
        searched = " ".join(map(str, field_filters.values()))
        if (
            not (requested_numbers or missing_data or pipeline_related)
            and _TIMEOUT_OR_VENDOR_OUTAGE_RE.search(ask)
            and not _TIMEOUT_OR_VENDOR_OUTAGE_RE.search(searched)
        ):
            raise ServiceNowToolInputError(
                "The question asks about timeouts or vendor outages, but this search "
                "carries neither, so it returns incidents that are not about them. "
                "Re-issue it with the recipe filter — timeout: "
                "description_contains_or='timeout,timed out'; vendor outage: "
                "description_contains_and='vendor,outage' — keeping any data source in "
                "description_contains."
            )
        client = await get_servicenow_client()

        if missing_data or pipeline_related:
            return await _list_classified_candidates(
                client,
                statuses=normalized_statuses,
                field_filters=field_filters,
                subject=description_contains,
                detail=detail,
                keep=has_missing_data_evidence if missing_data else is_pipeline_related_incident,
                suppress_header=missing_data,
                # Order the classified set when asked, but never CAP it: these modes
                # promise the COMPLETE classified answer and the prompt tells the agent
                # to list every row, so trimming here would quietly break that contract.
                newest_first=newest_first,
            )

        if newest_first:
            # This mode never NARROWS: it keeps every row the same filters would have
            # returned and re-orders them. So no subject re-check (unlike the
            # classified modes above, which exist to narrow) — otherwise asking for
            # "the latest 10" would silently return fewer rows than the identical
            # query without the flag, on a second hidden axis. It does WIDEN on
            # purpose: titles as well as descriptions, since the set is exhausted
            # anyway. ponytail: exhausts every page — if an explicit closed ask on a
            # broad subject proves slow, add a created_after window.
            return await _list_classified_candidates(
                client,
                statuses=normalized_statuses,
                field_filters=field_filters,
                subject=None,
                detail=detail,
                keep=lambda _incident: True,
                suppress_header=False,
                newest_first=True,
                cap=normalized_limit,
                search_titles=True,
            )

        # LOST-CURSOR RECOVERY. The caller sent only the "already shown" half
        # ("0|INC001,INC002,…") because the real cursor did not survive the
        # delegation, so the query restarts at offset 0 over rows the earlier pages
        # already showed. One window is then NOT enough: after 20 rows were shown,
        # the window 0-9 holds nothing new and rows 20+ are never requested — the
        # page comes back empty though more were due. Read forward instead, one
        # known-good ``normalized_limit`` window at a time, until the page can be
        # filled. Only on this path: a real cursor already starts past the shown
        # rows, so it still takes exactly one call.
        lost_cursor = bool(carried_seen) and int_offset == 0

        def _unseen(rows: Iterable[Mapping[str, Any]]) -> int:
            return sum(
                1
                for row in rows
                if _reference_value(row.get("number")).upper() not in carried_seen
            )

        async def _list_state(
            filters: Mapping[str, Any] | None, start: int
        ) -> Mapping[str, Any]:
            envelope = await client.list_incidents(
                filters=filters, limit=normalized_limit, offset=start
            )
            if not lost_cursor:
                return envelope
            # The FIRST response owns the total: it answers "how many match", which
            # reading further windows must not change (and a later degraded reply
            # reporting None must not erase it).
            total = envelope.get("total_count")
            rows = list(envelope.get("incidents", []))
            unseen = _unseen(rows)
            while unseen < normalized_limit and envelope.get("has_more"):
                nxt = envelope.get("next_offset")
                # Never trust the cursor to move: a repeated or backwards offset
                # would re-read the same window forever.
                if not isinstance(nxt, int) or nxt <= start:
                    break
                start = nxt
                envelope = await client.list_incidents(
                    filters=filters, limit=normalized_limit, offset=nxt
                )
                page = list(envelope.get("incidents", []))
                if not page:
                    # The wrapper does sometimes claim has_more and then serve an
                    # empty page (real_empty_incidents). Stop, and stop advertising.
                    envelope = {**envelope, "has_more": False}
                    break
                rows.extend(page)
                unseen += _unseen(page)
            return {**envelope, "incidents": rows, "total_count": total}

        # ONE call however many states were asked for: the wrapper takes a comma list
        # of state codes (live-verified state=6,7: one total, one next_offset) and
        # answers with ONE result set, so one record offset pages it honestly. Field
        # filters ride the same call (AND semantics).
        query = (
            (field_filters or None)
            if normalized_statuses is None
            else {**field_filters, **_status_filters(normalized_statuses)}
        )
        content_keys = ("description_contains", "description_contains_and", "description_contains_or")
        twins = _title_twins([query]) if query and any(key in query for key in content_keys) else []
        if twins and "description_contains_and" not in query:
            # Every page re-reads the description and title sets in full, so a
            # one-subject search adds the titles only while the two stay small.
            # ponytail: past _TITLE_UNION_MAX_ROWS it pages the description alone and
            # misses title-only tickets; read pages in parallel if that bites live.
            heads = await asyncio.gather(
                *(client.list_incidents(filters=f, limit=1, offset=0) for f in [query, *twins])
            )
            sizes = [head.get("total_count") for head in heads]
            if None in sizes or sum(sizes) > _TITLE_UNION_MAX_ROWS:
                twins = []
        if twins:
            # Any subject may sit only in the title (see _title_twins), so the
            # description query and its title twins are each exhausted and unioned,
            # and the offset indexes that union. ponytail: n+1 exhausted calls per
            # page; cache the union per query if paging gets slow.
            pages = await asyncio.gather(
                *(_exhaust_incidents(client, f) for f in [query, *twins])
            )
            union: dict[str, Mapping[str, Any]] = {}
            for page_rows, _mode, _degraded in pages:
                for row in page_rows:
                    union.setdefault(_reference_value(row.get("number")).upper(), row)
            envelope = {
                "incidents": list(union.values())[int_offset:],
                "total_count": len(union),
                "has_more": False,
                "mode": next((page_mode for _, page_mode, _ in pages if page_mode), None),
                "degraded": any(page_degraded for _, _, page_degraded in pages),
            }
        else:
            envelope = await _list_state(query, int_offset)
        degraded = bool(envelope.get("degraded"))
        mode: str | None = envelope.get("mode")
        has_more = bool(envelope.get("has_more"))
        # The wrapper's grand total for the whole query. A missing total stays None:
        # a guessed one would look real while undercounting — the exact failure that
        # made "10 of 28" render as a bare "10" on one run and correctly on the next.
        total_count = envelope.get("total_count")
        rows = list(envelope.get("incidents", []))

        # How many rows the user has ALREADY been shown, so the header can name this
        # page's position ("showing 11-20") instead of a bare size. A real cursor
        # carries that count as its offset; lost-cursor recovery has only the numbers
        # the parent already printed — which is the count too.
        shown_before = len(carried_seen) if lost_cursor else int_offset

        # STATE BACKSTOP set: drop any row whose state was not requested. The call
        # already sends the state filter, but if the live wrapper ever ignores or
        # mishandles it, closed rows would silently ride an open-only query. Enforce
        # the contract locally so that can never reach the user. (None = direct
        # ticket_numbers fetch — status is irrelevant there.)
        allowed = (
            {"closed" if s == "closed_state" else s for s in normalized_statuses}
            if normalized_statuses is not None
            else None
        )

        # Fill the page in the wrapper's order. ``consumed`` counts rows shown OR
        # passed over (duplicate, already shown, wrong state) — exactly how far past
        # this offset the NEXT page must start.
        merged: list[Mapping[str, Any]] = []
        seen: set[str] = set()
        consumed = 0
        for incident in rows:
            if len(merged) >= normalized_limit:
                break
            consumed += 1
            number = _reference_value(incident.get("number")).upper()
            # Skip rows this page already holds AND rows the PREVIOUS page showed
            # (carried on the cursor). Both still count as consumed, so the next
            # offset steps past them instead of re-reading them forever.
            if number in seen or number in carried_seen:
                continue
            if (
                allowed is not None
                and _canonical_status(_reference_value(incident.get("state")))
                not in allowed
            ):
                continue
            seen.add(number)
            merged.append(incident)
        has_more = has_more or consumed < len(rows)

        # Carry THIS page's numbers so the next one can skip them if the live result
        # set shifts under us between the two calls. Only attach it while paging
        # continues — a final page has no successor to warn.
        page_numbers = ",".join(
            _reference_value(incident.get("number")).upper() for incident in merged
        )
        next_cursor: str | None = None
        if has_more and page_numbers and normalized_statuses is not None:
            next_cursor = f"{int_offset + consumed}{_CURSOR_SEEN_SEP}{page_numbers}"

        return normalize_ticket_list(
            merged,
            statuses=normalized_statuses,
            limit=normalized_limit,
            offset=int_offset,
            mode=mode,
            degraded=degraded,
            has_more=has_more,
            next_cursor=next_cursor,
            filters=field_filters,
            detail=detail,
            total_count=total_count,
            consumed=consumed,
            shown_before=shown_before,
        )
    except ServiceNowToolInputError as exc:
        return _error_payload(exc, kind="invalid_input")
    except (ServiceNowError, ServiceNowConfigurationError) as exc:
        return _error_payload(exc)


@tool
async def servicenow_find_similar_resolutions(
    ticket_number: Annotated[
        str,
        Field(description="The parent incident number whose failure needs a historical match."),
    ],
    source_subject: Annotated[
        str,
        Field(
            description=(
                "Words copied exactly as written, in one contiguous run, from the parent "
                "ticket's short description or description. For the data-pipeline "
                "families: ONE short source word or identifier (a data source, table or "
                "job name). For failure_family='general': the words that say WHAT WENT "
                "WRONG, never WHICH thing it went wrong on — first strike the app, "
                "product or team name (every ticket of that team carries it) and the "
                "queue, dataset, job, package, request type, report or host that was hit, "
                "then pass the shortest run left that names the problem: the step or part "
                "that broke without its verb (other tickets word the verb their own way). "
                "Placeholders are rejected, and the value must actually occur in the "
                "parent ticket text."
            )
        ),
    ],
    failure_family: Annotated[
        str,
        Field(
            description=(
                "One of the data-pipeline failure patterns: pipeline, pipeline_api, "
                "notebook, data_reconciliation, file_delivery, vendor, lag, "
                "connectivity — or 'general' for any other kind of incident (server "
                "or disk alerts, app or config problems), which matches the symptom "
                "phrase in source_subject instead of a failure pattern."
            )
        ),
    ],
    same_assignment_group: Annotated[
        bool,
        Field(
            description=(
                "Keep only history owned by the SAME assignment group as the parent "
                "incident — the team that handled it. Defaults to TRUE: two tickets "
                "with the same symptom but different owning teams are different "
                "problems with different fixes, so the owning team is part of what "
                "makes history comparable. Set FALSE only when the user explicitly "
                "asks for matches from other teams — never because a same-team search "
                "came back empty. Has no effect when the parent carries no assignment "
                "group."
            )
        ),
    ] = True,
) -> dict[str, Any]:
    """Summarize a parent incident and find its similar historical resolutions.

    Read the parent with servicenow_get_ticket_detail FIRST: source_subject and
    failure_family must come from its text, which the request alone does not carry.

    This is the only tool for the combined "summarize INC and find resolution notes for
    similar incidents" workflow. It validates the source and family against the fetched
    parent, forces Resolved/Closed history (Cancelled is never surfaced),
    exhausts the matching closed history internally, and applies deterministic source/
    family filtering — so a genuine match buried past the first broad page is never
    missed and the returned candidate set is always complete."""

    try:
        normalized_number = validate_ticket_number(ticket_number)
        subject = validate_content_filter("source_subject", source_subject)
        family = normalize_similar_resolution_family(failure_family)

        client = await get_servicenow_client()
        incident, envelope = await _fetch_incident(client, normalized_number)
        if incident is None:
            return _not_found_payload(normalized_number, degraded=bool(envelope.get("degraded")))

        # A 'general' symptom phrase is matched as one run (see the candidate filter),
        # so words picked apart from the parent would find nothing live.
        if not incident_matches_subject(incident, subject) or (
            family == "general" and not _incident_contains_phrase(incident, subject)
        ):
            # Its text rides along, so a guessed subject costs one retry, not a separate
            # servicenow_get_ticket_detail call first. Capped: pasted logs can run long.
            text = " ".join(_LITERAL_ESCAPE_RE.sub(" ", _incident_search_text(incident)).split())
            raise ServiceNowToolInputError(
                f"source_subject '{subject}' is not evidenced in {normalized_number}. "
                "Copy it exactly from the text below — one source word or identifier, or "
                "for 'general' the words that say what went wrong, never the app or team "
                f"name: {text[:1000]}"
            )
        if not matches_similar_resolution_family(incident, family):
            # Name the families that DO fit: told only "not evidenced", the model guessed
            # through them, one rejected call and parent fetch per guess. It also swapped
            # an evidenced source for a phrase ('daily feed') on the retry and matched none.
            evidenced = sorted(
                (f for f in _SIMILAR_RESOLUTION_FAMILIES if matches_similar_resolution_family(incident, f)),
                key=lambda f: (f == "general", f),
            )
            raise ServiceNowToolInputError(
                f"failure_family '{family}' is not evidenced in {normalized_number}. "
                f"Evidenced here: {', '.join(evidenced)}. Retry once with the one that "
                f"fits and the SAME source_subject '{subject}' — only 'general' takes "
                "the words that say what went wrong instead."
            )
        hit = None
        if family == "general":
            # The team's or app's own name rides on every ticket the team owns, so as
            # the subject it matched most of their history (14 rows for 3 real twins).
            # ponytail: catches names in the group/CI only; an app named only in the
            # title text is left to the prompt's subject rule.
            owner_words = set(
                re.findall(
                    r"[a-z0-9]+",
                    " ".join(
                        _reference_display(incident.get(field)).lower()
                        for field in ("assignment_group", "cmdb_ci", "configuration_item")
                    ),
                )
            )
            subject_words = set(re.findall(r"[a-z0-9]+", subject.lower()))
            if subject_words and subject_words <= owner_words:
                raise ServiceNowToolInputError(
                    f"source_subject '{subject}' only names the team or application that "
                    f"owns {normalized_number}, which every ticket of that team carries. "
                    "Pass the words that say what went wrong — the symptom, or the step "
                    "that failed — copied exactly from its text."
                )
            # 'SLA timer not starting' missed its twin 'SLA timer ... not working', and
            # 'stalls at the scan handoff' its twins 'stuck in the scan handoff':
            # other tickets word the verb their own way. A one-word name ('queue') keeps it.
            # ponytail: only a 'not <x>ing' tail or a leading/trailing stall/fail-type
            # verb; other verbs are left to the prompt.
            verb = (
                _SUBJECT_NOT_VERB_RE.match(subject)
                or _SUBJECT_LEAD_VERB_RE.match(subject)
                or _SUBJECT_TRAIL_VERB_RE.match(subject)
            )
            name = verb["name"] if verb else subject
            # A name ending in the package, queue, ... that was hit is never offered back:
            # '<name> queue' finds only that one queue's tickets. See the empty-result check.
            hit = _SUBJECT_HIT_THING_RE.search(name)
            name_words = set(re.findall(r"[a-z0-9]+", name.lower())) - {"the", "a", "an"}
            if verb and not hit and len(name_words) >= 2 and not name_words <= owner_words:
                raise ServiceNowToolInputError(
                    f"source_subject '{subject}' keeps its verb, which other tickets about "
                    "the same problem word their own way. Retry once with the name alone: "
                    f"'{name}'."
                )

        statuses = ("resolved", "closed_state")

        # A pipeline/notebook name alone still matched ~500 historical rows. The CI is
        # populated on closed incidents and names the pipeline, so narrowing the
        # history to PL-* CIs is what turns that pool into the handful of records that
        # actually answer "which pipeline executes this notebook".
        pipeline_ci_filter = "pl" if family in {"pipeline", "pipeline_api", "notebook"} else None

        # Similarity is symptom AND OWNING TEAM. The group is a reference field, so
        # compare DISPLAY names (the raw value is a sys_id) case-insensitively. A
        # parent with no group falls through unfiltered rather than matching nothing:
        # an empty group would otherwise equal every other empty group, silently
        # pairing unrelated teams' tickets. The same exact name also goes on the wire,
        # so a generic symptom does not exhaust every team's history only to discard it
        # here — and a targeted query that only other teams match now falls back to
        # the broad same-team search instead of ending empty.
        parent_group = _reference_display(incident.get("assignment_group")).strip()
        scope_by_group = same_assignment_group and bool(parent_group)
        group_filter = {"assignment_group": parent_group} if scope_by_group else {}

        async def exhaust_query(
            content: Mapping[str, str], ci_filter: str | None = None
        ) -> tuple[list[Mapping[str, Any]], str | None, bool]:
            filter_sets = [
                {
                    **content,
                    **({"ci_contains": ci_filter} if ci_filter else {}),
                    **group_filter,
                    **_status_filters(statuses),
                }
            ]
            # A symptom phrase often lives only in the alert's title. The pattern
            # families keep their tuned description-only retrieval.
            if family == "general":
                filter_sets += _title_twins(filter_sets)
            pages = await asyncio.gather(*(_exhaust_incidents(client, filters) for filters in filter_sets))
            mode: str | None = None
            degraded = False
            # A closed parent matches its own search and was listed as its own match.
            # Dropped here, not later, so "only the parent came back" still falls back.
            seen: set[str] = {normalized_number}
            rows: list[Mapping[str, Any]] = []
            for page_rows, page_mode, page_degraded in pages:
                mode = mode or page_mode
                degraded = degraded or page_degraded
                for row in page_rows:
                    number = _reference_value(row.get("number")).upper()
                    if number in seen:
                        continue
                    seen.add(number)
                    rows.append(row)
            return rows, mode, degraded

        # A targeted fingerprint query has already paid for a much smaller, exact
        # historical result set. Only fall back to the broad source/family search
        # when the targeted query truly finds nothing — never re-run both. A general
        # parent has no fingerprint: its identifiers are hosts and paths, and the
        # symptom phrase IS the query.
        fingerprint_tokens = () if family == "general" else derive_similarity_fingerprint(incident, family)
        rows, mode, degraded = await exhaust_query(
            # EVERY token, each as its own term: the live wrapper matches a value as
            # one contiguous piece of text, so tokens joined into ONE value ('a_b c_d')
            # matched nothing there (the mock's word-split matching hid it).
            {"description_contains_and": ",".join(fingerprint_tokens)}
            if fingerprint_tokens
            else {"description_contains": subject},
            # The CI narrowing rides with the targeted query only: the broad query is
            # the "found nothing" safety net and must not inherit a second narrowing.
            ci_filter=pipeline_ci_filter if fingerprint_tokens else None,
        )
        if fingerprint_tokens and not rows:
            rows, mode, degraded = await exhaust_query({"description_contains": subject})

        candidates = [
            row
            for row in rows
            if incident_matches_subject(row, subject)
            and (family != "general" or _incident_contains_phrase(row, subject))
            and matches_similar_resolution_family(row, family)
            and (
                not scope_by_group
                or _reference_display(row.get("assignment_group")).strip().lower()
                == parent_group.lower()
            )
        ]
        # 'cannot install <name> package' found none of its twins, which name another
        # package. Only on an empty result: 'Access request' or 'sent to the wrong queue'
        # is the problem itself and finds its twins as typed.
        if hit and not candidates and not degraded:
            thing = hit["thing"].lower()
            raise ServiceNowToolInputError(
                f"No other closed incident matched '{subject}': it names which {thing} "
                f"was hit, and the same problem on another {thing} names that one instead. "
                "Retry once with the words that say what went wrong, leaving out which "
                f"{thing} it was."
            )
        candidate_tickets = [
            normalize_ticket_detail(row, mode=mode)["ticket"] for row in candidates
        ]
        rendered_answer = _render_similar_resolution_answer(
            normalize_ticket_detail(incident)["ticket"],
            _similar_resolution_header(len(candidate_tickets), parent_group if scope_by_group else ""),
            (_similar_resolution_candidate_block(ticket) for ticket in candidate_tickets),
            _historical_pipeline_ci_summary(candidate_tickets, family),
        )
        return {
            "ok": True,
            "source": SOURCE,
            "kind": "similar_resolution_search",
            "rendered_answer": rendered_answer,
            "degraded": degraded or bool(envelope.get("degraded")),
        }
    except ServiceNowToolInputError as exc:
        return _error_payload(exc, kind="invalid_input")
    except (ServiceNowError, ServiceNowConfigurationError) as exc:
        return _error_payload(exc)


SERVICENOW_TOOLS = [
    servicenow_get_ticket_detail,
    servicenow_list_tickets,
    servicenow_find_similar_resolutions,
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
    "derive_similarity_fingerprint",
    "is_pipeline_related_incident",
    "has_missing_data_evidence",
    "incident_matches_subject",
    "matches_similar_resolution_family",
    "normalize_cause",
    "normalize_similar_resolution_family",
    "normalize_status",
    "normalize_status_filters",
    "normalize_ticket_detail",
    "normalize_ticket_list",
    "resolve_ticket_limit",
    "servicenow_find_similar_resolutions",
    "servicenow_get_ticket_detail",
    "servicenow_list_tickets",
    "validate_content_filter",
    "validate_ticket_number",
]
