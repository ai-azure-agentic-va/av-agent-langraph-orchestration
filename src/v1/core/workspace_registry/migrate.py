"""Versioned SQL migrations for the ``workspace_registry`` schema.

Each ``migrations/*.sql`` file runs once, in name order, in its own transaction, and
is recorded in ``workspace_registry.schema_migrations``. Every transaction first takes
the same advisory lock, so replicas starting together apply each file exactly once.
The files are found next to this module, so an editable install reads them in place.
"""

from __future__ import annotations

import logging
from pathlib import Path

import psycopg

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).with_name("migrations")

_LOCK = "SELECT pg_advisory_xact_lock(hashtext('workspace_registry.migrate'))"


async def apply_migrations(conn: psycopg.AsyncConnection) -> list[str]:
    """Apply the migrations ``conn``'s database lacks; returns their names, in order."""

    async with conn.transaction():
        await conn.execute(_LOCK)
        await conn.execute("CREATE SCHEMA IF NOT EXISTS workspace_registry")
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS workspace_registry.schema_migrations"
            " (name text PRIMARY KEY, applied_at timestamptz NOT NULL)"
        )
    applied: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        async with conn.transaction():
            await conn.execute(_LOCK)
            cur = await conn.execute(
                "SELECT 1 FROM workspace_registry.schema_migrations WHERE name = %s",
                (path.name,),
            )
            if await cur.fetchone() is not None:
                continue
            await conn.execute(path.read_text(encoding="utf-8"))
            await conn.execute(
                "INSERT INTO workspace_registry.schema_migrations (name, applied_at)"
                " VALUES (%s, now())",
                (path.name,),
            )
        logger.info("Applied workspace registry migration %s", path.name)
        applied.append(path.name)
    return applied


__all__ = ["MIGRATIONS_DIR", "apply_migrations"]
