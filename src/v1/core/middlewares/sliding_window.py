"""Sliding-window safety floor: a hard, deterministic per-call context ceiling.

The deepagents ``SummarizationMiddleware`` gives a *soft* window — it compacts at
~0.85 of the model's input budget and offloads evicted history — plus a reactive
``ContextOverflowError`` retry. What neither guarantees is that a *single* model
call can never exceed the window: summarization can mis-fire (e.g. a custom
deployment name whose ``model.profile`` exposes no ``max_input_tokens`` degrades
it to coarser defaults), or one turn can arrive already oversized.

This middleware is that last-resort guarantee. In ``(a)wrap_model_call`` it counts
the outgoing request and, only if it exceeds a hard token ceiling, trims the
OLDEST messages out of the request *view* before the model call. It is:

- **View-only.** It calls ``handler(request.override(messages=...))`` and emits no
  state ``Command``, so ``state["messages"]`` and the summarization
  ``_summarization_event`` are left intact — the durable log and offloaded-history
  recall path are never mutated. The floor is a per-call cap, not compaction.
- **Innermost.** Wired as the LAST entry in ``create_deep_agent(middleware=[...])``
  it runs closest to the model, after summarization has already produced the
  effective messages, so it sees (and caps) exactly what would be sent.
- **Pair-safe.** It trims with :func:`langchain_core.messages.trim_messages`
  (``strategy="last"``, ``start_on="human"``), which never orphans a leading
  ``ToolMessage`` — a raw slice would, and Azure/OpenAI 400s on that.
- **Recall-preserving.** When the conversation has been summarized the first
  message is the summary ``HumanMessage`` (``lc_source == "summarization"``) that
  names the offloaded-history path; the floor holds it aside and re-prepends it so
  the recall path survives even under an aggressive trim.

Because the ceiling is an absolute token count computed in :mod:`v1.core.agent`
(from ``model.profile`` or the ``AI_LLM_MAX_INPUT_TOKENS`` fallback), this class
never depends on the model profile resolving at runtime.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage, trim_messages
from langchain_core.messages.utils import count_tokens_approximately

logger = logging.getLogger(__name__)


def _is_summary_message(message: AnyMessage) -> bool:
    """Whether ``message`` is the summarization summary that carries the recall path.

    The deepagents ``SummarizationMiddleware`` inserts a ``HumanMessage`` tagged
    ``additional_kwargs["lc_source"] == "summarization"`` as the first message of a
    compacted conversation; its body names the ``/conversation_history/...`` file so
    the agent can ``read_file`` older detail. It must never be trimmed away.
    """

    return (
        isinstance(message, HumanMessage)
        and message.additional_kwargs.get("lc_source") == "summarization"
    )


class SlidingWindowFloorMiddleware(AgentMiddleware):
    """Cap the per-call request at a hard token ceiling by trimming oldest messages.

    Args:
        max_tokens: Absolute ceiling (input tokens) for the message list plus system
            prompt. Computed once in ``v1.core.agent`` as
            ``context_window_floor_fraction * max_input_tokens`` so this middleware is
            independent of whether ``model.profile`` resolves at runtime.
    """

    def __init__(self, max_tokens: int) -> None:
        if max_tokens <= 0:
            raise ValueError(f"max_tokens must be positive, got {max_tokens}")
        self._max_tokens = max_tokens

    @staticmethod
    def _count(messages: list[AnyMessage]) -> int:
        """Approximate token count for a message list (matches the SDK's counter)."""

        return count_tokens_approximately(messages)

    def _apply_floor(
        self,
        messages: list[AnyMessage],
        system_message: SystemMessage | None,
    ) -> list[AnyMessage] | None:
        """Return a trimmed message view, or ``None`` when no trim is needed.

        Counts system prompt + messages; if within budget, returns ``None`` so the
        caller passes the request through untouched. Otherwise trims the oldest
        messages (preserving the summary message and tool-call pairs) to fit.
        """

        if not messages:
            return None

        system_overhead = self._count([system_message]) if system_message is not None else 0
        preamble = [system_message] if system_message is not None else []
        total = self._count([*preamble, *messages])
        if total <= self._max_tokens:
            return None

        # Budget for the message list itself, after reserving the system prompt.
        message_budget = max(self._max_tokens - system_overhead, 1)

        # Hold the summary message (recall path) aside so it is never trimmed.
        head: list[AnyMessage] = []
        body = messages
        if _is_summary_message(messages[0]):
            head = [messages[0]]
            body = messages[1:]
            message_budget = max(message_budget - self._count(head), 1)

        trimmed_body = trim_messages(
            body,
            max_tokens=message_budget,
            token_counter=count_tokens_approximately,
            strategy="last",
            start_on="human",
            include_system=False,
            allow_partial=False,
        )

        result = [*head, *trimmed_body]
        logger.warning(
            "Sliding-window floor engaged: request ~%d tokens exceeded the %d-token "
            "ceiling; trimmed %d of %d messages from the model view (state untouched).",
            total,
            self._max_tokens,
            len(messages) - len(result),
            len(messages),
        )
        return result

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        trimmed = self._apply_floor(request.messages, request.system_message)
        if trimmed is not None:
            request = request.override(messages=trimmed)
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        trimmed = self._apply_floor(request.messages, request.system_message)
        if trimmed is not None:
            request = request.override(messages=trimmed)
        return await handler(request)
