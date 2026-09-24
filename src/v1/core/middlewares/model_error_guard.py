"""Turn a model-call 400 into a graceful answer instead of a raw UI error.

Azure OpenAI's own content filter rejects some prompts/completions with HTTP 400
(an ``openai.BadRequestError``, typically ``code="content_filter"``); other 400s
(a malformed request, an unsupported parameter) can also surface mid-run.
Uncaught, any of these fail the whole graph run and the frontend renders a
generic error toast / raw 400 — bad UX.

This middleware wraps the top-level model call and, on a 400, returns a short
assistant message through the NORMAL response path, so it streams to the user as
a readable reply instead of a 400 error. It is the output-side companion to the
SafetyGateMiddleware input gate: when a request "doesn't pass the Azure safety
check" (the content filter) or otherwise 400s, the user still gets a clean
message. The underlying error is logged so genuine misconfigurations (e.g. a bad
api-version / parameter that 400s every call) stay visible in the backend logs.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import openai
from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage, BaseMessage

logger = logging.getLogger(__name__)

# A content-filter rejection is a safety block — mirror the SafetyGate refusal.
_CONTENT_FILTER_MESSAGE = "I can't help with that request."
# Any other 400 is an operational failure, not a policy one — say so plainly
# without leaking the raw error.
_GENERIC_400_MESSAGE = (
    "Sorry — I wasn't able to complete that request. Please try rephrasing it."
)


def _is_content_filter(err: openai.BadRequestError) -> bool:
    """Whether a 400 is an Azure content-filter rejection (vs a generic bad request).

    The openai SDK does not always populate ``err.code``, so also scan the error
    body/message for the content-filter / Responsible AI markers Azure returns.
    """

    if getattr(err, "code", None) == "content_filter":
        return True
    blob = f"{getattr(err, 'code', '')} {getattr(err, 'message', '')} {getattr(err, 'body', '')} {err}".lower()
    return (
        "content_filter" in blob
        or "responsibleaipolicyviolation" in blob
        or "content management policy" in blob
    )


def _graceful_response(err: openai.BadRequestError) -> ModelResponse:
    if _is_content_filter(err):
        logger.info("ModelErrorGuard: content filter rejected the request; returning refusal")
        text = _CONTENT_FILTER_MESSAGE
    else:
        # Not a policy block — likely a real misconfiguration. Surface a clean
        # message to the user but log the full error for operators.
        logger.warning(
            "ModelErrorGuard: model call returned 400; returning graceful message",
            exc_info=True,
        )
        text = _GENERIC_400_MESSAGE
    return ModelResponse(result=[AIMessage(content=text)], structured_response=None)


def _is_empty_content(content: object) -> bool:
    """Whether a message's content carries no visible text."""

    if content is None:
        return True
    if isinstance(content, str):
        return content.strip() == ""
    if isinstance(content, list):
        return not any(
            (isinstance(b, str) and b.strip())
            or (isinstance(b, dict) and isinstance(b.get("text"), str) and b["text"].strip())
            for b in content
        )
    return False


def _is_content_filtered_completion(message: BaseMessage) -> bool:
    """Whether a SUCCESSFUL (HTTP 200) final answer was blanked by the content filter.

    Some Azure deployments do not 400 on an output policy hit; they return 200 with
    ``finish_reason == "content_filter"`` and empty content (or filtered
    ``content_filter_results``). Left alone that renders as an empty assistant
    bubble — worse UX than the 400. Only a final answer (an ``AIMessage`` with no
    tool calls) and empty content qualifies, so a normal tool-calling turn (whose
    text is legitimately empty) is never touched.
    """

    if not isinstance(message, AIMessage) or message.tool_calls:
        return False
    if not _is_empty_content(message.content):
        return False
    meta = getattr(message, "response_metadata", None) or {}
    if meta.get("finish_reason") == "content_filter":
        return True
    results = meta.get("content_filter_results")
    return isinstance(results, dict) and any(
        isinstance(v, dict) and v.get("filtered") for v in results.values()
    )


def _guard_response(response: ModelResponse) -> ModelResponse:
    """Replace any content-filter-blanked final answer with the clean refusal.

    Preserves the message id/metadata (via ``model_copy``) so the frontend still
    threads it as the same assistant message.
    """

    changed = False
    new_result: list[BaseMessage] = []
    for message in response.result:
        if _is_content_filtered_completion(message):
            logger.info(
                "ModelErrorGuard: content filter blanked the completion (200); returning refusal"
            )
            new_result.append(message.model_copy(update={"content": _CONTENT_FILTER_MESSAGE}))
            changed = True
        else:
            new_result.append(message)
    if changed:
        response.result = new_result
    return response


class ModelErrorGuardMiddleware(AgentMiddleware):
    """Catch a model-call 400 and return a graceful assistant message.

    Sits OUTERMOST so it wraps the whole model-call chain: whatever inner
    middleware or the model itself raises a 400, the run still ends with a
    readable message instead of a raw error. Non-400 errors are left to
    propagate (they are genuine failures worth surfacing).
    """

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        try:
            return _guard_response(handler(request))
        except openai.BadRequestError as err:
            return _graceful_response(err)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        try:
            return _guard_response(await handler(request))
        except openai.BadRequestError as err:
            return _graceful_response(err)
