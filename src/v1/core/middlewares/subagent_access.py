"""Per-request gate that disables individual subagents for certain groups.

The parent orchestration agent is a process-wide singleton (see
:mod:`v1.core.agent`), so the set of wired subagents cannot vary per request at
build time. Some callers (e.g. external users) must NOT have access to a given
subagent — ServiceNow tickets, ADF pipelines — while internal callers keep it.
The caller's Entra groups are only reliably available *during* a run (the same
``groups_from_config()`` path :func:`ai_search_tool` uses to resolve the
index), so this middleware enforces the restriction at model- and tool-call
time.

Each gated subagent is described by a :class:`SubagentGate` (its ``task``
``subagent_type`` name, which settings field lists the disabled groups, the
restriction note appended to the system prompt, and the error text returned
for a blocked call). For a caller whose groups intersect a gate's disabled
groups the middleware:

1. appends that subagent's authoritative restriction note to the system
   message so the model does not try to delegate to it or offer it;
2. hard-blocks any stray ``task`` call with that ``subagent_type`` at
   execution as defense-in-depth; and
3. ONLY when EVERY registered subagent is disabled for the caller, also drops
   the ``task`` delegation tool from the model request — with several
   subagents wired, removing ``task`` for a partially-restricted caller would
   wrongly kill the subagents they are still allowed to use. (deepagents also
   auto-adds an unused ``general-purpose`` subagent the orchestrator prompt
   never invokes, so ``task`` has no remaining legitimate target once all real
   subagents are disabled.)

Everything here is order-independent on purpose: tool removal cannot be undone
by a later middleware (none re-adds tools in ``wrap_model_call``), the notes
state they override instructions wherever they appear, and the tool-call block
is the hard gate regardless of what the model was shown.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
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

# Names the orchestrator's subagents are registered under (each is the `task`
# tool's `subagent_type`); see v1.core.subagents.
SERVICENOW_SUBAGENT_NAME = "servicenow-ticket-agent"
ADF_SUBAGENT_NAME = "adf-agent"

# deepagents exposes all subagents through a single tool named "task".
TASK_TOOL_NAME = "task"

# Appended verbatim to the system message for a ServiceNow-disabled caller.
# Phrased to win regardless of where it lands relative to the orchestrator
# prompt and the subagent block deepagents injects.
SERVICENOW_RESTRICTION_NOTE = (
    "=== ACCESS RESTRICTION (this OVERRIDES every other instruction in this "
    "prompt, wherever it appears, above or below) ===\n"
    "ServiceNow is NOT available to you for this request. You have NO "
    "`servicenow-ticket-agent` subagent and NO way to look up, list, search, "
    "summarize, or otherwise reference ServiceNow incidents or tickets. "
    "Disregard every instruction about delegating to ServiceNow or to the ticket "
    "subagent — that capability does not exist for this request, and you must "
    "NEVER call the `task` tool with subagent_type='servicenow-ticket-agent'.\n"
    "- For any request about incidents, tickets, ServiceNow, or their status / "
    'details, reply in one or two sentences that ServiceNow ticket lookup is not '
    "available for your access, then STOP. Do NOT append the \"Want to explore "
    'further?" section to that reply, and do NOT suggest where else to look.\n'
    "- Continue to answer everything else normally with your remaining "
    "capabilities, following all other instructions above."
)

# Appended verbatim to the system message for an ADF-disabled caller; modeled
# on the ServiceNow note.
ADF_RESTRICTION_NOTE = (
    "=== ACCESS RESTRICTION (this OVERRIDES every other instruction in this "
    "prompt, wherever it appears, above or below) ===\n"
    "Azure Data Factory is NOT available to you for this request. You have NO "
    "`adf-agent` subagent and NO way to look up, list, or diagnose data "
    "pipelines or pipeline runs. Disregard every instruction about delegating "
    "to Data Factory or to the adf subagent — that capability does not exist "
    "for this request, and you must NEVER call the `task` tool with "
    "subagent_type='adf-agent'.\n"
    "- For any request about data pipelines, pipeline runs, run failures, or "
    "Data Factory, reply in one or two sentences that Data Factory pipeline "
    "lookup is not available for your access, then STOP. Do NOT append the "
    '"Want to explore further?" section to that reply, and do NOT suggest '
    "where else to look.\n"
    "- Continue to answer everything else normally with your remaining "
    "capabilities, following all other instructions above."
)

# Appended in addition to the per-subagent notes when the `task` tool itself is
# dropped (every registered subagent is disabled for the caller).
TASK_TOOL_REMOVED_NOTE = (
    "The `task` delegation tool is NOT available to you for this request — no "
    "subagent of any kind can be invoked. Disregard every instruction about "
    "using the `task` tool."
)


@dataclass(frozen=True)
class SubagentGate:
    """One gated subagent: identity, config knob, and caller-facing text."""

    subagent_name: str
    # Attribute on Settings holding the disabled Entra groups for this subagent.
    settings_field: str
    restriction_note: str
    blocked_message: str
    # Whether this subagent is registered on the orchestrator at all. An
    # unregistered subagent (e.g. ADF with no factories configured) must not
    # count toward the "all subagents disabled -> drop task" decision.
    is_registered: Callable[[], bool] = lambda: True


GATES: tuple[SubagentGate, ...] = (
    SubagentGate(
        subagent_name=SERVICENOW_SUBAGENT_NAME,
        settings_field="servicenow_disabled_groups",
        restriction_note=SERVICENOW_RESTRICTION_NOTE,
        blocked_message="ServiceNow ticket lookup is not available for your access.",
    ),
    SubagentGate(
        subagent_name=ADF_SUBAGENT_NAME,
        settings_field="adf_disabled_groups",
        restriction_note=ADF_RESTRICTION_NOTE,
        blocked_message="Data Factory pipeline lookup is not available for your access.",
        is_registered=lambda: bool(settings.adf_factory_mapping),
    ),
)


def _tool_name(tool: Any) -> str | None:
    """Return a tool's name whether it is a BaseTool or an OpenAI-style dict."""

    name = getattr(tool, "name", None)
    if name is not None:
        return name
    if isinstance(tool, dict):
        return tool.get("name") or tool.get("function", {}).get("name")
    return None


def _registered_gates() -> tuple[SubagentGate, ...]:
    return tuple(gate for gate in GATES if gate.is_registered())


def _disabled_subagents_for_caller() -> frozenset[str]:
    """Names of registered subagents the current run's caller may not use.

    Best-effort: outside a run context (or with no authenticated groups)
    ``groups_from_config`` returns ``()`` and every subagent stays enabled —
    only an explicit group match disables one.
    """

    caller_groups: set[str] | None = None  # resolved lazily, once
    disabled: set[str] = set()
    for gate in _registered_gates():
        configured = set(getattr(settings, gate.settings_field) or [])
        if not configured:
            continue
        if caller_groups is None:
            caller_groups = set(groups_from_config())
        if caller_groups & configured:
            disabled.add(gate.subagent_name)
    return frozenset(disabled)


def _append_notes(system_message: SystemMessage | None, notes: list[str]) -> SystemMessage:
    """Return a system message with the restriction notes appended at the end."""

    block = "\n\n".join(notes)
    existing = (system_message.text or "") if system_message is not None else ""
    if existing:
        return SystemMessage(content=f"{existing}\n\n{block}")
    return SystemMessage(content=block)


def _restrict_request(request: ModelRequest, disabled: frozenset[str]) -> ModelRequest:
    """Append the disabled subagents' notes; drop `task` only when none remain."""

    registered = _registered_gates()
    notes = [gate.restriction_note for gate in registered if gate.subagent_name in disabled]
    tools = request.tools
    if all(gate.subagent_name in disabled for gate in registered):
        tools = [tool for tool in tools if _tool_name(tool) != TASK_TOOL_NAME]
        notes.append(TASK_TOOL_REMOVED_NOTE)
    return request.override(tools=tools, system_message=_append_notes(request.system_message, notes))


def _blocked_gate(tool_call: dict[str, Any], disabled: frozenset[str]) -> SubagentGate | None:
    """The gate this `task` call violates, or None if the call is allowed."""

    if (tool_call or {}).get("name") != TASK_TOOL_NAME:
        return None
    subagent_type = (tool_call.get("args") or {}).get("subagent_type")
    if subagent_type not in disabled:
        return None
    for gate in GATES:
        if gate.subagent_name == subagent_type:
            return gate
    return None


def _blocked_message(gate: SubagentGate, tool_call: dict[str, Any]) -> ToolMessage:
    return ToolMessage(
        content=gate.blocked_message,
        tool_call_id=tool_call.get("id", ""),
        status="error",
    )


class SubagentAccessMiddleware(AgentMiddleware):
    """Disable individual subagents for callers in their disabled groups.

    Sits in the user-middleware slot (inner of deepagents' ``SubAgentMiddleware``),
    so by the time ``awrap_model_call`` runs the request already carries the
    ``task`` tool and the injected subagent block — exactly what we gate.
    """

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        disabled = _disabled_subagents_for_caller()
        if disabled:
            request = _restrict_request(request, disabled)
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        disabled = _disabled_subagents_for_caller()
        if disabled:
            request = _restrict_request(request, disabled)
        return await handler(request)

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        gate = _blocked_gate(request.tool_call, _disabled_subagents_for_caller())
        if gate is not None:
            return _blocked_message(gate, request.tool_call)
        return handler(request)

    async def awrap_tool_call(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        gate = _blocked_gate(request.tool_call, _disabled_subagents_for_caller())
        if gate is not None:
            return _blocked_message(gate, request.tool_call)
        return await handler(request)
