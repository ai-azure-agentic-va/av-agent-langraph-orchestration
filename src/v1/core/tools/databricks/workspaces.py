"""The ``list_workspaces`` tool: which Databricks workspaces the caller can use here.

It reads only the registry (no Databricks call, no audit line): the caller's visible
workspaces in registry order, which one is the chat's, whether job lookup is on and whether
its data is open or limited to an allowlist. Hosts, warehouses and hidden workspaces are
never shown. The tool can't change the chat's workspace; ``switch_hint`` points the user
to the chat's workspace picker.
"""

import json

from langchain_core.tools import BaseTool, tool

from v1.core.databricks_workspaces import WorkspaceUnavailable, visible_workspaces
from v1.core.tools.databricks.run_workspace import run_workspace, workspace_unavailable_payload
from v1.utils.group_routing import groups_from_config

# ``groups_from_config`` and ``run_workspace`` are looked up on this module at call time so
# tests can replace them.

LIST_WORKSPACES = "list_workspaces"
SWITCH_HINT = "To use another workspace, choose it in the chat's workspace picker."

LIST_DESCRIPTION = (
    "Lists the Databricks workspaces the user can use in this chat, marks the chat's current "
    "one, and says whether each has job lookup and open or limited data. It can't switch the "
    "chat's workspace."
)


def list_workspaces_payload() -> str:
    """The tool's JSON result: the caller's visible workspaces, in registry order.

    ``current`` is the chat's workspace alias, or None when the chat's saved workspace is
    unknown or hidden from the caller. With no visible workspace, the
    :func:`workspace_unavailable_payload` error.
    """

    visible = visible_workspaces(groups_from_config())
    if not visible:
        return workspace_unavailable_payload()
    try:
        current: str | None = run_workspace()[0].alias
    except WorkspaceUnavailable:
        current = None
    return json.dumps(
        {
            "status": "ok",
            "current": current,
            "workspaces": [
                {
                    "label": ws.label,
                    "alias": ws.alias,
                    "current": ws.alias == current,
                    "jobs_enabled": ws.jobs_enabled,
                    "data": "open" if ws.scope.is_open else "limited",
                }
                for ws in visible
            ],
            "switch_hint": SWITCH_HINT,
        }
    )


def build_workspace_tools() -> list[BaseTool]:
    """``[list_workspaces]``; it takes no arguments."""

    @tool(LIST_WORKSPACES, description=LIST_DESCRIPTION)
    def list_workspaces_tool() -> str:
        return list_workspaces_payload()

    return [list_workspaces_tool]


__all__ = [
    "LIST_WORKSPACES",
    "SWITCH_HINT",
    "build_workspace_tools",
    "list_workspaces_payload",
]
