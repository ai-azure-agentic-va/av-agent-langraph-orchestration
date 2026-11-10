"""Finds the run of a failed child notebook and reads its error.

A notebook started with `dbutils.notebook.run` runs as its own one-time WORKFLOW_RUN, and the
parent's trace only shows the call, not the child's error. Nothing links the two runs, so the
child is matched by time window (inside the parent task's run) and notebook path. When that
match isn't exactly one failed run, this says so rather than guessing. The runs are listed with
the app's client, so only runs the app can view are ever candidates.
"""

import logging

from databricks.sdk.service.jobs import BaseRun, RunTask

from v1.core.tools.databricks.jobs.client import JobsClient, failure_fields
from v1.core.tools.databricks.jobs.reports import (
    OUTPUT_EXPIRED,
    OUTPUT_UNREADABLE,
    is_failed,
    output_expired,
)
from v1.core.tools.databricks.jobs.traces import ChildFailure, child_call, resolve_child_path

logger = logging.getLogger(__name__)

MAX_CHILD_DEPTH = 3
MAX_CHILD_RUNS_SCANNED = 50

CHILD_NOT_FOUND = "child run not found"
CHILD_AMBIGUOUS = "several child runs matched"


async def find_child_failure(
    client: JobsClient, task: RunTask, trace: str | None, *, now_ms: int, depth: int = 1
) -> ChildFailure | None:
    """The failure of the child notebook named in trace, or None if no child call failed.

    Follows grandchildren to MAX_CHILD_DEPTH and returns the deepest error found. Never raises
    for a failed lookup: the note says why there is no error instead.
    """
    ref = child_call(trace)
    if ref is None:
        return None
    parent_path = task.notebook_task.notebook_path if task.notebook_task else None
    path = resolve_child_path(parent_path, ref)
    if path is None or task.start_time is None:
        return ChildFailure(notebook_path=path or ref, note=CHILD_NOT_FOUND)

    try:
        found = await _read_child(client, task, path, now_ms, depth=depth)
    except Exception as exc:  # noqa: BLE001 - a lookup never fails the tool
        # JobsApiError, and anything else JobsClient lets through unmapped (it only maps
        # DatabricksError): a child lookup must never fail the whole explanation.
        logger.warning(
            "jobs_api_failed run_id=%s depth=%s %s", task.run_id, depth, failure_fields(exc)
        )
        return ChildFailure(notebook_path=path, note=OUTPUT_UNREADABLE)
    if isinstance(found, ChildFailure):
        return found

    child_task, failure = found
    if depth < MAX_CHILD_DEPTH:
        deeper = await find_child_failure(
            client, child_task, failure.trace, now_ms=now_ms, depth=depth + 1
        )
        if deeper is not None and deeper.error is not None:
            return deeper
    return failure


async def _read_child(
    client: JobsClient, task: RunTask, path: str, now_ms: int, *, depth: int
) -> ChildFailure | tuple[RunTask, ChildFailure]:
    """The child's failure with error/trace, paired with its task run; or just a note."""
    runs = await client.list_workflow_runs(
        task.start_time, task.end_time or now_ms, limit=MAX_CHILD_RUNS_SCANNED
    )
    # The app's client lists only runs the app can view, so a hidden run neither hides the
    # child run nor shows up as "several matched".
    matches = [r for r in runs if is_failed(r.state) and _matching_tasks(r, path)]
    if len(matches) > 1:
        return ChildFailure(notebook_path=path, note=CHILD_AMBIGUOUS)
    if not matches:
        return ChildFailure(notebook_path=path, note=CHILD_NOT_FOUND)
    # A full page may have cut off a second match, so one match isn't certain.
    if len(runs) >= MAX_CHILD_RUNS_SCANNED:
        return ChildFailure(notebook_path=path, note=CHILD_AMBIGUOUS)

    [run] = matches
    child_task = max(_matching_tasks(run, path), key=lambda t: t.start_time or 0)
    run_url = child_task.run_page_url or run.run_page_url
    if output_expired(child_task, now_ms):
        return ChildFailure(notebook_path=path, run_url=run_url, note=OUTPUT_EXPIRED)

    output = await client.get_run_output(child_task.run_id)
    logger.info("child_run_read run_id=%s job_id=%s depth=%s", child_task.run_id, run.job_id, depth)
    failure = ChildFailure(
        notebook_path=path, error=output.error, trace=output.error_trace, run_url=run_url
    )
    return child_task, failure


def _matching_tasks(run: BaseRun, path: str) -> list[RunTask]:
    return [t for t in run.tasks or [] if t.notebook_task and t.notebook_task.notebook_path == path]
