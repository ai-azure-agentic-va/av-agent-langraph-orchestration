"""Strip invented citation markers from the model's final answer.

The knowledge-base answer path tells the model to reuse only the ``[n]`` markers
the ``ai_search_tool`` grounding assigns — a turn-stable ``1..n`` registry (see
:mod:`v1.core.tools.ai_search.ai_search`). Models occasionally emit numbers that
were never assigned: hallucinated, or copied out of a bracketed cross-reference
inside a source body. Such markers match no document, so the frontend leaves
them as raw ``[n]`` text and they render as ugly, un-clickable brackets (and are
persisted that way in the checkpoint).

This middleware is the deterministic backstop the prompt cannot guarantee. After
the model produces a FINAL answer (an ``AIMessage`` with no tool calls) it removes
any ``[n]`` whose number is not a valid CONVERSATION-level citation, so no orphan
marker ever reaches — or is persisted for — the UI.

Numbering is append-only across the whole conversation, so the valid set is this
turn's freshly-assigned numbers (the seeded ai_search registry) UNIONED with every
source accumulated so far (the ``all_sources`` state channel). That means a
follow-up turn which reuses an EARLIER turn's ``[n]`` without running its own search
is validated rather than left to chance — only genuinely invented markers are
stripped. It only no-ops when the conversation has no sources at all (nothing to
validate against).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable

from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage

from v1.core.tools.ai_search.ai_search import (
    _current_run_id,
    assigned_citation_indices,
)

logger = logging.getLogger(__name__)

# Optional leading whitespace + a bracketed integer. Capturing the whitespace lets
# us drop it ALONG WITH an invalid marker (so "resolved [1] [242]." -> "resolved
# [1].") while preserving it for a valid marker we keep (we return the whole match).
_CITATION_MARKER_RE = re.compile(r"(\s*)\[(\d+)\]")


def _accumulated_indices(state: object) -> set[int]:
    """The ``[n]`` numbers of every source accumulated across the conversation.

    Read off the ``all_sources`` state channel (durable, per-thread), so a marker
    that references a source from an earlier turn is treated as valid even on a turn
    that ran no new search.
    """

    raw = state.get("all_sources") if isinstance(state, dict) else None
    if not isinstance(raw, list):
        return set()
    return {
        doc["index"]
        for doc in raw
        if isinstance(doc, dict) and isinstance(doc.get("index"), int)
    }


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


class CitationGuardMiddleware(AgentMiddleware):
    """Remove citation markers the model invented, keyed to the turn's registry.

    Wraps the model call so it sees the produced response; only final answers
    (``AIMessage`` with no tool calls) are rewritten, and only when the conversation
    has at least one accumulated source. Position in the middleware list is
    immaterial to correctness — no other middleware rewrites answer text — so it
    sits outermost purely to post-process the fully assembled response.
    """

    def _guard(self, request: ModelRequest, response: ModelResponse) -> ModelResponse:
        # This turn's freshly-assigned numbers (seeded ai_search registry) UNIONED
        # with every source accumulated across the conversation (all_sources state),
        # so a marker reused from an earlier turn stays valid and only invented ones
        # are stripped.
        valid = set(assigned_citation_indices(_current_run_id()))
        valid |= _accumulated_indices(request.state)
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
                        "CitationGuard: stripped invalid [n] markers (valid=%s)",
                        sorted(valid),
                    )
                new_result.append(sanitized)
            else:
                new_result.append(message)

        if changed:
            response.result = new_result
        return response

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        return self._guard(request, handler(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return self._guard(request, await handler(request))
