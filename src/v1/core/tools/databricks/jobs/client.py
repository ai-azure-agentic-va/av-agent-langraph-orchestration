"""Async, read-only wrapper around the Jobs API, run as the app.

It wraps a ``WorkspaceClient`` acting as the app (``app_workspace_client``), so Databricks
applies the app's job permissions: a run or job the app can't view comes back as one of
``_ABSENT``, exactly like a missing one. The SDK is synchronous, so every call runs in a worker
thread with a timeout.
"""

import asyncio
from collections.abc import Callable, Iterable
from itertools import islice
from typing import Any, TypeVar

from databricks.sdk.errors import (
    BadRequest,
    DatabricksError,
    InvalidParameterValue,
    NotFound,
    PermissionDenied,
    ResourceDoesNotExist,
    Unauthenticated,
)
from databricks.sdk.service.jobs import BaseRun, Run, RunOutput, RunType

T = TypeVar("T")

# "Doesn't exist" and "can't see it" look the same to the caller.
# InvalidParameterValue is what the API returns for a run ID that was never issued.
_ABSENT = (NotFound, PermissionDenied, ResourceDoesNotExist, InvalidParameterValue)


# The SDK's message when an error response isn't the API's JSON: a workspace that rejects a
# credential minted for another workspace answers 400 with a non-JSON body.
_UNPARSEABLE = "unable to parse response"


class JobsApiError(Exception):
    """A Jobs call failed or timed out. The message is detail for debugging only, never logged."""


class JobsClient:
    def __init__(self, workspace_client: Any, *, timeout_s: float):
        self._jobs = workspace_client.jobs
        self._timeout_s = timeout_s

    async def _call(self, fn: Callable[[], T]) -> T:
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn), self._timeout_s)
        except TimeoutError as exc:
            raise JobsApiError(f"no response after {self._timeout_s:g} seconds") from exc
        except DatabricksError as exc:
            raise JobsApiError(str(exc)) from exc

    async def _call_or_none(self, fn: Callable[[], T]) -> T | None:
        try:
            return await self._call(fn)
        except JobsApiError as exc:
            if isinstance(exc.__cause__, _ABSENT):
                return None
            raise

    async def get_run(self, run_id: int) -> Run | None:
        # The SDK's jobs mixin pages through and merges `tasks` and `iterations`.
        return await self._call_or_none(lambda: self._jobs.get_run(run_id, include_history=True))

    async def get_run_output(self, run_id: int) -> RunOutput:
        return await self._call(lambda: self._jobs.get_run_output(run_id))

    async def list_runs(self, job_id: int, *, limit: int) -> list[BaseRun]:
        return await self._call(
            lambda: _first(
                self._jobs.list_runs(job_id=job_id, completed_only=True, expand_tasks=True), limit
            )
        )

    async def list_workflow_runs(self, start_ms: int, end_ms: int, *, limit: int) -> list[BaseRun]:
        # Notebooks started with dbutils.notebook.run are separate WORKFLOW_RUNs, not tasks.
        return await self._call(
            lambda: _first(
                self._jobs.list_runs(
                    run_type=RunType.WORKFLOW_RUN,
                    start_time_from=start_ms,
                    start_time_to=end_ms,
                    completed_only=True,
                    expand_tasks=True,
                ),
                limit,
            )
        )

    async def job_name(self, job_id: int) -> str | None:
        job = await self._call_or_none(lambda: self._jobs.get(job_id))
        return job.settings.name if job and job.settings else None

    async def find_jobs(self, name: str, *, limit: int) -> list[tuple[int, str]]:
        # `name` is the API's exact, case-insensitive filter.
        jobs = await self._call(lambda: _first(self._jobs.list(name=name), limit))
        return [(j.job_id, j.settings.name if j.settings else name) for j in jobs]


def _first(items: Iterable[T], limit: int) -> list[T]:
    # The SDK iterators page lazily, so stopping early also stops the API calls.
    return list(islice(items, limit))


def failure_fields(exc: BaseException) -> str:
    """``error=<type> cause=<type>`` for a log line: class names only, never the error text,
    which can hold job output or request detail."""
    cause = exc.__cause__
    return f"error={type(exc).__name__} cause={type(cause).__name__ if cause else '-'}"


def is_access_refused(exc: BaseException) -> bool:
    """Whether ``exc`` (or the SDK error it wraps) is the workspace refusing the app outright,
    which no retry fixes: a 401, a 403, or the SDK's unparseable 400.

    A 403 for one run or job never gets here: ``_call_or_none`` reads it as absent first.
    """
    for error in (exc, exc.__cause__):
        if isinstance(error, (Unauthenticated, PermissionDenied)):
            return True
        if isinstance(error, BadRequest) and str(error).startswith(_UNPARSEABLE):
            return True
    return False
