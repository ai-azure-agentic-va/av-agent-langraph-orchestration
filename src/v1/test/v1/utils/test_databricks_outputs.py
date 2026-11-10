"""Tests for DatabricksOutputsMiddleware (``v1.core.middlewares.databricks_outputs``).

The middleware opens a channel around each ``task`` call to the Databricks subagent and
attaches the query results and table choice its tools recorded to the ``task`` ToolMessage's
artifact. The handler here is a fake standing in for deepagents' ``task`` tool, except in the
subgraph test, which runs a real compiled LangGraph graph the way ``task`` runs a subagent.

Runs standalone (``python test_databricks_outputs.py``) or under pytest.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, TypedDict

from databricks_test_support import run_tests
from langchain_core.messages import ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from v1.core.middlewares.databricks_outputs import DatabricksOutputsMiddleware
from v1.core.databricks_workspaces import DATABRICKS_SUBAGENT_NAME
from v1.core.middlewares.access_control import TASK_TOOL_NAME
from v1.core.tools.databricks.channel import emit_status, record_choices, record_result

RESULT = {"sql": "SELECT 1", "columns": ["n"], "rows": [["1"]], "row_count": 1, "truncated": False}
DOC = {"index": 1, "title": "VPN reset runbook", "url": "https://kb.example.test/vpn-reset"}


def _request(call_id: str, subagent: str = DATABRICKS_SUBAGENT_NAME, writer: Any = None) -> Any:
    return SimpleNamespace(
        tool_call={
            "name": TASK_TOOL_NAME,
            "id": call_id,
            "args": {"subagent_type": subagent, "description": "q"},
        },
        runtime=SimpleNamespace(stream_writer=writer),
        state={},
    )


def _command(call_id: str, artifact: Any = None) -> Command:
    return Command(
        update={
            "messages": [
                ToolMessage(content="answer", tool_call_id=call_id, name="task", artifact=artifact)
            ]
        }
    )


def _run(request: Any, handler: Any) -> Any:
    return asyncio.run(DatabricksOutputsMiddleware().awrap_tool_call(request, handler))


def test_attaches_outputs() -> None:
    """Results and choices land on the ``task`` message, as a ToolMessage or in a Command,
    next to any sources artifact (a list moves under "documents"; a dict is merged)."""

    choices = {"question": "Which table should I use?", "tables": ["c.s.a", "c.s.b"]}
    results = {"databricks": {"results": [RESULT]}}
    chosen = {"databricks": {"results": [], "choices": choices}}
    for record, artifact, expected in (
        (lambda: record_result(RESULT), None, results),
        (lambda: record_choices(choices), None, chosen),
        (lambda: record_result(RESULT), [DOC], {"documents": [DOC], **results}),
        (lambda: record_result(RESULT), {"documents": [DOC]}, {"documents": [DOC], **results}),
    ):
        for as_command in (True, False):

            async def handler(request: Any, record=record, artifact=artifact, wrap=as_command):
                record()
                message = ToolMessage("answer", tool_call_id="call-1", artifact=artifact)
                return Command(update={"messages": [message]}) if wrap else message

            out = _run(_request("call-1"), handler)
            message = out.update["messages"][0] if as_command else out
            assert message.artifact == expected, (expected, as_command)


def test_passes_through_untouched() -> None:
    """Another subagent or tool, a call that recorded nothing (or was blocked), and the
    sync path all return the handler's result as is."""

    expected = _command("call-1", artifact=[DOC])

    async def records(request: Any) -> Command:
        record_result(RESULT)  # no channel is open for another subagent: a no-op
        return expected

    async def silent(request: Any) -> Command:
        return expected

    other_tool = SimpleNamespace(tool_call={"name": "ai_search_tool", "id": "x", "args": {}})
    assert _run(_request("call-1", subagent="servicenow-ticket-agent"), records) is expected
    assert _run(other_tool, records) is expected
    assert _run(_request("call-1"), silent) is expected
    sync = DatabricksOutputsMiddleware().wrap_tool_call(_request("call-1"), lambda r: expected)
    assert sync is expected


def test_handler_result_not_mutated_and_other_messages_kept() -> None:
    other = ToolMessage(content="other", tool_call_id="other-call", name="task")

    async def handler(request: Any) -> Command:
        record_result(RESULT)
        return Command(
            update={
                "messages": [
                    other,
                    ToolMessage(content="answer", tool_call_id="call-1", name="task"),
                ]
            }
        )

    out = _run(_request("call-1"), handler)
    assert out.update["messages"][0] is other
    assert out.update["messages"][1].artifact == {"databricks": {"results": [RESULT]}}


def test_parallel_task_calls_isolated() -> None:
    async def handler(request: Any) -> Command:
        call_id = request.tool_call["id"]
        await asyncio.sleep(0)
        record_result({**RESULT, "sql": f"SELECT '{call_id}'"})
        await asyncio.sleep(0)
        return _command(call_id)

    async def both() -> list[Any]:
        middleware = DatabricksOutputsMiddleware()
        return await asyncio.gather(
            middleware.awrap_tool_call(_request("a"), handler),
            middleware.awrap_tool_call(_request("b"), handler),
        )

    first, second = asyncio.run(both())
    assert first.update["messages"][0].artifact == {
        "databricks": {"results": [{**RESULT, "sql": "SELECT 'a'"}]}
    }
    assert second.update["messages"][0].artifact == {
        "databricks": {"results": [{**RESULT, "sql": "SELECT 'b'"}]}
    }


class _State(TypedDict, total=False):
    done: bool


async def _async_node(state: _State) -> _State:
    emit_status("x")
    record_result(RESULT)
    return {"done": True}


def _sync_node(state: _State) -> _State:
    emit_status("y")
    return {"done": True}


def test_status_reaches_orchestrator_writer_through_subgraph() -> None:
    builder = StateGraph(_State)
    builder.add_node("query", _async_node)
    builder.add_node("summarise", _sync_node)
    builder.add_edge(START, "query")
    builder.add_edge("query", "summarise")
    builder.add_edge("summarise", END)
    subagent = builder.compile()
    sent: list[dict] = []

    async def handler(request: Any) -> Command:
        await subagent.ainvoke({})
        return _command(request.tool_call["id"])

    out = _run(_request("call-1", writer=sent.append), handler)
    assert sent == [{"type": "status", "text": "x"}, {"type": "status", "text": "y"}]
    assert out.update["messages"][0].artifact == {"databricks": {"results": [RESULT]}}


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
