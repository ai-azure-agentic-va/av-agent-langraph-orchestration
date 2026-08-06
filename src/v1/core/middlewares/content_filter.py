"""Refusal fallback for Azure OpenAI content-filter rejections.

Azure's platform-level content filter can reject a request at the INPUT stage
(prompt shield): the chat completion then fails with a 400 BadRequestError
(code ``content_filter``) before a single token is generated. Unhandled, that
exception kills the whole LangGraph run — the caller gets a run-level error
and no assistant message at all, which a chat UI renders as a dead request.

This middleware wraps the model call and converts exactly that failure into a
normal refusal message, so a filter-rejected prompt behaves like any other
refused request (visible, scoreable, thread stays consistent). Every other
error — including non-filter 400s — still raises.
"""

from __future__ import annotations

import openai
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage

_REFUSAL = (
    "I can't help with that request. If you have a question about the FIN "
    "knowledge base, data pipelines, the data lake, or ServiceNow incidents, "
    "I'm happy to help with that."
)

# Azure surfaces the filter either as a structured error code or only in the
# message text, depending on which filter stage (prompt shield vs completion)
# rejected the call.
_FILTER_MARKERS = ("content_filter", "content management policy")


def _is_content_filter(exc: openai.BadRequestError) -> bool:
    code = getattr(exc, "code", None)
    return code == "content_filter" or any(
        marker in str(exc) for marker in _FILTER_MARKERS
    )


class ContentFilterRefusalMiddleware(AgentMiddleware):
    """Short-circuit a content-filter 400 into a refusal AIMessage."""

    async def awrap_model_call(self, request, handler):
        try:
            return await handler(request)
        except openai.BadRequestError as exc:
            if not _is_content_filter(exc):
                raise
            return AIMessage(content=_REFUSAL)

    def wrap_model_call(self, request, handler):
        try:
            return handler(request)
        except openai.BadRequestError as exc:
            if not _is_content_filter(exc):
                raise
            return AIMessage(content=_REFUSAL)
