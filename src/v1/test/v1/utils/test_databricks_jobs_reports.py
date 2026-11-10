"""Tests for run phase and task reports (ported from databricks-agent ``test_reports.py``).

Pure: no I/O. Runs standalone (``python test_databricks_jobs_reports.py``).
"""

from __future__ import annotations

from databricks.sdk.service.jobs import (
    DbtTask,
    ForEachTask,
    NotebookTask,
    PipelineTask,
    PythonWheelTask,
    Run,
    RunLifeCycleState,
    RunOutput,
    RunResultState,
    RunState,
    RunTask,
    SparkJarTask,
    SparkPythonTask,
    SqlTask,
)
from databricks_jobs_fakes import PARENT as CAPTURED
from databricks_test_support import run_tests

from v1.core.tools.databricks.jobs.reports import (
    MAX_FAILED_TASKS,
    OUTPUT_EXPIRED,
    OUTPUT_RETENTION_DAYS,
    OUTPUT_UNREADABLE,
    TaskReport,
    has_error_output,
    is_failed,
    latest_attempts,
    output_expired,
    run_as_task,
    run_phase,
    state_name,
    task_report,
    task_type,
)
from v1.core.tools.databricks.jobs.traces import ChildFailure

PARENT = "/Users/dev@example.com/run-failures-fixture/example_parent"
CHILD = "/Users/dev@example.com/run-failures-fixture/example_child"

TERMINATED = RunLifeCycleState.TERMINATED
FAILED = RunResultState.FAILED


def state(life=TERMINATED, result=FAILED, message=None) -> RunState:
    return RunState(life_cycle_state=life, result_state=result, state_message=message)


def task(key="load", start_time=0, attempt_number=0, end_time=0, run_id=1, **kwargs) -> RunTask:
    return RunTask(
        task_key=key,
        start_time=start_time,
        attempt_number=attempt_number,
        end_time=end_time,
        run_id=run_id,
        **kwargs,
    )


def test_constants() -> None:
    assert MAX_FAILED_TASKS == 10 and OUTPUT_RETENTION_DAYS == 60
    assert OUTPUT_EXPIRED == "output no longer available"
    assert OUTPUT_UNREADABLE == "output could not be read"


def test_retries_keep_last_attempt_and_count() -> None:
    runs = [task(start_time=t, attempt_number=n, run_id=n) for n, t in enumerate([10, 20, 30])]
    [(latest, attempts)] = latest_attempts(runs[::-1])
    assert latest.run_id == 2 and attempts == 3


def test_repair_rerun_replaces_original_task_run() -> None:
    # A repair re-run starts again at attempt 0, but later.
    original = task(start_time=10, attempt_number=1, run_id=1)
    repaired = task(start_time=99, attempt_number=0, run_id=2)
    [(latest, attempts)] = latest_attempts([original, repaired])
    assert latest.run_id == 2 and attempts == 2


def test_start_time_tie_goes_to_higher_attempt() -> None:
    [(latest, _)] = latest_attempts(
        [task(start_time=5, attempt_number=1, run_id=1), task(start_time=5, run_id=2)]
    )
    assert latest.run_id == 1


def test_first_seen_order_is_kept() -> None:
    tasks = [task("b"), task("a"), task("b", start_time=9, run_id=3), task("c")]
    assert [(t.task_key, n) for t, n in latest_attempts(tasks)] == [("b", 2), ("a", 1), ("c", 1)]
    assert latest_attempts([]) == []


L, R = RunLifeCycleState, RunResultState


def test_is_failed_states() -> None:
    assert is_failed(state(result=R.FAILED)) and is_failed(state(result=R.TIMEDOUT))
    assert is_failed(state(life=L.INTERNAL_ERROR, result=None))
    for result in (R.UPSTREAM_FAILED, R.CANCELED, R.SUCCESS, R.SUCCESS_WITH_FAILURES):
        assert not is_failed(state(result=result)), result
    assert not is_failed(None) and not is_failed(RunState())


RUN_PHASES = [
    (L.PENDING, None, "not_finished"),
    (L.QUEUED, None, "not_finished"),
    (L.RUNNING, None, "not_finished"),
    (L.TERMINATING, None, "not_finished"),
    (L.BLOCKED, None, "not_finished"),
    (L.WAITING_FOR_RETRY, None, "not_finished"),
    (L.TERMINATED, R.FAILED, "failed"),
    (L.TERMINATED, R.TIMEDOUT, "failed"),
    (L.INTERNAL_ERROR, None, "failed"),
    # A top-level run whose task failed, as seen in the fixture run.
    (L.INTERNAL_ERROR, R.FAILED, "failed"),
    (L.TERMINATED, R.SUCCESS_WITH_FAILURES, "failed"),
    (L.TERMINATED, R.SUCCESS, "succeeded"),
    (L.TERMINATED, R.CANCELED, "ended"),
    (L.SKIPPED, None, "ended"),
    (L.TERMINATED, R.UPSTREAM_FAILED, "ended"),
    (None, None, "ended"),
]


def test_run_phase() -> None:
    for life, result, phase in RUN_PHASES:
        got = run_phase(RunState(life_cycle_state=life, result_state=result))
        assert got == phase, (life, result)
    assert run_phase(None) == "ended"


def test_task_types() -> None:
    kinds = {
        "notebook": task(notebook_task=NotebookTask(notebook_path=PARENT)),
        "python": task(spark_python_task=SparkPythonTask(python_file="/x.py")),
        "pipeline": task(pipeline_task=PipelineTask(pipeline_id="p")),
        "sql": task(sql_task=SqlTask(warehouse_id="w")),
        "dbt": task(dbt_task=DbtTask(commands=["dbt run"])),
        "for_each": task(for_each_task=ForEachTask(inputs="[1]", task=None)),
        "other": task(spark_jar_task=SparkJarTask(main_class_name="Main")),
    }
    assert {kind: task_type(t) for kind, t in kinds.items()} == {k: k for k in kinds}
    wheel = task(python_wheel_task=PythonWheelTask(package_name="p", entry_point="main"))
    assert task_type(wheel) == "python"
    assert task_type(task()) == "other"


def test_only_notebook_and_python_have_error_output() -> None:
    assert has_error_output("notebook") and has_error_output("python")
    for kind in ("pipeline", "sql", "dbt", "for_each", "other"):
        assert not has_error_output(kind)


def test_output_expired_after_60_days() -> None:
    day = 86_400_000
    assert output_expired(task(end_time=0), now_ms=61 * day)
    assert not output_expired(task(end_time=0), now_ms=59 * day)
    assert not output_expired(task(end_time=None), now_ms=61 * day)
    # Exactly 60 days is not "more than" 60 days.
    assert not output_expired(task(end_time=day), now_ms=61 * day)


def test_task_report_carries_full_error_and_child() -> None:
    parent = task(
        "parent",
        attempt_number=1,
        notebook_task=NotebookTask(notebook_path=PARENT),
        state=state(message="Workload failed, see run output for details"),
        run_page_url="https://example.cloud.databricks.com/jobs/1/runs/2",
    )
    output = RunOutput(error=CAPTURED["error"], error_trace=CAPTURED["error_trace"])
    child = ChildFailure(notebook_path=CHILD, error="ValueError: bad")
    report = task_report(parent, attempts=2, output=output, output_note=None, child=child)
    assert report == TaskReport(
        task_key="parent",
        task_type="notebook",
        notebook_path=PARENT,
        attempts=2,
        state="FAILED",
        state_message="Workload failed, see run output for details",
        run_url="https://example.cloud.databricks.com/jobs/1/runs/2",
        error=CAPTURED["error"],
        trace=CAPTURED["error_trace"],
        child=child,
        output_note=None,
    )


def test_task_report_without_output_keeps_the_note() -> None:
    parent = task(notebook_task=NotebookTask(notebook_path=PARENT), state=state())
    report = task_report(parent, attempts=1, output=None, output_note=OUTPUT_EXPIRED)
    assert report.error is None and report.trace is None and report.child is None
    assert report.output_note == OUTPUT_EXPIRED


def test_task_report_for_pipeline_has_no_error_fields() -> None:
    pipeline = task("dlt", pipeline_task=PipelineTask(pipeline_id="p"), state=state())
    output = RunOutput(error="should not be shown", error_trace="nor this")
    report = task_report(pipeline, attempts=1, output=output, output_note=None)
    assert report.task_type == "pipeline" and report.notebook_path is None
    assert report.error is None and report.trace is None


def test_run_as_task_uses_run_name() -> None:
    run = Run(
        run_id=42,
        run_name="parent",
        state=state(),
        start_time=10,
        end_time=20,
        attempt_number=1,
        run_page_url="https://example.cloud.databricks.com/jobs/1/runs/42",
    )
    wrapped = run_as_task(run)
    assert wrapped.task_key == "parent" and wrapped.run_id == 42
    assert wrapped.state == run.state and wrapped.attempt_number == 1
    assert (wrapped.start_time, wrapped.end_time) == (10, 20)
    assert wrapped.run_page_url == run.run_page_url
    [(latest, attempts)] = latest_attempts([wrapped])
    assert latest is wrapped and attempts == 1


def test_state_name_prefers_result_then_life_cycle() -> None:
    assert state_name(state()) == "FAILED"
    assert state_name(RunState(life_cycle_state=L.RUNNING)) == "RUNNING"
    assert state_name(RunState()) == "UNKNOWN" and state_name(None) == "UNKNOWN"
    # A task report's state is read the same way.
    report = task_report(task(state=state(L.INTERNAL_ERROR, None)), 1, None, None)
    assert report.state == "INTERNAL_ERROR"
    assert task_report(task(), attempts=1, output=None, output_note=None).state == "UNKNOWN"


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
