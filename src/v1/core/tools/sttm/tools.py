"""The sttm-agent's tools: STTM sessions kept in the conversation's state.

Each tool reads the sessions from the ``sttm_sessions`` channel (through the
injected :class:`ToolRuntime`), works on one session with :mod:`.session`, and
writes it back with a :class:`Command`. ``sttm_workbook`` builds a session's
workbooks (:mod:`.workbook`): it keeps their layout steps in the
``sttm_layouts`` channel, and the files in the platform's store for the analyst
to download (:mod:`.files`), never in the state. What a tool records is the
analyst's latest message as they sent it, with the text of any PDF attached to
it, never the model's restatement of it: the sttm-agent reads it from the
conversation in code (:mod:`v1.core.subagents.sttm.runnable`) and hands it to
the tools in the run's config (:data:`MESSAGE_KEY`). A value said to be the
analyst's has to be found in those words, and an input other than a
specification has to come with the words of the message that hold it. The
message keeps its id (the turn's run id when it has none), so every task in a
turn shares it, and a review opened in one turn can only be accepted in a later
one.

An answer says what changed, what was refused and why, and what comes next. A
call that changes nothing answers without touching state. No STTM call runs
after one that changes a session or a layout in the same model step, and of the
STTM tasks the orchestrator sends in one step, only one may change them
(:func:`_sessions`).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import traceback
from collections import OrderedDict
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal, Required

from deepagents.backends.utils import create_file_data
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.types import Command

# pydantic reads a tool argument's TypedDict only from typing_extensions on Python < 3.12.
from typing_extensions import TypedDict

from v1.core.middlewares.user_context import caller_field
from v1.core.tools.sttm.export import export_session
from v1.core.tools.sttm.files import (
    KeptFile,
    download_url,
    keep_file,
    new_file_id,
    workbook_file_id,
)
from v1.core.tools.sttm.render import (
    attributes_view,
    change_text,
    render,
    sessions_view,
    summary_view,
    table_view,
    unresolved_required,
    unset_optional,
    validation_view,
)
from v1.core.tools.sttm.rules import available_rule_files
from v1.core.tools.sttm.session import (
    GATES,
    GENERATED,
    INPUT_KINDS,
    REQUIRED_INPUTS,
    TABLES,
    Change,
    FeedbackEntry,
    Session,
    SessionError,
    Step,
    Tables,
    advance,
    approve,
    ensure_editable,
    gaps,
    input_blockers,
    log_message,
    new_version,
    record_input,
    set_audit_fields,
    set_derived_fields,
    set_details,
    set_review_mode,
    start_session,
    timestamp,
    update_cells,
)
from v1.core.tools.sttm.spec import normalize_name
from v1.core.tools.sttm.workbook import (
    MAX_STEPS,
    MEDIA_TYPE,
    Built,
    Match,
    Op,
    StepError,
    build_workbooks,
    describe_step,
    normalize_steps,
)

logger = logging.getLogger(__name__)

InputKind = Literal[
    "source_system_instructions",
    "source_instructions",
    "specification",
    "derived_fields",
    "asl_schema",
]
ReviewMode = Literal["fast", "moderate", "extensive", "default"]
View = Literal[
    "summary",
    "inputs",
    "attributes",
    "file_details",
    "data_dictionary",
    "raw_to_curated",
    "curated_to_asl",
    "validation",
    "feedback",
    "layout",
    "sessions",
]
# Changes and refusals listed in one answer; the rest are counted.
MAX_LISTED = 30
# Where the sttm-agent puts the analyst's latest message in the run's config
# (subagents/sttm/runnable.py): its id, its text with any attached PDF's, and
# notes on the files not read. A "__" key is not copied into the run's metadata.
MESSAGE_KEY = "__sttm_message"
# Orchestrator steps whose claim (_claim) is remembered; the oldest are forgotten.
MAX_CLAIMS = 1024
# The arguments with which sttm_workbook changes a layout; without them it only builds.
LAYOUT_ARGS = ("steps", "remove_steps", "reset")


class InputItem(TypedDict, total=False):
    """One input the analyst's message holds."""

    kind: Required[InputKind]
    # The words of the message that hold it, copied exactly; for a specification,
    # only when the message holds several sources' tables (its table's heading or row).
    passage: str


class DerivedFieldInput(TypedDict, total=False):
    """A derived field, in the analyst's words."""

    name: Required[str]
    logic: str
    join_key: str


class CellUpdate(TypedDict):
    """One cell to set, or to clear with a null value."""

    table: str
    attribute: str
    column: str
    value: str | None


class FileDetails(TypedDict, total=False):
    """The file details to set; leave out what is not given."""

    primary_key: list[str]
    join_keys: list[str]
    cmdb_number: str
    file_pattern: str
    delimiter: str
    header_rows: int
    trailer_rows: int
    frequency: str
    expected_arrival: str


class LayoutStepInput(TypedDict, total=False):
    """One layout step: its op, and the fields that op takes."""

    op: Required[Op]
    sheet: str
    columns: list[str]
    name: str
    position: int
    width: float
    wrap: bool
    on: bool
    descending: bool
    match: Match
    value: str
    color: str
    choices: list[str]


# --- Helpers -----------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _build_label(session: Session | None, session_id: str | None, thread_id: str | None) -> str:
    """Which session and thread a workbook build's log line is about."""

    if session is None:
        about = f"session {session_id or '(the latest)'}"
    else:
        about = (
            f"session {session.id} v{session.version} ({session.status}, "
            f"{len(session.attributes)} attribute(s))"
        )
    return f"{about}, thread {thread_id or '(none)'}"


def _built_line(built: Built) -> str:
    """Each built workbook: from the template or generic (and why), and its size."""

    parts = []
    for workbook in built.workbooks:
        how = "from the template" if workbook.template else f"generic ({workbook.reason})"
        parts.append(f"{workbook.key} {how}, {len(workbook.content)} bytes")
    return "; ".join(parts)


def _error_line(error: BaseException) -> str:
    """``error`` in one line: type, message, where it was raised and its notes.

    Container log viewers show each line of a traceback as its own row, so the
    line that reports a failure says what is needed to place it; the traceback
    still follows. A library's frame is followed by the last one of ours.
    """

    message = " ".join(str(error).split())[:300]
    frames = traceback.extract_tb(error.__traceback__)
    ours = [frame for frame in frames if _source(frame.filename).startswith("v1/")]
    shown = frames[-1:] + [frame for frame in ours[-1:] if frame is not frames[-1]]
    where = " via ".join(f"{_source(f.filename)}:{f.lineno} in {f.name}" for f in shown)
    line = f"{type(error).__name__}: {message}"
    if where:
        line += f" at {where}"
    return line + "".join(f", {note}" for note in getattr(error, "__notes__", ()))


def _source(filename: str) -> str:
    """``filename`` from its package down (``v1/...``, ``openpyxl/...``)."""

    path = filename.replace("\\", "/")
    for marker in ("/site-packages/", "/src/"):
        if marker in path:
            return path.rsplit(marker, 1)[1]
    return path.rsplit("/", 1)[-1]


# The STTM task that may change the sessions in each orchestrator step, by the
# step's checkpoint id.
_CLAIMS: OrderedDict[str, str] = OrderedDict()
_CLAIMS_LOCK = threading.Lock()


def _claim(runtime: ToolRuntime) -> None:
    """Refuse a change from a second STTM task sent in the same orchestrator step.

    Tasks sent together each work on the sessions as the step found them, and
    the state keeps one task's copy of a session, so only the first of them to
    change a session may change any. LangGraph names the task in the call's
    checkpoint namespace (``<task's node>:<task id>|<tool's node>:<id>``, after
    any parent graphs) and the step in its checkpoint map. A call made outside a
    graph has neither, and is not refused.
    """

    configurable = (runtime.config or {}).get("configurable") or {}
    segments = str(configurable.get("checkpoint_ns") or "").split("|")
    if len(segments) < 2:
        return
    step = (configurable.get("checkpoint_map") or {}).get("|".join(segments[:-2]))
    if not step:
        return
    task = segments[-2]
    with _CLAIMS_LOCK:
        owner = _CLAIMS.setdefault(step, task)
        _CLAIMS.move_to_end(step)
        while len(_CLAIMS) > MAX_CLAIMS:
            _CLAIMS.popitem(last=False)
    if owner != task:
        raise SessionError(
            "Another STTM task sent at the same time is changing the sessions, and this task "
            "works on the copy from before it, so nothing it changes could be kept. Nothing was "
            "changed. Answer only that this task has to be sent again, on its own."
        )


def _changes_state(call: Mapping[str, Any]) -> bool:
    """Whether a tool call may change a session or a layout."""

    if call.get("name") == "sttm_workbook":
        args = call.get("args") or {}
        return any(args.get(name) for name in LAYOUT_ARGS)
    return call.get("name") in SESSION_CHANGING_TOOLS


def _sessions(runtime: ToolRuntime, *, changes: bool) -> dict[str, Session]:
    """The conversation's sessions, for an STTM call that may run now.

    Calls made in one model step all start from the same copy of a session and
    its layout, and the state keeps only one of their results, so no STTM call
    runs after one that changes either in the same step: it is refused, and
    made again once that one has returned. The same holds for the STTM tasks the
    orchestrator sends in one step: a call that ``changes`` them is refused
    unless its task is the first of them to make a change (:func:`_claim`).
    """

    state = runtime.state or {}
    for message in reversed(state.get("messages") or []):
        calls = getattr(message, "tool_calls", None) or []
        ids = [call.get("id") for call in calls]
        if runtime.tool_call_id in ids:
            earlier = calls[: ids.index(runtime.tool_call_id)]
            changing = next((call["name"] for call in earlier if _changes_state(call)), None)
            if changing:
                what = "a layout" if changing == "sttm_workbook" else "the session"
                raise SessionError(
                    f"{changing} changes {what} in this same step, and this call would not see "
                    "that change. Make this call again now."
                )
            break
    if changes:
        _claim(runtime)
    stored = state.get("sttm_sessions") or {}
    return {session_id: Session.model_validate(data) for session_id, data in stored.items()}


def _message(runtime: ToolRuntime) -> Mapping[str, Any]:
    """The analyst's latest message, as the sttm-agent read it from the conversation.

    It keeps the message's id, or takes the turn's run id when it has none, so
    every task in one turn shares it, and a review it opens can only be accepted
    in a later turn.
    """

    config = runtime.config or {}
    configurable = config.get("configurable") or {}
    message = configurable.get(MESSAGE_KEY)
    if not isinstance(message, Mapping):
        raise SessionError(
            "The analyst's message did not reach this tool, so nothing can be recorded. This is "
            "a configuration problem: tell the analyst, and do not retry."
        )
    # The run id reaches a subagent's tools in the metadata, as ai_search reads it.
    turn = (
        message.get("id")
        or (config.get("metadata") or {}).get("run_id")
        or configurable.get("run_id")
    )
    if not turn:
        raise SessionError(
            "This run has no run id to tell the analyst's messages apart, so nothing can be "
            "recorded. This is a configuration problem: tell the analyst, and do not retry."
        )
    return {**message, "id": str(turn)}


def _pick(sessions: Mapping[str, Session], session_id: str | None) -> Session:
    """The session named, or the one changed last."""

    if session_id and session_id.strip():
        # Ids are upper case; one copied in lower case still finds its session.
        session = sessions.get(session_id.strip().upper())
        if session is None:
            raise SessionError(
                f"No STTM session {session_id!r} in this conversation (sessions: "
                f"{', '.join(sessions) or 'none'})."
            )
        return session
    if not sessions:
        raise SessionError(
            "There is no STTM session in this conversation yet; start one with sttm_start_session."
        )
    return max(
        sessions.values(), key=lambda session: (session.updated_at, session.revision, session.id)
    )


def _saved(
    session: Session, runtime: ToolRuntime, text: str, *, now: datetime, stale: bool = True
) -> Command:
    """``text`` as the tool's answer, and ``session`` written back to state.

    ``stale``: the change may leave the final summary out of date, so the
    session has to be advanced to it again before it can be approved.
    """

    session.revision += 1
    session.updated_at = timestamp(now)
    if stale:
        session.ready_on = None
        session.validation = None
    return Command(
        update={
            "sttm_sessions": {session.id: session.model_dump(mode="json")},
            "messages": [ToolMessage(text, tool_call_id=runtime.tool_call_id)],
        }
    )


def _record_feedback(
    session: Session,
    message: Mapping[str, Any],
    what: str,
    changes: list[Change],
    refused: list[str],
    now: datetime,
) -> None:
    summary = f"{what}: {len(changes)} changed" + (f", {len(refused)} refused" if refused else "")
    session.feedback.append(
        FeedbackEntry(
            at=timestamp(now), message_id=str(message["id"]), summary=summary, changes=changes
        )
    )


def _listed(items: list[str]) -> list[str]:
    lines = [f"- {item}" for item in items[:MAX_LISTED]]
    if len(items) > MAX_LISTED:
        lines.append(f"- … and {len(items) - MAX_LISTED} more")
    return lines


def _outcome_text(session: Session, changes: list[Change], refused: list[str]) -> str:
    lines = [f"Session {session.id}: {len(changes)} change(s):"]
    lines += _listed([change_text(change) for change in changes])
    if refused:
        lines.append(f"Refused ({len(refused)}); these did not change:")
        lines += _listed(refused)
    return "\n".join(lines)


def _unchanged(session: Session, refused: list[str]) -> str:
    if not refused:
        return f"Session {session.id}: nothing changed; the values are already as given."
    return "\n".join(
        [f"Session {session.id}: nothing changed. Refused ({len(refused)}):", *_listed(refused)]
    )


def _missing_inputs(session: Session) -> list[str]:
    recorded = {record.kind for record in session.inputs}
    return [INPUT_KINDS[kind] for kind in REQUIRED_INPUTS if kind not in recorded]


def _approver() -> str | None:
    """The signed-in user approving, as "name (email)", or whichever of them is known."""

    name, email = (str(caller_field(key) or "").strip() for key in ("name", "email"))
    if name and email:
        return f"{name} ({email})"
    return name or email or None


_REVIEW_WAIT = (
    "Then wait for the analyst's answer: make the corrections they ask for first, and call "
    "sttm_advance(confirm_review=True) in the message that accepts the review."
)
_GATE_ASK = {
    "input_review": (
        "Show the analyst what was understood from their inputs, below, with the open "
        "questions, and ask them to confirm it or correct it."
    ),
    "dd_review": (
        "Show the analyst the Data Dictionary, below, with the values not decided yet, and ask "
        "them to confirm it or correct it."
    ),
    "sttm_review": (
        "Show the analyst the Raw-to-Curated and Curated-to-ASL STTMs, below, with the values "
        "not decided yet, and ask them to confirm them or correct them."
    ),
}
_EXTENSIVE = (
    "Extensive review: offer to list every row, not just this page (sttm_show with offset), "
    "so the analyst can review each field."
)


def _gate_text(session: Session, gate: str) -> str:
    head = f"Session {session.id}: the {GATES[gate]} is open. {_GATE_ASK[gate]} {_REVIEW_WAIT}"
    parts = [head]
    if gate == "input_review":
        parts += [summary_view(session), attributes_view(session)]
    elif gate == "dd_review":
        parts.append(table_view(session, "data_dictionary"))
    else:
        parts += [table_view(session, "raw_to_curated"), table_view(session, "curated_to_asl")]
    if session.review_mode == "extensive":
        parts.append(_EXTENSIVE)
    return "\n\n".join(parts)


def _generated_text(session: Session, stage: str) -> str:
    tables = Tables(session)
    built = (
        ["data_dictionary"] if stage == "data_dictionary" else ["raw_to_curated", "curated_to_asl"]
    )
    lines = [f"Session {session.id}: built the {GENERATED[stage]}."]
    if stage == "data_dictionary" and session.gates["input_review"].status == "skipped":
        questions = gaps(session)
        if questions:
            lines.append(
                f"The input review is skipped in {session.review_mode} mode, so tell the "
                "analyst these are still open:"
            )
            lines += _listed(questions)
    for table in built:
        missing = unresolved_required(tables, table)
        open_ = ", ".join(f"{column} ({count} row(s))" for column, count in missing.items())
        optional = unset_optional(tables, table)
        lines.append(
            f"- {TABLES[table]}: {len(tables.rows(table))} row(s); required, not decided yet: "
            f"{open_ or 'nothing'}"
            + (f"; optional, still empty: {', '.join(optional)}" if optional else "")
        )
    present = ", ".join(available_rule_files())
    rules = (
        f"values a rule file gives (source='rule', naming it; present: {present})"
        if present
        else "no values from rule files (none is present)"
    )
    lines.append(
        "Before the next step, fill in what the inputs decide (sttm_update with cells): values "
        f"the analyst gave (source='analyst'), {rules}, and drafts for descriptive columns only "
        "(source='draft'). attribute='*' sets a table default, used by every row without a "
        "value of its own. A value nothing decides stays unresolved: do not guess it. Then "
        "call sttm_advance again."
    )
    return "\n".join(lines)


def _step_text(session: Session, step: Step) -> str:
    if step.kind == "blocked":
        lines = [f"Session {session.id} cannot move on yet:", *_listed(step.problems)]
        questions = gaps(session)
        if questions:
            lines.append("Ask the analyst about these at the same time:")
            lines += _listed(questions)
        lines.append(
            "Meanwhile, set the values the analyst gave (sttm_update). If they asked for the "
            "workbooks, build them now (sttm_workbook): what is missing does not stop a build."
        )
        return "\n".join(lines)
    if step.kind == "gate":
        if step.reminder:
            return f"Session {session.id}: the {GATES[step.gate]} is still open. {step.reminder}"
        return _gate_text(session, step.gate)
    if step.kind == "generated":
        return _generated_text(session, step.stage)
    return "\n\n".join(
        [
            f"Session {session.id}: every review is done. Show the analyst this final summary "
            "and the validation results, and ask them to approve. Approval has to come in a "
            "later message from the analyst (sttm_advance with approve=True); approving over a "
            "failed check needs their reason.",
            summary_view(session, results=session.validation),
            validation_view(session, session.validation),
        ]
    )


def _approve(
    session: Session, runtime: ToolRuntime, message: Mapping[str, Any], reason: str | None
) -> Command:
    """Approve ``session`` and say so; raises SessionError when it cannot be approved."""

    now = _now()
    approve(session, message, override_reason=reason, by=_approver(), now=now)
    approval = session.approval
    if approval is None:  # approve() records it or raises
        raise SessionError(f"session {session.id} was not approved.")
    by = f" by {approval.by}" if approval.by else ""
    lines = [
        f"Session {session.id} (version {session.version}) is approved{by} at {approval.at}, "
        "and is now read-only."
    ]
    if approval.override_reason:
        lines.append(
            f"Approved over failed checks ({', '.join(approval.failed_checks)}), for the "
            f'analyst\'s reason: "{approval.override_reason}".'
        )
    lines.append(
        f"Changing it takes a new version: sttm_start_session(new_version_of='{session.id}'). "
        "sttm_workbook builds its workbooks for the analyst to download; "
        "sttm_show(export='json') writes it to files."
    )
    return _saved(session, runtime, "\n".join(lines), now=now, stale=False)


def _export(session: Session, format: str, runtime: ToolRuntime) -> Command:
    """``session`` written to the conversation's files; raises SessionError when it can't be."""

    files = export_session(session, format)
    status = "approved" if session.locked else "not approved yet"
    lines = [
        f"Exported session {session.id} (version {session.version}, {status}) to the "
        "conversation's files:",
        *(f"- {file.path}: {file.description}" for file in files),
        "They describe the session as it is now; export again after a change.",
    ]
    return Command(
        update={
            "files": {file.path: create_file_data(file.content) for file in files},
            "messages": [ToolMessage("\n".join(lines), tool_call_id=runtime.tool_call_id)],
        }
    )


def _layout(
    session: Session, sessions: Mapping[str, Session], layouts: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], Session | None]:
    """The layout steps of ``session``'s workbooks, and the version they come from
    when they are not its own.

    A new version uses the layout of the nearest earlier version that has one,
    until its first layout change gives it its own copy.
    """

    current: Session | None = session
    seen: set[str] = set()
    while current is not None and current.id not in seen:
        seen.add(current.id)
        entry = layouts.get(current.id)
        if entry is not None:
            steps = [dict(step) for step in entry.get("steps") or []]
            return steps, None if current is session else current
        current = sessions.get(current.supersedes) if current.supersedes else None
    return [], None


# After a layout change: how each request became a step shows only in the answer.
_SHOW_LAYOUT = (
    "Show the analyst the layout below, numbered as here, so they can check how their request "
    "was read."
)


def _layout_lines(steps: list[dict[str, Any]], inherited: Session | None) -> list[str]:
    if not steps:
        return ["Layout: the default, with no layout steps."]
    if inherited is None:
        head = (
            f"Layout: {len(steps)} step(s), applied to every build of this session and of its "
            "later versions:"
        )
    else:
        head = (
            f"Layout: {len(steps)} step(s) from version {inherited.version} (session "
            f"{inherited.id}); the first layout change gives this version its own copy:"
        )
    return [head, *(f"{number}. {describe_step(step)}" for number, step in enumerate(steps, 1))]


def _kept_steps(
    current: list[dict[str, Any]], remove: list[int] | None, reset: bool
) -> list[tuple[int, dict[str, Any]]]:
    """The steps a call keeps, each with its number in the layout as it was."""

    if reset:
        return []
    numbers = set(remove or [])
    unknown = sorted(number for number in numbers if not 1 <= number <= len(current))
    if unknown:
        held = f"{len(current)} step(s)" if current else "no steps"
        raise SessionError(
            f"there is no step {', '.join(map(str, unknown))} to remove: the layout has {held}. "
            "Nothing was changed."
        )
    return [(number, step) for number, step in enumerate(current, 1) if number not in numbers]


def _step_refusal(error: StepError, kept: list[tuple[int, dict[str, Any]]]) -> str:
    """A step the workbooks refused, numbered as the analyst knows it: a kept step
    by its number in the layout, a new one by its place in the call."""

    if error.number <= len(kept):
        number = kept[error.number - 1][0]
        return (
            f"kept step {number}{error.reason} Nothing was changed. Remove that step "
            f"(remove_steps=[{number}]) or start over (reset=True)."
        )
    return f"new step {error.number - len(kept)}{error.reason} Nothing was changed."


def _numbers(noun: str, numbers: list[int]) -> str:
    if len(numbers) == 1:
        return f"{noun} {numbers[0]}"
    return f"{noun}s {', '.join(map(str, numbers[:-1]))} and {numbers[-1]}"


def _workbook_text(
    session: Session,
    built: Built,
    files: list[KeptFile],
    changes: list[str],
    layout: list[dict[str, Any]],
    inherited: Session | None,
) -> str:
    status = "approved" if session.locked else "not approved yet"
    lines = [
        (
            f"Session {session.id} (version {session.version}, {status}): built both workbooks "
            "from the session as it is now."
        )
    ]
    for workbook, kept in zip(built.workbooks, files, strict=True):
        origin = (
            "from the organization's template"
            if workbook.template
            else f"laid out from the session's own columns, because {workbook.reason}"
        )
        line = (
            f"- [{workbook.label}]({download_url(kept.id)}), {origin}. Sheets: "
            f"{', '.join(sheet.title for sheet in workbook.sheets)}."
        )
        if workbook.drafts:
            line += f" Your drafts: {workbook.drafts} cell(s), filled yellow."
        if workbook.unresolved:
            line += f" Not decided yet: {workbook.unresolved} cell(s), filled red."
        lines.append(line)
    lines.append(
        f"Each link works until {files[0].expires_at:%Y-%m-%d %H:%M} UTC, for the signed-in "
        "analyst only. Give the analyst the links exactly as written here, each in its "
        "[label](path) form (the chat cannot open a bare path), and until when they work. The "
        "files hold the session as it is now: build them again after a change."
    )
    blockers = input_blockers(session)
    if blockers:
        lines.append(f"Not complete yet (tell the analyst, with the links): {' '.join(blockers)}")
    if changes:
        lines.append(" ".join([*changes, _SHOW_LAYOUT]))
    lines += _layout_lines(layout, inherited)
    if built.notes:
        lines.append("Notes:")
        lines += _listed(list(built.notes))
    lines.append("For your layout steps, not for the analyst: the columns of each mapping sheet.")
    lines += [
        f"- {sheet.title}: {', '.join(sheet.columns)}"
        + (f" (hidden: {', '.join(sheet.hidden)})" if sheet.hidden else "")
        for workbook in built.workbooks
        for sheet in workbook.sheets
        if sheet.kind == "table"
    ]
    return "\n".join(lines)


# --- Tools -------------------------------------------------------------------


@tool
def sttm_start_session(
    runtime: ToolRuntime,
    source_system: str = "",
    source: str = "",
    review_mode: ReviewMode | None = None,
    new_version_of: str | None = None,
    new_session: bool = False,
) -> Command | str:
    """Start an STTM session for one source, or a new version of an approved session.

    One session covers one source's Data Dictionary and both STTMs. A new session
    carries over the latest source-system and source instructions recorded in this
    conversation for the same source system and source. An approved session is
    read-only: changing it takes a new version, which starts as a copy of it. A
    source with a session not approved yet gets no second one unless the analyst
    asks for it: carry on with that session instead.

    Args:
        source_system: The source system, as the analyst names it.
        source: The source (feed or file) within it, as the analyst names it.
        review_mode: fast, moderate or extensive, only when the analyst asked for one by
            name; default stops at every review.
        new_version_of: The id of an approved session to start a new version of; leave
            source_system and source empty.
        new_session: True only when the analyst asked for a new session for a source that
            already has one not approved yet.
    """

    try:
        message = _message(runtime)
        sessions = _sessions(runtime, changes=True)
        now = _now()
        notes: list[str] = []
        if new_version_of:
            previous = _pick(sessions, new_version_of)
            session = new_version(previous, sessions, now=now)
            log_message(session, message)
            opening = (
                f"Started session {session.id}: version {session.version} of "
                f"{session.source_system} / {session.source}, a copy of approved session "
                f"{previous.id}, which stays as it was. Make the analyst's changes, then "
                "sttm_advance to the final summary for approval."
            )
        else:
            same = sorted(
                (
                    other
                    for other in sessions.values()
                    if not other.locked
                    and normalize_name(other.source_system) == normalize_name(source_system)
                    and normalize_name(other.source) == normalize_name(source)
                ),
                key=lambda other: (other.updated_at, other.revision, other.id),
            )
            if same and not new_session:
                raise SessionError(
                    f"{source_system.strip()} / {source.strip()} already has session(s) "
                    f"{', '.join(other.id for other in same)}, not approved yet, so no new "
                    f"session was started. Carry on with session {same[-1].id}: record this "
                    f"message's inputs and changes with session_id={same[-1].id}. Only if the "
                    "analyst asked for a new session, call again with new_session=true."
                )
            session, notes = start_session(
                sessions, source_system=source_system, source=source, message=message, now=now
            )
            opening = (
                f"Started STTM session {session.id} for {session.source_system} / {session.source}."
            )
            if same:
                notes.append(
                    f"Session(s) {', '.join(other.id for other in same)} for the same source are "
                    "not approved yet; the analyst asked for a new one."
                )
        if review_mode is not None:
            try:
                set_review_mode(session, review_mode, message)
            except SessionError as error:
                notes.append(f"Review mode not set: {error}")
    except SessionError as error:
        return f"Not done: {error}"
    lines = [opening, *_listed(notes)]
    lines.append(f"Review mode: {session.review_mode or 'none chosen, so every review stops'}.")
    missing = _missing_inputs(session)
    if missing:
        lines.append(
            "Still needed: " + ", ".join(missing) + ". Record what the analyst's message holds "
            "(sttm_record_input) and ask for the rest."
        )
    return _saved(session, runtime, "\n".join(lines), now=now)


@tool
def sttm_record_input(
    runtime: ToolRuntime,
    inputs: list[InputItem],
    replace: bool = False,
    session_id: str | None = None,
) -> Command | str:
    """Record the analyst's current message, exactly as they sent it, as session inputs.

    The tool reads the analyst's message itself, with the text of the PDFs they
    attached: never paste or restate it. A message holding several inputs
    (instructions and a specification, say) is recorded once, with an item for each.
    Each input but a specification needs its passage: a sentence or so of the input
    itself, copied exactly from where the message holds it (the typed text, a skill
    block or an attached document; at least 4 words in a row, not from the
    specification's table). A sentence that only says where an input is ("the
    instructions are in the attached PDF") is not its passage. Instructions count only
    where the analyst names them so: under a heading or after a line such as
    "Source-system instructions:", or in a file or skill named for them; values for the
    tables under other headings are for sttm_update. An input whose passage is not
    found is not recorded; the others are. A specification has to be a table with a
    header row, pipe- or tab-separated (a PDF's ruled table is read as one); its
    attributes are read exactly as written, and a specification sent over several
    messages adds up. A message with several sources' specifications is recorded in
    each source's session, each with the passage of its own table.

    Args:
        inputs: The inputs the message holds, each with a kind and its passage. The
            kinds: source_system_instructions (how the source system's data is to be
            handled), source_instructions (how this one source's data is to be handled;
            often not given), specification, derived_fields, asl_schema. A
            specification's passage only when the message holds several sources'
            tables: the heading above this source's table or a row of it. The analyst's
            request (a session to start, its source and review mode, what to build) is
            none of them.
        replace: True when the message replaces what earlier messages gave for these kinds
            (a corrected specification, say) instead of adding to it, or records this
            message again (with another passage).
        session_id: The session; by default the one changed last.
    """

    try:
        message = _message(runtime)
        sessions = _sessions(runtime, changes=True)
        session = _pick(sessions, session_id)
        now = _now()
        others = [other for other in sessions.values() if other.id != session.id]
        result = record_input(
            session, list(inputs), message, replace=replace, now=now, others=others
        )
    except SessionError as error:
        return f"Not done: {error}"
    refusals = (
        [f"Not recorded: {', '.join(result.refused)}.", *_listed(result.reasons)]
        if result.refused
        else []
    )
    if not result.recorded:
        return "\n".join(
            [
                (
                    f"Session {session.id}: this message is already recorded as "
                    f"{', '.join(result.already)}; nothing changed."
                ),
                *refusals,
            ]
        )
    lines = [
        f"Session {session.id}: recorded the analyst's message as {', '.join(result.recorded)}."
    ]
    lines += _listed([f'{label}, from: "{text}"' for label, text in result.passages.items()])
    if result.already:
        lines.append(f"Already recorded as: {', '.join(result.already)}.")
    lines += refusals
    if INPUT_KINDS["specification"] in result.recorded:
        lines.append(
            f"Specification: {result.attributes_added} attribute(s) read from this message, "
            f"{len(session.attributes)} in the session."
        )
        lines += _listed(result.tables)
        lines += _listed([f"Note: {note}" for note in result.notes])
    if INPUT_KINDS["derived_fields"] in result.recorded:
        lines.append(
            "Declare each derived field it defines with sttm_update(source='analyst', "
            "derived_fields=...), copying the analyst's name, logic and join key."
        )
    missing = _missing_inputs(session)
    lines.append(
        f"Still needed: {', '.join(missing)}."
        if missing
        else "Next: set the file details and derived fields the analyst gave (sttm_update), "
        "then sttm_advance."
    )
    replaced = " (replacing earlier messages)" if replace else ""
    changes = [
        Change(
            table="inputs",
            attribute=label,
            column="recorded",
            new=f"message {message['id']}{replaced}",
        )
        for label in result.recorded
    ]
    _record_feedback(session, message, "Inputs", changes, result.refused, now)
    return _saved(session, runtime, "\n".join(lines), now=now)


@tool
def sttm_update(
    runtime: ToolRuntime,
    source: Literal["analyst", "rule", "draft"],
    rule_file: str | None = None,
    cells: list[CellUpdate] | None = None,
    details: FileDetails | None = None,
    derived_fields: list[DerivedFieldInput] | None = None,
    remove_derived_fields: list[str] | None = None,
    no_derived_fields: bool | None = None,
    audit_fields: list[str] | None = None,
    review_mode: ReviewMode | None = None,
    session_id: str | None = None,
) -> Command | str:
    """Set or correct what a session holds, every value in the call from one source.

    Each value is checked against its source on its own: one that fails is refused
    with the reason, and the others still change. Nothing is guessed. A correction
    changes only what it names, and the latest value wins.
    - analyst: in the analyst's own messages to this session (the pasted
      specification does not count). The only source of derived fields and of the
      review mode.
    - rule: from rule_file, which has to be present; a value from a closed list
      (classification, PII/PCI, handling) has to appear in it. The only source of
      audit fields, the standard technical columns a rule file adds.
    - draft: your own wording, for descriptive columns only (business description,
      business attribute name), and file details proposed for the analyst to
      confirm at the input review while it is still ahead; never a CMDB number.

    Args:
        source: Where every value in this call comes from: analyst, rule or draft.
        rule_file: The rule file the values come from, when source is rule.
        cells: Table values, each with table (data_dictionary, raw_to_curated or
            curated_to_asl), attribute (a row's name, or "*" for the table default,
            used by every row without a value of its own), column (its key or label)
            and value (null clears it). The rows are the specification's attributes,
            the derived fields and the audit fields; no other row can be added.
        details: File details: primary_key, join_keys (an empty list when the analyst
            said there are none), cmdb_number, file_pattern, delimiter (tab, pipe,
            comma, semicolon, fixed width, or the character itself), header_rows,
            trailer_rows, frequency, expected_arrival.
        derived_fields: Derived fields to declare or change, each with its name, logic and
            join key exactly as the analyst wrote them.
        remove_derived_fields: Names of derived fields to remove.
        no_derived_fields: True when the analyst said the source has no derived fields.
        audit_fields: The audit field names, exactly as the rule file writes them. They
            replace the whole list, all or nothing; an empty list removes them.
        review_mode: fast, moderate or extensive, only when the analyst asked for one by
            name; default stops at every review.
        session_id: The session; by default the one changed last.
    """

    given = {key: value for key, value in (details or {}).items() if value is not None}
    try:
        message = _message(runtime)
        session = _pick(_sessions(runtime, changes=True), session_id)
        ensure_editable(session)
    except SessionError as error:
        return f"Not done: {error}"
    now = _now()
    changes: list[Change] = []
    refused: list[str] = []
    what: list[str] = []
    # The mode first: it decides whether file details can still be proposed.
    if review_mode is not None:
        what.append("review mode")
        try:
            if source != "analyst":
                raise SessionError("Review mode: only the analyst chooses it (source='analyst').")
            change = set_review_mode(session, review_mode, message)
        except SessionError as error:
            refused.append(str(error))
        else:
            changes += [change] if change else []
    if given:
        what.append("file details (proposed)" if source == "draft" else "file details")
        if source == "rule":
            refused.append(
                "File details: not from a rule file. They are the analyst's (source='analyst'), "
                "or proposed for the analyst to confirm (source='draft')."
            )
        else:
            outcome = set_details(session, given, message, proposed=source == "draft", now=now)
            changes += outcome.changes
            refused += outcome.refused
    if derived_fields or remove_derived_fields or no_derived_fields is not None:
        what.append("derived fields")
        if source != "analyst":
            refused.append("Derived fields: only the analyst declares them (source='analyst').")
        else:
            outcome = set_derived_fields(
                session,
                [dict(field) for field in derived_fields or []],
                list(remove_derived_fields or []),
                no_derived_fields,
                message,
                now=now,
            )
            changes += outcome.changes
            refused += outcome.refused
    if audit_fields is not None:
        what.append("audit fields")
        try:
            if audit_fields and source != "rule":
                raise SessionError(
                    "Audit fields: they come from a rule file (source='rule', with rule_file)."
                )
            outcome = set_audit_fields(session, list(audit_fields), rule_file, message)
        except SessionError as error:
            refused.append(str(error))
        else:
            changes += outcome.changes
    if cells:
        what.append(f"values ({source})")
        try:
            outcome = update_cells(
                session,
                [dict(cell) for cell in cells],
                message,
                source=source,
                rule_file=rule_file,
                now=now,
            )
        except SessionError as error:
            refused.append(str(error))
        else:
            changes += outcome.changes
            refused += outcome.refused
    if not what:
        return "Not done: nothing was given to set."
    if not changes:
        return _unchanged(session, refused)
    _record_feedback(session, message, ", ".join(what).capitalize(), changes, refused, now)
    return _saved(session, runtime, _outcome_text(session, changes, refused), now=now)


@tool
def sttm_advance(
    runtime: ToolRuntime,
    confirm_review: bool = False,
    approve: bool = False,
    override_reason: str | None = None,
    session_id: str | None = None,
) -> Command | str:
    """Move the session to its next step and get what to show the analyst there, or approve it.

    The steps, in order: the input review (gate 1), building the Data Dictionary, the
    Data Dictionary review (gate 2), building both STTMs, the STTM review (gate 3), and
    the final summary for approval. The review mode decides which reviews are
    skipped. A review stops for the analyst: show what the answer gives you and wait.
    It can only be confirmed in a later analyst message, by calling this with
    confirm_review=True when that message accepts it. A built stage stops for you:
    fill in its values, then call this again.

    approve=True approves the session instead, only after the final summary and only
    in a later analyst message that approves it. When validation fails, approving
    needs the analyst's reason for approving anyway, quoted from that message. The
    approver and the time are recorded; the session becomes read-only, and changing
    it takes a new version.

    Args:
        confirm_review: True only when the analyst's current message accepts the open review.
        approve: True only when the analyst's current message approves the final summary.
        override_reason: With approve, the analyst's reason for approving despite failed
            checks, in their own words.
        session_id: The session; by default the one changed last.
    """

    try:
        message = _message(runtime)
        session = _pick(_sessions(runtime, changes=True), session_id)
        if approve:
            return _approve(session, runtime, message, override_reason)
        before = session.model_dump(mode="json", exclude={"messages"})
        step = advance(session, message, confirm_review=confirm_review)
    except SessionError as error:
        return f"Not done: {error}"
    text = _step_text(session, step)
    if session.model_dump(mode="json", exclude={"messages"}) == before:
        return text
    return _saved(session, runtime, text, now=_now(), stale=False)


@tool
def sttm_show(
    runtime: ToolRuntime,
    view: View = "summary",
    offset: int = 0,
    limit: int = 25,
    columns: list[str] | None = None,
    export: Literal["json"] | None = None,
    session_id: str | None = None,
) -> Command | str:
    """Show part of a session as Markdown, to show the analyst, or write it to files.

    Views: summary (where the session stands, and the open questions), inputs,
    attributes (as the specification gives them), file_details, data_dictionary,
    raw_to_curated, curated_to_asl, validation (runs every check now), feedback (every
    change, newest first), layout (the workbooks' layout steps, numbered as
    sttm_workbook's remove_steps takes them) and sessions (every session in this
    conversation). Tables are paged.

    export='json' writes the session to the conversation's files instead of showing a
    view: the whole session (every table cell with its source) and the session
    summary, at any stage; the files say whether the session is approved. It does not
    change the session. The Data Dictionary and STTM workbooks come from sttm_workbook.

    Args:
        view: The view.
        offset: The first row to show, for the next page.
        limit: How many rows to show, at most 100.
        columns: Only these columns of a table view.
        export: json, to write the session to files instead.
        session_id: The session; by default the one changed last.
    """

    try:
        sessions = _sessions(runtime, changes=False)
        if export is not None:
            return _export(_pick(sessions, session_id), export, runtime)
        if view == "sessions":
            return sessions_view(sessions)
        session = _pick(sessions, session_id)
        if view == "layout":
            layouts = (runtime.state or {}).get("sttm_layouts") or {}
            steps, inherited = _layout(session, sessions, layouts)
            return "\n".join(
                [f"Session {session.id}: the workbooks' layout.", *_layout_lines(steps, inherited)]
            )
        return render(session, view, offset=offset, limit=limit, columns=columns)
    except SessionError as error:
        return f"Not done: {error}"


@tool
async def sttm_workbook(
    runtime: ToolRuntime,
    steps: list[LayoutStepInput] | None = None,
    remove_steps: list[int] | None = None,
    reset: bool = False,
    session_id: str | None = None,
) -> Command | str:
    """Build the session's Data Dictionary and STTM workbooks for the analyst to download.

    Builds both .xlsx files from the session as it is now, at any stage, and answers
    with a download link to each, which works for 24 hours, for the signed-in analyst
    only. Values come from the session alone: change them with sttm_update, never
    here. Layout steps change only how the workbooks look, as the analyst asks. They
    are kept for the session and its later versions, and every build applies them;
    sttm_show(view='layout') lists them. A step names a sheet by its title and
    columns by their headers.

    Ops, with their fields (? optional):
    - rename_sheet: sheet, name. move_sheet: sheet, position (from 1).
    - order_columns: sheet, columns (these first, in this order).
    - hide_columns, show_columns: sheet, columns (hidden in Excel, data kept).
    - remove_columns: sheet, columns (left out of the file).
    - add_column: sheet, name, position?: an empty column, for the analyst's notes.
    - format_columns: sheet, columns, width? (4-100), wrap?.
    - freeze: sheet, columns? (frozen through the last one), on? (false unfreezes).
    - filter: sheet, on?. sort: sheet, columns (the first sorts first), descending?.
    - highlight: sheet, columns (one), match (equals, contains, empty, not_empty),
      value? (for equals and contains), color: fills the rows that match.
    - header: color, sheet? (every mapping sheet when none).
    - dropdown: sheet, columns, choices (an empty list removes it).
    Colors: yellow, orange, red, green, blue, grey, purple, each also as dark <color>
    (dark blue), or a hex code.

    Args:
        steps: Layout steps to add after the kept ones, applied in order.
        remove_steps: Numbers of kept steps to remove, as the layout lists them.
        reset: True to drop every kept step first, for the default layout.
        session_id: The session; by default the one changed last.
    """

    configurable = (runtime.config or {}).get("configurable") or {}
    thread_id = str(configurable.get("thread_id") or "") or None
    owner = str(caller_field("identity") or "")
    store = runtime.store
    if not owner or store is None:
        logger.warning(
            "STTM workbooks not built (thread %s): the run has %s",
            thread_id or "(none)",
            "no signed-in user" if not owner else "no store",
        )
        return (
            "Not done: this run has no signed-in user or no store to keep files in, so no "
            "workbook can be offered for download. This is a configuration problem: tell the "
            "analyst, and do not retry."
        )
    given = list(steps or [])
    now = _now()
    session: Session | None = None
    layout: list[dict[str, Any]] = []
    try:
        sessions = _sessions(runtime, changes=bool(given or remove_steps or reset))
        session = _pick(sessions, session_id)
        layouts = (runtime.state or {}).get("sttm_layouts") or {}
        current, inherited = _layout(session, sessions, layouts)
        kept = _kept_steps(current, remove_steps, reset)
        if len(kept) + len(given) > MAX_STEPS:
            raise SessionError(
                f"a layout holds at most {MAX_STEPS} steps, and this one would have "
                f"{len(kept) + len(given)}. Nothing was changed: remove some (remove_steps) or "
                "start over (reset=True)."
            )
        try:
            new = normalize_steps(given)
        except StepError as error:
            raise SessionError(
                f"new step {error.number}{error.reason} Nothing was changed."
            ) from None
        layout = [step for _, step in kept] + new
        try:
            built = await asyncio.to_thread(build_workbooks, session, layout, now=now)
        except StepError as error:
            raise SessionError(_step_refusal(error, kept)) from None
    except SessionError as error:
        logger.info(
            "STTM workbooks not built for %s: %s",
            _build_label(session, session_id, thread_id),
            error,
        )
        return f"Not done: {error}"
    except Exception as error:  # a bug, or a template openpyxl cannot write: the turn goes on
        logger.exception(
            "Could not build the workbooks of STTM %s, with %d layout step(s): %s",
            _build_label(session, session_id, thread_id),
            len(layout),
            _error_line(error),  # noqa: TRY401 - the traceback lands on rows of its own
        )
        return (
            "Not done: the workbooks could not be built, because of a problem on our side, so "
            "nothing was changed. Tell the analyst, and do not retry."
        )
    build_id = new_file_id()
    files: list[KeptFile] = []
    try:
        for workbook in built.workbooks:
            kept_file = await keep_file(
                store,
                file_id=workbook_file_id(build_id, workbook.key),
                owner=owner,
                name=workbook.name,
                media_type=MEDIA_TYPE,
                content=workbook.content,
                now=now,
                thread_id=thread_id,
                session_id=session.id,
                version=session.version,
            )
            files.append(kept_file)
    except Exception as error:  # the store is down, or refused the files
        logger.exception(
            "Could not keep the workbooks of STTM %s (%s): %s",
            _build_label(session, session_id, thread_id),
            _built_line(built),
            _error_line(error),  # noqa: TRY401 - the traceback lands on rows of its own
        )
        return (
            "Not done: the workbooks were built but could not be kept for download, because of a "
            "problem on our side, so nothing was changed. Tell the analyst, and do not retry now."
        )
    logger.info(
        "Built the workbooks of STTM %s, with %d layout step(s): %s",
        _build_label(session, session_id, thread_id),
        len(layout),
        _built_line(built),
    )
    changes: list[str] = []
    if reset and current:
        changes.append(f"Dropped the {len(current)} kept step(s).")
    elif remove_steps:
        changes.append(f"Removed {_numbers('step', sorted(set(remove_steps)))}.")
    if new:
        changes.append(f"Added {_numbers('step', list(range(len(kept) + 1, len(layout) + 1)))}.")
    changed = layout != current
    text = _workbook_text(session, built, files, changes, layout, None if changed else inherited)
    if not changed:
        return text
    own = layouts.get(session.id) or {}
    entry = {
        "revision": int(own.get("revision", 0)) + 1,
        "steps": layout,
        "updated_at": timestamp(now),
    }
    return Command(
        update={
            "sttm_layouts": {session.id: entry},
            "messages": [ToolMessage(text, tool_call_id=runtime.tool_call_id)],
        }
    )


STTM_TOOLS = [
    sttm_start_session,
    sttm_record_input,
    sttm_update,
    sttm_advance,
    sttm_show,
    sttm_workbook,
]
STTM_TOOL_NAMES = frozenset(tool.name for tool in STTM_TOOLS)
# Every STTM tool but sttm_show may change a session or a layout (sttm_workbook
# only with LAYOUT_ARGS), so no STTM call runs after one in the same model step
# (_sessions).
SESSION_CHANGING_TOOLS = STTM_TOOL_NAMES - {sttm_show.name}

__all__ = [
    "LAYOUT_ARGS",
    "SESSION_CHANGING_TOOLS",
    "STTM_TOOLS",
    "STTM_TOOL_NAMES",
    "CellUpdate",
    "DerivedFieldInput",
    "FileDetails",
    "LayoutStepInput",
    "sttm_advance",
    "sttm_record_input",
    "sttm_show",
    "sttm_start_session",
    "sttm_update",
    "sttm_workbook",
]
