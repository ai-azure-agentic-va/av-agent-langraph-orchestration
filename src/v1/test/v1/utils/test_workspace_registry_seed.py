"""Tests for the workspace seed file.

``load_seed_file`` reads a YAML mapping of alias to entry, keeps file order, and
leaves out (with one ERROR each) entries that fail validation; a missing or broken
file is one ERROR and nothing. ``seed_from_file`` only fills in aliases the registry
has never had, once, even when two callers race (Postgres cases only when
``TEST_POSTGRES_URL`` is set).

Runs standalone (``python test_workspace_registry_seed.py``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

from databricks_test_support import LogCapture, captured_logs, collect_tests, run_checks
from databricks_test_support import run_async as _run

from v1.core.workspace_registry.memory_store import MemoryWorkspaceStore
from v1.core.workspace_registry.models import Actor
from v1.core.workspace_registry.seed import load_seed_file, seed_from_file
from v1.core.workspace_registry.store import WorkspaceStore

ADMIN = Actor("oid-admin", "Ada Admin")

TWO_WORKSPACES = """\
a:
  label: A
  host: https://adb-1.azuredatabricks.net
  sql_warehouse_id: wh-1
b:
  label: B
  host: https://adb-2.azuredatabricks.net
  sql_warehouse_id: wh-2
  data_allowlist: main.sales
  jobs_enabled: false
  groups: [ops]
"""


def _captured() -> AbstractContextManager[LogCapture]:
    return captured_logs("v1.core.workspace_registry.seed")


@contextmanager
def _seed_file(text: str) -> Iterator[str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "seed.yaml"
        path.write_text(text, encoding="utf-8")
        yield str(path)


def test_seed_file_order_and_skips() -> None:
    text = """\
b:
  label: B
  host: https://adb-2.azuredatabricks.net
  sql_warehouse_id: wh-2
x:
  host: http://adb-9.azuredatabricks.net
  sql_warehouse_id: wh-9
y:
  host: https://adb-1.azuredatabricks.net
  sql_warehouse_id: wh-1
  owner: me
Bad_Alias:
  host: https://adb-1.azuredatabricks.net
  sql_warehouse_id: wh-1
c: not-a-mapping
a:
  label: A
  host: https://adb-1.azuredatabricks.net
  sql_warehouse_id: wh-1
"""
    with _seed_file(text) as path, _captured() as logs:
        entries = load_seed_file(path)
    assert [alias for alias, _ in entries] == ["b", "a"]
    assert entries[0][1]["host"] == "https://adb-2.azuredatabricks.net"
    assert entries[1][1]["label"] == "A"
    assert logs.at(logging.ERROR) == [
        "databricks seed entry x skipped: host is invalid or missing",
        "databricks seed entry y skipped: owner is invalid or missing",
        "databricks seed entry Bad_Alias skipped: alias is invalid or missing",
        "databricks seed entry c skipped: entry is invalid or missing",
    ], logs.at(logging.ERROR)


def test_unreadable_or_empty_file_is_empty() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        missing = str(Path(tmp) / "absent.yaml")
        with _captured() as logs:
            assert load_seed_file(missing) == []
        assert len(logs.at(logging.ERROR)) == 1, logs.at(logging.ERROR)
    # Not a mapping or not YAML: one ERROR. An empty mapping: none.
    for text, errors in (("[1, 2]", 1), (":", 1), ("{}\n", 0), ("# only a comment\n{}\n", 0)):
        with _seed_file(text) as path, _captured() as logs:
            assert load_seed_file(path) == []
        assert len(logs.at(logging.ERROR)) == errors, (text, logs.at(logging.ERROR))


def test_seed_from_file() -> None:
    async def case() -> None:
        store = MemoryWorkspaceStore()
        with _captured() as logs:
            assert await seed_from_file(store, None) == []
        assert logs.records == []
        assert await store.list(include_retired=True) == []
        with _seed_file(TWO_WORKSPACES) as path, _captured() as logs:
            assert await seed_from_file(store, path) == ["a", "b"]
        assert logs.at(logging.INFO) == ["databricks seed inserted ['a', 'b']"]
        rows = await store.list()
        assert [r.alias for r in rows] == ["a", "b"]
        assert rows[1].groups == ("ops",) and rows[1].jobs_enabled is False

    _run(case())


def test_retired_alias_not_reseeded() -> None:
    async def case() -> None:
        store = MemoryWorkspaceStore()
        a = await store.create(
            "a",
            {"label": "Mine", "host": "https://adb-1.azuredatabricks.net", "sql_warehouse_id": "w"},
            ADMIN,
        )
        await store.retire("a", a.version, ADMIN)
        text = "a:\n  host: https://adb-1.azuredatabricks.net\n  sql_warehouse_id: wh-1\n"
        with _seed_file(text) as path:
            assert await seed_from_file(store, path) == []
        (row,) = await store.list(include_retired=True)
        assert row.retired_at is not None and row.label == "Mine"
        assert [h.action for h in await store.history("a")] == ["retired", "created"]

    _run(case())


async def _assert_seeded_once(first: WorkspaceStore, second: WorkspaceStore) -> None:
    with _seed_file(TWO_WORKSPACES) as path:
        results = await asyncio.gather(seed_from_file(first, path), seed_from_file(second, path))
    assert sorted(alias for result in results for alias in result) == ["a", "b"], results
    assert [r.alias for r in await first.list()] == ["a", "b"]
    for alias in ("a", "b"):
        assert [h.action for h in await first.history(alias)] == ["seeded"]


def test_concurrent_seeds_insert_once() -> None:
    store = MemoryWorkspaceStore()
    _run(_assert_seeded_once(store, store))


def test_concurrent_seeds_insert_once_postgres() -> None:
    async def case() -> None:
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo

        from v1.core.workspace_registry.postgres_store import PostgresWorkspaceStore

        url = os.environ["TEST_POSTGRES_URL"]
        name = f"wsreg_{uuid.uuid4().hex}"
        async with await psycopg.AsyncConnection.connect(url, autocommit=True) as admin:
            await admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        dsn = make_conninfo(url, dbname=name)
        first, second = PostgresWorkspaceStore(dsn), PostgresWorkspaceStore(dsn)
        try:
            await first.migrate()
            await second.migrate()
            await _assert_seeded_once(first, second)
        finally:
            await first.close()
            await second.close()
            async with await psycopg.AsyncConnection.connect(url, autocommit=True) as admin:
                await admin.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
                )

    _run(case())


def _main() -> int:
    checks = collect_tests(globals())
    if not os.getenv("TEST_POSTGRES_URL"):
        print("skip test_concurrent_seeds_insert_once_postgres (TEST_POSTGRES_URL not set)")
        checks = [(n, c) for n, c in checks if not n.endswith("_postgres")]
    return run_checks(checks)


if __name__ == "__main__":
    raise SystemExit(_main())
