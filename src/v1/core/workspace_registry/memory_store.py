"""The workspace registry kept in process memory (``PERSISTENCE_BACKEND=memory``).

One ``asyncio.Lock`` serialises every write, so ``seed`` is atomic against itself.
"""

from __future__ import annotations

import asyncio
import builtins
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from v1.core.databricks_workspaces import Workspace, workspace_fields
from v1.core.workspace_registry.checks import absent, edit, moves, only, validated
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


class MemoryWorkspaceStore:
    def __init__(self) -> None:
        self._rows: dict[str, WorkspaceRow] = {}
        self._history: builtins.list[HistoryEntry] = []
        self._registry_version = 0
        self._lock = asyncio.Lock()

    async def migrate(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def list(self, *, include_retired: bool = False) -> builtins.list[WorkspaceRow]:
        rows = sorted(self._rows.values(), key=lambda r: r.position)
        return [r for r in rows if include_retired or r.retired_at is None]

    async def registry_version(self) -> int:
        return self._registry_version

    async def history(self, alias: str, limit: int = 100) -> builtins.list[HistoryEntry]:
        entries = [h for h in reversed(self._history) if h.alias == alias]
        return entries[:limit]

    async def create(self, alias: str, fields: Mapping[str, Any], actor: Actor) -> WorkspaceRow:
        ws = validated(alias, fields)
        async with self._lock:
            if alias in self._rows:
                raise AliasTaken(alias)
            row = self._insert(ws, actor)
            self._record(row, actor, "created", None, row.fields())
            self._registry_version += 1
            return row

    async def update(
        self, alias: str, changes: Mapping[str, Any], expected_version: int, actor: Actor
    ) -> WorkspaceRow:
        only(changes, EDITABLE_FIELDS)
        async with self._lock:
            row = self._current(alias, expected_version)
            edited = edit(row, changes)
            if edited is None:
                return row
            fields, before, after = edited
            new = self._write(replace(row, **{**fields, "groups": tuple(fields["groups"])}), actor)
            self._record(new, actor, "edited", before, after)
            self._registry_version += 1
            return new

    async def retire(self, alias: str, expected_version: int, actor: Actor) -> WorkspaceRow:
        async with self._lock:
            row = self._current(alias, expected_version)
            if row.retired_at is not None:
                return row
            new = self._write(replace(row, retired_at=datetime.now(UTC)), actor)
            self._record(new, actor, "retired", None, None)
            self._registry_version += 1
            return new

    async def restore(self, alias: str, expected_version: int, actor: Actor) -> WorkspaceRow:
        async with self._lock:
            row = self._current(alias, expected_version)
            if row.retired_at is None:
                return row
            validated(alias, row.fields())
            new = self._write(replace(row, retired_at=None), actor)
            self._record(new, actor, "restored", None, None)
            self._registry_version += 1
            return new

    async def reorder(
        self, aliases: Sequence[str], expected_registry_version: int, actor: Actor
    ) -> bool:
        async with self._lock:
            if expected_registry_version != self._registry_version:
                raise StaleVersion()
            moved = moves(aliases, await self.list())
            if not moved:
                return False
            for row, position in moved:
                new = self._write(replace(row, position=position), actor)
                self._record(
                    new, actor, "reordered", {"position": row.position}, {"position": position}
                )
            self._registry_version += 1
            return True

    async def seed(self, entries: Sequence[tuple[str, Mapping[str, Any]]]) -> builtins.list[str]:
        async with self._lock:
            new = absent(entries, self._rows)
            for ws in new.values():
                row = self._insert(ws, SEED_ACTOR)
                self._record(row, SEED_ACTOR, "seeded", None, row.fields())
            if new:
                self._registry_version += 1
            return builtins.list(new)

    def _current(self, alias: str, expected_version: int) -> WorkspaceRow:
        row = self._rows.get(alias)
        if row is None:
            raise NotFound(alias)
        if row.version != expected_version:
            raise StaleVersion(alias)
        return row

    def _insert(self, ws: Workspace, actor: Actor) -> WorkspaceRow:
        now = datetime.now(UTC)
        position = max((r.position for r in self._rows.values()), default=0) + 1
        row = WorkspaceRow(
            alias=ws.alias,
            **{**workspace_fields(ws), "groups": ws.groups},
            position=position,
            retired_at=None,
            version=1,
            created_at=now,
            created_by=actor.identity,
            updated_at=now,
            updated_by=actor.identity,
        )
        self._rows[row.alias] = row
        return row

    def _write(self, row: WorkspaceRow, actor: Actor) -> WorkspaceRow:
        """Store ``row`` as the next version of its alias."""

        new = replace(
            row, version=row.version + 1, updated_at=datetime.now(UTC), updated_by=actor.identity
        )
        self._rows[new.alias] = new
        return new

    def _record(
        self,
        row: WorkspaceRow,
        actor: Actor,
        action: Action,
        before: dict | None,
        after: dict | None,
    ) -> None:
        self._history.append(HistoryEntry(row.alias, row.updated_at, actor, action, before, after))


__all__ = ["MemoryWorkspaceStore"]
