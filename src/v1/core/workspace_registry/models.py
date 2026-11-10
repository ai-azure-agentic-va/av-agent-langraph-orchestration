"""The workspace registry's rows, history entries, actors and errors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from v1.core.databricks_workspaces import workspace_fields


@dataclass(frozen=True)
class Actor:
    """Who made a change: an Entra object id, with a display name and email if known."""

    identity: str
    name: str | None = None
    email: str | None = None


SEED_ACTOR = Actor("seed-file", "seed file")

# The fields an admin may change; ``alias`` and ``host`` never change after create.
EDITABLE_FIELDS = ("label", "sql_warehouse_id", "data_allowlist", "jobs_enabled", "groups")

Action = Literal["created", "edited", "retired", "restored", "reordered", "seeded"]


@dataclass(frozen=True)
class WorkspaceRow:
    alias: str
    label: str
    host: str
    sql_warehouse_id: str
    data_allowlist: str | None
    jobs_enabled: bool
    groups: tuple[str, ...]
    position: int
    retired_at: datetime | None
    version: int
    created_at: datetime
    created_by: str
    updated_at: datetime
    updated_by: str

    def fields(self) -> dict[str, Any]:
        """The row's workspace entry, as ``validate_entry`` takes it."""

        return workspace_fields(self)


@dataclass(frozen=True)
class HistoryEntry:
    alias: str
    at: datetime
    actor: Actor
    action: Action
    before: dict | None
    after: dict | None


class RegistryError(Exception):
    """A registry write was refused; ``code`` says why."""

    code: str = "invalid"


class AliasTaken(RegistryError):
    """The alias exists already (retired aliases included)."""

    code = "alias_taken"


class NotFound(RegistryError):
    """No workspace has this alias."""

    code = "not_found"


class StaleVersion(RegistryError):
    """The row or registry changed since the caller read it."""

    code = "stale"


class InvalidWorkspace(RegistryError):
    """A field is missing, malformed or not editable; ``field`` names it."""

    code = "invalid"

    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field


class BadOrder(InvalidWorkspace):
    """A reorder did not list exactly the live aliases, each once."""

    def __init__(self) -> None:
        super().__init__("order")


__all__ = [
    "EDITABLE_FIELDS",
    "SEED_ACTOR",
    "Action",
    "Actor",
    "AliasTaken",
    "BadOrder",
    "HistoryEntry",
    "InvalidWorkspace",
    "NotFound",
    "RegistryError",
    "StaleVersion",
    "WorkspaceRow",
]
