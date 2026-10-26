"""Put back a run ID, a run date or a name that a model changed while copying it.

An ADF answer is copied twice on its way to the user: the ``adf-agent``
subagent copies its tools' results into its answer, and the orchestrator copies
that answer into its reply. Both copies are written token by token, and graded
runs caught each of them changing what it copied where a reader cannot see it:

- a run ID given another run's ending, its [open run] link opening no run. The
  reply did it in UC2-07 (run 3), and in SUP-16 (run 4), SUP-06 (run 8) and
  SUP-02 (its re-run) to the same ID each time. The subagent did it in SUP-06
  (the re-run), writing a group twice ('…-11f1-11f1-…').
- a run's date changed on its row (SUP-02, run 8, in the reply).
- a hyphen written twice in a pipeline's name, as the label of its Monitor link
  ('pl-100-00-ex--surveyco-daily-ingest_data'). The reply did it in QA-07, in
  run 3 and again in run 8's re-run.
- a trigger named in the follow-up suggestions as the user typed it, not as the
  answer names it ('TRIG_FX_60_00_EX_OPS_DAILY_INGEST_DATA' for
  'trig-fx-60-00-ex-ops-daily-ingest_data'). The reply did it in QA-18 in all
  three write runs, though it is told to name each as the answer does; a
  suggestion is sent as the user's next message when clicked.

So :mod:`v1.core.middlewares.subagent_relay` (the reply) and
:mod:`v1.core.middlewares.adf_result_keep` (the subagent's answer) check a final
answer against the results it copied from and put back those slips only:

- a run ID that appears nowhere in the conversation, and whose first three
  groups match exactly one ID in those results, becomes that ID;
- a name that appears nowhere in the conversation, and matches exactly one name
  in those results once each run of '-' or '_' is read as one, becomes that
  name;
- in the follow-up suggestions (the bullets the UI shows under 'Want to explore
  further?'), a name written as the user typed it, in any case and with any
  separators, becomes the one name in those results it matches, the user's own
  spellings left out, unless only its first letter's case differs (a capital
  opening a sentence: 'Re-run' for 're-run');
- on a line carrying one of those IDs, a time that appears nowhere in the
  conversation gets its date back when the results' lines for that ID hold
  exactly one time at the same clock time that the answer's line lacks.

Anything else is left as written: a near miss with two candidates, an ID, a
name or a time the user typed, an ID cut short, a name changed any other way (a
letter; its case, unless a suggestion names it as the user typed it), a time of
the model's own (a window, another time zone). A message that calls tools is
not an answer and is not checked. Like
:mod:`v1.core.middlewares.citations`, this changes the stored message
after it streamed; the UI shows the stored reply once the run ends.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass

from langchain.agents.middleware import ExtendedModelResponse, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage

logger = logging.getLogger(__name__)

# A run ID as written, a malformed one included: the slips added a group
# ('…-1d80-0123456789ab', '…-11f1-11f1-…'), and one could drop one. It ends with
# a full last group, so a word joined on with a hyphen ('…-89abcdef0123-failed')
# is not read as part of it. The subscription in a link's factory path is not a
# run.
_RUN_ID = re.compile(
    r"(?<!subscriptions/)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}(?:-[0-9a-f]+)*-[0-9a-f]{12,}",
    re.IGNORECASE,
)
_WELL_FORMED_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# 'xxxxxxxx-xxxx-xxxx': the groups that differ from run to run. The slips kept
# them and lost the rest.
_ID_PREFIX = 18
# 'YYYY-MM-DD HH:MM:SS', or with a 'T' between and a 'Z' after.
_TIME = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})([ T])(\d{2}:\d{2}:\d{2})(?!\d)")
# A name as written: letters and digits joined by '-' or '_' (a pipeline, a
# trigger, a factory; a run ID reads as one too), taken whole or not at all.
_NAME = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9]+(?:[-_]+[A-Za-z0-9]+)+(?![A-Za-z0-9_-])")
# A separator written more than once in a row: 'ex--surveyco'.
_REPEATED_SEPARATOR = re.compile(r"([-_])\1+")
# The follow-up suggestions, found as the UI finds them (agent-web-ui's
# extractFollowUps): the first three bullets under this heading, on a line of
# its own, up to the next heading or text that is not a bullet.
_FOLLOW_UP_HEADING = re.compile(
    r"[ \t]{0,3}(?:#{1,6}[ \t]+)?\*{0,2}[ \t]*want to explore further[\s*?:.]*", re.IGNORECASE
)
_FOLLOW_UPS_SHOWN = 3
_MD_HEADING = re.compile(r"#{1,6}\s")
_MD_BULLET = re.compile(r"(?:[-*+]|\d+[.)])\s+\S")


def tool_call_ids(messages: Iterable[AnyMessage]) -> set[str]:
    """Ids of every tool call in ``messages``."""

    return {
        call["id"]
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls or []
        if call.get("id")
    }


def _text(message: AnyMessage) -> str:
    """``message``'s text, whether its content is a string or content blocks."""

    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block if isinstance(block, str) else str(block.get("text", ""))
            for block in content
            if isinstance(block, str | dict)
        )
    return ""


def _times(text: str) -> set[str]:
    """Every date and time in ``text``, as 'YYYY-MM-DD HH:MM:SS'."""

    return {f"{match.group(1)} {match.group(3)}" for match in _TIME.finditer(text)}


def _squeezed(name: str) -> str:
    """``name`` with each run of '-' or '_' read as one."""

    return _REPEATED_SEPARATOR.sub(r"\1", name)


def _folded(name: str) -> str:
    """``name`` in lower case, without its separators: 'TRIG_FX-60' -> 'trigfx60'."""

    return name.replace("-", "").replace("_", "").casefold()


@dataclass(frozen=True)
class _CopySources:
    """What an answer is checked against."""

    # Every run ID, time and name anywhere in the conversation the model saw:
    # these are never changed, whoever wrote them.
    known_ids: frozenset[str]
    known_times: frozenset[str]
    known_names: frozenset[str]
    # Each well-formed run ID in the results copied from -> their lines that
    # carry it.
    lines_by_id: dict[str, list[str]]
    # Each name in the results copied from, by its squeezed form.
    names_by_squeezed: dict[str, set[str]]
    # The same names by their folded form, for the forms the user typed, less
    # the user's own spellings (a result can quote one: "'TRIG_A' matched
    # trigger 'trig-a'").
    names_by_folded: dict[str, set[str]]


def _copy_sources(request: ModelRequest, call_ids: Collection[str]) -> _CopySources:
    lines_by_id: dict[str, list[str]] = {}
    names_by_squeezed: dict[str, set[str]] = {}
    names_by_folded: dict[str, set[str]] = {}
    for message in request.messages:
        if (
            isinstance(message, ToolMessage)
            and message.tool_call_id in call_ids
            and message.status != "error"
            and isinstance(message.content, str)
        ):
            for line in message.content.splitlines():
                for token in _RUN_ID.findall(line):
                    if _WELL_FORMED_ID.fullmatch(token.lower()):
                        lines_by_id.setdefault(token.lower(), []).append(line)
                for name in _NAME.findall(line):
                    names_by_squeezed.setdefault(_squeezed(name), set()).add(name)
                    names_by_folded.setdefault(_folded(name), set()).add(name)
    typed = {
        name
        for message in request.messages
        if isinstance(message, HumanMessage)
        for name in _NAME.findall(_text(message))
    }
    typed_folded = {_folded(name) for name in typed}
    system = getattr(request, "system_message", None)
    known = "\n".join(_text(m) for m in [*([system] if system else []), *request.messages])
    return _CopySources(
        known_ids=frozenset(token.lower() for token in _RUN_ID.findall(known)),
        known_times=frozenset(_times(known)),
        known_names=frozenset(_NAME.findall(known)),
        lines_by_id=lines_by_id,
        names_by_squeezed=names_by_squeezed,
        names_by_folded={
            key: names - typed for key, names in names_by_folded.items() if key in typed_folded
        },
    )


def _follow_ups_named(text: str, sources: _CopySources, changes: list[str]) -> str:
    """``text`` with each name in its follow-up suggestions written as the results write it."""

    lines = text.split("\n")
    start = next(
        (i for i, line in enumerate(lines) if _FOLLOW_UP_HEADING.fullmatch(line.strip())), None
    )
    if start is None:
        return text

    def _name(match: re.Match[str]) -> str:
        token = match.group(0)
        found = sources.names_by_folded.get(_folded(token), set())
        if len(found) != 1 or token in found:
            return token
        (name,) = found
        if token[1:] == name[1:]:
            # A capital opening a sentence: 'Re-run' for 're-run'.
            return token
        changes.append(f"follow-up name {token} -> {name}")
        return name

    shown = 0
    for index in range(start + 1, len(lines)):
        line = lines[index].strip()
        if not line:
            continue
        if shown == _FOLLOW_UPS_SHOWN or _MD_HEADING.match(line):
            break
        if not _MD_BULLET.match(line):
            if shown:
                break
            continue
        lines[index] = _NAME.sub(_name, lines[index])
        shown += 1
    return "\n".join(lines)


def _put_back(text: str, sources: _CopySources) -> tuple[str, list[str]]:
    """``text`` with slipped run IDs, names and run dates put back, and what changed."""

    changes: list[str] = []

    def _run_id(match: re.Match[str]) -> str:
        token = match.group(0)
        lowered = token.lower()
        # Written before, by the user, a tool or an earlier answer. (An ID cut
        # short, '…-a9a3-7a1b…', has no full last group and is never matched.)
        if lowered in sources.known_ids:
            return token
        found = [rid for rid in sources.lines_by_id if rid[:_ID_PREFIX] == lowered[:_ID_PREFIX]]
        if len(found) != 1:
            return token
        changes.append(f"run ID {token} -> {found[0]}")
        return found[0]

    def _name(match: re.Match[str]) -> str:
        token = match.group(0)
        if token in sources.known_names:
            return token
        found = sources.names_by_squeezed.get(_squeezed(token), set())
        if len(found) != 1:
            return token
        (name,) = found
        changes.append(f"name {token} -> {name}")
        return name

    # Dates last: a run ID with a hyphen written twice is read as one only once
    # it is put back as a name.
    text = _follow_ups_named(_NAME.sub(_name, _RUN_ID.sub(_run_id, text)), sources, changes)
    lines = text.split("\n")
    for index, line in enumerate(lines):
        ids = {token.lower() for token in _RUN_ID.findall(line)} & sources.lines_by_id.keys()
        if not ids:
            continue
        lacking = {
            time
            for rid in ids
            for answer_line in sources.lines_by_id[rid]
            for time in _times(answer_line)
        } - _times(line)

        def _run_date(match: re.Match[str], lacking: set[str] = lacking) -> str:
            written = f"{match.group(1)} {match.group(3)}"
            if written in sources.known_times:
                return match.group(0)
            same_clock = [time for time in lacking if time[11:] == match.group(3)]
            if len(same_clock) != 1:
                return match.group(0)
            changes.append(f"time {written} -> {same_clock[0]}")
            return f"{same_clock[0][:10]}{match.group(2)}{match.group(3)}"

        lines[index] = _TIME.sub(_run_date, line)
    return "\n".join(lines), changes


def _checked_message(message: AIMessage, sources: _CopySources, label: str) -> AIMessage:
    """``message`` with its slips put back (the same instance when there were none)."""

    changes: list[str] = []
    content = message.content
    if isinstance(content, str):
        fixed, changes = _put_back(content, sources)
        new_content: str | list = fixed
    elif isinstance(content, list):
        new_content = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                fixed, block_changes = _put_back(block["text"], sources)
                if block_changes:
                    changes += block_changes
                    block = {**block, "text": fixed}
            new_content.append(block)
    if not changes:
        return message
    logger.warning("Copy check put back what %s changed: %s", label, "; ".join(changes))
    return message.model_copy(update={"content": new_content})


def copy_checked(
    request: ModelRequest, response: object, call_ids: Collection[str], label: str
) -> object:
    """``response`` with each final answer in it checked against the results of
    ``call_ids`` (``label`` names the answer in the log)."""

    if isinstance(response, ExtendedModelResponse):
        response.model_response = copy_checked(request, response.model_response, call_ids, label)
        return response
    if isinstance(response, AIMessage):
        messages = [response]
    elif isinstance(response, ModelResponse):
        messages = response.result
    else:
        return response
    sources: _CopySources | None = None
    checked = []
    for message in messages:
        if isinstance(message, AIMessage) and not message.tool_calls:
            sources = sources or _copy_sources(request, call_ids)
            message = _checked_message(message, sources, label)
        checked.append(message)
    if isinstance(response, AIMessage):
        return checked[0]
    response.result = checked
    return response
