"""Which Databricks workspaces this deployment knows, and which one a chat uses.

A workspace is an alias with ``{label, host, sql_warehouse_id, data_allowlist,
jobs_enabled, groups}``. Every write is checked by :func:`validate_entry`, which names
the first bad field; a host must be an ``https://`` URL on ``*.azuredatabricks.net``.
:func:`workspaces` is the registry, in order (this process's snapshot of it, see
``v1.core.workspace_registry.snapshot``).

A chat's alias resolves to a workspace the caller can see, or is refused with
:data:`WORKSPACE_UNAVAILABLE_MESSAGE`. Only a *missing* alias (or ``default``)
falls back: to ``DATABRICKS_LEGACY_WORKSPACE``, else the caller's default
workspace. A blank alias that was sent is refused. Never another workspace.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from v1.core.config import get_settings

if TYPE_CHECKING:
    from v1.core.tools.databricks.sql.scope import DataScope

logger = logging.getLogger(__name__)

LEGACY_ALIAS = "default"

# The databricks-agent's name: its ``task`` ``subagent_type`` and its name in the
# access-control file.
DATABRICKS_SUBAGENT_NAME = "databricks-agent"
# A workspace alias (used with ``fullmatch``); ``default`` is refused separately.
ALIAS_PATTERN = re.compile(r"[a-z0-9-]{1,64}")
WORKSPACE_UNAVAILABLE_MESSAGE = (
    "This chat's Databricks workspace isn't available to you. Pick another in the workspace menu."
)
_EVERYONE = "*"
# A SQL warehouse ID (used with ``fullmatch``): it is put into the warehouses API path.
_WAREHOUSE_ID = re.compile(r"[A-Za-z0-9-]{1,64}")
# An Azure Databricks workspace hostname (urlsplit lowercases it).
_AZURE_DATABRICKS_HOST = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+azuredatabricks\.net"
)


class WorkspaceUnavailable(Exception):
    """The requested workspace is unknown or not visible to the caller."""

    def __init__(self, message: str = WORKSPACE_UNAVAILABLE_MESSAGE) -> None:
        super().__init__(message)


@dataclass(frozen=True)
class Workspace:
    alias: str
    label: str
    host: str
    sql_warehouse_id: str
    data_allowlist: str | None  # as written; ``scope`` is what it allows
    scope: DataScope
    jobs_enabled: bool
    groups: tuple[str, ...]

    @property
    def everyone(self) -> bool:
        """Visible to every signed-in user (no groups, or ``"*"``)."""

        return not self.groups or _EVERYONE in self.groups


def _text(value: Any) -> str | None:
    """A stripped non-empty string, or None."""

    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def check_host(value: Any) -> str | None:
    """The workspace URL, stripped, when it is ``https://`` on an Azure Databricks host
    (``*.azuredatabricks.net``, any case, with at most a trailing ``/``); else None.

    No other path, port, user, query or fragment: the host is where the app's token goes.
    """

    text = _text(value)
    if text is None:
        return None
    try:
        url = urlsplit(text)
        port = url.port
    except ValueError:
        return None
    if (
        url.scheme.lower() != "https"
        or port is not None
        or url.username is not None
        or url.password is not None
        or url.path not in ("", "/")
        or url.query
        or url.fragment
        or not _AZURE_DATABRICKS_HOST.fullmatch(url.hostname or "")
    ):
        return None
    return text


def check_warehouse_id(value: Any) -> str | None:
    """``value`` stripped when it is a SQL warehouse ID :func:`validate_entry` accepts
    (letters, digits and ``-``, at most 64), else None. It goes into an API path."""

    text = _text(value)
    return text if text is not None and _WAREHOUSE_ID.fullmatch(text) else None


def _scope(raw: Any) -> DataScope | None:
    """The allowlist's scope; None when set but blank or malformed (never open scope)."""

    # Imported here: importing it loads v1.core.tools, whose tools import this module.
    from v1.core.tools.databricks.sql.scope import DataScope

    if raw is None:
        return DataScope()
    if not isinstance(raw, str) or not any(entry.strip() for entry in raw.split(",")):
        return None
    try:
        return DataScope.parse(raw)
    except ValueError:
        return None


def validate_entry(alias: str, entry: Mapping[str, Any]) -> Workspace | str:
    """The workspace ``entry`` describes under ``alias``, or the first bad field's name
    (``alias``, ``entry``, ``host``, ``sql_warehouse_id``, ``data_allowlist``, ``label``,
    ``jobs_enabled``, ``groups``). Every workspace write goes through here."""

    if not isinstance(alias, str) or not ALIAS_PATTERN.fullmatch(alias) or alias == LEGACY_ALIAS:
        return "alias"
    if not isinstance(entry, Mapping):
        return "entry"
    host = check_host(entry.get("host"))
    if host is None:
        return "host"
    warehouse = check_warehouse_id(entry.get("sql_warehouse_id"))
    if warehouse is None:
        return "sql_warehouse_id"
    allowlist = entry.get("data_allowlist")
    scope = _scope(allowlist)
    if scope is None:
        return "data_allowlist"
    label = entry.get("label")
    if label is None:
        label = alias
    elif _text(label) is None:
        return "label"
    jobs_enabled = entry.get("jobs_enabled")
    if jobs_enabled is None:  # absent or null: the default, as for the other optional fields
        jobs_enabled = True
    if not isinstance(jobs_enabled, bool):
        return "jobs_enabled"
    # Only an absent key, None or an explicit [] means everyone. Anything else
    # malformed refuses the entry, never widens who can see it.
    groups = entry.get("groups")
    if groups is None:
        groups = []
    if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
        return "groups"
    if groups and not any(g.strip() for g in groups):
        return "groups"
    return Workspace(
        alias=alias,
        label=label.strip(),
        host=host,
        sql_warehouse_id=warehouse,
        data_allowlist=allowlist,
        scope=scope,
        jobs_enabled=jobs_enabled,
        groups=tuple(g.strip() for g in groups if g.strip()),
    )


def workspace_fields(ws: Any) -> dict[str, Any]:
    """The entry :func:`validate_entry` turns back into ``ws`` (under ``ws.alias``); also
    a registry row's entry."""

    return {
        "label": ws.label,
        "host": ws.host,
        "sql_warehouse_id": ws.sql_warehouse_id,
        "data_allowlist": ws.data_allowlist,
        "jobs_enabled": ws.jobs_enabled,
        "groups": list(ws.groups),
    }


def workspaces() -> tuple[Workspace, ...]:
    """The registry's live workspaces, in order, as this process last read them."""

    # Imported here: the snapshot module imports this one.
    from v1.core.workspace_registry.snapshot import get_snapshot

    return get_snapshot().current()


def log_config_problem(problem: str) -> None:
    """Log at startup why the databricks-agent is off."""

    logger.error("databricks-agent disabled: %s is invalid or missing", problem)


def databricks_group_keys() -> frozenset[str]:
    """The groups Databricks decides on, which auth exports beside the access-control file's.

    Every workspace's ``groups`` (except ``"*"``, which means everyone), read from the live
    snapshot so a new workspace's groups are exported at the next sign-in without a
    restart, plus the ``DATABRICKS_ADMIN_GROUPS`` object IDs. Empty while the
    databricks-agent is off.
    """

    # Imported here: config must not import this module at load time.
    from v1.core.config import admin_object_ids, databricks_registered

    if not databricks_registered():
        return frozenset()
    return frozenset(
        (
            *(g for ws in workspaces() for g in ws.groups if g != _EVERYONE),
            *admin_object_ids(get_settings().databricks_admin_groups),
        )
    )


def visible_workspaces(groups: Sequence[str]) -> tuple[Workspace, ...]:
    """The workspaces the caller may use, in config order.

    Same rule as ``group_routing.resolve_for_groups``: exact match on a group id or
    display name (auth exports both forms).
    """

    member = set(groups)
    return tuple(w for w in workspaces() if w.everyone or member.intersection(w.groups))


def legacy_alias() -> str | None:
    """The alias chats with no saved workspace (or ``default``) belong to: the configured
    ``DATABRICKS_LEGACY_WORKSPACE``, if any. (No workspace is aliased ``default``:
    :func:`validate_entry` refuses it.)"""

    return _text(get_settings().databricks_legacy_workspace)


def canonical_alias(raw: object) -> str | None:
    """The workspace alias a chat's saved value names: one rule for the chat's workspace
    (``databricks_workspace``) and the workspace its tables were picked in
    (``databricks_tables_workspace``), so the two always compare alike.

    Missing (None) or ``default`` names :func:`legacy_alias` (None when there is none: the
    caller's default stands in). Any other string names itself, stripped. A blank string or
    a non-string was sent malformed: raises :class:`WorkspaceUnavailable`, never read as
    missing (which would fall back to another workspace).
    """

    if raw is None:
        return legacy_alias()
    alias = _text(raw)
    if alias is None:
        raise WorkspaceUnavailable()
    return legacy_alias() if alias == LEGACY_ALIAS else alias


def resolve_workspace(groups: Sequence[str], alias: object) -> tuple[Workspace, bool]:
    """The chat's workspace and whether the caller's default stood in for a missing alias.

    ``alias`` is read by :func:`canonical_alias`. ``legacy_fallback`` is True only when the
    alias was missing (or ``default``) and there is no legacy alias; callers then drop the
    chat's saved tables. Raises :class:`WorkspaceUnavailable` for an unknown, hidden, blank
    or malformed alias (including a hidden legacy alias) or when the caller sees no
    workspace. Never substitutes.
    """

    visible = visible_workspaces(groups)
    if not visible:
        raise WorkspaceUnavailable()
    wanted = canonical_alias(alias)
    if wanted is None:
        return visible[0], True
    for ws in visible:
        if ws.alias == wanted:
            return ws, False
    raise WorkspaceUnavailable()


__all__ = [
    "ALIAS_PATTERN",
    "LEGACY_ALIAS",
    "WORKSPACE_UNAVAILABLE_MESSAGE",
    "Workspace",
    "WorkspaceUnavailable",
    "canonical_alias",
    "check_host",
    "check_warehouse_id",
    "legacy_alias",
    "log_config_problem",
    "resolve_workspace",
    "validate_entry",
    "visible_workspaces",
    "workspace_fields",
    "workspaces",
]
