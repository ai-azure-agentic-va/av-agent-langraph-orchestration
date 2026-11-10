"""Tests for the per-process snapshot of the workspace registry.

``refresh`` re-reads the store only when its ``registry_version`` moved, skips (with one
ERROR) a row that fails validation, swaps the workspaces in one assignment and tells the
listeners which aliases changed or went. ``start_registry`` never stops startup: a store
it can't reach is logged and the snapshot stays empty; the poller keeps the last
snapshot when a refresh fails.

Runs standalone (``python test_workspace_registry_snapshot.py``).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from databricks_test_support import LogCapture, captured_logs, patched_env, run_tests
from databricks_test_support import run_async as _run

import v1.core.databricks_workspaces as dw
import v1.core.workspace_registry.snapshot as snap
from v1.core.config import Settings
from v1.core.databricks_workspaces import WorkspaceUnavailable
from v1.core.tools.databricks.jobs.explain import Explanation, ExplanationCache
from v1.core.workspace_registry.memory_store import MemoryWorkspaceStore
from v1.core.workspace_registry.models import Actor, WorkspaceRow
from v1.core.workspace_registry.snapshot import RegistrySnapshot, get_snapshot

ADMIN = Actor("oid-admin", "Ada Admin")
SNAPSHOT_LOGGER = "v1.core.workspace_registry.snapshot"


def _entry(n: int, **extra: Any) -> dict[str, Any]:
    return {"host": f"https://adb-{n}.azuredatabricks.net", "sql_warehouse_id": f"wh-{n}", **extra}


def _logs() -> AbstractContextManager[LogCapture]:
    return captured_logs(SNAPSHOT_LOGGER)


async def _two() -> MemoryWorkspaceStore:
    store = MemoryWorkspaceStore()
    await store.create("a", _entry(1), ADMIN)
    await store.create("b", _entry(2, label="B"), ADMIN)
    return store


@contextmanager
def _legacy(legacy: str | None = None) -> Iterator[None]:
    """Settings with ``DATABRICKS_LEGACY_WORKSPACE`` as given, for the block."""

    with patched_env({"DATABRICKS_LEGACY_WORKSPACE": None}):
        settings = Settings(_env_file=None, DATABRICKS_LEGACY_WORKSPACE=legacy)
    saved_settings = dw.get_settings
    dw.get_settings = lambda: settings
    try:
        yield
    finally:
        dw.get_settings = saved_settings


@contextmanager
def _patched(**names: Any) -> Iterator[None]:
    """Replace attributes of the snapshot module for the block."""

    saved = {name: getattr(snap, name) for name in names}
    for name, value in names.items():
        setattr(snap, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(snap, name, value)


def _start_settings(
    poll_seconds: float = 30, seed_file: str | None = None, admin_groups: list[str] | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        databricks_registry_poll_seconds=poll_seconds,
        databricks_workspaces_seed_file=seed_file,
        databricks_admin_groups=admin_groups or [],
        persistence_backend="memory",
    )


def test_refresh_swaps_and_reports_changes() -> None:
    async def case() -> None:
        store = await _two()
        snapshot = RegistrySnapshot()
        seen: list[frozenset[str]] = []
        snapshot.on_change(seen.append)
        assert snapshot.current() == () and snapshot.version is None
        assert await snapshot.refresh(store) is True
        assert tuple(w.alias for w in snapshot.current()) == ("a", "b")
        assert snapshot.version == await store.registry_version()
        assert seen == []  # nothing was there before, so nothing changed
        before = snapshot.current()
        assert await snapshot.refresh(store) is False  # the version did not move
        assert snapshot.current() is before and seen == []
        b = next(r for r in await store.list() if r.alias == "b")
        await store.update("b", {"label": "Bee"}, b.version, ADMIN)
        assert await snapshot.refresh(store) is True
        assert [w.label for w in snapshot.current()] == ["a", "Bee"]
        assert seen == [frozenset({"b"})], seen
        a = next(r for r in await store.list() if r.alias == "a")
        await store.retire("a", a.version, ADMIN)
        assert await snapshot.refresh(store) is True
        assert seen[-1] == frozenset({"a"}), seen

    _run(case())


def test_invalid_row_is_skipped() -> None:
    async def case() -> None:
        store = await _two()
        good = (await store.list())[0]
        now = datetime.now(UTC)
        # A row written around validation (by hand in the database).
        store._rows["bad"] = replace(
            good, alias="bad", host="http://x", position=99, created_at=now, updated_at=now
        )
        store._registry_version += 1
        snapshot = RegistrySnapshot()
        with _logs() as logs:
            assert await snapshot.refresh(store) is True
        assert tuple(w.alias for w in snapshot.current()) == ("a", "b")
        errors = logs.at(logging.ERROR)
        assert errors == ["databricks workspace bad skipped: host is invalid or missing"], errors

    _run(case())


def test_retired_workspace_is_refused_after_refresh() -> None:
    async def case() -> None:
        store = await _two()
        try:
            with _legacy():
                assert await get_snapshot().refresh(store) is True
                ws, fallback = dw.resolve_workspace([], "a")
                assert ws.alias == "a" and fallback is False
                a = next(r for r in await store.list() if r.alias == "a")
                await store.retire("a", a.version, ADMIN)
                assert await get_snapshot().refresh(store) is True
                try:
                    dw.resolve_workspace([], "a")
                except WorkspaceUnavailable:
                    pass
                else:
                    raise AssertionError("a retired workspace still resolved")
        finally:
            get_snapshot().replace(())

    _run(case())


def test_workspaces_reads_the_snapshot() -> None:
    ws = dw.validate_entry("a", _entry(1))
    assert isinstance(ws, dw.Workspace)
    get_snapshot().replace((ws,), 7)
    try:
        assert dw.workspaces() == (ws,) and get_snapshot().version == 7
    finally:
        get_snapshot().replace(())
    assert dw.workspaces() == ()


def test_legacy_setting_not_a_workspace_is_logged_when_aliases_change() -> None:
    async def case() -> None:
        store = await _two()
        snapshot = RegistrySnapshot()
        expected = (
            "DATABRICKS_LEGACY_WORKSPACE gone is not a configured workspace; "
            "chats without a workspace will be refused"
        )
        with _legacy("gone"):
            with _logs() as logs:
                await snapshot.refresh(store)
            assert logs.at(logging.ERROR) == [expected], logs.at(logging.ERROR)
            # A change that keeps the same aliases logs nothing again.
            b = next(r for r in await store.list() if r.alias == "b")
            await store.update("b", {"label": "Bee"}, b.version, ADMIN)
            with _logs() as logs:
                assert await snapshot.refresh(store) is True
            assert logs.at(logging.ERROR) == [], logs.at(logging.ERROR)
            # A new alias is a new set: logged again.
            await store.create("c", _entry(3), ADMIN)
            with _logs() as logs:
                await snapshot.refresh(store)
            assert logs.at(logging.ERROR) == [expected], logs.at(logging.ERROR)
            # No workspaces at all: nothing to say about the legacy alias.
            empty = RegistrySnapshot()
            with _logs() as logs:
                await empty.refresh(MemoryWorkspaceStore())
            assert logs.at(logging.ERROR) == [], logs.at(logging.ERROR)

    _run(case())


class _FlakyStore(MemoryWorkspaceStore):
    """A store whose ``registry_version`` fails after its first call."""

    def __init__(self) -> None:
        super().__init__()
        self.version_calls = 0

    async def registry_version(self) -> int:
        self.version_calls += 1
        if self.version_calls > 1:
            raise ConnectionError("database went away")
        return await super().registry_version()


def test_poll_failure_keeps_last_snapshot() -> None:
    async def case() -> None:
        store = _FlakyStore()
        await store.create("a", _entry(1), ADMIN)
        with _patched(make_store=lambda settings: store, databricks_registered=lambda: True):
            with _logs() as logs:
                await snap.start_registry(_start_settings(poll_seconds=0.01))
                try:
                    assert [w.alias for w in dw.workspaces()] == ["a"]
                    await asyncio.sleep(0.1)
                    assert [w.alias for w in dw.workspaces()] == ["a"]
                finally:
                    await snap.stop_registry()
        failed = store.version_calls - 1
        errors = logs.at(logging.ERROR)
        assert failed >= 2, failed
        assert len(errors) == failed, (failed, errors)
        assert all("ConnectionError" in e for e in errors), errors
        assert not any("database went away" in e for e in errors), errors  # class name only

    try:
        _run(case())
    finally:
        get_snapshot().replace(())


class _UnreachableStore(MemoryWorkspaceStore):
    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    async def migrate(self) -> None:
        raise OSError("connection refused")

    async def registry_version(self) -> int:
        raise OSError("connection refused")

    async def close(self) -> None:
        self.closed = True


def test_start_survives_unreachable_store() -> None:
    async def case() -> None:
        store = _UnreachableStore()
        with _patched(make_store=lambda settings: store, databricks_registered=lambda: True):
            with _logs() as logs:
                await snap.start_registry(_start_settings())
                assert dw.workspaces() == ()
                assert snap.get_store() is store
                await snap.stop_registry()
                await snap.stop_registry()  # idempotent
        assert store.closed and snap.get_store() is None
        errors = logs.at(logging.ERROR)
        assert errors and all("OSError" in e for e in errors), errors

    _run(case())


class _SlowFirstStore(MemoryWorkspaceStore):
    """The first ``list`` waits for ``release``: a refresh that read the version, then
    stalls while another write and refresh happen."""

    def __init__(self) -> None:
        super().__init__()
        self.stalled = asyncio.Event()
        self.release = asyncio.Event()
        self._lists = 0

    async def list(self, *, include_retired: bool = False) -> list[WorkspaceRow]:
        self._lists += 1
        if self._lists == 1:
            rows = await super().list(include_retired=include_retired)
            self.stalled.set()
            await self.release.wait()
            return rows
        return await super().list(include_retired=include_retired)


def test_interleaved_refreshes_keep_the_newer_rows() -> None:
    """A post-write refresh racing the poller's: the older read never wins."""

    async def case() -> None:
        store = _SlowFirstStore()
        await store.create("a", _entry(1, label="Old"), ADMIN)
        snapshot = RegistrySnapshot()
        poller = asyncio.create_task(snapshot.refresh(store))  # reads version 1, stalls
        await store.stalled.wait()
        a = (await MemoryWorkspaceStore.list(store))[0]
        await store.update("a", {"label": "New"}, a.version, ADMIN)  # version 2
        writer = asyncio.create_task(snapshot.refresh(store))
        for _ in range(10):  # unserialised, the writer would finish here
            await asyncio.sleep(0)
        store.release.set()
        await asyncio.gather(poller, writer)
        assert [w.label for w in snapshot.current()] == ["New"], snapshot.current()
        assert snapshot.version == await store.registry_version() == 2

    _run(case())


def test_lower_registry_version_is_reloaded_with_warning() -> None:
    # A restored or recreated database restarts the counter: its rows still win.
    async def case() -> None:
        store = await _two()  # version 2
        snapshot = RegistrySnapshot()
        snapshot.replace((), 5)
        with _logs() as logs:
            assert await snapshot.refresh(store) is True
        assert [w.alias for w in snapshot.current()] == ["a", "b"]
        assert snapshot.version == 2
        warnings = logs.at(logging.WARNING)
        assert warnings == ["registry version went backwards (5 → 2); reloading"], warnings

    _run(case())


def test_start_logs_admin_group_names_and_blank_seed_file_means_no_seed() -> None:
    async def case() -> None:
        store = MemoryWorkspaceStore()
        paths: list[Any] = []

        async def fake_seed(s: Any, path: Any) -> list[str]:
            paths.append(path)
            return []

        groups = ["Databricks Admins", "0b3c2a8e-5d41-4f6a-9e7b-1c2d3e4f5a6b", "Ops", " "]
        with _patched(
            make_store=lambda settings: store,
            databricks_registered=lambda: True,
            seed_from_file=fake_seed,
        ), _logs() as logs:
            await snap.start_registry(_start_settings(seed_file="   ", admin_groups=groups))
            await snap.stop_registry()
        assert paths == [None], paths
        errors = [e for e in logs.at(logging.ERROR) if "DATABRICKS_ADMIN_GROUPS" in e]
        assert len(errors) == 1 and "2 entries" in errors[0], errors
        assert "Databricks Admins" not in errors[0] and "Ops" not in errors[0], errors

    _run(case())


def test_start_when_databricks_is_off_only_names_retired_settings() -> None:
    async def case() -> None:
        def no_store(settings: Any) -> Any:
            raise AssertionError("the store must not be made")

        saved = os.environ.get("DATABRICKS_HOST")
        os.environ["DATABRICKS_HOST"] = "https://adb-old.azuredatabricks.net"
        try:
            off = _patched(make_store=no_store, databricks_registered=lambda: False)
            with off, _logs() as logs:
                await snap.start_registry(_start_settings())
                assert snap.get_store() is None
                await snap.stop_registry()
        finally:
            if saved is None:
                os.environ.pop("DATABRICKS_HOST", None)
            else:
                os.environ["DATABRICKS_HOST"] = saved
        errors = logs.at(logging.ERROR)
        assert len(errors) == 1 and "DATABRICKS_HOST" in errors[0], errors
        assert "are no longer read" in errors[0], errors

    _run(case())


def test_explanations_dropped_for_changed_alias() -> None:
    explanation = Explanation(why="it broke")
    cache = ExplanationCache()
    cache.put(("a", 1, "h"), explanation)
    cache.put(("b", 2, "h"), explanation)
    cache.drop_workspaces({"b"})
    assert cache.get(("a", 1, "h")) is not None and cache.get(("b", 2, "h")) is None

    # The routes module listens on the process snapshot.
    import v1.api.routes.databricks as routes

    async def case() -> None:
        store = await _two()
        saved = routes.EXPLANATIONS
        routes.EXPLANATIONS = ExplanationCache()
        try:
            await get_snapshot().refresh(store)
            routes.EXPLANATIONS.put(("a", 1, "h"), explanation)
            routes.EXPLANATIONS.put(("b", 2, "h"), explanation)
            b = next(r for r in await store.list() if r.alias == "b")
            await store.update("b", {"sql_warehouse_id": "wh-new"}, b.version, ADMIN)
            await get_snapshot().refresh(store)
            assert routes.EXPLANATIONS.get(("a", 1, "h")) is not None
            assert routes.EXPLANATIONS.get(("b", 2, "h")) is None
        finally:
            routes.EXPLANATIONS = saved
            get_snapshot().replace(())

    _run(case())


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
