"""Shared fakes for the Databricks job tool tests (ported from databricks-agent ``conftest.py``).

Not a test file: the ``test_databricks_jobs_*.py`` files import it from their own folder,
which Python puts on ``sys.path`` when a test file runs as a script.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from databricks.sdk.service.jobs import BaseRun, Run, RunOutput
from databricks_test_support import captured_logs
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "databricks"
PARENT = json.loads((FIXTURES / "nested_failure_output.json").read_text("utf-8"))
CHILD = json.loads((FIXTURES / "child_failure_output.json").read_text("utf-8"))

JOBS_LOGGER = "v1.core.tools.databricks.jobs"


class FakeJobsClient:
    """Stands in for JobsClient over plain dicts. `fail` maps a method name to an error to raise.

    It only holds what the app's client could see: a run or job the app can't read is
    simply absent, as the Jobs API reports it.
    """

    def __init__(self) -> None:
        self.runs: dict[int, Run] = {}
        self.outputs: dict[int, RunOutput | Exception] = {}
        self.job_runs: dict[int, list[BaseRun]] = {}
        self.workflow_runs: list[BaseRun] = []
        self.names: dict[int, str] = {}
        self.calls: list[tuple[str, Any]] = []
        self.fail: dict[str, Exception] = {}

    def _record(self, method: str, arg: Any) -> None:
        self.calls.append((method, arg))
        if method in self.fail:
            raise self.fail[method]

    async def get_run(self, run_id: int) -> Run | None:
        self._record("get_run", run_id)
        return self.runs.get(run_id)

    async def get_run_output(self, run_id: int) -> RunOutput:
        self._record("get_run_output", run_id)
        outcome = self.outputs.get(run_id, RunOutput())
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def list_runs(self, job_id: int, *, limit: int) -> list[BaseRun]:
        self._record("list_runs", job_id)
        return self.job_runs.get(job_id, [])[:limit]

    async def list_workflow_runs(self, start_ms: int, end_ms: int, *, limit: int) -> list[BaseRun]:
        self._record("list_workflow_runs", (start_ms, end_ms))
        in_window = [r for r in self.workflow_runs if start_ms <= (r.start_time or 0) <= end_ms]
        return in_window[:limit]

    async def job_name(self, job_id: int) -> str | None:
        self._record("job_name", job_id)
        return self.names.get(job_id)

    async def find_jobs(self, name: str, *, limit: int) -> list[tuple[int, str]]:
        self._record("find_jobs", name)
        found = [(i, n) for i, n in self.names.items() if n.casefold() == name.casefold()]
        return found[:limit]


@contextmanager
def capture_logs(name: str = JOBS_LOGGER) -> Iterator[list[logging.LogRecord]]:
    """The log records the job tool modules emit inside the block."""

    with captured_logs(name) as capture:
        yield capture.records


def rendered(records: list[logging.LogRecord]) -> str:
    """Everything a log formatter could print for these records, tracebacks included."""

    formatter = logging.Formatter()
    return "\n".join(formatter.format(record) for record in records)


def events(records: list[logging.LogRecord], event: str) -> list[logging.LogRecord]:
    """The records whose message starts with ``event``."""

    return [r for r in records if r.getMessage().split(" ", 1)[0] == event]


def fields(record: logging.LogRecord) -> dict[str, str]:
    """The ``key=value`` pairs after the event name in a record's message."""

    return dict(part.split("=", 1) for part in record.getMessage().split(" ")[1:])


class ScriptedChatModel(BaseChatModel):
    """Returns queued AIMessages in order, then a plain 'done' answer. Records what it was sent."""

    script: list[AIMessage] = Field(default_factory=list)
    seen: list = Field(default_factory=list)  # messages received on each call

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        message = self.script.pop(0) if self.script else AIMessage(content="done")
        return ChatResult(generations=[ChatGeneration(message=message)])


def make_model(*messages: AIMessage) -> ScriptedChatModel:
    return ScriptedChatModel(script=list(messages))


class StubAPI:
    """Records each call; returns, computes or raises what each method name is given."""

    def __init__(self) -> None:
        self.results: dict = {}
        self.calls: list[tuple[str, tuple, dict]] = []

    @property
    def last_kwargs(self) -> dict:
        return self.calls[-1][2]

    def __getattr__(self, name):
        if name.startswith("_") or name not in self.results:
            raise AttributeError(name)

        def method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            result = self.results[name]
            if isinstance(result, Exception):
                raise result
            return result(*args, **kwargs) if callable(result) else result

        return method


class StubWorkspace:
    """The user's WorkspaceClient as far as the job tools use it: the Jobs API only."""

    def __init__(self) -> None:
        self.jobs = StubAPI()


def raises(error: type[BaseException], call: Callable[[], object]) -> BaseException:
    """The ``error`` that ``call()`` raises; fails when it raises nothing."""
    try:
        call()
    except error as exc:
        return exc
    raise AssertionError(f"expected {error.__name__}")
