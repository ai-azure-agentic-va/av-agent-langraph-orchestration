"""Tests for finding a failed child notebook run (ported from databricks-agent
``test_children.py``, with the ACL cases removed).

The app's client lists the runs, so a run the app can't read never reaches the matcher;
the fake holds only runs the app can see. Runs standalone
(``python test_databricks_jobs_children.py``).
"""

from __future__ import annotations

import asyncio

from databricks.sdk.service.jobs import (
    BaseRun,
    NotebookTask,
    RunLifeCycleState,
    RunOutput,
    RunResultState,
    RunState,
    RunTask,
)
from databricks_jobs_fakes import (
    CHILD,
    PARENT,
    FakeJobsClient,
    capture_logs,
    events,
    fields,
    rendered,
)
from databricks_test_support import run_tests

from v1.core.tools.databricks.jobs.children import (
    CHILD_AMBIGUOUS,
    CHILD_NOT_FOUND,
    MAX_CHILD_DEPTH,
    MAX_CHILD_RUNS_SCANNED,
    find_child_failure,
)
from v1.core.tools.databricks.jobs.client import JobsApiError
from v1.core.tools.databricks.jobs.reports import OUTPUT_EXPIRED, OUTPUT_UNREADABLE

FOLDER = "/Users/dev@example.com/run-failures-fixture"
PARENT_PATH = f"{FOLDER}/example_parent"
CHILD_PATH = f"{FOLDER}/example_child"
NOW = 60_000
HOST = "https://adb-123.azuredatabricks.net"


def state(result=RunResultState.FAILED) -> RunState:
    return RunState(life_cycle_state=RunLifeCycleState.TERMINATED, result_state=result)


def parent_task(path: str | None = PARENT_PATH) -> RunTask:
    return RunTask(
        task_key="parent",
        run_id=1,
        notebook_task=NotebookTask(notebook_path=path) if path else None,
        start_time=1_000,
        end_time=50_000,
        state=state(),
    )


def child_run(
    run_id: int,
    path: str = CHILD_PATH,
    *,
    start: int = 10_000,
    end: int = 40_000,
    result=RunResultState.FAILED,
    job_id: int | None = None,
) -> BaseRun:
    job_id = job_id or run_id + 1000
    url = f"{HOST}/?o=123#job/{job_id}/run/1"
    task = RunTask(
        task_key="Untitled",
        run_id=run_id,
        notebook_task=NotebookTask(notebook_path=path),
        start_time=start,
        end_time=end,
        state=state(result),
        run_page_url=url,
    )
    return BaseRun(
        run_id=run_id,
        job_id=job_id,
        run_name="Untitled",
        start_time=start,
        end_time=end,
        state=state(result),
        run_page_url=url,
        tasks=[task],
    )


def listed(fake: FakeJobsClient, *runs: BaseRun) -> None:
    """Adds runs the user's client can list."""
    fake.workflow_runs.extend(runs)


def arrowed(ref: str) -> str:
    return f'----> 2 dbutils.notebook.run("{ref}", 60)\n'


def methods(fake: FakeJobsClient) -> list[str]:
    return [name for name, _ in fake.calls]


def find(fake: FakeJobsClient, task: RunTask, trace: str | None, now: int = NOW):
    return asyncio.run(find_child_failure(fake, task, trace, now_ms=now))


def test_constants() -> None:
    assert MAX_CHILD_DEPTH == 3 and MAX_CHILD_RUNS_SCANNED == 50
    assert CHILD_NOT_FOUND == "child run not found"
    assert CHILD_AMBIGUOUS == "several child runs matched"


def test_finds_child_run_reads_its_error_and_logs_metadata() -> None:
    fake = FakeJobsClient()
    listed(fake, child_run(7))
    fake.outputs[7] = RunOutput(error=CHILD["error"], error_trace=CHILD["error_trace"])
    with capture_logs() as logs:
        child = find(fake, parent_task(), PARENT["error_trace"])
    assert child.notebook_path == CHILD_PATH
    assert "ValueError" in child.error and child.note is None
    assert child.trace == CHILD["error_trace"]
    assert child.run_url == f"{HOST}/?o=123#job/1007/run/1"
    # Only runs the user's client can list: no user, group or ACL lookups.
    assert fake.calls == [("list_workflow_runs", (1_000, 50_000)), ("get_run_output", 7)]
    [entry] = events(logs, "child_run_read")
    assert fields(entry) == {"run_id": "7", "job_id": "1007", "depth": "1"}
    assert "ValueError" not in rendered(logs)


def test_no_child_call_returns_none() -> None:
    fake = FakeJobsClient()
    assert find(fake, parent_task(), "Traceback ...\nValueError: bad") is None
    assert find(fake, parent_task(), None) is None
    assert fake.calls == []


def test_child_outside_window_or_other_path_is_not_found() -> None:
    fake = FakeJobsClient()
    listed(fake, child_run(7, start=60_000), child_run(8, f"{FOLDER}/other"))
    child = find(fake, parent_task(), PARENT["error_trace"])
    assert child.notebook_path == CHILD_PATH
    assert child.note == CHILD_NOT_FOUND and child.error is None
    assert "get_run_output" not in methods(fake)


def test_unfinished_parent_window_ends_now() -> None:
    fake = FakeJobsClient()
    task = parent_task()
    task.end_time = None
    listed(fake, child_run(7, start=55_000))
    fake.outputs[7] = RunOutput(error="boom")
    child = find(fake, task, arrowed("./example_child"))
    assert child.error == "boom"
    assert ("list_workflow_runs", (1_000, NOW)) in fake.calls


def test_succeeded_child_runs_are_ignored() -> None:
    fake = FakeJobsClient()
    listed(fake, child_run(7, result=RunResultState.SUCCESS))
    child = find(fake, parent_task(), PARENT["error_trace"])
    assert child.note == CHILD_NOT_FOUND


def test_two_matching_child_runs_are_not_guessed() -> None:
    fake = FakeJobsClient()
    listed(fake, child_run(7), child_run(8, start=20_000))
    child = find(fake, parent_task(), PARENT["error_trace"])
    assert child.note == CHILD_AMBIGUOUS and child.error is None
    assert "get_run_output" not in methods(fake)


def test_child_run_without_a_job_id_still_matches() -> None:
    # No ACL lookup by job ID any more: the user's client listing the run is enough.
    fake = FakeJobsClient()
    run = child_run(7)
    run.job_id = None
    listed(fake, run)
    fake.outputs[7] = RunOutput(error="mine")
    assert find(fake, parent_task(), PARENT["error_trace"]).error == "mine"


def test_full_page_with_one_match_is_ambiguous() -> None:
    # A full page may have cut off a second match, so one match can't be trusted.
    fake = FakeJobsClient()
    others = [
        child_run(100 + i, f"{FOLDER}/other", start=2_000 + i)
        for i in range(MAX_CHILD_RUNS_SCANNED - 1)
    ]
    listed(fake, child_run(7), *others)
    fake.outputs[7] = RunOutput(error="boom")
    child = find(fake, parent_task(), PARENT["error_trace"])
    assert child.note == CHILD_AMBIGUOUS and child.error is None
    assert "get_run_output" not in methods(fake)


def test_expired_child_output_is_not_read() -> None:
    fake = FakeJobsClient()
    listed(fake, child_run(7))
    child = find(fake, parent_task(), PARENT["error_trace"], now=40_000 + 61 * 86_400_000)
    assert child.note == OUTPUT_EXPIRED and child.error is None
    assert child.run_url is not None
    assert "get_run_output" not in methods(fake)


def test_latest_matching_task_run_is_read() -> None:
    fake = FakeJobsClient()
    run = child_run(7)
    retry = RunTask(
        task_key="Untitled",
        run_id=9,
        notebook_task=NotebookTask(notebook_path=CHILD_PATH),
        start_time=30_000,
        end_time=40_000,
        state=state(),
    )
    run.tasks.append(retry)
    listed(fake, run)
    fake.outputs[9] = RunOutput(error="retry error")
    child = find(fake, parent_task(), PARENT["error_trace"])
    assert child.error == "retry error"
    assert child.run_url == run.run_page_url  # the retry has no URL of its own


def test_grandchild_error_is_reported() -> None:
    fake = FakeJobsClient()
    listed(
        fake,
        child_run(7, start=10_000, end=40_000),
        child_run(8, f"{FOLDER}/grandchild", start=20_000, end=30_000),
    )
    fake.outputs[7] = RunOutput(error="child wrapper", error_trace=arrowed("./grandchild"))
    fake.outputs[8] = RunOutput(error=CHILD["error"], error_trace=CHILD["error_trace"])
    child = find(fake, parent_task(), PARENT["error_trace"])
    assert child.notebook_path == f"{FOLDER}/grandchild"
    assert "ValueError" in child.error
    assert ("list_workflow_runs", (10_000, 40_000)) in fake.calls


def test_grandchild_not_found_keeps_child_error() -> None:
    fake = FakeJobsClient()
    listed(fake, child_run(7))
    fake.outputs[7] = RunOutput(error="child error", error_trace=arrowed("./gone"))
    child = find(fake, parent_task(), PARENT["error_trace"])
    assert child.notebook_path == CHILD_PATH and child.error == "child error"


def test_depth_is_capped() -> None:
    # parent -> c1 -> c2 -> c3 -> c4 -> c5; only the first MAX_CHILD_DEPTH are read.
    fake = FakeJobsClient()
    runs = [child_run(i, f"{FOLDER}/c{i}", start=1_000 + i, end=50_000) for i in range(1, 6)]
    listed(fake, *runs)
    for i in range(1, 6):
        fake.outputs[i] = RunOutput(error=f"c{i} error", error_trace=arrowed(f"./c{i + 1}"))
    child = find(fake, parent_task(), arrowed("./c1"))
    assert child.error == "c3 error"
    reads = [arg for name, arg in fake.calls if name == "get_run_output"]
    assert reads == [1, 2, 3]


def test_lookup_errors_degrade_to_note_and_log_metadata() -> None:
    # A JobsApiError, or anything JobsClient lets through unmapped, listing or reading output.
    for method, error in (
        ("list_workflow_runs", JobsApiError("detail 403 secret")),
        ("list_workflow_runs", ValueError("detail secret, not a DatabricksError")),
        ("get_run_output", JobsApiError("detail secret")),
    ):
        fake = FakeJobsClient()
        listed(fake, child_run(7))
        if method == "get_run_output":
            fake.outputs[7] = error
        else:
            fake.fail[method] = error
        with capture_logs() as logs:
            child = find(fake, parent_task(), PARENT["error_trace"])
        assert child.notebook_path == CHILD_PATH and child.note == OUTPUT_UNREADABLE
        assert child.error is None
        [entry] = events(logs, "jobs_api_failed")
        assert fields(entry)["run_id"] == "1" and fields(entry)["depth"] == "1"
        assert fields(entry)["error"] == type(error).__name__
        assert entry.exc_info is None  # a traceback would carry the error text
        assert "secret" not in rendered(logs) and "notebook.run" not in rendered(logs)


def test_relative_ref_without_parent_path_names_child() -> None:
    fake = FakeJobsClient()
    child = find(fake, parent_task(path=None), arrowed("./x"))
    assert child.notebook_path == "./x" and child.note == CHILD_NOT_FOUND
    assert fake.calls == []


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
