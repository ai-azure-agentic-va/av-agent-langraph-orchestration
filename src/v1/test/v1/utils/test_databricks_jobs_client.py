"""Tests for the Jobs API client (ported from databricks-agent ``test_client.py``).

The client wraps the app's ``WorkspaceClient``; a stub stands in for its ``jobs``
API. The SCIM user/group lookups and ACL reads are gone with the service principal.
Runs standalone (``python test_databricks_jobs_client.py``).
"""

from __future__ import annotations

import asyncio
import itertools
import time

from databricks.sdk.errors import (
    InvalidParameterValue,
    NotFound,
    PermissionDenied,
    ResourceDoesNotExist,
    TooManyRequests,
)
from databricks.sdk.service.jobs import BaseJob, BaseRun, Job, JobSettings, Run, RunOutput, RunType
from databricks_jobs_fakes import FakeJobsClient, StubWorkspace, raises
from databricks_test_support import run_tests

from v1.core.tools.databricks.jobs.client import JobsApiError, JobsClient

ABSENT_ERRORS = [
    NotFound("gone"),
    PermissionDenied("no"),
    ResourceDoesNotExist("gone"),
    InvalidParameterValue("bad id"),
]


def _client(stub: StubWorkspace, timeout_s: float = 5) -> JobsClient:
    return JobsClient(stub, timeout_s=timeout_s)


def endless_runs(*args, **kwargs):
    return (BaseRun(run_id=i) for i in itertools.count())


def test_get_run_asks_for_history() -> None:
    stub = StubWorkspace()
    stub.jobs.results["get_run"] = Run(run_id=7)
    assert asyncio.run(_client(stub).get_run(7)).run_id == 7
    assert stub.jobs.calls == [("get_run", (7,), {"include_history": True})]


def test_not_found_and_denied_are_none() -> None:
    for error in ABSENT_ERRORS:
        stub = StubWorkspace()
        stub.jobs.results["get_run"] = error
        stub.jobs.results["get"] = error
        client = _client(stub)
        assert asyncio.run(client.get_run(7)) is None, error
        assert asyncio.run(client.job_name(12)) is None, error


def test_other_errors_raise_jobs_api_error() -> None:
    stub = StubWorkspace()
    for method in ("get_run", "get", "get_run_output", "list_runs", "list"):
        stub.jobs.results[method] = TooManyRequests("slow down")
    client = _client(stub)
    calls = [
        lambda: client.get_run(7),
        lambda: client.job_name(12),
        lambda: client.get_run_output(7),
        lambda: client.list_runs(12, limit=3),
        lambda: client.find_jobs("x", limit=3),
        lambda: client.list_workflow_runs(0, 1, limit=3),
    ]
    for call in calls:
        assert "slow down" in str(raises(JobsApiError, lambda call=call: asyncio.run(call())))


def test_slow_call_times_out() -> None:
    stub = StubWorkspace()
    stub.jobs.results["get_run"] = lambda *a, **kw: time.sleep(0.5)
    raises(JobsApiError, lambda: asyncio.run(_client(stub, timeout_s=0.01).get_run(7)))


def test_get_run_output_errors_are_never_none() -> None:
    stub = StubWorkspace()
    stub.jobs.results["get_run_output"] = RunOutput(error="boom")
    assert asyncio.run(_client(stub).get_run_output(7)).error == "boom"
    assert stub.jobs.calls == [("get_run_output", (7,), {})]
    stub.jobs.results["get_run_output"] = NotFound("gone")
    raises(JobsApiError, lambda: asyncio.run(_client(stub).get_run_output(7)))


def test_list_runs_stops_at_limit() -> None:
    stub = StubWorkspace()
    stub.jobs.results["list_runs"] = endless_runs
    runs = asyncio.run(_client(stub).list_runs(12, limit=3))
    assert [r.run_id for r in runs] == [0, 1, 2]
    assert stub.jobs.last_kwargs == {"job_id": 12, "completed_only": True, "expand_tasks": True}


def test_job_name() -> None:
    stub = StubWorkspace()
    stub.jobs.results["get"] = Job(job_id=12, settings=JobSettings(name="Nightly load"))
    assert asyncio.run(_client(stub).job_name(12)) == "Nightly load"
    assert stub.jobs.calls == [("get", (12,), {})]


def test_find_jobs_uses_the_name_filter() -> None:
    stub = StubWorkspace()
    stub.jobs.results["list"] = lambda **kw: (
        BaseJob(job_id=i, settings=JobSettings(name="Nightly load")) for i in itertools.count(1)
    )
    assert asyncio.run(_client(stub).find_jobs("Nightly load", limit=2)) == [
        (1, "Nightly load"),
        (2, "Nightly load"),
    ]
    assert stub.jobs.last_kwargs == {"name": "Nightly load"}


def test_list_workflow_runs_filters_type_and_window() -> None:
    stub = StubWorkspace()
    stub.jobs.results["list_runs"] = endless_runs
    runs = asyncio.run(_client(stub).list_workflow_runs(1000, 2000, limit=4))
    assert len(runs) == 4
    assert stub.jobs.last_kwargs == {
        "run_type": RunType.WORKFLOW_RUN,
        "start_time_from": 1000,
        "start_time_to": 2000,
        "completed_only": True,
        "expand_tasks": True,
    }


def test_client_needs_only_the_jobs_api() -> None:
    # The client reads the runs as the app, so no SCIM user or group lookup and no ACL read.
    client = _client(StubWorkspace())
    for name in ("find_user", "get_group", "job_acl", "_users", "_groups"):
        assert not hasattr(client, name), name


def test_fake_workflow_runs_in_window_and_limit() -> None:
    fake = FakeJobsClient()
    fake.workflow_runs = [BaseRun(run_id=i, start_time=t) for i, t in enumerate([5, 10, 20])]
    assert [r.run_id for r in asyncio.run(fake.list_workflow_runs(5, 10, limit=5))] == [0, 1]
    assert [r.run_id for r in asyncio.run(fake.list_workflow_runs(0, 99, limit=1))] == [0]


def test_fake_find_jobs_and_failures() -> None:
    fake = FakeJobsClient()
    fake.names = {1: "Nightly Load", 2: "Other"}
    assert asyncio.run(fake.find_jobs("nightly load", limit=5)) == [(1, "Nightly Load")]
    fake.fail["get_run"] = JobsApiError("boom")
    raises(JobsApiError, lambda: asyncio.run(fake.get_run(1)))
    assert fake.calls == [("find_jobs", "nightly load"), ("get_run", 1)]


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
