"""Attach the Databricks subagent's query results and table choice to its ``task`` result.

The subagent's tools send status lines and record UI results on a per-call channel
(``v1.core.tools.databricks.channel``) instead of returning them in their own messages, which
stay inside the subagent. For each ``task`` call to the Databricks subagent this middleware
opens that channel with the orchestrator's stream writer, whose events reach the client, runs
the call, and then sets what was recorded as the ``task`` ToolMessage's artifact (never sent to
the model):

    {"databricks": {"results": [...], "choices": {...}}}   # "choices" only when one was offered

When SubagentSourcesMiddleware already gave the message its knowledge-base documents (a list),
they move under ``"documents"``; a dict artifact is merged. A call that recorded nothing,
including one AccessControlMiddleware refused, passes through untouched.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from v1.core.databricks_workspaces import DATABRICKS_SUBAGENT_NAME
from v1.core.middlewares.access_control import TASK_TOOL_NAME
from v1.core.tools.databricks.channel import TaskChannel, open_channel


def merge_artifact(existing: Any, databricks: dict) -> dict:
    """``existing`` with the Databricks outputs added under ``"databricks"``."""

    if isinstance(existing, list):
        return {"documents": existing, "databricks": databricks}
    if isinstance(existing, dict):
        return {**existing, "databricks": databricks}
    return {"databricks": databricks}


def _with_outputs(request: Any, result: Any, channel: TaskChannel) -> Any:
    """``result`` with the channel's outputs on its ``task`` ToolMessage, if any were recorded."""

    if not channel.results and channel.choices is None:
        return result
    databricks: dict = {"results": list(channel.results)}
    if channel.choices is not None:
        databricks["choices"] = channel.choices
    call_id = request.tool_call.get("id")

    def attach(message: Any) -> Any:
        if isinstance(message, ToolMessage) and message.tool_call_id == call_id:
            return message.model_copy(
                update={"artifact": merge_artifact(message.artifact, databricks)}
            )
        return message

    if isinstance(result, ToolMessage):
        return attach(result)
    update = result.update if isinstance(result, Command) else None
    if isinstance(update, dict) and "messages" in update:
        messages = [attach(message) for message in update["messages"]]
        return dataclasses.replace(result, update={**update, "messages": messages})
    return result


class DatabricksOutputsMiddleware(AgentMiddleware):
    """Give each Databricks ``task`` result the query results and table choice its tools recorded.

    Must sit on the orchestrator, whose stream writer reaches the client, and outside
    SubagentSourcesMiddleware so it sees the sources artifact. Async only: the agent runs
    tools asynchronously, so the sync path passes through.
    """

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return handler(request)

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        call = request.tool_call
        args = call.get("args")
        if call.get("name") != TASK_TOOL_NAME or not (
            isinstance(args, dict) and args.get("subagent_type") == DATABRICKS_SUBAGENT_NAME
        ):
            return await handler(request)
        with open_channel(getattr(request.runtime, "stream_writer", None)) as channel:
            result = await handler(request)
        return _with_outputs(request, result, channel)
