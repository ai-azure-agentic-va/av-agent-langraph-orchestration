"""The workspace registry kept in Postgres, in the ``workspace_registry`` schema.

Every write runs in one transaction that first locks the ``registry_version`` row, so
writes are serialised across replicas: positions, the registry version and the history
always agree. Row writes still check the caller's version in the ``UPDATE`` itself.
"""

from __future__ import annotations

import asyncio
import builtins
from collections.abc import Mapping, Sequence
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from v1.core.databricks_workspaces import Workspace, workspace_fields
from v1.core.workspace_registry.checks import absent, edit, moves, only, validated
from v1.core.workspace_registry.migrate import apply_migrations
from v1.core.workspace_registry.models import (
    EDITABLE_FIELDS,
    SEED_ACTOR,
    Action,
    Actor,
    AliasTaken,
    HistoryEntry,
    NotFound,
    StaleVersion,
    WorkspaceRow,
)

# How long the first connection may take before a call (or startup) gives up.
OPEN_TIMEOUT_SECONDS = 10.0

_COLUMNS = (
    "alias, label, host, sql_warehouse_id, data_allowlist, jobs_enabled, groups, position,"
    " retired_at, version, created_at, created_by, updated_at, updated_by"
)

_INSERT = (
    f"INSERT INTO workspace_registry.workspaces ({_COLUMNS})"
    " VALUES (%(alias)s, %(label)s, %(host)s, %(sql_warehouse_id)s, %(data_allowlist)s,"
    " %(jobs_enabled)s, %(groups)s,"
    " (SELECT coalesce(max(position), 0) + 1 FROM workspace_registry.workspaces),"
    " NULL, 1, now(), %(actor)s, now(), %(actor)s)"
    f" ON CONFLICT (alias) DO NOTHING RETURNING {_COLUMNS}"
)


def _row(record: Mapping[str, Any]) -> WorkspaceRow:
    return WorkspaceRow(**{**record, "groups": tuple(record["groups"])})


def _insert_params(ws: Workspace, actor: Actor) -> dict[str, Any]:
    return {**workspace_fields(ws), "alias": ws.alias, "actor": actor.identity}


class PostgresWorkspaceStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: AsyncConnectionPool | None = None
        self._pool_lock = asyncio.Lock()

    async def _connections(self) -> AsyncConnectionPool:
        """The pool, opened on first use."""

        if self._pool is None:
            async with self._pool_lock:
                if self._pool is None:
                    pool = AsyncConnectionPool(
                        self._dsn,
                        min_size=1,
                        max_size=4,
                        kwargs={"autocommit": True, "row_factory": dict_row},
                        open=False,
                    )
                    try:
                        # Fail fast (the default waits 30 s): an unreachable database
                        # must not hold every admin request; the next call retries.
                        await pool.open(wait=True, timeout=OPEN_TIMEOUT_SECONDS)
                    except BaseException:
                        await pool.close()
                        raise
                    self._pool = pool
        return self._pool

    async def migrate(self) -> None:
        pool = await self._connections()
        async with pool.connection() as conn:
            await apply_migrations(conn)

    async def close(self) -> None:
        if self._pool is not None:
            pool, self._pool = self._pool, None
            await pool.close()

    async def list(self, *, include_retired: bool = False) -> builtins.list[WorkspaceRow]:
        pool = await self._connections()
        async with pool.connection() as conn:
            return await self._list(conn, include_retired)

    async def registry_version(self) -> int:
        pool = await self._connections()
        async with pool.connection() as conn:
            return await self._version(conn)

    async def history(self, alias: str, limit: int = 100) -> builtins.list[HistoryEntry]:
        pool = await self._connections()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT alias, at, actor_identity, actor_name, actor_email, action, before,"
                " after FROM workspace_registry.history WHERE alias = %s"
                " ORDER BY at DESC, id DESC LIMIT %s",
                (alias, limit),
            )
            return [
                HistoryEntry(
                    alias=r["alias"],
                    at=r["at"],
                    actor=Actor(r["actor_identity"], r["actor_name"], r["actor_email"]),
                    action=r["action"],
                    before=r["before"],
                    after=r["after"],
                )
                for r in await cur.fetchall()
            ]

    async def create(self, alias: str, fields: Mapping[str, Any], actor: Actor) -> WorkspaceRow:
        ws = validated(alias, fields)
        pool = await self._connections()
        async with pool.connection() as conn, conn.transaction():
            await self._version(conn, lock=True)
            cur = await conn.execute(_INSERT, _insert_params(ws, actor))
            record = await cur.fetchone()
            if record is None:
                raise AliasTaken(alias)
            row = _row(record)
            await self._record(conn, row, actor, "created", None, row.fields())
            await self._bump(conn)
            return row

    async def update(
        self, alias: str, changes: Mapping[str, Any], expected_version: int, actor: Actor
    ) -> WorkspaceRow:
        only(changes, EDITABLE_FIELDS)
        pool = await self._connections()
        async with pool.connection() as conn, conn.transaction():
            await self._version(conn, lock=True)
            row = await self._current(conn, alias, expected_version)
            edited = edit(row, changes)
            if edited is None:
                return row
            fields, before, after = edited
            assignments = ", ".join(f"{k} = %({k})s" for k in EDITABLE_FIELDS)
            new = await self._write(conn, alias, expected_version, actor, assignments, fields)
            await self._record(conn, new, actor, "edited", before, after)
            await self._bump(conn)
            return new

    async def retire(self, alias: str, expected_version: int, actor: Actor) -> WorkspaceRow:
        pool = await self._connections()
        async with pool.connection() as conn, conn.transaction():
            await self._version(conn, lock=True)
            row = await self._current(conn, alias, expected_version)
            if row.retired_at is not None:
                return row
            new = await self._write(conn, alias, expected_version, actor, "retired_at = now()")
            await self._record(conn, new, actor, "retired", None, None)
            await self._bump(conn)
            return new

    async def restore(self, alias: str, expected_version: int, actor: Actor) -> WorkspaceRow:
        pool = await self._connections()
        async with pool.connection() as conn, conn.transaction():
            await self._version(conn, lock=True)
            row = await self._current(conn, alias, expected_version)
            if row.retired_at is None:
                return row
            validated(alias, row.fields())
            new = await self._write(conn, alias, expected_version, actor, "retired_at = NULL")
            await self._record(conn, new, actor, "restored", None, None)
            await self._bump(conn)
            return new

    async def reorder(
        self, aliases: Sequence[str], expected_registry_version: int, actor: Actor
    ) -> bool:
        pool = await self._connections()
        async with pool.connection() as conn, conn.transaction():
            if await self._version(conn, lock=True) != expected_registry_version:
                raise StaleVersion()
            moved = moves(aliases, await self._list(conn, include_retired=False))
            if not moved:
                return False
            for row, position in moved:
                before, after = {"position": row.position}, {"position": position}
                new = await self._write(
                    conn, row.alias, row.version, actor, "position = %(position)s", after
                )
                await self._record(conn, new, actor, "reordered", before, after)
            await self._bump(conn)
            return True

    async def seed(self, entries: Sequence[tuple[str, Mapping[str, Any]]]) -> builtins.list[str]:
        pool = await self._connections()
        async with pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('workspace_registry.seed'))")
            await self._version(conn, lock=True)
            cur = await conn.execute(
                "SELECT alias FROM workspace_registry.workspaces WHERE alias = ANY(%s)",
                ([alias for alias, _ in entries],),
            )
            present = {r["alias"] for r in await cur.fetchall()}
            inserted: builtins.list[str] = []
            for ws in absent(entries, present).values():
                cur = await conn.execute(_INSERT, _insert_params(ws, SEED_ACTOR))
                record = await cur.fetchone()
                if record is None:
                    continue
                row = _row(record)
                await self._record(conn, row, SEED_ACTOR, "seeded", None, row.fields())
                inserted.append(row.alias)
            if inserted:
                await self._bump(conn)
            return inserted

    @staticmethod
    async def _list(conn: AsyncConnection, include_retired: bool) -> builtins.list[WorkspaceRow]:
        where = "" if include_retired else " WHERE retired_at IS NULL"
        cur = await conn.execute(
            f"SELECT {_COLUMNS} FROM workspace_registry.workspaces{where} ORDER BY position"
        )
        return [_row(r) for r in await cur.fetchall()]

    @staticmethod
    async def _version(conn: AsyncConnection, lock: bool = False) -> int:
        """The registry version; ``lock`` locks the registry against other writers until commit."""

        cur = await conn.execute(
            "SELECT version FROM workspace_registry.registry_version"
            + (" FOR UPDATE" if lock else "")
        )
        record = await cur.fetchone()
        assert record is not None
        return record["version"]

    @staticmethod
    async def _bump(conn: AsyncConnection) -> None:
        await conn.execute("UPDATE workspace_registry.registry_version SET version = version + 1")

    @staticmethod
    async def _current(conn: AsyncConnection, alias: str, expected_version: int) -> WorkspaceRow:
        cur = await conn.execute(
            f"SELECT {_COLUMNS} FROM workspace_registry.workspaces WHERE alias = %s",
            (alias,),
        )
        record = await cur.fetchone()
        if record is None:
            raise NotFound(alias)
        row = _row(record)
        if row.version != expected_version:
            raise StaleVersion(alias)
        return row

    @staticmethod
    async def _write(
        conn: AsyncConnection,
        alias: str,
        expected_version: int,
        actor: Actor,
        assignments: str,
        params: Mapping[str, Any] | None = None,
    ) -> WorkspaceRow:
        """Apply ``assignments`` as the next version of ``alias``, if it is still at
        ``expected_version``."""

        cur = await conn.execute(
            f"UPDATE workspace_registry.workspaces SET {assignments},"
            " version = version + 1, updated_at = now(), updated_by = %(actor)s"
            " WHERE alias = %(alias)s AND version = %(expected_version)s"
            f" RETURNING {_COLUMNS}",
            {
                **(params or {}),
                "actor": actor.identity,
                "alias": alias,
                "expected_version": expected_version,
            },
        )
        record = await cur.fetchone()
        if record is None:  # each caller read the row under the registry lock: unreachable
            raise StaleVersion(alias)
        return _row(record)

    @staticmethod
    async def _record(
        conn: AsyncConnection,
        row: WorkspaceRow,
        actor: Actor,
        action: Action,
        before: dict | None,
        after: dict | None,
    ) -> None:
        await conn.execute(
            "INSERT INTO workspace_registry.history (alias, at, actor_identity, actor_name,"
            " actor_email, action, before, after) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                row.alias,
                row.updated_at,
                actor.identity,
                actor.name,
                actor.email,
                action,
                None if before is None else Jsonb(before),
                None if after is None else Jsonb(after),
            ),
        )


__all__ = ["PostgresWorkspaceStore"]
