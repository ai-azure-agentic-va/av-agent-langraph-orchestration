"""Shared runner and helpers for the Databricks and workspace-registry test scripts.

Not a test file: the ``test_databricks_*.py`` and ``test_workspace_registry_*.py`` files
import it from their own folder, which Python puts on ``sys.path`` when a test file runs
as a script.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import sys
from collections.abc import Callable, Coroutine, Iterable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

Check = tuple[str, Callable[[], object]]


# -- runner --------------------------------------------------------------------------


def collect_tests(namespace: Mapping[str, Any]) -> list[Check]:
    """Every ``test_*`` function in ``namespace`` (a test module's globals), by name."""

    return [(name, value) for name, value in sorted(namespace.items()) if name.startswith("test_")]


def run_checks(checks: Iterable[Check]) -> int:
    """Runs every named check (awaiting async ones), reporting all; 1 if any failed."""

    checks = list(checks)
    failures = 0
    for name, check in checks:
        try:
            result = check()
            if inspect.iscoroutine(result):
                run_async(result)
        except Exception as exc:  # noqa: BLE001 - standalone runner reports all
            failures += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {name}")
    print(f"\n{len(checks) - failures}/{len(checks)} passed")
    return 1 if failures else 0


def run_tests(namespace: Mapping[str, Any]) -> int:
    """Runs every ``test_*`` function in ``namespace``; use as ``run_tests(globals())``."""

    return run_checks(collect_tests(namespace))


def run_async(coro: Coroutine[Any, Any, Any]) -> Any:
    """Runs ``coro`` to completion on a fresh loop.

    psycopg's async connections cannot run on Windows' default Proactor loop, so this uses
    a selector loop there; harmless elsewhere.
    """

    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        return runner.run(coro)


# -- logs ----------------------------------------------------------------------------


class LogCapture(logging.Handler):
    """Keeps every record it sees.

    ``records`` and ``messages`` grow as records arrive; ``getvalue()`` is what a
    ``StreamHandler`` with this handler's formatter would have written.
    """

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        self.messages.append(record.getMessage())

    def at(self, level: int) -> list[str]:
        return [r.getMessage() for r in self.records if r.levelno == level]

    def getvalue(self) -> str:
        return "".join(self.format(record) + "\n" for record in self.records)


@contextmanager
def captured_logs(*names: str, fmt: str | None = None) -> Iterator[LogCapture]:
    """Captures, at DEBUG, what the named loggers emit inside the ``with`` body."""

    capture = LogCapture()
    if fmt is not None:
        capture.setFormatter(logging.Formatter(fmt))
    loggers = [logging.getLogger(name) for name in names]
    saved = [(log, log.level, log.propagate) for log in loggers]
    for log in loggers:
        log.addHandler(capture)
        log.setLevel(logging.DEBUG)
    try:
        yield capture
    finally:
        for log, level, propagate in saved:
            log.removeHandler(capture)
            log.setLevel(level)
            log.propagate = propagate


# -- environment ---------------------------------------------------------------------


def set_env(values: Mapping[str, str | None]) -> None:
    """Sets each variable, or unsets it for None."""

    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@contextmanager
def patched_env(values: Mapping[str, str | None]) -> Iterator[None]:
    """Set (or, for None, unset) environment variables for the ``with`` body."""

    saved = {key: os.environ.get(key) for key in values}
    set_env(values)
    try:
        yield
    finally:
        set_env(saved)


# -- platform user -------------------------------------------------------------------


class _ProxyUser:
    """Offline copy of langgraph_api's ProxyUser over a dict user (not a Mapping)."""

    def __init__(self, user: dict[str, Any]) -> None:
        self._user = user

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_") or name not in self._user:
            raise AttributeError(name)
        return self._user[name]

    def get(self, key: str, default: Any = None) -> Any:
        return self.model_dump().get(key, default)

    def model_dump(self) -> dict[str, Any]:
        identity = self._user["identity"]
        return {
            "identity": identity,
            "is_authenticated": True,
            "display_name": identity,
            **self._user,
        }


def platform_user(
    user: dict[str, Any] | None = None,
    *,
    identity: str | None = None,
    object_id: str | None = None,
) -> Any:
    """``user`` wrapped as the platform does; a stand-in when langgraph_api isn't installed.

    Without ``user``, wraps ``{"identity": identity}`` plus ``"object_id"`` when given
    (``AuthenticatedPrincipal.to_langgraph_user`` emits it only when set).
    """

    if user is None:
        if not identity:
            raise ValueError("platform_user needs a user dict or an identity")
        user = {"identity": identity}
        if object_id is not None:
            user["object_id"] = object_id
    elif identity is not None or object_id is not None:
        raise ValueError("pass either a user dict or identity/object_id, not both")

    # langgraph_api.config reads these at import; any value will do for normalize_user.
    defaults = {"REDIS_URI": "redis://localhost:6379/0", "DATABASE_URI": "postgres://localhost/x"}
    try:
        with patched_env({key: os.environ.get(key, value) for key, value in defaults.items()}):
            from langgraph_api.auth.custom import normalize_user
    except ModuleNotFoundError as exc:
        if exc.name != "langgraph_api":
            raise
        # CI installs no langgraph_api; the image supplies it.
        return _ProxyUser(user)
    return normalize_user(user)


# -- SQL -----------------------------------------------------------------------------


class FakeStatementClient:
    """Stands in for StatementClient: records calls and returns queued results in order.

    Once the queue is empty it returns ``default`` (one row by default). Each call reports
    ``states`` to ``on_state`` first. ``warehouses`` and ``hosts`` are for tests to record
    the warehouse it was made for and the host each workspace client was built for.
    """

    def __init__(
        self, *results: object, default: object = None, states: tuple | None = None
    ) -> None:
        # Imported here so the registry tests don't load the SQL tool modules.
        from databricks.sdk.service.sql import StatementState

        from v1.core.tools.databricks.sql.statements import StatementResult

        self.results = list(results)
        if default is None:
            default = StatementResult("completed", ["n"], [["1"]])
        self.default = default
        self.states = (StatementState.RUNNING,) if states is None else states
        self.calls: list[dict] = []
        self.warehouses: list[str] = []
        self.hosts: list[str] = []

    async def execute(self, sql, *, parameters=None, max_rows, on_state=None):
        self.calls.append({"sql": sql, "parameters": parameters, "max_rows": max_rows})
        for state in self.states:
            if on_state:
                on_state(state)
        if self.results:
            return self.results.pop(0)
        return self.default
