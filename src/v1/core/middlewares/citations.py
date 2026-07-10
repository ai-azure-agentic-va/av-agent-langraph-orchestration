"""Middleware that reconciles streamed sources with the answer's citations.

``ai_search_tool`` streams a ``search_complete`` event for EVERY document a
search retrieves above the relevance floor, and the UI appends each one under
its turn-stable ``[n]``. But the model only adds an inline ``[n]`` marker for
the passages it actually used, so the appended list ends up showing retrieved
documents the answer never cites.

This middleware closes that gap. After the agent finishes, it reads the markers
present in the final answer and emits a single authoritative
``sources_final`` custom-stream event holding ONLY the cited sources (in
citation-number order). The UI treats this event as the replacement for the
incrementally appended ``search_complete`` sources.
"""

from __future__ import annotations

import logging
from typing import Any, List

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.config import get_stream_writer

from v1.core.tools.ai_search.ai_search import cited_sources_for_current_run

logger = logging.getLogger(__name__)


def _content_to_text(content: Any) -> str:
    """Flatten message content (string or list of content blocks) to text."""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return " ".join(parts)
    return ""


def _final_answer_text(state: AgentState) -> str:
    """Join the assistant text produced since the last user message.

    Citation markers only appear in user-facing assistant content, and a single
    turn's answer may span more than one ``AIMessage``. We concatenate every
    AI message after the last human turn so markers in any of them are counted,
    while ignoring earlier turns' citations.
    """

    messages = state.get("messages", []) or []
    texts: List[str] = []
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage) or getattr(msg, "type", None) == "human":
            break
        if isinstance(msg, AIMessage) or getattr(msg, "type", None) == "ai":
            text = _content_to_text(getattr(msg, "content", ""))
            if text:
                texts.append(text)
    texts.reverse()
    return "\n".join(texts)


class CitationFilterMiddleware(AgentMiddleware):
    """Emit a ``sources_final`` event with only the inline-cited sources."""

    async def aafter_agent(self, state: AgentState, runtime) -> dict[str, Any] | None:
        try:
            writer = get_stream_writer()
        except Exception:
            writer = None
        if writer is None:
            # Non-streaming invocation (e.g. a plain ``ainvoke``): nothing to
            # reconcile because no source chips were streamed.
            return None

        answer = _final_answer_text(state)
        sources = cited_sources_for_current_run(answer)
        try:
            writer({"type": "sources_final", "sources": sources})
        except Exception:  # pragma: no cover - streaming is non-essential
            logger.debug("sources_final stream emit failed", exc_info=True)
        return None
