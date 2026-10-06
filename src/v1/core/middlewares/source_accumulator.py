"""Persist each turn's ai_search documents into the cumulative ``all_sources``.

Referenced sources are append-only across the WHOLE conversation: every document
``ai_search_tool`` surfaces is numbered once (a conversation-stable ``[n]``) and
kept for the life of the thread, so the frontend shows one growing (collapsed)
"Referenced Sources" panel and resolves inline ``[n]`` markers from any turn.

``ai_search_tool`` already returns the CUMULATIVE numbered set as its ToolMessage
``artifact`` (it seeds its numbering from this same channel). This middleware copies
the current turn's ai_search artifacts into the ``all_sources`` state channel after
the turn completes; the channel's ``merge_sources`` reducer unions + de-dupes them
into the running list. Writing STATE (not only the live custom ``documents`` event)
is what makes the cumulative list DURABLE — it survives a thread reopened from
history, where the live event never replays, and a process restart.

Note: the ServiceNow subagent runs ``ai_search_tool`` inside its own subgraph, whose
ToolMessages do not surface in the parent ``messages``; those subagent sources are
therefore not accumulated here (a known boundary, not a regression — subagent
searches did not contribute to the parent's sources before either).
"""

from __future__ import annotations

import logging
from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import ToolMessage

logger = logging.getLogger(__name__)

# The name ai_search_tool is registered under; its ToolMessage carries the turn's
# cumulative referenced-source set on `.artifact` (see ai_search_tool).
_AI_SEARCH_TOOL_NAME = "ai_search_tool"


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


class SourceAccumulatorMiddleware(AgentMiddleware):
    """Append this turn's ai_search documents to the cumulative ``all_sources``."""

    def _update(self, state: AgentState) -> dict[str, Any] | None:
        sources = _collect_turn_sources(state)
        if not sources:
            # No KB search this turn -> nothing to add; leave the channel untouched.
            return None
        logger.debug("SourceAccumulator: merging %d source(s) into all_sources", len(sources))
        return {"all_sources": sources}

    def after_agent(self, state: AgentState, runtime: Any) -> dict[str, Any] | None:
        return self._update(state)

    async def aafter_agent(self, state: AgentState, runtime: Any) -> dict[str, Any] | None:
        return self._update(state)
