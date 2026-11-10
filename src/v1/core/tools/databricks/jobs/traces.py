"""Trims run errors for the model and reads the failing child notebook call from a trace. Pure.

Run output text is long and full of ANSI colour codes, so it is stripped and cut before it
reaches the model or the UI. A notebook that fails inside `dbutils.notebook.run` only shows the
call in its own trace; the child's error lives in the child's run, found later from this path.
"""

import posixpath
import re
from dataclasses import dataclass

MAX_ERROR_CHARS = 1000
MAX_TRACE_CHARS = 2000
MAX_CODE_CHARS = 300

# Colour codes (ESC [ ... m) plus any other CSI sequence a terminal-styled trace may carry.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_CALL = "dbutils.notebook.run("
# The arrowed (failing) line of an IPython trace, as captured in
# src/v1/test/v1/fixtures/databricks/nested_failure_output.json (after ANSI codes are stripped):
#   ----> 2 dbutils.notebook.run("./example_child", 300)
# Unarrowed lines are context only, so they never name the failing call. Tried once per line,
# from its start (re.match). Every quantifier before and after the lazy [^\n]*? is possessive
# (*+, ++, {}+), so a failed match never hands characters back for it to rescan: linear time on
# hostile traces (long dash or whitespace runs, no newlines).
_CHILD_CALL = re.compile(
    r"[ \t]*+-{2,}+>[ \t]*+\d++[ \t]++[^\n]*?"
    r"dbutils\.notebook\.run\([ \t]*+(['\"])([^'\"\n]{1,1024}+)\1"
)


# The user's own cells in a trace start a frame like "File <command-1122334455667701>, line 1"
# (Databricks) or "Cell In[3], line 4" (IPython); library frames start "File /databricks/...".
# Captured in nested_failure_output.json and child_failure_output.json.
_USER_FRAME = re.compile(
    r"(?:File <command-(\d++)>|Cell In\[(\d++)\]), line \d++(?:, in ([^\s(]{1,200}+))?"
)
_OTHER_FRAME = re.compile(r"File ")
# A new traceback block: chained exceptions ("During handling of the above exception...") print
# one per exception, and the last is the one reported.
_TRACEBACK = "Traceback (most recent call last)"
# The arrowed line in a frame: "----> 1 total = ...". Possessive quantifiers keep it linear.
_ARROW_LINE = re.compile(r"[ \t]*+-{2,}+>[ \t]*+(\d++)[ \t]++(.*)")


@dataclass(frozen=True)
class ChildFailure:
    """A child notebook's failure. error/trace are its full text when its run was read;
    otherwise note says why not."""

    notebook_path: str
    error: str | None = None
    trace: str | None = None
    run_url: str | None = None
    note: str | None = None


def strip_ansi(text: str | None) -> str | None:
    """text without ANSI colour codes."""
    return _ANSI.sub("", text) if text else text


def head(text: str | None, limit: int) -> str | None:
    """The first limit chars of text, with "…" appended when cut."""
    return text if not text or len(text) <= limit else text[:limit] + "…"


def trim_error(text: str | None) -> str | None:
    """The first MAX_ERROR_CHARS of text, ANSI-free, with "…" appended when cut."""
    return head(strip_ansi(text), MAX_ERROR_CHARS)


def trace_tail(text: str | None) -> str | None:
    """The last MAX_TRACE_CHARS of text, with "…" prepended when cut."""
    # The tail, because Python traces end with the raised error.
    text = strip_ansi(text)
    if not text or len(text) <= MAX_TRACE_CHARS:
        return text
    return "…" + text[-MAX_TRACE_CHARS:]


def child_call(trace: str | None) -> str | None:
    """The notebook path in the failing dbutils.notebook.run(...) call of trace, or None."""
    found = None
    for line in (strip_ansi(trace) or "").splitlines():
        # The substring check skips most lines cheaply; the last arrowed frame is the innermost.
        if _CALL in line and (match := _CHILD_CALL.match(line)):
            found = match.group(2)
    return found


def resolve_child_path(parent_path: str | None, ref: str) -> str | None:
    """ref as an absolute workspace path; relative refs are taken from the parent's folder."""
    if ref.startswith("/"):
        # normpath keeps a leading "//" (POSIX allows it); workspace paths have one slash.
        return posixpath.normpath("/" + ref.lstrip("/"))
    if not parent_path:
        return None
    return posixpath.normpath(posixpath.join(posixpath.dirname(parent_path), ref))


def _user_frames(trace: str | None) -> list[tuple[str, str | None, int, str]]:
    """(cell id, function, line, code) for each user-cell frame with an arrowed line, in the
    last traceback block that has any (the reported exception's, for chained exceptions)."""
    blocks: list[list[tuple[str, str | None, int, str]]] = [[]]
    frame: tuple[str, str | None] | None = None
    for line in (strip_ansi(trace) or "").splitlines():
        if _TRACEBACK in line:
            blocks.append([])
            frame = None
        elif match := _USER_FRAME.match(line):
            frame = (match.group(1) or match.group(2), match.group(3))
        elif _OTHER_FRAME.match(line):
            frame = None  # a library frame: its arrowed lines aren't the user's code
        elif frame and (arrow := _ARROW_LINE.match(line)):
            code = head(arrow.group(2).strip(), MAX_CODE_CHARS)
            blocks[-1].append((*frame, int(arrow.group(1)), code))
    return next((b for b in reversed(blocks) if b), [])


def locate(trace: str | None) -> dict | None:
    """The failing line in the cell that ran, and where the error was raised when that was in
    another cell (a function defined elsewhere, or a %run notebook), whose line numbers are
    that cell's, not the failing cell's."""
    frames = _user_frames(trace)
    if not frames:
        return None
    ran = frames[0][0]
    _, _, line, code = [f for f in frames if f[0] == ran][-1]
    cell, function, raised_line, raised_code = frames[-1]
    raised = None
    if cell != ran:
        raised = {"function": function, "line": raised_line, "code": raised_code}
    return {"line": line, "code": code, "raised": raised}


def where_failed(task: dict) -> dict:
    """Where a reported task failed, read from its output: the deepest notebook whose output
    was read and, when the trace shows it, the line of the user's code. Never guessed."""
    child = task.get("child_notebook")
    found = locate(task.get("child_trace")) if child else None
    in_child = found is not None
    if not in_child:
        # The child's run wasn't read: what's known is the parent's call to it.
        found = locate(task.get("trace"))
    found = found or {"line": None, "code": None, "raised": None}
    return {
        "task_key": task.get("task_key"),
        "iteration": task.get("iteration"),
        "notebook": child if in_child else task.get("notebook_path"),
        "line": found["line"],
        "code": found["code"],
        "raised": found["raised"],
        "calls": child if child and not in_child else None,
    }
