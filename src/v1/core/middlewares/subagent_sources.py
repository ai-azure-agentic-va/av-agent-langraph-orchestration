"""Surface a subagent's knowledge-base sources on the orchestrator's ``task`` result.

A subagent that calls ``ai_search_tool`` numbers its documents in the run's
shared registry (one per ``run_id``, see ``ai_search._turn_documents``), so its
``[n]`` markers continue the orchestrator's numbering and survive
CitationGuardMiddleware. None of the search's outputs reach the UI, though:

- the live ``documents`` custom event is dropped: a subagent's stream writer only
  reaches the client when the run streams subgraphs, and the UI's normal submit
  does not;
- the search's ToolMessage, whose artifact the UI rebuilds sources from after a
  reload, stays in the subagent's own messages. deepagents returns only the final
  text, as a ``task`` ToolMessage without an artifact;
- ``all_sources``, next turn's numbering seed, only gets the documents when the
  subagent runs its own SourceAccumulatorMiddleware, as adf-agent does.

So the answer showed a bare ``[n]`` and no "Referenced Sources". For a ``task``
call during which the registry gained documents, this middleware gives the result
what ``ai_search_tool`` gives its own: the same top-level ``documents`` event, the
documents as the ``task`` ToolMessage's artifact (never sent to the model), and an
``all_sources`` update. Like the tool's output, the list is the cumulative set
(earlier turns plus this one). Every other call, and a delegation that numbered
nothing new, passes through untouched.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from v1.core.middlewares.subagent_access import TASK_TOOL_NAME
from v1.core.tools.ai_search.ai_search import (
    _current_run_id,
    _emit,
    assigned_citation_indices,
    turn_documents_snapshot,
)


def _indices(documents: Any) -> set[int]:
    return {
        doc["index"]
        for doc in documents or []
        if isinstance(doc, dict) and isinstance(doc.get("index"), int)
    }


def _known_indices(request: Any) -> tuple[str | None, set[int]]:
    """The run id and the ``[n]`` numbers known before the subagent runs: those
    assigned this turn plus the conversation's persisted ones. Read in the
    orchestrator's run context, where the run id resolves."""

    run_id = _current_run_id()
    state = request.state if isinstance(request.state, dict) else {}
    return run_id, assigned_citation_indices(run_id) | _indices(state.get("all_sources"))


def _with_sources(request: Any, result: Any, run_id: str | None, known: set[int]) -> Any:
    """``result`` carrying the run's documents if the subagent numbered new ones."""

    documents = turn_documents_snapshot(run_id)
    if not (
        isinstance(result, Command)
        and isinstance(result.update, dict)
        and _indices(documents) - known
    ):
        return result
    call_id = request.tool_call.get("id")
    update = dict(result.update)
    if "messages" in update:
        update["messages"] = [
            message.model_copy(update={"artifact": documents})
            if isinstance(message, ToolMessage) and message.tool_call_id == call_id
            else message
            for message in update["messages"]
        ]
    update["all_sources"] = [*(update.get("all_sources") or []), *documents]
    _emit(
        getattr(request.runtime, "stream_writer", None),
        {"type": "documents", "documents": documents},
    )
    return dataclasses.replace(result, update=update)


class SubagentSourcesMiddleware(AgentMiddleware):
    """Give each ``task`` result the sources its subagent's searches numbered.

    Must sit on the orchestrator, whose stream writer reaches the client.
    deepagents' FilesystemMiddleware wraps outside it and keeps the artifact if it
    evicts a large result; a call SubagentAccessMiddleware blocks comes back as a
    ToolMessage and passes through.
    """

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        if request.tool_call.get("name") != TASK_TOOL_NAME:
            return handler(request)
        run_id, known = _known_indices(request)
        return _with_sources(request, handler(request), run_id, known)

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        if request.tool_call.get("name") != TASK_TOOL_NAME:
            return await handler(request)
        run_id, known = _known_indices(request)
        return _with_sources(request, await handler(request), run_id, known)
