"""Keep knowledge-base citations working from the model's answer to the UI.

``ai_search_tool`` numbers every document it returns once per conversation, a
stable ``[n]``. The registry is shared by the whole run (orchestrator and
subagents, keyed on ``run_id``, see :mod:`v1.core.tools.ai_search.ai_search`)
and seeded from the cumulative ``all_sources`` state channel. The tool sends its
own documents to the UI: a live ``documents`` event, and its ToolMessage's
``artifact``, which the UI rebuilds sources from after a reload.

CitationsMiddleware covers the rest of the path, at three points of a turn:

- **The answer** (``wrap_model_call``). Models occasionally emit a number that
  was never assigned: hallucinated, or copied out of a bracketed cross-reference
  inside a source body. It matches no document, so the UI would show it as a raw,
  un-clickable ``[n]``, persisted that way. After a FINAL answer (an ``AIMessage``
  with no tool calls) every ``[n]`` that is not a valid citation is removed. Valid
  means numbered in this run's registry or accumulated in ``all_sources``, so a
  follow-up that reuses an earlier turn's ``[n]`` without searching keeps it. It
  only no-ops when the conversation has no sources at all.
- **A subagent's searches** (``wrap_tool_call`` on ``task``). A subagent numbers
  its documents in the same registry, so its markers continue the orchestrator's
  numbering and pass the check above, but none of the search's outputs reach the
  UI. Its live event is dropped (a subagent's stream writer only reaches the
  client when the run streams subgraphs, and the UI's normal submit does not), and
  its ToolMessage stays in the subagent's own messages: deepagents returns only
  the final text, as a ``task`` ToolMessage without an artifact. When the registry
  gained documents during the call, the ``task`` result gets what
  ``ai_search_tool`` gives its own: the same top-level ``documents`` event, the
  documents as its artifact (never sent to the model) and an ``all_sources``
  update. Like the tool's output, the list is the cumulative set (earlier turns
  plus this one).
- **The end of the turn** (``after_agent``). This turn's ``ai_search_tool``
  artifacts are copied into ``all_sources``, whose ``merge_sources`` reducer
  unions and de-dupes them into the running list. Writing state, not only the
  live event, is what makes the list durable: it survives a thread reopened from
  history, where the live event never replays, and a process restart.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from v1.core.middlewares.subagent_access import TASK_TOOL_NAME
from v1.core.tools.ai_search.ai_search import (
    _current_run_id,
    _emit,
    assigned_citation_indices,
    turn_documents_snapshot,
)

logger = logging.getLogger(__name__)

# The name ai_search_tool is registered under; its ToolMessage carries the turn's
# cumulative referenced-source set on `.artifact` (see ai_search_tool).
_AI_SEARCH_TOOL_NAME = "ai_search_tool"

# Optional leading whitespace + a bracketed integer. Capturing the whitespace lets
# us drop it ALONG WITH an invalid marker (so "resolved [1] [242]." -> "resolved
# [1].") while preserving it for a valid marker we keep (we return the whole match).
_CITATION_MARKER_RE = re.compile(r"(\s*)\[(\d+)\]")


def _indices(documents: Any) -> set[int]:
    """The ``[n]`` numbers of a document list (``all_sources``, an artifact)."""

    if not isinstance(documents, list):
        return set()
    return {
        doc["index"]
        for doc in documents
        if isinstance(doc, dict) and isinstance(doc.get("index"), int)
    }


def _known_indices(run_id: str | None, state: Any) -> set[int]:
    """Every ``[n]`` that points at a document: those numbered in this run's
    registry plus the conversation's persisted ones (``all_sources``)."""

    sources = state.get("all_sources") if isinstance(state, dict) else None
    return assigned_citation_indices(run_id) | _indices(sources)


# --- The answer ---------------------------------------------------------------------


def _strip_invalid_markers(text: str, valid: set[int]) -> str:
    """Drop ``[n]`` markers (and their leading space) whose ``n`` is not ``valid``."""

    def _repl(match: "re.Match[str]") -> str:
        if int(match.group(2)) in valid:
            return match.group(0)
        return ""

    return _CITATION_MARKER_RE.sub(_repl, text)


def _sanitize_message(message: AIMessage, valid: set[int]) -> AIMessage:
    """Return ``message`` with invalid ``[n]`` markers removed from its text.

    Handles both string content and the list-of-content-blocks shape; returns the
    SAME instance unchanged when nothing was stripped so callers can cheaply tell
    whether the response needs rebuilding.
    """

    content = message.content
    if isinstance(content, str):
        stripped = _strip_invalid_markers(content, valid)
        if stripped == content:
            return message
        return message.model_copy(update={"content": stripped})

    if isinstance(content, list):
        new_blocks: list = []
        changed = False
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                stripped = _strip_invalid_markers(block["text"], valid)
                if stripped != block["text"]:
                    changed = True
                    block = {**block, "text": stripped}
            new_blocks.append(block)
        if not changed:
            return message
        return message.model_copy(update={"content": new_blocks})

    return message


def _guarded(request: ModelRequest, response: ModelResponse) -> ModelResponse:
    """``response`` with the markers its final answer invented removed."""

    valid = _known_indices(_current_run_id(), request.state)
    if not valid:
        # No sources anywhere in this conversation -> nothing to validate
        # against; leave the answer's markers untouched.
        return response

    changed = False
    new_result = []
    for message in response.result:
        if isinstance(message, AIMessage) and not message.tool_calls:
            sanitized = _sanitize_message(message, valid)
            if sanitized is not message:
                changed = True
                logger.debug(
                    "Citations: stripped invalid [n] markers (valid=%s)",
                    sorted(valid),
                )
            new_result.append(sanitized)
        else:
            new_result.append(message)

    if changed:
        response.result = new_result
    return response


# --- A subagent's searches ----------------------------------------------------------


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


# --- The end of the turn ------------------------------------------------------------


def _documents_from_artifact(artifact: Any) -> list[dict]:
    """The document list off an ai_search ToolMessage artifact (list or wrapper)."""

    if isinstance(artifact, list):
        return [d for d in artifact if isinstance(d, dict)]
    if isinstance(artifact, dict) and isinstance(artifact.get("documents"), list):
        return [d for d in artifact["documents"] if isinstance(d, dict)]
    return []


def _collect_turn_sources(state: AgentState) -> list[dict]:
    """The ai_search documents produced in the CURRENT turn (since the last human).

    Walks messages back to the most recent human message, collecting each
    ai_search ToolMessage's documents. Each artifact is already the cumulative set,
    so the newest alone would suffice — but collecting every ai_search message this
    turn and letting the reducer de-dupe is robust to intra-turn ordering.
    """

    messages = state.get("messages") or []
    collected: list[dict] = []
    for message in reversed(messages):
        if getattr(message, "type", None) == "human":
            break
        if (
            isinstance(message, ToolMessage)
            and getattr(message, "name", None) == _AI_SEARCH_TOOL_NAME
        ):
            collected.extend(_documents_from_artifact(message.artifact))
    return collected


def _persisted(state: AgentState) -> dict[str, Any] | None:
    """The ``all_sources`` update for this turn's searches, or None."""

    sources = _collect_turn_sources(state)
    if not sources:
        # No KB search this turn -> nothing to add; leave the channel untouched.
        return None
    logger.debug("Citations: merging %d source(s) into all_sources", len(sources))
    return {"all_sources": sources}


class CitationsMiddleware(AgentMiddleware):
    """Check the answer's ``[n]``, surface a subagent's sources, persist the turn's.

    Must sit on the orchestrator, whose stream writer reaches the client, outside
    every middleware that rewrites the answer (SubagentRelay's copy check) so it
    checks the text the user is shown. deepagents' FilesystemMiddleware wraps
    outside it and keeps the artifact if it evicts a large ``task`` result; a call
    SubagentAccessMiddleware blocks comes back as a ToolMessage and passes through.

    adf-agent runs it as well, to persist its own searches: deepagents copies the
    subagent's ``all_sources`` back on return, so they are kept even if the run's
    registry were evicted. There the answer check sees the same valid numbers (the
    shared registry and the ``all_sources`` copied in), and with no ``task`` tool
    the subagent hook never fires.
    """

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        return _guarded(request, handler(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return _guarded(request, await handler(request))

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        if request.tool_call.get("name") != TASK_TOOL_NAME:
            return handler(request)
        # Read before the subagent runs, in the orchestrator's run context, where
        # the run id resolves.
        run_id = _current_run_id()
        known = _known_indices(run_id, request.state)
        return _with_sources(request, handler(request), run_id, known)

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        if request.tool_call.get("name") != TASK_TOOL_NAME:
            return await handler(request)
        run_id = _current_run_id()
        known = _known_indices(run_id, request.state)
        return _with_sources(request, await handler(request), run_id, known)

    def after_agent(self, state: AgentState, runtime: Any) -> dict[str, Any] | None:
        return _persisted(state)

    async def aafter_agent(self, state: AgentState, runtime: Any) -> dict[str, Any] | None:
        return _persisted(state)
