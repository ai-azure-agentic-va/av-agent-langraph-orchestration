"""Decides what a run means: its phase, its failed tasks, and what to say about each. Pure.

get-run lists every task run, including auto-retries and repair re-runs, so each task_key is
read from its latest run. The data is fetched elsewhere; this module only interprets it.
"""

from dataclasses import dataclass
from typing import Literal

from databricks.sdk.service.jobs import (
    Run,
    RunLifeCycleState,
    RunOutput,
    RunResultState,
    RunState,
    RunTask,
)

from v1.core.tools.databricks.jobs.traces import ChildFailure

MAX_FAILED_TASKS = 10
OUTPUT_RETENTION_DAYS = 60

OUTPUT_EXPIRED = "output no longer available"
OUTPUT_UNREADABLE = "output could not be read"

Phase = Literal["failed", "succeeded", "not_finished", "ended"]

_DAY_MS = 86_400_000
_FAILED_RESULTS = {RunResultState.FAILED, RunResultState.TIMEDOUT}
_NOT_FINISHED = {
    RunLifeCycleState.PENDING,
    RunLifeCycleState.QUEUED,
    RunLifeCycleState.RUNNING,
    RunLifeCycleState.TERMINATING,
    RunLifeCycleState.BLOCKED,
    RunLifeCycleState.WAITING_FOR_RETRY,
}
_ERROR_OUTPUT_TYPES = {"notebook", "python"}


@dataclass(frozen=True)
class TaskReport:
    """One failed task. error/trace are the full text; they are trimmed for the model later."""

    task_key: str
    task_type: str
    notebook_path: str | None
    attempts: int
    state: str
    state_message: str | None
    run_url: str | None
    error: str | None
    trace: str | None
    child: ChildFailure | None
    output_note: str | None


def is_failed(state: RunState | None) -> bool:
    """True for result FAILED or TIMEDOUT, or life-cycle INTERNAL_ERROR."""
    # UPSTREAM_FAILED is not a failure of its own: the task never ran.
    if state is None:
        return False
    return (
        state.result_state in _FAILED_RESULTS
        or state.life_cycle_state == RunLifeCycleState.INTERNAL_ERROR
    )


def run_phase(state: RunState | None) -> Phase:
    """Where a run (or task) stands: failed, succeeded, not finished, or ended some other way."""
    if state is None:
        return "ended"
    # SUCCESS_WITH_FAILURES means some tasks failed, so the run is explained as a failure.
    if is_failed(state) or state.result_state == RunResultState.SUCCESS_WITH_FAILURES:
        return "failed"
    if state.life_cycle_state in _NOT_FINISHED:
        return "not_finished"
    if state.result_state == RunResultState.SUCCESS:
        return "succeeded"
    return "ended"


def latest_attempts(tasks: list[RunTask]) -> list[tuple[RunTask, int]]:
    """Per task_key, in first-seen order: the latest task run and how many runs it had."""
    latest: dict[str, RunTask] = {}
    counts: dict[str, int] = {}
    for task in tasks:
        key = task.task_key
        counts[key] = counts.get(key, 0) + 1
        # A repair re-run restarts attempt_number at 0, so start_time decides first.
        if key not in latest or _recency(task) > _recency(latest[key]):
            latest[key] = task
    return [(task, counts[key]) for key, task in latest.items()]


def _recency(task: RunTask) -> tuple[int, int]:
    return task.start_time or 0, task.attempt_number or 0


def task_type(task: RunTask) -> str:
    """The kind of task: notebook, python, pipeline, sql, dbt, for_each or other."""
    if task.notebook_task:
        return "notebook"
    if task.spark_python_task or task.python_wheel_task:
        return "python"
    if task.pipeline_task:
        return "pipeline"
    if task.sql_task:
        return "sql"
    if task.dbt_task:
        return "dbt"
    if task.for_each_task:
        return "for_each"
    return "other"


def has_error_output(kind: str) -> bool:
    """True when get-run-output gives an error and trace for this kind of task."""
    return kind in _ERROR_OUTPUT_TYPES


def output_expired(task: RunTask, now_ms: int) -> bool:
    """True when the task ended more than OUTPUT_RETENTION_DAYS before now_ms."""
    # Databricks keeps run output for 60 days; after that get-run-output has nothing to give.
    if task.end_time is None:
        return False
    return now_ms - task.end_time > OUTPUT_RETENTION_DAYS * _DAY_MS


def state_name(state: RunState | None) -> str:
    """The result state if set, else the life-cycle state, else UNKNOWN."""
    if state and state.result_state:
        return state.result_state.value
    if state and state.life_cycle_state:
        return state.life_cycle_state.value
    return "UNKNOWN"


def task_report(
    task: RunTask,
    attempts: int,
    output: RunOutput | None,
    output_note: str | None,
    child: ChildFailure | None = None,
) -> TaskReport:
    """The report for one failed task. child is found by the caller: that needs I/O."""
    kind = task_type(task)
    # Only notebook and Python tasks put the failure in error/error_trace.
    shown = output if output and has_error_output(kind) else None
    return TaskReport(
        task_key=task.task_key,
        task_type=kind,
        notebook_path=task.notebook_task.notebook_path if task.notebook_task else None,
        attempts=attempts,
        state=state_name(task.state),
        state_message=task.state.state_message if task.state else None,
        run_url=task.run_page_url,
        error=shown.error if shown else None,
        trace=shown.error_trace if shown else None,
        child=child,
        output_note=output_note,
    )


def run_as_task(run: Run) -> RunTask:
    """A task-level run (get-run returns no tasks) as its own single task, keyed by run_name."""
    return RunTask(
        task_key=run.run_name or str(run.run_id),
        run_id=run.run_id,
        attempt_number=run.attempt_number,
        start_time=run.start_time,
        end_time=run.end_time,
        state=run.state,
        run_page_url=run.run_page_url,
    )
