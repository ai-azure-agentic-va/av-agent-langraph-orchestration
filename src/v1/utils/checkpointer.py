from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

_MEMORY_BACKEND = "memory"


@dataclass
class AsyncCheckpointerBundle:
    pool: AsyncConnectionPool
    saver: AsyncPostgresSaver

    async def close(self) -> None:
        logger.info("Closing async Postgres checkpointer pool")
        await self.pool.close()


async def create_postgres_checkpointer(database_url: str) -> AsyncCheckpointerBundle:
    pool = AsyncConnectionPool(
        conninfo=database_url,
        min_size=1,
        max_size=10,
        kwargs={
            "autocommit": True,
            "row_factory": dict_row,
            "prepare_threshold": 0,
        },
        open=False,
    )

    await pool.open()
    await pool.wait()

    saver = AsyncPostgresSaver(pool)

    # Required the first time so LangGraph creates/migrates checkpoint tables.
    await saver.setup()

    logger.info("Async Postgres checkpointer is ready")
    return AsyncCheckpointerBundle(pool=pool, saver=saver)


# --- Process-wide checkpointer selected by the configured persistence backend ---
#
# Both savers are created lazily and cached for the lifetime of the process so
# that conversation/thread state is shared across every ``build_agent`` call
# (checkpointers are keyed by ``thread_id``, so sharing is what lets state
# persist between requests) and the Postgres connection pool is opened only
# once. Use ``close_checkpointer`` on shutdown to release the pool.
_checkpointer_lock = asyncio.Lock()
_postgres_bundle: AsyncCheckpointerBundle | None = None
_memory_saver: InMemorySaver | None = None


async def get_checkpointer(
    persistence_backend: str, database_url: str
) -> InMemorySaver | AsyncPostgresSaver:
    """Return the process-wide checkpointer for ``persistence_backend``.

    ``"memory"`` (case-insensitive) yields a singleton :class:`InMemorySaver`;
    any other value yields a singleton :class:`AsyncPostgresSaver` backed by a
    connection pool created once via :func:`create_postgres_checkpointer`.
    """
    global _postgres_bundle, _memory_saver

    if persistence_backend.strip().lower() == _MEMORY_BACKEND:
        if _memory_saver is None:
            async with _checkpointer_lock:
                if _memory_saver is None:
                    logger.info("Using in-memory checkpointer")
                    _memory_saver = InMemorySaver()
        return _memory_saver

    if _postgres_bundle is None:
        async with _checkpointer_lock:
            if _postgres_bundle is None:
                logger.info("Using Postgres checkpointer")
                _postgres_bundle = await create_postgres_checkpointer(database_url)
    return _postgres_bundle.saver


async def close_checkpointer() -> None:
    """Close the Postgres connection pool if one was opened. Idempotent."""
    global _postgres_bundle

    if _postgres_bundle is not None:
        bundle, _postgres_bundle = _postgres_bundle, None
        await bundle.close()