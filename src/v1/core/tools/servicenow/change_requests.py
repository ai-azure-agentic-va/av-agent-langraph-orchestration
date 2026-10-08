"""LangChain tool for ServiceNow change requests (``CHG…``).

Answers "are there any recent changes related to <app>?": CLOSED changes about
the subject by default, most recently implemented first, each with its closure
code and notes, so a support team can see what changed before an incident. Open
or other states only when the user names them ("open changes for <app>").
Registered on the ServiceNow subagent next to the incident and knowledge tools.

The endpoint has no sort parameter and returns the most recently UPDATED changes
first, while the ask is "most recently IMPLEMENTED" (the Actual End Date,
``work_end``), so this tool reads the matching rows and sorts them itself.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Mapping
from typing import Annotated, Any

from langchain_core.tools import tool
from pydantic import Field

from v1.core.tools.servicenow.tools import (
    SOURCE,
    ServiceNowToolInputError,
    _error_payload,
    _incident_timestamp,
    _validate_date_bound,
    get_servicenow_client,
    strip_meta_words,
)
from v1.utils.clients.servicenow import (
    ServiceNowClient,
    ServiceNowConfigurationError,
    ServiceNowError,
    _reference_display,
    _reference_value,
)

MAX_CHANGE_LIMIT = 20
try:
    DEFAULT_CHANGE_LIMIT = min(
        max(int(os.getenv("SERVICENOW_CHANGE_DEFAULT_LIMIT", "10")), 1), MAX_CHANGE_LIMIT
    )
except ValueError:
    DEFAULT_CHANGE_LIMIT = 10

# ponytail: reads at most 4 pages of 50 per search before sorting. Past that, only the
# 200 most recently UPDATED matches are sorted, which still holds the most recently
# ended ones because a change is closed (updated) right after its work ends. Raise the
# cap if a team's closed history outgrows it.
_PAGE_SIZE = 50
_MAX_ROWS_READ = 200

# ServiceNow change states. A change is "open" until it is Closed or Cancelled.
_STATE_CODES = {
    "new": "-5",
    "assess": "-4",
    "authorize": "-3",
    "scheduled": "-2",
    "implement": "-1",
    "review": "0",
    "closed": "3",
    "cancelled": "4",
}
_OPEN_STATES = ("new", "assess", "authorize", "scheduled", "implement", "review")
# The user's words for a set of states. Cancelled is only ever searched when named.
_STATUS_WORDS = {
    "open": _OPEN_STATES,
    "pending": _OPEN_STATES,
    "upcoming": _OPEN_STATES,
    "in progress": ("implement",),
    "canceled": ("cancelled",),
    "all": (*_OPEN_STATES, "closed"),
}

# Words that describe the ASK, not its subject. The endpoint ANDs every word of the
# keyword, so "Ledger Hub changes" would require the word "changes" in the text too.
_CHANGE_META_RE = re.compile(r"\b(?:recent(?:ly)?|change\s+requests?|changes?)\b", re.IGNORECASE)

# Where a change says what it is about. The configuration item and business service
# name the application even when the title does not ("Deploy <app> notice letter").
_SUBJECT_FIELDS = ("short_description", "description", "cmdb_ci", "business_service")

_NOT_AVAILABLE = "Not available"


def _plain(raw: Any) -> str:
    """A field's ``value`` side: UTC for dates, line-normalized for notes.

    ``display_value`` is US Eastern for dates and raw CRLF text for notes. Dates are
    shown through ``_incident_timestamp``, labelled UTC so the UI localizes them.
    """

    if isinstance(raw, Mapping):
        raw = raw.get("value")
    return "" if raw is None else str(raw).strip()


def _text(raw: Any) -> str:
    """Free text on one line, so it stays inside its ``- **Label:**`` bullet."""

    return " ".join(_plain(raw).split())


def resolve_states(statuses: str) -> list[str]:
    """The state names to search. Nothing named means Closed, the default ask."""

    names: list[str] = []
    for word in (w.strip().lower() for w in statuses.split(",") if w.strip()):
        for name in _STATUS_WORDS.get(word, (word,)):
            if name not in _STATE_CODES:
                raise ServiceNowToolInputError(
                    f"unknown status '{word}'. Use 'open', 'closed', 'all', or one of: "
                    + ", ".join(_STATE_CODES)
                )
            if name not in names:
                names.append(name)
    return names or ["closed"]


def _status_label(names: list[str]) -> str:
    """'closed', 'open', '' for open + closed, else the states named."""

    if names == ["closed"]:
        return "closed"
    if set(names) == set(_OPEN_STATES):
        return "open"
    if set(names) == {*_OPEN_STATES, "closed"}:
        return ""
    return " or ".join(names)


def subject_terms(keyword: str) -> list[str]:
    """The subject's words as search terms, each in its singular form.

    'Ledger Invoices' -> ['Ledger', 'Invoice']. A change about an application rarely repeats
    the user's exact phrase: it says "<app> Invoice Details", or names the app only in
    its configuration item. The singular is a substring of the plural, so it finds
    both, and each word is searched on its own and then required together.
    """

    terms: list[str] = []
    for word in keyword.split():
        word = word.strip(".,;:!?()[]{}\"'")
        if len(word) > 3 and word[-1] in "sS" and word[-2:].lower() != "ss":
            word = word[:-1]
        # A lone '-' or '&' between words is not a search term.
        if any(c.isalnum() for c in word) and word.lower() not in (t.lower() for t in terms):
            terms.append(word)
    return terms


def _about(record: Mapping[str, Any], terms: list[str]) -> bool:
    """Whether every term appears in the change's title, description, CI or service."""

    text = " ".join(_reference_value(record.get(field)) for field in _SUBJECT_FIELDS).lower()
    return all(term.lower() in text for term in terms)


def normalize_change(record: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce a raw change request to the fields the agent may show. ``sys_id`` is dropped."""

    return {
        "number": _plain(record.get("number")).upper(),
        "title": " ".join(_reference_value(record.get("short_description")).split()),
        "type": _reference_display(record.get("type")),
        # The gateway drops a zero integer, so a Review (state 0) change has no state.
        "state": _reference_display(record.get("state")) or "Review",
        "risk": _reference_display(record.get("risk")),
        # Live often sends people with an empty display name; never the raw sys_id.
        "assigned_to": _reference_display(record.get("assigned_to")),
        "assignment_group": _reference_display(record.get("assignment_group")),
        "planned_start": _plain(record.get("start_date")),
        "planned_end": _plain(record.get("end_date")),
        "actual_start": _plain(record.get("work_start")),
        "actual_end": _plain(record.get("work_end")),
        "close_code": _reference_display(record.get("close_code")),
        "close_notes": _text(record.get("close_notes")),
        "description": _text(record.get("description")),
        "implementation_plan": _text(record.get("implementation_plan")),
        "backout_plan": _text(record.get("backout_plan")),
        "change_url": _reference_value(record.get("change_url")) or None,
    }


def _render_change(item: Mapping[str, Any], *, full: bool = False) -> str:
    """One change in the requirement template's shape.

    A Closed change always shows the template's fields (Change Number, Assigned To,
    Assignment Group, Actual End Date, Closure Code, Closure Notes), 'Not available'
    when empty. A change that is not Closed has no closure yet, so it shows its
    State and planned dates instead. ``full`` is the CHG-number lookup: the whole
    change, plans included, its extra fields dropped when empty. The record already
    carries every field, so it costs no extra call.
    """

    number = item["number"] or "Unknown"
    head = f"[{number}]({item['change_url']})" if item["change_url"] else number
    head = f"**Change Number:** {head}"
    if item["title"]:
        head = f"{head} — {item['title']}"
    closed = item["state"] == "Closed"
    # (label, value, always_shown). Always-shown fields print 'Not available' when
    # blank; the rest only when they have a value.
    if full:
        fields = (
            ("Type", item["type"], False),
            ("State", item["state"], False),
            ("Risk", item["risk"], False),
            ("Assigned To", item["assigned_to"], True),
            ("Assignment Group", item["assignment_group"], True),
            ("Planned Start", _incident_timestamp(item["planned_start"]), False),
            ("Planned End", _incident_timestamp(item["planned_end"]), False),
            ("Actual Start Date", _incident_timestamp(item["actual_start"]), False),
            ("Actual End Date", _incident_timestamp(item["actual_end"]), closed),
            ("Closure Code", item["close_code"], closed),
            ("Closure Notes", item["close_notes"], closed),
            ("Description", item["description"], False),
            ("Implementation Plan", item["implementation_plan"], False),
            ("Backout Plan", item["backout_plan"], False),
        )
    elif closed:
        fields = (
            ("Assigned To", item["assigned_to"], True),
            ("Assignment Group", item["assignment_group"], True),
            ("Actual End Date", _incident_timestamp(item["actual_end"]), True),
            ("Closure Code", item["close_code"], True),
            ("Closure Notes", item["close_notes"], True),
        )
    else:
        fields = (
            ("State", item["state"], True),
            ("Assigned To", item["assigned_to"], True),
            ("Assignment Group", item["assignment_group"], True),
            ("Planned Start", _incident_timestamp(item["planned_start"]), False),
            ("Planned End", _incident_timestamp(item["planned_end"]), False),
            ("Actual End Date", _incident_timestamp(item["actual_end"]), False),
            ("Closure Code", item["close_code"], False),
            ("Closure Notes", item["close_notes"], False),
        )
    return "\n".join(
        [head]
        + [
            f"- **{label}:** {value or _NOT_AVAILABLE}"
            for label, value, always in fields
            if value or always
        ]
    )


def _render_answer(
    items: list[dict[str, Any]], *, total: int | None, subject: str, title: str, label: str
) -> str:
    kind = f"{label} change requests" if label else "change requests"
    if not items:
        return f"No {kind} found{subject}."
    # Closed changes sort by when the work ended; the rest by when it is planned.
    order = {"closed": " by Actual End Date", "": ""}.get(label, " by Planned Start")
    shown = len(items)
    if total is None:
        # A search hit the read cap, so the full match count is unknown.
        header = f"Showing the {shown} most recent {kind}{subject}{order}."
    else:
        if total == 1:
            kind = kind.replace("requests", "request")
        header = f"Found {total} {kind}{subject}"
        if total > shown:
            header += f"; showing the {shown} most recent{order}."
        else:
            header += f", most recent first{order}."
    # Numbered, with detail lines indented to nest under the number.
    return "\n\n".join(
        [f"### {title}", header]
        + [
            f"{index}. " + _render_change(item).replace("\n", "\n" + " " * len(f"{index}. "))
            for index, item in enumerate(items, 1)
        ]
    )


def _resolve_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_CHANGE_LIMIT
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ServiceNowToolInputError("limit must be an integer")
    return min(max(limit, 1), MAX_CHANGE_LIMIT)


async def _read(
    client: ServiceNowClient, filters: dict[str, Any]
) -> tuple[list[dict[str, Any]], bool]:
    """Every row of one search, up to the read cap. Returns ``(rows, truncated)``."""

    rows: list[dict[str, Any]] = []
    while len(rows) < _MAX_ROWS_READ:
        envelope = await client.list_change_requests(
            filters=filters, limit=_PAGE_SIZE, offset=len(rows)
        )
        page = envelope.get("change_requests") or []
        rows.extend(page)
        if not page or not envelope.get("has_more"):
            return rows, False
    return rows, True


@tool
async def servicenow_list_change_requests(
    query: Annotated[
        str,
        Field(
            description=(
                "The application, system or subject the changes are about, e.g. "
                "'Ledger Hub'. Matched in each change's title, description, "
                "configuration item and business service. Pass the NAME only, never "
                "the user's sentence and never words like 'recent', 'changes' or "
                "'change requests'. Empty lists every change in the asked states."
            )
        ),
    ] = "",
    statuses: Annotated[
        str,
        Field(
            description=(
                "OMIT unless the user's own words name a state; omitted means Closed. "
                "'open' (also pending / upcoming) = every state before Closed: New, "
                "Assess, Authorize, Scheduled, Implement, Review. 'all' = open and "
                "closed. Or name states, comma-separated: new, assess, authorize, "
                "scheduled, implement (in progress), review, closed, cancelled. "
                "Cancelled only when the user says cancelled."
            )
        ),
    ] = "",
    change_number: Annotated[
        str,
        Field(
            description=(
                "ONE change number the user named (e.g. 'CHG0001234'). Fetches that "
                "change in any state and ignores the other fields."
            )
        ),
    ] = "",
    assignment_group: Annotated[
        str,
        Field(
            description=(
                "Optional EXACT full assignment-group name, only when the user names "
                "a team (e.g. '<APP> PROD SUPPORT')."
            )
        ),
    ] = "",
    assigned_to: Annotated[
        str,
        Field(
            description=(
                "Optional person the changes are assigned to: their name as the user "
                "wrote it, or an employee ID. Never put a person in `query`."
            )
        ),
    ] = "",
    ended_after: Annotated[
        str,
        Field(
            description=(
                "Only changes implemented (Actual End Date) on/after this moment: "
                "'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS'. Only for an explicit window "
                "('last 7 days', 'since October 1'); compute it from "
                "get_current_datetime. Omit for a plain 'recent'."
            )
        ),
    ] = "",
    limit: Annotated[
        int,
        Field(description=f"Max changes to return (1-{MAX_CHANGE_LIMIT})."),
    ] = DEFAULT_CHANGE_LIMIT,
) -> dict[str, Any]:
    """Search ServiceNow CHANGE REQUESTS (CHG…): the changes made to an application.

    Use for "are there any recent changes related to <app>?", "retrieve recent change
    requests for <app>", "what changed in <app> lately", "open changes for <app>", or
    a CHG number. Returns CLOSED changes by default, most recently implemented first
    (Actual End Date), each with its assignee, assignment group, closure code and
    closure notes; open or other states when the user names them. Not for incidents
    or procedures: those have their own tools.
    """

    try:
        resolved_limit = _resolve_limit(limit)
        names = resolve_states(statuses if isinstance(statuses, str) else "")
        label = _status_label(names)
        filters: dict[str, Any] = {"state": ",".join(_STATE_CODES[n] for n in names)}
        subject = ""
        # "recent changes" alone names no subject: that lists every change asked for.
        keyword = " ".join(_CHANGE_META_RE.sub(" ", query if isinstance(query, str) else "").split())
        if keyword:
            keyword = strip_meta_words(keyword)
            subject = f" related to '{keyword}'"
        prefix = "Recent" if label == "closed" else label.capitalize()
        title = " ".join(part for part in (prefix, keyword, "Changes") if part)
        group = assignment_group.strip() if isinstance(assignment_group, str) else ""
        if group:
            filters["assignment_group"] = group
            subject += f" for {group}"
        person = assigned_to.strip() if isinstance(assigned_to, str) else ""
        if person:
            filters["assigned_to"] = person
            subject += f" assigned to {person}"
        if isinstance(ended_after, str) and ended_after.strip():
            filters["actual_end_time_after"] = _validate_date_bound("ended_after", ended_after)
        number = re.sub(r"\s", "", change_number).upper() if isinstance(change_number, str) else ""
        if number and not re.fullmatch(r"CHG\d+", number):
            raise ServiceNowToolInputError(
                "change_number must be ONE change number like 'CHG0001234'; "
                "call once per number."
            )

        client = await get_servicenow_client()
        if number:
            # A direct fetch, in whatever state the change is in.
            rows, _ = await _read(client, {"number": number})
            items = [normalize_change(row) for row in rows[:1]]
            rendered = (
                _render_change(items[0], full=True)
                if items
                else f"No change request {number} was found."
            )
        else:
            terms = subject_terms(keyword)
            # One search per term, then only the changes that carry EVERY term.
            # ponytail: one search per word; fine for a 1-3 word name. Pick the
            # rarest words first if long subjects ever make this slow.
            searches = [{**filters, "keyword": term} for term in terms] or [filters]
            results = await asyncio.gather(*(_read(client, f) for f in searches))
            unique: dict[str, dict[str, Any]] = {}
            for rows, _ in results:
                for row in rows:
                    unique.setdefault(_plain(row.get("number")).upper(), row)
            matches = [row for row in unique.values() if _about(row, terms)]
            truncated = any(cut for _, cut in results)
            if not matches and keyword and not group:
                # "Changes related to <TEAM>": a team name never appears in a change's
                # text, so an empty subject search gets one try as the exact group.
                rows, truncated = await _read(client, {**filters, "assignment_group": keyword})
                if rows:
                    matches, subject = rows, f" for {keyword}"
            items = sorted(
                (normalize_change(row) for row in matches),
                # When the work ended, else when it is planned to start. A change
                # with neither sorts last, never posing as the newest.
                key=lambda item: item["actual_end"] or item["planned_start"],
                reverse=True,
            )[:resolved_limit]
            rendered = _render_answer(
                items,
                total=None if truncated else len(matches),
                subject=subject,
                title=title,
                label=label,
            )
        return {
            "ok": True,
            "source": SOURCE,
            "kind": "change_request_search",
            "rendered_answer": rendered,
            "change_requests": items,
        }
    except ServiceNowToolInputError as exc:
        return _error_payload(exc, kind="invalid_input")
    except (ServiceNowError, ServiceNowConfigurationError) as exc:
        return _error_payload(exc)


__all__ = [
    "DEFAULT_CHANGE_LIMIT",
    "MAX_CHANGE_LIMIT",
    "normalize_change",
    "resolve_states",
    "servicenow_list_change_requests",
    "subject_terms",
]
