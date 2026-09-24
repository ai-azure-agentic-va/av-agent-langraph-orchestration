"""Regression tests for the summary-failure guard in v1.core.agent.

langchain 1.4.2 made SummarizationMiddleware._(a)create_summary raise once the
summary model's retries are exhausted. deepagents' summarization middleware
delegates to it and sits outside ModelErrorGuardMiddleware, so without the guard
one failed summary call fails the whole turn. These drive the REAL deepagents
middleware and its REAL langchain helper against a summary model that errors.

Runs standalone (``python test_summary_guard.py``) or under pytest.
"""

from __future__ import annotations

import asyncio
from typing import Any

from deepagents.backends.state import StateBackend
from deepagents.middleware.summarization import _DeepAgentsSummarizationMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.errors import GraphInterrupt

import v1.core.agent as agent_mod


class _ScriptedModel(BaseChatModel):
    """Chat model that raises ``error`` if set, hangs if ``hang``, else replies."""

    error: Any = None
    hang: bool = False
    reply: str = "a real summary"

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        if self.error is not None:
            raise self.error
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.reply))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        if self.hang:
            await asyncio.Event().wait()
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def _middleware(model: _ScriptedModel) -> _DeepAgentsSummarizationMiddleware:
    mw = _DeepAgentsSummarizationMiddleware(model=model, backend=StateBackend())
    # Skip with_retry's backoff sleeps; the raise-after-retries path is the same.
    mw._lc_helper._summary_model = model
    return mw


_HISTORY = [HumanMessage(content="hello"), AIMessage(content="hi there")]


def test_async_summary_failure_degrades_to_placeholder() -> None:
    agent_mod._ensure_summary_failures_degrade()
    mw = _middleware(_ScriptedModel(error=RuntimeError("429 Too Many Requests")))
    assert asyncio.run(mw._acreate_summary(_HISTORY)) == agent_mod._SUMMARY_FALLBACK


def test_sync_summary_failure_degrades_to_placeholder() -> None:
    agent_mod._ensure_summary_failures_degrade()
    mw = _middleware(_ScriptedModel(error=TimeoutError("summary model timed out")))
    assert mw._create_summary(_HISTORY) == agent_mod._SUMMARY_FALLBACK


def test_successful_summary_passes_through() -> None:
    agent_mod._ensure_summary_failures_degrade()
    mw = _middleware(_ScriptedModel(reply="user asked about VPN resets"))
    assert asyncio.run(mw._acreate_summary(_HISTORY)) == "user asked about VPN resets"
    assert mw._create_summary(_HISTORY) == "user asked about VPN resets"


def test_graph_interrupt_still_propagates() -> None:
    agent_mod._ensure_summary_failures_degrade()
    mw = _middleware(_ScriptedModel(error=GraphInterrupt()))
    try:
        asyncio.run(mw._acreate_summary(_HISTORY))
    except GraphInterrupt:
        return
    raise AssertionError("GraphInterrupt was swallowed by the summary guard")


def test_cancellation_still_propagates() -> None:
    # Cancel the run while the summary model call is in flight -- the way a
    # client disconnect or run cancel actually reaches this code.
    agent_mod._ensure_summary_failures_degrade()
    mw = _middleware(_ScriptedModel(hang=True))

    async def _cancel_mid_summary() -> None:
        task = asyncio.create_task(mw._acreate_summary(_HISTORY))
        await asyncio.sleep(0.05)
        task.cancel()
        await task

    try:
        asyncio.run(_cancel_mid_summary())
    except asyncio.CancelledError:
        return
    raise AssertionError("CancelledError was swallowed by the summary guard")


def test_guard_is_idempotent() -> None:
    agent_mod._ensure_summary_failures_degrade()
    first = _DeepAgentsSummarizationMiddleware._acreate_summary
    agent_mod._ensure_summary_failures_degrade()
    assert _DeepAgentsSummarizationMiddleware._acreate_summary is first
    # Wrapped exactly once: one hop back lands on deepagents' own method.
    inner = first.__wrapped__
    assert not getattr(inner, "_degrades_on_failure", False)
    assert inner.__module__ == "deepagents.middleware.summarization"


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
