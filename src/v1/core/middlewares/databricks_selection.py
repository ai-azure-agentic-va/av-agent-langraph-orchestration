"""Tell the orchestrator and the databricks-agent which workspace and tables a chat uses.

On every model call this middleware appends, for the run's resolved workspace
(:func:`v1.core.tools.databricks.run_workspace.run_workspace`), the workspace's label, then
:data:`JOBS_OFF_NOTE` when job lookup is off there, then :data:`WORKSPACE_CHANGED_NOTE` when
the workspace differs from the previous turn's (``state["last_databricks_workspace"]``), then
either :data:`TABLES_DROPPED_NOTE` (the selection belongs to another workspace; see
:mod:`v1.core.tools.databricks.sql.selection`) or a note naming the tables the web client
sent as ``configurable["databricks_tables"]``.

The notes are hints only; the tools enforce the workspace and the selection themselves.
Nothing is added when the chat's workspace is unavailable, and with ``require_access`` (the
orchestrator's instance) nothing is added when the caller can't use the databricks-agent.
Appended at the end of the system message, like ``UserContextMiddleware``, so the static
prompt stays a cacheable prefix.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import HumanMessage, SystemMessage

from v1.core.config import databricks_registered
from v1.core.databricks_workspaces import (
    DATABRICKS_SUBAGENT_NAME,
    WorkspaceUnavailable,
    workspaces,
)
from v1.core.middlewares import access_control
from v1.core.tools.databricks.run_workspace import run_workspace
from v1.core.tools.databricks.sql.scope import format_name
from v1.core.tools.databricks.sql.selection import run_scope, selection_dropped

# ``run_workspace`` and ``workspaces`` are looked up on this module at call time so tests can
# replace them.

# The graph-state key (on ``ChatAgentState``) holding the alias the previous turn used.
LAST_WORKSPACE_KEY = "last_databricks_workspace"

WORKSPACE_NOTE = "Databricks workspace for this chat: {label}."
JOBS_OFF_NOTE = "Job lookup isn't enabled in this workspace; don't offer it."
WORKSPACE_CHANGED_NOTE = (
    "The user switched this chat's Databricks workspace from {old} to {new}; "
    "earlier results came from {old}."
)
TABLES_DROPPED_NOTE = "No tables are selected for this workspace."

SUBAGENT_SELECTION_NOTE = (
    "The user limited this chat to these tables: {tables}. Use them directly: describe them "
    "and query them; do not call search_tables or choose_tables."
)
# Used for the subagent when some selected names were not echoed: search_tables only returns
# the selected tables (the tools are narrowed), so it can look the rest up there.
SUBAGENT_SELECTION_NOTE_HIDDEN = (
    "The user limited this chat to these tables: {tables}. Call search_tables to find the "
    "names not listed here (it only returns the selected tables), then describe and query "
    "them; do not call choose_tables."
)
ORCHESTRATOR_SELECTION_NOTE = (
    "This conversation is limited to these Databricks tables: {tables}. Send data questions, "
    "including short follow-ups, to the databricks-agent."
)

# A name part safe to echo into the system prompt. A backticked part can hold any printable
# text the client sent, so any other name is counted, never echoed.
_ECHOABLE = re.compile(r"[A-Za-z0-9_-]{1,128}")


def databricks_notes(
    note: str,
    *,
    require_access: bool = False,
    hidden_note: str | None = None,
    state: Mapping[str, Any] | None = None,
) -> str | None:
    """This run's workspace and table notes, one per line, or None when there are none.

    ``note`` names the selected tables; when some names can't be echoed and ``hidden_note``
    is given, that note is used instead. ``state`` is read for the previous turn's workspace.
    """

    # ``caller_access`` is read on access_control at call time, so tests can patch it there.
    if require_access and (
        not databricks_registered()
        or DATABRICKS_SUBAGENT_NAME in access_control.caller_access().blocked_subagents
    ):
        return None
    try:
        workspace, legacy_fallback = run_workspace()
    except WorkspaceUnavailable:
        return None
    lines = [WORKSPACE_NOTE.format(label=workspace.label)]
    if not workspace.jobs_enabled:
        lines.append(JOBS_OFF_NOTE)
    last = (state or {}).get(LAST_WORKSPACE_KEY)
    if isinstance(last, str) and last and last != workspace.alias:
        # The saved alias is client-writable thread state: only a configured label is echoed.
        old = next((w.label for w in workspaces() if w.alias == last), "another workspace")
        lines.append(WORKSPACE_CHANGED_NOTE.format(old=old, new=workspace.label))
    if selection_dropped(workspace, legacy_fallback):
        lines.append(TABLES_DROPPED_NOTE)
        return "\n".join(lines)
    try:
        selected, scope = run_scope(workspace, legacy_fallback)
    except ValueError:  # SelectionInvalid: the tools reject it
        selected = None
    if selected:
        plain = [
            format_name(*parts)
            for parts in scope.entries
            if all(_ECHOABLE.fullmatch(part) for part in parts)
        ]
        hidden = len(scope.entries) - len(plain)
        tables = ", ".join(plain)
        if hidden:
            noun = "selected table" + ("s" if hidden != 1 else "")
            tables = f"{tables} and {hidden} more {noun}" if plain else f"{hidden} {noun}"
        lines.append((hidden_note if hidden and hidden_note else note).format(tables=tables))
    return "\n".join(lines)


def _turn_used_databricks(state: Mapping[str, Any]) -> bool:
    """Whether the latest turn (messages after the last human one) delegated to databricks-agent."""

    for message in reversed(state.get("messages") or ()):
        if isinstance(message, HumanMessage):
            return False
        for call in getattr(message, "tool_calls", None) or ():
            args = call.get("args")
            if call.get("name") == access_control.TASK_TOOL_NAME and (
                isinstance(args, dict)
                and args.get("subagent_type") == DATABRICKS_SUBAGENT_NAME
            ):
                return True
    return False


class DatabricksSelectionMiddleware(AgentMiddleware):
    """Append the run's Databricks workspace and selected tables to the system prompt.

    ``note`` is ``ORCHESTRATOR_SELECTION_NOTE`` (with ``require_access=True`` and
    ``remember_workspace=True``) for the orchestrator, or ``SUBAGENT_SELECTION_NOTE`` (with
    ``hidden_note=SUBAGENT_SELECTION_NOTE_HIDDEN``) for the databricks-agent's spec.

    With ``remember_workspace``, ``after_agent`` writes the run's workspace alias to
    ``last_databricks_workspace``. Only the orchestrator does: deepagents merges the
    subagent's state back when ``task`` returns, so a subagent write would land mid-turn and
    hide the change from the orchestrator's later model calls. The subagent still reads the
    inherited (previous turn's) value, so it gets the same change note.
    """

    def __init__(
        self,
        note: str,
        *,
        require_access: bool = False,
        hidden_note: str | None = None,
        remember_workspace: bool = False,
    ) -> None:
        self.note = note
        self.require_access = require_access
        self.hidden_note = hidden_note
        self.remember_workspace = remember_workspace

    def _with_notes(self, request: ModelRequest) -> ModelRequest:
        text = databricks_notes(
            self.note,
            require_access=self.require_access,
            hidden_note=self.hidden_note,
            state=getattr(request, "state", None),
        )
        if text is None:
            return request
        existing = request.system_message.text if request.system_message is not None else ""
        content = f"{existing}\n\n{text}" if existing else text
        return request.override(system_message=SystemMessage(content=content))

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        return handler(self._with_notes(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return await handler(self._with_notes(request))

    def after_agent(self, state: Mapping[str, Any], runtime: Any) -> dict[str, Any] | None:
        """Record this run's workspace when this turn used the databricks-agent.

        Nothing when it didn't (the change note says earlier results came from the old
        workspace), when unchanged, or when no workspace is usable.
        """

        if not self.remember_workspace or not _turn_used_databricks(state):
            return None
        try:
            workspace, _ = run_workspace()
        except WorkspaceUnavailable:
            return None
        if state.get(LAST_WORKSPACE_KEY) == workspace.alias:
            return None
        return {LAST_WORKSPACE_KEY: workspace.alias}

    async def aafter_agent(
        self, state: Mapping[str, Any], runtime: Any
    ) -> dict[str, Any] | None:
        return self.after_agent(state, runtime)
