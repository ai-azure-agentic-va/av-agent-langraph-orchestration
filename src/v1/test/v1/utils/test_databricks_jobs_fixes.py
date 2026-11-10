"""Tests for whether a failed run was since fixed (ported from databricks-agent ``test_fixes.py``).

Runs against the fake ``JobsClient``. Runs standalone (``python test_databricks_jobs_fixes.py``).
"""

from __future__ import annotations

import asyncio
import random

from databricks.sdk.service.jobs import (
    BaseRun,
    Run,
    RunLifeCycleState,
    RunResultState,
    RunState,
    RunType,
)
from databricks_jobs_fakes import FakeJobsClient
from databricks_test_support import run_tests

from v1.core.tools.databricks.jobs.fixes import fixed_status
from v1.core.tools.databricks.jobs.tools import MAX_RUNS_SCANNED, iso_time

JOB = 42
FAILED = RunResultState.FAILED
FAILED_AT = 10_000
HOST = "https://adb-123.azuredatabricks.net"


def ended(result: RunResultState) -> RunState:
    return RunState(life_cycle_state=RunLifeCycleState.TERMINATED, result_state=result)


def failed_run(run_type=RunType.JOB_RUN) -> Run:
    state = ended(RunResultState.FAILED)
    return Run(run_id=1, job_id=JOB, run_type=run_type, start_time=FAILED_AT, state=state)


def later(run_id, start, result=RunResultState.SUCCESS) -> BaseRun:
    url = f"{HOST}/jobs/{JOB}/runs/{run_id}"
    return BaseRun(
        run_id=run_id, job_id=JOB, start_time=start, run_page_url=url, state=ended(result)
    )


def ref(run_id, start):
    return {
        "run_id": run_id,
        "start_time": iso_time(start),
        "run_url": f"{HOST}/jobs/{JOB}/runs/{run_id}",
    }


def saved_job() -> FakeJobsClient:
    fake = FakeJobsClient()
    fake.names[JOB] = "Nightly load"
    return fake


def status(fake: FakeJobsClient, run: Run | None = None) -> dict:
    return asyncio.run(fixed_status(fake, run or failed_run()))


def test_submit_and_workflow_runs_are_one_off() -> None:
    for run_type in (RunType.SUBMIT_RUN, RunType.WORKFLOW_RUN):
        fake = saved_job()
        assert status(fake, failed_run(run_type)) == {"status": "one_off"}
        assert not any(name == "list_runs" for name, _ in fake.calls)


def test_missing_job_is_one_off() -> None:
    assert status(FakeJobsClient()) == {"status": "one_off"}


def test_status_from_the_later_runs() -> None:
    shuffled = [later(2, 20_000), later(3, 30_000), later(4, 40_000, FAILED)]
    random.Random(7).shuffle(shuffled)
    cases = [
        ([later(3, 30_000), later(2, 20_000, FAILED), failed_run()], "fixed", 3, None),
        ([later(3, 30_000, FAILED), later(2, 20_000)], "failed_again", 2, 3),
        ([later(2, 20_000, FAILED)], "not_fixed", None, None),
        ([later(0, 5_000)], "not_fixed", None, None),  # succeeded before the failure
        # Unordered runs are sorted; a run with no start time is ignored.
        ([*shuffled, later(5, None)], "failed_again", 3, 4),
        # The latest run that ran to a result is the fix; a cancel isn't a new failure.
        ([later(3, 30_000, RunResultState.CANCELED), later(2, 20_000)], "fixed", 2, None),
    ]
    for runs, expected, fix, latest in cases:
        fake = saved_job()
        fake.job_runs[JOB] = runs
        want = {"status": expected}
        if fix is not None:
            want["fix"] = ref(fix, fix * 10_000)
        if latest is not None:
            want["latest"] = ref(latest, latest * 10_000)
        assert status(fake) == want, (expected, fix, latest)


def test_scan_is_bounded() -> None:
    fake = saved_job()
    limits = []
    original = fake.list_runs

    async def recording(job_id, *, limit):
        limits.append(limit)
        return await original(job_id, limit=limit)

    fake.list_runs = recording
    status(fake)
    assert limits == [MAX_RUNS_SCANNED] == [100]


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
