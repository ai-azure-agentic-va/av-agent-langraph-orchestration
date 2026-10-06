"""Per-request gate that disables individual subagents for certain groups.

The parent orchestration agent is a process-wide singleton (see
:mod:`v1.core.agent`), so the set of wired subagents cannot vary per request at
build time. Some callers (e.g. external users) must NOT have access to a given
subagent — ADF pipelines — while internal callers keep it. The caller's Entra
groups are only reliably available *during* a run (the same
``groups_from_config()`` path :func:`ai_search_tool` uses to resolve the
index), so this middleware enforces the restriction at model- and tool-call
time.

Each gated subagent is described by a :class:`SubagentGate` (its ``task``
``subagent_type`` name, which settings field lists the disabled groups, the
restriction note appended to the system prompt, and the error text returned
for a blocked call). For a caller whose groups intersect a gate's disabled
groups the middleware:

1. appends that subagent's authoritative restriction note to the system
   message so the model does not try to delegate to it or offer it; and
2. hard-blocks any stray ``task`` call with that ``subagent_type`` at
   execution as defense-in-depth.

The ``task`` delegation tool itself is NEVER removed. ServiceNow is always
registered on the orchestrator (see ``v1.core.subagents.ENABLED_SUBAGENTS``) and
is deliberately NOT gated here, so every caller keeps at least one legitimate
delegation target and stripping ``task`` would wrongly kill the ServiceNow
subagent they are still allowed to use. (If a future change ever gates ServiceNow
or makes it optional, this is the place to reinstate a "no target remains -> drop
``task``" branch.)

INCIDENTS are not scoped per team anywhere else either: both support teams are
meant to read each other's incidents, which is a product decision, not an accident
of the plumbing.

Be precise about why, because the reason is not the obvious one. ServiceNow does
NOT scope our callers: the integration authenticates with ONE service account, so
the instance sees the application and never the signed-in person, and an unscoped
``/incidents`` query returns every team's records under HTTP 200. (An earlier
comment here claimed the assignment / ownership groups on the records restricted
callers by themselves. That premise is false — it is a filterable field, not an
ACL — and it is what justified removing the previous gate. Do not reinstate it as
a justification for anything.) Incidents are open because the teams want them open;
if that ever changes, the check belongs on the tools' shared ``/incidents`` call,
not here, where it would take every ServiceNow capability down at once.

KB articles used to be the exception, gated inside the knowledge tool by an Entra
allowlist. The teams confirmed they want that corpus shared too, so the gate is
gone and ServiceNow is open end to end.

Everything here is order-independent on purpose: the notes state they override
instructions wherever they appear, and the tool-call block is the hard gate
regardless of what the model was shown.
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
ADF_SUBAGENT_NAME = "adf-agent"

# deepagents exposes all subagents through a single tool named "task".
TASK_TOOL_NAME = "task"

# Appended verbatim to the system message for an ADF-disabled caller. Phrased to
# win regardless of where it lands relative to the orchestrator prompt and the
# subagent block deepagents injects.
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


@dataclass(frozen=True)
class SubagentGate:
    """One gated subagent: identity, config knob, and caller-facing text."""

    subagent_name: str
    # Attribute on Settings holding the disabled Entra groups for this subagent.
    settings_field: str
    restriction_note: str
    blocked_message: str
    # Whether this subagent is registered on the orchestrator at all. Gating an
    # unregistered subagent (e.g. ADF with no factories configured) would append
    # a restriction note about a capability the deployment never had.
    is_registered: Callable[[], bool] = lambda: True


GATES: tuple[SubagentGate, ...] = (
    SubagentGate(
        subagent_name=ADF_SUBAGENT_NAME,
        settings_field="adf_disabled_groups",
        restriction_note=ADF_RESTRICTION_NOTE,
        blocked_message="Data Factory pipeline lookup is not available for your access.",
        is_registered=lambda: bool(settings.adf_factory_mapping),
    ),
)


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
    """Append the disabled subagents' restriction notes to the system message.

    The `task` tool stays on the request: ServiceNow is always registered and
    never gated, so delegation always has a legitimate target (see the module
    docstring).
    """

    if not disabled:
        return request

    notes = [
        gate.restriction_note
        for gate in _registered_gates()
        if gate.subagent_name in disabled
    ]
    return request.override(system_message=_append_notes(request.system_message, notes))


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


def _blocked_tool_message(tool_call: dict[str, Any]) -> ToolMessage | None:
    """Refusal for a gated call, or None when the call is allowed through.

    Defense in depth: the restriction note already told the model this subagent
    does not exist for the request, so reaching here means it ignored the note.
    """

    gate = _blocked_gate(tool_call, _disabled_subagents_for_caller())
    return _blocked_message(gate, tool_call) if gate is not None else None


class SubagentAccessMiddleware(AgentMiddleware):
    """Disable individual subagents for callers in their disabled groups.

    Sits in the user-middleware slot (inner of deepagents' ``SubAgentMiddleware``),
    so by the time ``awrap_model_call`` runs the request already carries the
    ``task`` tool and the injected subagent block the restriction note overrides.
    """

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        return handler(_restrict_request(request, _disabled_subagents_for_caller()))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return await handler(_restrict_request(request, _disabled_subagents_for_caller()))

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        blocked = _blocked_tool_message(request.tool_call)
        return blocked if blocked is not None else handler(request)

    async def awrap_tool_call(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        blocked = _blocked_tool_message(request.tool_call)
        return blocked if blocked is not None else await handler(request)
