"""The checks and change rules every workspace store shares, so the stores agree."""

from __future__ import annotations

from collections.abc import Container, Mapping, Sequence
from typing import Any

from v1.core.databricks_workspaces import Workspace, validate_entry, workspace_fields
from v1.core.workspace_registry.models import (
    EDITABLE_FIELDS,
    BadOrder,
    InvalidWorkspace,
    WorkspaceRow,
)

# The keys a new workspace may carry: the editable fields plus its fixed ``host``.
CREATE_FIELDS = (*EDITABLE_FIELDS, "host")


def only(keys: Any, allowed: Sequence[str]) -> None:
    """Refuse the first key outside ``allowed`` (a non-mapping is left to the validator)."""

    if isinstance(keys, Mapping):
        for key in keys:
            if key not in allowed:
                raise InvalidWorkspace(key)


def validated(alias: Any, fields: Any) -> Workspace:
    """The workspace ``fields`` describe as ``create`` takes them, or ``InvalidWorkspace``
    naming the first bad field (a key outside ``CREATE_FIELDS`` first)."""

    only(fields, CREATE_FIELDS)
    checked = validate_entry(alias, fields)
    if isinstance(checked, str):
        raise InvalidWorkspace(checked)
    return checked


def edit(
    row: WorkspaceRow, changes: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None:
    """The row's validated entry after ``changes``, with the before and after of each
    field that changed; None when nothing would."""

    old = row.fields()
    new = workspace_fields(validated(row.alias, {**old, **changes}))
    changed = [k for k in EDITABLE_FIELDS if old[k] != new[k]]
    if not changed:
        return None
    return new, {k: old[k] for k in changed}, {k: new[k] for k in changed}


def moves(aliases: Sequence[str], live: Sequence[WorkspaceRow]) -> list[tuple[WorkspaceRow, int]]:
    """Each live row ``aliases`` moves, with its new position: the live rows take the
    positions they held, in the new order (retired rows keep theirs). ``BadOrder``
    unless ``aliases`` is exactly the live aliases, each once."""

    if len(aliases) != len(live) or set(aliases) != {r.alias for r in live}:
        raise BadOrder()
    by_alias = {r.alias: r for r in live}
    return [
        (by_alias[alias], position)
        for alias, position in zip(aliases, [r.position for r in live])
        if by_alias[alias].position != position
    ]


def absent(
    entries: Sequence[tuple[str, Mapping[str, Any]]], present: Container[str]
) -> dict[str, Workspace]:
    """The entries ``seed`` inserts, validated, in order: the first for each alias not
    in ``present``."""

    found: dict[str, Workspace] = {}
    for alias, fields in entries:
        if alias not in present and alias not in found:
            found[alias] = validated(alias, fields)
    return found


__all__ = ["CREATE_FIELDS", "absent", "edit", "moves", "only", "validated"]
