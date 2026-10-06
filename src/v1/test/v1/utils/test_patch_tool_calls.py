"""Regression tests: a tool call cut off part-way must not break the thread.

When a run stops between the model's tool call and the tool's result (the user
presses Stop, Azure returns a 429, a tool raises), the checkpoint keeps an
AIMessage whose tool_calls have no ToolMessage. Azure OpenAI rejects every later
request on that thread with a 400 ("tool_calls must be followed by tool
messages"), which ModelErrorGuardMiddleware turns into the generic apology on
every turn, so only a new chat helped. deepagents' PatchToolCallsMiddleware
answers each such call with a "did not complete" ToolMessage before the next
turn; ``_EXCLUDED_MIDDLEWARE`` used to switch it off.

These build the REAL agent (``_build_agent_sync``) around a stand-in model that
enforces Azure's tool-message rule. It subclasses AzureChatOpenAI so deepagents
resolves the real "azure" harness profile, i.e. the real exclusions apply.

Runs standalone (``python test_patch_tool_calls.py``) or under pytest.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import openai
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langchain_openai import AzureChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

import v1.core.agent as agent_mod
from v1.core.middlewares.model_error_guard import _GENERIC_400_MESSAGE
from v1.core.middlewares.safety import SAFETY_CLASSIFIER_SYSTEM_PROMPT

_CUT_OFF_CALL_ID = "call_cut_off"
_RECOVERED = "Back on track."


def _unanswered_tool_call_ids(messages: list[BaseMessage]) -> list[str]:
    """Tool-call ids Azure OpenAI 400s on: each must be answered by the
    ToolMessages directly after its assistant message."""

    missing: list[str] = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, AIMessage):
            continue
        answered: set[str] = set()
        for following in messages[i + 1 :]:
            if not isinstance(following, ToolMessage):
                break
            answered.add(following.tool_call_id)
        for call in (*msg.tool_calls, *msg.invalid_tool_calls):
            if call.get("id") and call["id"] not in answered:
                missing.append(call["id"])
    return missing


def _azure_400(missing: list[str]) -> openai.BadRequestError:
    message = (
        "An assistant message with 'tool_calls' must be followed by tool messages "
        "responding to each 'tool_call_id'. The following tool_call_ids did not "
        f"have response messages: {', '.join(missing)}"
    )
    resp = httpx.Response(400, request=httpx.Request("POST", "https://x/chat"))
    body = {"error": {"message": message, "type": "invalid_request_error", "code": None}}
    return openai.BadRequestError(message, response=resp, body=body)


class _AzureStandIn(AzureChatOpenAI):
    """AzureChatOpenAI that never touches the network.

    Allows the safety classifier, 400s like Azure on an unanswered tool call,
    and otherwise plays ``replies`` in order.
    """

    replies: list[AIMessage] = Field(default_factory=list)
    classifier_calls: int = 0

    def _reply(self, messages: list[BaseMessage]) -> ChatResult:
        first = messages[0] if messages else None
        if isinstance(first, SystemMessage) and first.content == SAFETY_CLASSIFIER_SYSTEM_PROMPT:
            self.classifier_calls += 1
            message = AIMessage(content="ALLOW")
        elif missing := _unanswered_tool_call_ids(messages):
            raise _azure_400(missing)
        else:
            message = self.replies.pop(0)
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return self._reply(messages)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return self._reply(messages)


@tool("ai_search_tool")
async def _search_that_dies(query: str) -> str:
    """Stand-in for ai_search_tool that dies mid-call, like a 429 or a crash."""

    raise RuntimeError("429 Too Many Requests")


def _stand_in_model(replies: list[AIMessage]) -> _AzureStandIn:
    return _AzureStandIn(
        azure_endpoint="https://dummy.openai.azure.com",
        azure_deployment="gpt-test",
        api_version="2024-10-21",
        api_key="dummy-key",
        disable_streaming=True,
        replies=replies,
    )


@contextmanager
def _agent(model: _AzureStandIn) -> Iterator[Any]:
    # Patched for the whole test, not just the build: the safety classifier
    # fetches get_azure_chat_model() on every turn.
    saved = (agent_mod.get_azure_chat_model, agent_mod.ai_search_tool)
    agent_mod.get_azure_chat_model = lambda: model
    agent_mod.ai_search_tool = _search_that_dies
    try:
        yield agent_mod._build_agent_sync(InMemorySaver())
    finally:
        agent_mod.get_azure_chat_model, agent_mod.ai_search_tool = saved


def _user(text: str) -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": text}]}


def test_patch_tool_calls_middleware_is_in_the_agent() -> None:
    with _agent(_stand_in_model([])) as graph:
        assert "PatchToolCallsMiddleware.before_agent" in graph.nodes, (
            "PatchToolCallsMiddleware is missing from the orchestrator stack; is it "
            "back in _EXCLUDED_MIDDLEWARE?"
        )


def test_thread_recovers_after_a_cut_off_tool_call() -> None:
    model = _stand_in_model(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "ai_search_tool", "args": {"query": "VPN reset"}, "id": _CUT_OFF_CALL_ID}
                ],
            ),
            AIMessage(content=_RECOVERED),
            AIMessage(content=_RECOVERED),
        ]
    )
    config = {"configurable": {"thread_id": "cut-off-thread"}}

    async def _scenario(graph: Any) -> None:
        # Turn 1: the tool dies mid-call, leaving the tool call unanswered.
        try:
            await graph.ainvoke(_user("Search the KB for VPN reset steps"), config)
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected the dying tool to fail the run")
        cut = (await graph.aget_state(config)).values["messages"][-1]
        assert isinstance(cut, AIMessage) and cut.tool_calls[0]["id"] == _CUT_OFF_CALL_ID

        # Turn 2: a normal answer, not ModelErrorGuard's apology for Azure's 400.
        out = await graph.ainvoke(_user("Are you still there?"), config)
        assert out["messages"][-1].content != _GENERIC_400_MESSAGE, (
            "the cut-off tool call still 400s the next turn"
        )
        assert out["messages"][-1].content == _RECOVERED
        patched = next(
            m for m in out["messages"]
            if isinstance(m, ToolMessage) and m.tool_call_id == _CUT_OFF_CALL_ID
        )
        assert patched.status == "error"

        # Turn 3: the patch was persisted, so the thread stays healthy.
        out = await graph.ainvoke(_user("Thanks"), config)
        assert out["messages"][-1].content == _RECOVERED

    with _agent(model) as graph:
        asyncio.run(_scenario(graph))
    assert model.classifier_calls == 3, "the safety classifier bypassed the stand-in"


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
