"""Tests for ModelErrorGuardMiddleware: a model-call 400 becomes a graceful
assistant message (streamed through the normal path) instead of a raw UI error,
while non-400 failures still propagate.

Runs standalone (``python test_model_error_guard.py``) or under pytest.
"""

from __future__ import annotations

import asyncio

import httpx
import openai
from langchain.agents.middleware import ModelResponse
from langchain_core.messages import AIMessage

import v1.core.middlewares.model_error_guard as m


def _bad_request(body, message="Bad request"):
    resp = httpx.Response(400, request=httpx.Request("POST", "https://x/chat"))
    return openai.BadRequestError(message, response=resp, body=body)


def test_is_content_filter_detection() -> None:
    cf = _bad_request({"error": {"code": "content_filter", "message": "filtered"}},
                      message="The response was filtered")
    assert m._is_content_filter(cf) is True
    generic = _bad_request({"error": {"code": "invalid_request_error", "message": "bad param"}},
                           message="Unsupported parameter 'foo'")
    assert m._is_content_filter(generic) is False


def test_content_filter_400_returns_refusal() -> None:
    mw = m.ModelErrorGuardMiddleware()

    async def handler(_req):
        raise _bad_request({"error": {"code": "content_filter"}},
                           message="The response was filtered due to content management policy")

    out = asyncio.run(mw.awrap_model_call(None, handler))
    assert isinstance(out, ModelResponse)
    assert out.result[0].content == m._CONTENT_FILTER_MESSAGE


def test_generic_400_returns_graceful_message() -> None:
    mw = m.ModelErrorGuardMiddleware()

    async def handler(_req):
        raise _bad_request({"error": {"code": "invalid_request_error", "message": "bad"}},
                           message="Unsupported value for parameter")

    out = asyncio.run(mw.awrap_model_call(None, handler))
    assert out.result[0].content == m._GENERIC_400_MESSAGE


def test_success_passes_through_unchanged() -> None:
    mw = m.ModelErrorGuardMiddleware()
    resp = ModelResponse(result=[AIMessage(content="normal answer")], structured_response=None)

    async def handler(_req):
        return resp

    out = asyncio.run(mw.awrap_model_call(None, handler))
    assert out is resp  # untouched on success


def test_non_400_error_propagates() -> None:
    # A 429/500/etc. is a genuine failure worth surfacing — not swallowed.
    mw = m.ModelErrorGuardMiddleware()
    resp = httpx.Response(429, request=httpx.Request("POST", "https://x"))

    async def handler(_req):
        raise openai.RateLimitError("slow down", response=resp, body=None)

    raised = False
    try:
        asyncio.run(mw.awrap_model_call(None, handler))
    except openai.RateLimitError:
        raised = True
    assert raised, "non-400 error should propagate, not be swallowed"


def test_content_filtered_200_completion_becomes_refusal() -> None:
    # 200 response, empty content, finish_reason=content_filter -> refusal.
    mw = m.ModelErrorGuardMiddleware()
    blanked = AIMessage(content="", response_metadata={"finish_reason": "content_filter"})
    blanked.id = "msg-1"
    resp = ModelResponse(result=[blanked], structured_response=None)

    async def handler(_req):
        return resp

    out = asyncio.run(mw.awrap_model_call(None, handler))
    assert out.result[0].content == m._CONTENT_FILTER_MESSAGE
    assert out.result[0].id == "msg-1"  # id preserved for frontend threading


def test_content_filtered_via_results_flag() -> None:
    mw = m.ModelErrorGuardMiddleware()
    blanked = AIMessage(
        content="",
        response_metadata={"content_filter_results": {"hate": {"filtered": True, "severity": "high"}}},
    )
    resp = ModelResponse(result=[blanked], structured_response=None)

    async def handler(_req):
        return resp

    out = asyncio.run(mw.awrap_model_call(None, handler))
    assert out.result[0].content == m._CONTENT_FILTER_MESSAGE


def test_empty_tool_call_message_is_not_touched() -> None:
    # A tool-calling AIMessage legitimately has empty text — must pass through.
    mw = m.ModelErrorGuardMiddleware()
    tool_msg = AIMessage(
        content="",
        tool_calls=[{"name": "ai_search_tool", "args": {"query": "x"}, "id": "t1", "type": "tool_call"}],
    )
    resp = ModelResponse(result=[tool_msg], structured_response=None)

    async def handler(_req):
        return resp

    out = asyncio.run(mw.awrap_model_call(None, handler))
    assert out.result[0] is tool_msg  # untouched


def test_normal_answer_passes_through() -> None:
    mw = m.ModelErrorGuardMiddleware()
    normal = AIMessage(content="Here is the answer.", response_metadata={"finish_reason": "stop"})
    resp = ModelResponse(result=[normal], structured_response=None)

    async def handler(_req):
        return resp

    out = asyncio.run(mw.awrap_model_call(None, handler))
    assert out.result[0] is normal


def test_sync_wrap_also_guards() -> None:
    mw = m.ModelErrorGuardMiddleware()

    def handler(_req):
        raise _bad_request({"error": {"code": "content_filter"}}, message="filtered")

    out = mw.wrap_model_call(None, handler)
    assert out.result[0].content == m._CONTENT_FILTER_MESSAGE


def _main() -> int:
    checks = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for check in checks:
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - standalone runner reports all
            failures += 1
            print(f"FAIL {check.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {check.__name__}")
    print(f"\n{len(checks) - failures}/{len(checks)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
