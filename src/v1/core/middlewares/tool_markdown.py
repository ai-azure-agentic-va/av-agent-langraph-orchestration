"""View-only markdown layout for a subagent's own tool results.

The ADF and ADLS tools print plain-text blocks: a header line, then
'  Pipeline Name : pl_x' fields, '•' items, 'a | b | c' rows and nested detail,
told apart by line breaks and indentation alone. Markdown keeps neither — the
lines of a paragraph render as one — and the subagent copies those lines into
its answer as the tool wrote them (AdfResultKeepMiddleware asks it to). So a
recovery validation reached the chat as one run-on paragraph: 'Pipeline Name :
pl_x Failed Run ID : … Run Started : … Trigger Name : …'.

So for each of the subagent's model calls this middleware lays every result of
the tools it is given out as markdown, line for line:

- the first line, and any later line at the left margin (a second header, a
  run-tree node, 'Folders checked:'), is a paragraph of its own;
- every indented line is a bullet, nested by its indentation. A '•' or '- '
  marker becomes the bullet's, and a 'Label : value' field reads
  '**Label:** value';
- a column-header row ('Trigger | Factory | Result') is a bold paragraph above
  its rows, and a heading that ends in ':' over rows at its own indentation
  (GROUP A, GROUP B) a plain one.

Never a markdown table: the orchestrator's prompt forbids tables in answers.
Each row stays one bullet with its cells in order, so the copy check that puts
back a slipped run ID or run date by line still finds them.

It is **view-only**, like AdfResultKeepMiddleware: it calls
``handler(request.override(...))`` with patched copies, so the results in state,
the tests that pin them and the logs keep the plain text. A failed call
(``status="error"``) and non-text content are left as they are.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Iterable

from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage, AnyMessage, ToolMessage

logger = logging.getLogger(__name__)

# The tools' own item markers; each becomes the bullet's.
_MARKER = re.compile(r"[-•][ \t]+")
# 'Pipeline Name : pl_x', 'Recovery Decision: Recovered', 'error 2200: sink
# timeout'. A short label from the start of the line — no '=' or '|', so a
# 'Current Run ID=q0 | …' row is never one — then a colon and a value.
_FIELD = re.compile(r"(?P<label>[A-Za-z][\w ()'/.-]{0,39}?)[ \t]*:[ \t]+(?P<value>\S.*)")
# A value holding further fields ('start: …, end: …, duration: 1s') is a line
# of several, so none of them is bolded.
_MORE_FIELDS = re.compile(r",[ \t]*[A-Za-z][\w ]{0,20}:[ \t]")
# A column-header cell: a capitalised title, no name, date, id or link in it.
_COLUMN = re.compile(r"[A-Z][A-Za-z0-9 ()/.]*")
_CELL = " | "
# A line that would open a heading, a quote or a list of its own, not text: the
# backslash goes before the '#', '>' or marker, after a number's digits.
_BLOCK_START = re.compile(r"(?:#|>|[*+-](?:\s|$))")
_NUMBERED = re.compile(r"(\d{1,9})([.)](?:\s|$))")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _is_row(text: str) -> bool:
    return text.count(_CELL) >= 2


def _is_column_header(text: str) -> bool:
    cells = text.split(_CELL)
    return len(cells) >= 3 and all(_COLUMN.fullmatch(cell.strip()) for cell in cells)


def _escaped(text: str) -> str:
    """``text`` with a leading '#', '>', '*' or '1.' kept as text."""

    if _BLOCK_START.match(text):
        return "\\" + text
    return _NUMBERED.sub(r"\1\\\2", text, count=1) if _NUMBERED.match(text) else text


def _field(text: str) -> str:
    """'**Label:** value' for a field line, else ``text`` unchanged."""

    match = _FIELD.fullmatch(text)
    if match is None or _MORE_FIELDS.search(match["value"]):
        return text
    return f"**{match['label'].strip()}:** {match['value']}"


def as_markdown(text: str) -> str:
    """``text``, a tool's plain-text block, laid out as markdown line for line.

    Every line keeps its words in its order; only markers, indentation, blank
    lines and the bold around a label or column header change.
    """

    lines = [line.rstrip() for line in text.split("\n")]
    out: list[str] = []
    # The indentation of each open list level; empty outside a list.
    levels: list[int] = []

    def paragraph(line: str) -> None:
        if out and out[-1]:
            out.append("")
        out.extend([line, ""])
        levels.clear()

    for index, line in enumerate(lines):
        if not line.strip():
            if out and out[-1]:
                out.append("")
            levels.clear()
            continue
        indent, body = _indent(line), line.strip()
        marked = _MARKER.match(body)
        top = not levels or indent <= levels[0]
        later = next((rest for rest in lines[index + 1 :] if rest.strip()), "")
        # The rows a column header or a group heading stands over sit at its own
        # indentation, right below it.
        heads_rows = bool(later) and _indent(later) == indent
        if index == 0 or (indent == 0 and not marked):
            paragraph(_escaped(body))
            continue
        if top and not marked and heads_rows:
            above = lines[index - 1]
            if _is_column_header(body) and not (
                _indent(above) == indent and _is_row(above.strip())
            ):
                paragraph(f"**{body}**")
                continue
            if body.endswith(":") and not _is_row(body):
                paragraph(_escaped(body))
                continue
        if marked:
            body = body[marked.end() :]
        while levels and indent < levels[-1]:
            levels.pop()
        if not levels or indent > levels[-1]:
            levels.append(indent)
        out.append(f"{'  ' * (len(levels) - 1)}- {_escaped(_field(body))}")
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def _call_ids(messages: list[AnyMessage], tool_names: frozenset[str]) -> set[str]:
    """Ids of the calls in ``messages`` made to one of ``tool_names``."""

    return {
        call["id"]
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls or []
        if call.get("name") in tool_names and call.get("id")
    }


def _markdown_view(
    messages: list[AnyMessage], tool_names: frozenset[str]
) -> list[AnyMessage] | None:
    """``messages`` with those tools' results laid out as markdown.

    ``None`` when nothing changes, so the caller passes the request through
    untouched.
    """

    ids = _call_ids(messages, tool_names)
    if not ids:
        return None
    view = list(messages)
    patched = 0
    for index, message in enumerate(messages):
        if (
            not isinstance(message, ToolMessage)
            or message.tool_call_id not in ids
            or message.status == "error"
            or not isinstance(message.content, str)
        ):
            continue
        content = as_markdown(message.content)
        if content == message.content:
            continue
        view[index] = message.model_copy(update={"content": content})
        patched += 1
    if not patched:
        return None
    logger.debug("Laid out %d tool result(s) as markdown in the model view", patched)
    return view


class ToolMarkdownMiddleware(AgentMiddleware):
    """Show the model its tools' plain-text results laid out as markdown, so the
    lines it copies into an answer render one per line instead of as a paragraph."""

    def __init__(self, tool_names: Iterable[str]) -> None:
        self.tool_names = frozenset(tool_names)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        view = _markdown_view(request.messages, self.tool_names)
        return handler(request if view is None else request.override(messages=view))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        view = _markdown_view(request.messages, self.tool_names)
        return await handler(request if view is None else request.override(messages=view))
