"""The workspace registry store contract, and the store the settings pick.

Every write validates the merged entry with ``validate_entry`` (a bad field raises
``InvalidWorkspace``), checks the caller's ``expected_version`` (``StaleVersion``),
bumps the row's ``version`` and the ``registry_version``, and records a history entry.
A write that would change nothing (an edit to the same values, retiring a retired
workspace, restoring a live one, the order they already have) returns the row as it is
(``reorder``: False) and records nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol

from v1.core.config import databricks_local_dev
from v1.core.workspace_registry.models import Actor, HistoryEntry, WorkspaceRow

if TYPE_CHECKING:
    from v1.core.config import Settings

logger = logging.getLogger(__name__)


class WorkspaceStore(Protocol):
    async def migrate(self) -> None:
        """Create the store's tables if needed."""

    async def list(self, *, include_retired: bool = False) -> list[WorkspaceRow]:
        """The workspaces by ``position``; retired ones only when asked."""

    async def registry_version(self) -> int:
        """A counter every write bumps."""

    async def create(self, alias: str, fields: Mapping[str, Any], actor: Actor) -> WorkspaceRow:
        """Append a workspace last. ``AliasTaken`` if the alias exists, even retired; a key
        outside ``EDITABLE_FIELDS`` and ``host`` raises ``InvalidWorkspace``."""

    async def update(
        self, alias: str, changes: Mapping[str, Any], expected_version: int, actor: Actor
    ) -> WorkspaceRow:
        """Change ``EDITABLE_FIELDS`` only; any other key raises ``InvalidWorkspace``."""

    async def retire(self, alias: str, expected_version: int, actor: Actor) -> WorkspaceRow:
        """Hide a workspace from ``list()``; its alias stays taken."""

    async def restore(self, alias: str, expected_version: int, actor: Actor) -> WorkspaceRow:
        """Bring a retired workspace back, re-validated."""

    async def reorder(
        self, aliases: Sequence[str], expected_registry_version: int, actor: Actor
    ) -> bool:
        """Put the live workspaces in this order; ``BadOrder`` unless exactly them.
        Whether anything moved: the order they already have writes nothing."""

    async def history(self, alias: str, limit: int = 100) -> list[HistoryEntry]:
        """The workspace's history, newest first."""

    async def seed(self, entries: Sequence[tuple[str, Mapping[str, Any]]]) -> list[str]:
        """Insert, in order, the aliases the table has never had; returns them. Their
        entries are checked as ``create`` checks them."""

    async def close(self) -> None:
        """Release the store's connections."""


def make_store(settings: Settings) -> WorkspaceStore:
    """The in-memory store for the ``memory`` backend, else the Postgres store.

    The in-memory store outside local dev is logged once (WARNING): each replica keeps
    its own copy, and admin edits are lost on restart.
    """

    if settings.persistence_backend.strip().lower() == "memory":
        from v1.core.workspace_registry.memory_store import MemoryWorkspaceStore

        if not databricks_local_dev():
            logger.warning(
                "workspace registry is in memory: admin edits are per-replica and lost "
                "on restart (set PERSISTENCE_BACKEND=postgres)"
            )
        return MemoryWorkspaceStore()
    from v1.core.workspace_registry.postgres_store import PostgresWorkspaceStore

    return PostgresWorkspaceStore(settings.postgress_url)


__all__ = ["WorkspaceStore", "make_store"]
