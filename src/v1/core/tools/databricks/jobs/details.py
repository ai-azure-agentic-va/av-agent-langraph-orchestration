"""The "Run and task details" block that ends every Databricks job-run answer. Pure.

Built here rather than by the model so the block is the same every time: the subagent copies
it as given and the orchestrator relays it whole. The state message, task keys and errors are
job-authored text, so each is flattened onto its own line and its backticks escaped: nothing
the job wrote can start a new line or open a code span.
"""

from v1.core.tools.databricks.jobs.traces import head

MAX_DETAIL_ERROR_CHARS = 300
MISSING = "—"


def _one_line(text: str | None) -> str:
    """text with every run of whitespace (newlines included) collapsed to one space."""
    return " ".join((text or "").split())


def _escaped(text: str) -> str:
    """text with backslashes, then backticks, escaped, so a job's own "\\`" can't unescape one."""
    return text.replace("\\", "\\\\").replace("`", "\\`")


def _value(text: str | None) -> str:
    """Job text as one escaped line, or the missing marker when blank."""
    return _escaped(_one_line(text)) or MISSING


def _key(task_key: str | None) -> str:
    """The task key in backticks, or plain (escaped) when it holds a backtick of its own."""
    key = _one_line(task_key)
    if not key:
        return MISSING
    return _escaped(key) if "`" in key else f"`{key}`"


def _error(error: str | None) -> str | None:
    """The error's first non-blank line, cut to MAX_DETAIL_ERROR_CHARS ("…" when cut)."""
    first = next((line for line in (error or "").splitlines() if line.strip()), "")
    cut = head(_one_line(first), MAX_DETAIL_ERROR_CHARS)
    return _escaped(cut) if cut else None


def _failed_task(task_key: str | None, iteration: int | None, error: str | None) -> str:
    line = f"- **Failed task:** {_key(task_key)}"
    if iteration is not None:
        line += f" (iteration {iteration})"
    cause = _error(error)
    return f"{line} — {cause}" if cause else line


def run_details_block(
    *,
    workspace: str,
    job_id: int | None,
    run_url: str | None,
    state_message: str | None,
    failed_tasks: list[tuple[str | None, int | None, str | None]],
    more_failed_tasks: int = 0,
) -> str:
    """The block's markdown: workspace, job ID, run link and state message, then one line per
    failed task (key, its for_each iteration number when it has one, and the first line of its
    error), then the count of failed tasks not listed, when there are some. Missing values
    read "—"."""
    lines = [
        "**Run and task details**",
        "",
        f"- **Workspace:** {_value(workspace)}",
        f"- **Job ID:** {MISSING if job_id is None else job_id}",
        f"- **Run URL:** {_value(run_url)}",
        f"- **Overall state message:** {_value(state_message)}",
        *(_failed_task(*task) for task in failed_tasks),
    ]
    if more_failed_tasks > 0:
        lines.append(f"- **More failed tasks:** {more_failed_tasks}")
    return "\n".join(lines)
