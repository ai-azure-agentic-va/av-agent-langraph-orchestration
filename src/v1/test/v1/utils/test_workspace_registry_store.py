"""Contract tests for the workspace registry stores.

Each ``contract_*`` case takes a store factory and runs against every store: the
in-memory one always, Postgres only when ``TEST_POSTGRES_URL`` is set. Every write is
validated, versioned and recorded in the history; stale versions and unknown aliases
are refused; ``seed`` only fills in aliases the table has never had. The
``postgres_*`` cases check the migrations and Postgres-only behaviour. Each Postgres
case gets fresh ``wsreg_<uuid>`` databases, dropped afterwards.

Runs standalone (``python test_workspace_registry_store.py``).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from typing import Any

from databricks_test_support import captured_logs, collect_tests, patched_env, run_checks
from databricks_test_support import run_async as _run

from v1.core.config import Settings
from v1.core.workspace_registry.memory_store import MemoryWorkspaceStore
from v1.core.workspace_registry.models import (
    SEED_ACTOR,
    Actor,
    AliasTaken,
    BadOrder,
    InvalidWorkspace,
    NotFound,
    StaleVersion,
)
from v1.core.workspace_registry.store import WorkspaceStore, make_store

StoreFactory = Callable[[], Awaitable[WorkspaceStore]]
# Makes a fresh, empty Postgres database and returns its connection string.
NewDatabase = Callable[[], Awaitable[str]]

ADMIN = Actor("oid-admin", "Ada Admin", "ada@example.com")


def _fields(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "label": "Workspace",
        "host": "https://adb-1.azuredatabricks.net",
        "sql_warehouse_id": "wh-1",
        "data_allowlist": None,
        "jobs_enabled": True,
        "groups": [],
    }
    fields.update(overrides)
    return fields


async def _expect(exc_type: type[Exception], awaitable: Awaitable[Any]) -> Any:
    try:
        await awaitable
    except exc_type as exc:
        return exc
    raise AssertionError(f"expected {exc_type.__name__}")


async def _aliases(store: WorkspaceStore, *, include_retired: bool = False) -> list[str]:
    return [row.alias for row in await store.list(include_retired=include_retired)]


async def contract_create_then_list(make: StoreFactory) -> None:
    store = await make()
    start = await store.registry_version()
    a = await store.create("a", _fields(label="A"), ADMIN)
    b = await store.create("b", _fields(label="B"), ADMIN)
    rows = await store.list()
    assert [r.alias for r in rows] == ["a", "b"]
    assert rows[0].position < rows[1].position
    assert await store.registry_version() == start + 2
    assert a.version == 1 and a.retired_at is None
    assert a.created_by == ADMIN.identity and a.updated_by == ADMIN.identity
    assert b.fields() == _fields(label="B")
    history = await store.history("a")
    assert [h.action for h in history] == ["created"]
    assert history[0].actor == ADMIN
    assert history[0].before is None and history[0].after == _fields(label="A")


async def contract_create_and_seed_refuse_bad_fields(make: StoreFactory) -> None:
    store = await make()
    start = await store.registry_version()
    cases = [
        ("host", store.create("a", _fields(host="http://adb-1.azuredatabricks.net"), ADMIN)),
        ("owner", store.create("a", _fields(owner="me"), ADMIN)),
        ("position", store.create("a", _fields(position=1), ADMIN)),
        ("alias", store.seed([("b", _fields(alias="b"))])),
    ]
    for field, write in cases:
        exc = await _expect(InvalidWorkspace, write)
        assert exc.field == field and exc.code == "invalid", (field, exc.field)
    assert await _aliases(store, include_retired=True) == []
    assert await store.history("a") == [] and await store.history("b") == []
    assert await store.registry_version() == start


async def contract_create_rejects_taken_and_retired_alias(make: StoreFactory) -> None:
    store = await make()
    await store.create("a", _fields(), ADMIN)
    exc = await _expect(AliasTaken, store.create("a", _fields(label="Other"), ADMIN))
    assert exc.code == "alias_taken"
    b = await store.create("b", _fields(), ADMIN)
    await store.retire("b", b.version, ADMIN)
    await _expect(AliasTaken, store.create("b", _fields(), ADMIN))
    assert await _aliases(store, include_retired=True) == ["a", "b"]


async def contract_update_changes_only_editable(make: StoreFactory) -> None:
    store = await make()
    a = await store.create("a", _fields(label="Old"), ADMIN)
    exc = await _expect(
        InvalidWorkspace,
        store.update("a", {"host": "https://adb-2.azuredatabricks.net"}, a.version, ADMIN),
    )
    assert exc.field == "host"
    await _expect(NotFound, store.update("zz", {"label": "X"}, 1, ADMIN))
    bad = await _expect(InvalidWorkspace, store.update("a", {"groups": "ops"}, a.version, ADMIN))
    assert bad.field == "groups"
    start = await store.registry_version()
    editor = Actor("oid-editor")
    updated = await store.update("a", {"label": "X", "jobs_enabled": True}, a.version, editor)
    assert updated.label == "X" and updated.version == a.version + 1
    assert updated.updated_by == "oid-editor" and updated.created_by == ADMIN.identity
    assert updated.host == a.host
    assert await store.registry_version() == start + 1
    latest = (await store.history("a"))[0]
    assert latest.action == "edited" and latest.actor == editor
    assert latest.before == {"label": "Old"} and latest.after == {"label": "X"}
    # The version just read is now stale.
    stale = await _expect(StaleVersion, store.update("a", {"label": "Y"}, a.version, ADMIN))
    assert stale.code == "stale"
    assert (await store.list())[0].label == "X"
    await _expect(StaleVersion, store.retire("a", a.version, ADMIN))


async def contract_retire_hides_and_restore_returns(make: StoreFactory) -> None:
    store = await make()
    await store.create("a", _fields(), ADMIN)
    b = await store.create("b", _fields(), ADMIN)
    retired = await store.retire("b", b.version, ADMIN)
    assert retired.retired_at is not None and retired.version == b.version + 1
    assert await _aliases(store) == ["a"]
    rows = await store.list(include_retired=True)
    assert [r.alias for r in rows] == ["a", "b"] and rows[1].retired_at is not None
    await _expect(NotFound, store.retire("zz", 1, ADMIN))
    restored = await store.restore("b", retired.version, ADMIN)
    assert restored.retired_at is None and restored.version == retired.version + 1
    assert await _aliases(store) == ["a", "b"]
    assert [h.action for h in await store.history("b")] == ["restored", "retired", "created"]


async def contract_noop_writes_nothing(make: StoreFactory) -> None:
    store = await make()
    a = await store.create("a", _fields(label="Same", groups=["ops"]), ADMIN)
    start = await store.registry_version()
    # The same values, as sent and after normalisation (stripped label and groups), and
    # restoring a live workspace.
    assert await store.update("a", {"label": "Same", "jobs_enabled": True}, a.version, ADMIN) == a
    assert await store.update("a", {"label": " Same ", "groups": [" ops "]}, a.version, ADMIN) == a
    assert await store.restore("a", a.version, ADMIN) == a
    assert await store.registry_version() == start
    # Retiring a retired workspace.
    retired = await store.retire("a", a.version, ADMIN)
    again = await store.retire("a", retired.version, ADMIN)
    assert again == retired and again.retired_at == retired.retired_at
    assert await store.registry_version() == start + 1
    assert [h.action for h in await store.history("a")] == ["retired", "created"]
    await _expect(StaleVersion, store.retire("a", a.version, ADMIN))


async def contract_reorder(make: StoreFactory) -> None:
    store = await make()
    for alias in ("a", "b", "c"):
        await store.create(alias, _fields(), ADMIN)
    d = await store.create("d", _fields(), ADMIN)
    await store.retire("d", d.version, ADMIN)
    version = await store.registry_version()
    exc = await _expect(BadOrder, store.reorder(["c", "a"], version, ADMIN))
    assert exc.field == "order" and exc.code == "invalid"
    await _expect(BadOrder, store.reorder(["c", "a", "b", "d"], version, ADMIN))
    await _expect(BadOrder, store.reorder(["c", "a", "a"], version, ADMIN))
    await _expect(StaleVersion, store.reorder(["c", "a", "b"], version - 1, ADMIN))
    await _expect(StaleVersion, store.reorder(["a", "b", "c"], version - 1, ADMIN))
    # The order they already have writes nothing.
    before = await store.list()
    assert await store.reorder(["a", "b", "c"], version, ADMIN) is False
    assert await store.list() == before and await store.registry_version() == version
    assert [h.action for h in await store.history("a")] == ["created"]
    assert await store.reorder(["c", "a", "b"], version, ADMIN) is True
    assert await _aliases(store) == ["c", "a", "b"]
    assert await _aliases(store, include_retired=True) == ["c", "a", "b", "d"]
    assert await store.registry_version() == version + 1
    assert (await store.history("c"))[0].action == "reordered"
    assert (await store.history("d"))[0].action == "retired"


async def contract_seed_inserts_only_absent(make: StoreFactory) -> None:
    store = await make()
    await store.create("live", _fields(label="Live"), ADMIN)
    gone = await store.create("gone", _fields(label="Gone"), ADMIN)
    await store.retire("gone", gone.version, ADMIN)
    inserted = await store.seed(
        [
            ("live", _fields(label="Seeded live")),
            ("new-1", _fields(label="New 1")),
            ("gone", _fields(label="Seeded gone")),
            ("new-2", _fields(label="New 2")),
        ]
    )
    assert inserted == ["new-1", "new-2"]
    rows = {r.alias: r for r in await store.list(include_retired=True)}
    assert [r.alias for r in await store.list()] == ["live", "new-1", "new-2"]
    assert rows["live"].label == "Live"
    assert rows["gone"].label == "Gone" and rows["gone"].retired_at is not None
    assert rows["new-1"].created_by == SEED_ACTOR.identity
    history = await store.history("new-1")
    assert [h.action for h in history] == ["seeded"] and history[0].actor == SEED_ACTOR
    assert history[0].after == _fields(label="New 1")
    assert await store.seed([("new-1", _fields())]) == []


@asynccontextmanager
async def _memory_stores() -> AsyncIterator[StoreFactory]:
    async def make() -> WorkspaceStore:
        return MemoryWorkspaceStore()

    yield make


@asynccontextmanager
async def _postgres_databases() -> AsyncIterator[NewDatabase]:
    """Fresh ``wsreg_<uuid>`` databases on ``TEST_POSTGRES_URL``, all dropped afterwards."""

    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    url = os.environ["TEST_POSTGRES_URL"]
    created: list[str] = []

    async def new_database() -> str:
        name = f"wsreg_{uuid.uuid4().hex}"
        async with await psycopg.AsyncConnection.connect(url, autocommit=True) as admin:
            await admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        created.append(name)
        return make_conninfo(url, dbname=name)

    try:
        yield new_database
    finally:
        async with await psycopg.AsyncConnection.connect(url, autocommit=True) as admin:
            for name in created:
                await admin.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
                )


@asynccontextmanager
async def _postgres_stores() -> AsyncIterator[StoreFactory]:
    from v1.core.workspace_registry.postgres_store import PostgresWorkspaceStore

    stores: list[PostgresWorkspaceStore] = []
    async with _postgres_databases() as new_database:

        async def make() -> WorkspaceStore:
            store = PostgresWorkspaceStore(await new_database())
            stores.append(store)
            await store.migrate()
            return store

        try:
            yield make
        finally:
            for store in stores:
                await store.close()


STORES: list[tuple[str, Callable[[], AbstractAsyncContextManager[StoreFactory]]]] = [
    ("memory", _memory_stores),
    ("postgres", _postgres_stores),
]

CONTRACT = [value for name, value in sorted(globals().items()) if name.startswith("contract_")]


async def postgres_migrations_are_idempotent(new_database: NewDatabase) -> None:
    import psycopg

    from v1.core.workspace_registry.migrate import apply_migrations

    async with await psycopg.AsyncConnection.connect(await new_database()) as conn:
        assert await apply_migrations(conn) == ["0001_init.sql"]
        assert await apply_migrations(conn) == []
        cur = await conn.execute(
            "SELECT name FROM workspace_registry.schema_migrations ORDER BY name"
        )
        assert [r[0] for r in await cur.fetchall()] == ["0001_init.sql"]
        cur = await conn.execute("SELECT version FROM workspace_registry.registry_version")
        assert await cur.fetchall() == [(0,)]


async def postgres_restore_revalidates(new_database: NewDatabase) -> None:
    import psycopg

    from v1.core.workspace_registry.postgres_store import PostgresWorkspaceStore

    dsn = await new_database()
    store = PostgresWorkspaceStore(dsn)
    try:
        await store.migrate()
        # A retired row whose host no longer passes validation, as only SQL could leave it.
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            await conn.execute(
                "INSERT INTO workspace_registry.workspaces (alias, label, host,"
                " sql_warehouse_id, data_allowlist, jobs_enabled, groups, position,"
                " retired_at, version, created_at, created_by, updated_at, updated_by)"
                " VALUES ('a', 'A', 'http://evil.example.com', 'wh-1', NULL, true, '{}', 1,"
                " now(), 2, now(), 'sql', now(), 'sql')"
            )
        start = await store.registry_version()
        exc = await _expect(InvalidWorkspace, store.restore("a", 2, ADMIN))
        assert exc.field == "host"
        (row,) = await store.list(include_retired=True)
        assert row.retired_at is not None and row.version == 2
        assert await store.list() == []
        assert await store.history("a") == []
        assert await store.registry_version() == start
    finally:
        await store.close()


POSTGRES_ONLY = [value for name, value in sorted(globals().items()) if name.startswith("postgres_")]


async def _contract(
    case: Callable[[StoreFactory], Awaitable[None]],
    stores: Callable[[], AbstractAsyncContextManager[StoreFactory]],
) -> None:
    async with stores() as make:
        await case(make)


async def _postgres_only(case: Callable[[NewDatabase], Awaitable[None]]) -> None:
    async with _postgres_databases() as new_database:
        await case(new_database)


@contextmanager
def _make_store_logs(backend: str, **env: str | None) -> Iterator[list[str]]:
    """The messages ``make_store`` logs for ``backend`` with ``env`` set."""

    with captured_logs("v1.core.workspace_registry.store") as capture, patched_env(env):
        make_store(Settings(_env_file=None, PERSISTENCE_BACKEND=backend))
        yield list(capture.messages)


def test_make_store() -> None:
    from v1.core.workspace_registry.postgres_store import PostgresWorkspaceStore

    for backend in ("memory", "MEMORY"):
        settings = Settings(_env_file=None, PERSISTENCE_BACKEND=backend)
        assert isinstance(make_store(settings), MemoryWorkspaceStore)
    # The in-memory store warns once outside local dev; Postgres never does.
    with _make_store_logs("memory", AGENT_AUTH_MODE=None, APP_ENV=None) as logs:
        assert len(logs) == 1, logs
        assert "workspace registry is in memory" in logs[0], logs
        assert "per-replica and lost on restart" in logs[0], logs
    with _make_store_logs("memory", AGENT_AUTH_MODE="local", APP_ENV="local") as logs:
        assert logs == [], logs
    with _make_store_logs("postgres", AGENT_AUTH_MODE=None, APP_ENV=None) as logs:
        assert logs == [], logs
    # Postgres connects lazily: nothing has connected, so closing has nothing to release.
    store = make_store(Settings(_env_file=None, PERSISTENCE_BACKEND="postgres"))
    assert isinstance(store, PostgresWorkspaceStore)
    _run(store.close())


def test_postgres_pool_open_fails_fast_and_retries() -> None:
    """The first connection waits at most OPEN_TIMEOUT_SECONDS (not psycopg's 30 s), and a
    failed open is retried on the next call."""

    import v1.core.workspace_registry.postgres_store as pg

    opened: list[dict[str, Any]] = []

    class _FakePool:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def open(self, wait: bool = False, timeout: float = 30.0) -> None:
            opened.append({"wait": wait, "timeout": timeout})
            if len(opened) == 1:
                raise TimeoutError("pool open timed out")

        async def close(self) -> None:
            pass

    async def case() -> None:
        store = pg.PostgresWorkspaceStore("postgresql://nobody@127.0.0.1:1/none")
        await _expect(TimeoutError, store._connections())
        pool = await store._connections()
        assert isinstance(pool, _FakePool)
        assert await store._connections() is pool

    saved = pg.AsyncConnectionPool
    pg.AsyncConnectionPool = _FakePool
    try:
        _run(case())
    finally:
        pg.AsyncConnectionPool = saved
    assert pg.OPEN_TIMEOUT_SECONDS == 10
    assert opened == [{"wait": True, "timeout": 10}] * 2, opened


def _main() -> int:
    checks = collect_tests(globals())
    for store_name, stores in STORES:
        if store_name == "postgres" and not os.getenv("TEST_POSTGRES_URL"):
            print(
                f"skip {store_name}: contract and Postgres-only cases (TEST_POSTGRES_URL not set)"
            )
            continue
        for case in CONTRACT:
            checks.append(
                (f"{case.__name__}[{store_name}]", lambda c=case, s=stores: _run(_contract(c, s)))
            )
        if store_name == "postgres":
            for case in POSTGRES_ONLY:
                checks.append((case.__name__, lambda c=case: _run(_postgres_only(c))))
    return run_checks(checks)


if __name__ == "__main__":
    raise SystemExit(_main())
