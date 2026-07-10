"""Per-request gate that disables the ServiceNow subagent for certain groups.

The parent orchestration agent is a process-wide singleton (see
:mod:`v1.core.agent`), so the set of wired subagents cannot vary per request at
build time. Some callers (e.g. external users) must NOT have access to
the ServiceNow ticket subagent, while internal callers keep it. The caller's
Entra groups are only reliably available *during* a run (the same
``groups_from_config()`` path :func:`ai_search_tool` uses to resolve the index),
so this middleware enforces the restriction at model- and tool-call time.

For a caller whose groups intersect ``settings.servicenow_disabled_groups`` it:

1. drops the ``task`` delegation tool from the model request — the only real
   delegate target is ``servicenow-ticket-agent`` (deepagents also auto-adds an
   unused ``general-purpose`` subagent the orchestrator prompt never invokes),
   so removing the tool removes ServiceNow access without changing tuned
   behaviour for the knowledge-base path;
2. appends an authoritative restriction note to the system message so the model
   does not try to delegate or offer ServiceNow; and
3. hard-blocks any stray ``task`` -> ``servicenow-ticket-agent`` call at
   execution as defense-in-depth (the tool is normally already gone).

Everything here is order-independent on purpose: tool removal cannot be undone
by a later middleware (none re-adds tools in ``wrap_model_call``), the note
states it overrides instructions wherever they appear, and the tool-call block
is the hard gate regardless of what the model was shown.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import SystemMessage, ToolMessage

from v1.core.config import get_settings
from v1.utils.group_routing import groups_from_config

settings = get_settings()

# Name the orchestrator's ServiceNow subagent is registered under (the
# `task` tool's `subagent_type`); see v1.core.subagents.servicenow.subagent.
SERVICENOW_SUBAGENT_NAME = "servicenow-ticket-agent"

# deepagents exposes all subagents through a single tool named "task".
TASK_TOOL_NAME = "task"

# Appended verbatim to the system message for a disabled caller. Phrased to win
# regardless of where it lands relative to the orchestrator prompt and the
# subagent block deepagents injects.
SERVICENOW_RESTRICTION_NOTE = (
    "=== ACCESS RESTRICTION (this OVERRIDES every other instruction in this "
    "prompt, wherever it appears, above or below) ===\n"
    "ServiceNow is NOT available to you for this request. You have NO `task` "
    "tool, NO `servicenow-ticket-agent` subagent, and NO way to look up, list, "
    "search, summarize, or otherwise reference ServiceNow incidents or tickets. "
    "Disregard every instruction about delegating to ServiceNow or to the ticket "
    "subagent and about using the `task` tool — those capabilities do not exist "
    "for this request.\n"
    "- For any request about incidents, tickets, ServiceNow, or their status / "
    'details, reply in one or two sentences that ServiceNow ticket lookup is not '
    "available for your access, then STOP. Do NOT append the \"Want to explore "
    'further?" section to that reply, and do NOT suggest where else to look.\n'
    "- Continue to answer knowledge-base questions normally using `ai_search_tool`, "
    "following all other instructions above."
)


def _tool_name(tool: Any) -> str | None:
    """Return a tool's name whether it is a BaseTool or an OpenAI-style dict."""

    name = getattr(tool, "name", None)
    if name is not None:
        return name
    if isinstance(tool, dict):
        return tool.get("name") or tool.get("function", {}).get("name")
    return None


def _servicenow_disabled_for_caller() -> bool:
    """Whether the current run's caller is in a ServiceNow-disabled group.

    Best-effort: outside a run context (or with no authenticated groups)
    ``groups_from_config`` returns ``()`` and ServiceNow stays enabled — only an
    explicit group match disables it.
    """

    disabled = settings.servicenow_disabled_groups
    if not disabled:
        return False
    groups = groups_from_config()
    if not groups:
        return False
    return bool(set(groups) & set(disabled))


def _append_restriction(system_message: SystemMessage | None) -> SystemMessage:
    """Return a system message with the restriction note appended at the end."""

    if system_message is None:
        return SystemMessage(content=SERVICENOW_RESTRICTION_NOTE)
    existing = system_message.text or ""
    if existing:
        return SystemMessage(content=f"{existing}\n\n{SERVICENOW_RESTRICTION_NOTE}")
    return SystemMessage(content=SERVICENOW_RESTRICTION_NOTE)


def _restrict_request(request: ModelRequest) -> ModelRequest:
    """Drop the `task` tool and append the restriction note to the prompt."""

    tools = [tool for tool in request.tools if _tool_name(tool) != TASK_TOOL_NAME]
    system_message = _append_restriction(request.system_message)
    return request.override(tools=tools, system_message=system_message)


def _is_blocked_servicenow_task(tool_call: dict[str, Any]) -> bool:
    """Whether this tool call is a `task` delegation to the ServiceNow subagent."""

    if (tool_call or {}).get("name") != TASK_TOOL_NAME:
        return False
    args = tool_call.get("args") or {}
    return args.get("subagent_type") == SERVICENOW_SUBAGENT_NAME


def _servicenow_blocked_message(tool_call: dict[str, Any]) -> ToolMessage:
    return ToolMessage(
        content="ServiceNow ticket lookup is not available for your access.",
        tool_call_id=tool_call.get("id", ""),
        status="error",
    )


class ServiceNowAccessMiddleware(AgentMiddleware):
    """Disable the ServiceNow subagent for callers in disabled groups.

    Sits in the user-middleware slot (inner of deepagents' ``SubAgentMiddleware``),
    so by the time ``awrap_model_call`` runs the request already carries the
    ``task`` tool and the injected subagent block — exactly what we strip.
    """

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        if _servicenow_disabled_for_caller():
            request = _restrict_request(request)
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        if _servicenow_disabled_for_caller():
            request = _restrict_request(request)
        return await handler(request)

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        if _is_blocked_servicenow_task(request.tool_call) and _servicenow_disabled_for_caller():
            return _servicenow_blocked_message(request.tool_call)
        return handler(request)

    async def awrap_tool_call(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        if _is_blocked_servicenow_task(request.tool_call) and _servicenow_disabled_for_caller():
            return _servicenow_blocked_message(request.tool_call)
        return await handler(request)
