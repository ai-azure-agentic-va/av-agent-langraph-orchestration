"""The Databricks workspace this run uses.

The web client sends the chat's workspace alias as ``configurable["databricks_workspace"]``.
:func:`run_workspace` resolves it for the run's caller with
:func:`v1.core.databricks_workspaces.resolve_workspace`: a visible alias is used, an unknown or
hidden one is refused (:class:`WorkspaceUnavailable`), and only a missing alias falls back to
the legacy or default workspace. Never another workspace.

Tools catch the refusal and return :func:`workspace_unavailable_payload`.
"""

import json

from langgraph.config import get_config

from v1.core.databricks_workspaces import (
    WORKSPACE_UNAVAILABLE_MESSAGE,
    Workspace,
    WorkspaceUnavailable,
    resolve_workspace,
)
from v1.utils.group_routing import groups_from_config

# ``get_config`` and ``groups_from_config`` are looked up on this module at call time so
# tests can replace them.

WORKSPACE_KEY = "databricks_workspace"


def _requested_alias() -> str | None:
    """The run's ``databricks_workspace``; None outside a run or when it was not sent.

    Raises :class:`WorkspaceUnavailable` when it was sent but isn't a string: a malformed
    alias is refused, never read as missing (which would fall back to another workspace).
    """

    try:
        config = get_config()
    except RuntimeError:  # not inside a graph run
        return None
    configurable = (config or {}).get("configurable") or {}
    alias = configurable.get(WORKSPACE_KEY)
    if alias is None or isinstance(alias, str):
        return alias
    raise WorkspaceUnavailable()


def run_workspace() -> tuple[Workspace, bool]:
    """The run's workspace and whether the caller's default stood in for a missing alias.

    Raises :class:`WorkspaceUnavailable` when the alias is unknown, hidden from the caller or
    malformed, or the caller sees no workspace.
    """

    return resolve_workspace(groups_from_config(), _requested_alias())


def workspace_unavailable_payload() -> str:
    """Tool result when :func:`run_workspace` raises :class:`WorkspaceUnavailable`."""

    return json.dumps({"status": "error", "message": WORKSPACE_UNAVAILABLE_MESSAGE})
