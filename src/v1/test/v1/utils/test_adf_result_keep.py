"""Regression tests for the view-only keep requirement on ``adf-agent``'s tool results.

The subagent dropped lines its tools had printed while writing its answer (run 7:
a row's Status and [open run] link, the GROUP B heading, the 'Completion
estimate' line, the factories searched, the closest names), so each of its model
calls now sees a KEEP REQUIREMENT beside the current task's READ-tool results.
Only that call's view changes: persisted messages, earlier turns, the write
tools, other tools, and failed or empty results are left exactly as they were.

The subagent's final answer also goes through the copy check, so a run ID it
changed while copying a result (SUP-06, run 8's re-run), or a name, is put back.

Runs standalone (``python test_adf_result_keep.py``) or under pytest.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Any

from langchain.agents.middleware import ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import v1.core.middlewares.adf_result_keep as keep
from v1.core.middlewares import AdfResultKeepMiddleware

READ_TOOLS = ("discover_pipelines_by_source_system", "get_pipeline_runtime_estimate")
RESULT = (
    "[adf-agent] Runtime estimate for 'pl_x' in factory 'example-data-factory'\n"
    "  Completion estimate: Not applicable because the pipeline is not currently running."
)
UNTAGGED = RESULT.replace("[adf-agent] ", "")


# --- lightweight stand-in so the test stays offline (no real ModelRequest) ---


@dataclass
class _FakeModelRequest:
    messages: list

    def override(self, **overrides: Any) -> _FakeModelRequest:
        return replace(self, **overrides)


def _call(call_id: str, name: str) -> dict:
    return {"name": name, "args": {"pipeline_name": "pl_x"}, "id": call_id, "type": "tool_call"}


def _middleware(names: tuple[str, ...] = READ_TOOLS) -> AdfResultKeepMiddleware:
    return AdfResultKeepMiddleware(names)


def _forwarded(
    request: _FakeModelRequest, middleware: AdfResultKeepMiddleware | None = None
) -> _FakeModelRequest:
    seen: list[_FakeModelRequest] = []
    (middleware or _middleware()).wrap_model_call(request, lambda r: seen.append(r) or "response")
    return seen[0]


def _task_turn(
    content: Any = RESULT, name: str = "get_pipeline_runtime_estimate", **tool_kwargs: Any
) -> list:
    return [
        HumanMessage(content='User asked: "Is pl_x running?"'),
        AIMessage(content="", tool_calls=[_call("c1", name)]),
        ToolMessage(content=content, tool_call_id="c1", name=name, **tool_kwargs),
    ]


def test_a_read_result_carries_the_requirement_in_the_view_only() -> None:
    messages = _task_turn()
    request = _FakeModelRequest(messages=messages)
    forwarded = _forwarded(request)

    patched = forwarded.messages[-1]
    assert patched.content == f"{UNTAGGED}\n\n{keep.KEEP_REQUIREMENT}"
    assert patched.tool_call_id == "c1" and patched.status == "success"
    assert forwarded.messages[:2] == messages[:2]
    # View-only: the persisted message and the request's own list are untouched.
    assert messages[-1].content == RESULT
    assert request.messages is messages and len(messages) == 3


def test_the_requirement_names_what_went_missing_in_run_7() -> None:
    text = keep.KEEP_REQUIREMENT
    assert text.startswith("KEEP REQUIREMENT (an instruction to you, not text for the user)")
    # Only a result the answer uses: an exploratory call is not forced into it.
    assert "if your answer uses the result above" in text
    assert "as the tool wrote them, in their order" in text
    assert "GROUP A and GROUP B stay two separate groups" in text  # UC1-09
    assert "(Status, Last Run, start, end, runtime, factory, folder)" in text  # UC1-01
    assert "and its [open run] link" in text  # UC1-06
    assert "'Completion estimate'" in text  # UC3-02/03
    assert "'Recommendation'" in text  # UC4-01
    assert "factories-searched" in text  # UC8-10
    assert "closest-names lines" in text  # UC2-08
    assert "never drop, merge or summarize a line" in text
    # What stays out: the tag, the model-directed lines, argument names (UC8-11).
    assert "Leave out only an ANSWER REQUIREMENT" in text
    # The view drops the tag itself (run 8), so the reminder need not name it.
    assert "[adf-agent]" not in text
    assert "any sentence naming a tool or an argument (opened_at=, factory=)" in text


def test_the_tag_is_dropped_from_every_line_in_the_view_only() -> None:
    """SRCH-03, run 8: a tagged run listing was copied into the answer whole."""

    tagged = (
        "[adf-agent] NOTE: 'PL_X' is not in factory 'adf-fin'; using 'pl_x' from 'adf-risk'.\n"
        "[adf-agent] 1 run(s) (newest first) in factory 'adf-risk' for 'pl_x'\n"
        "  - runId=r1 | pl_x | Succeeded | [open run](<https://adf.azure.com/r1>)\n"
        "[adf-agent] ERROR querying runs in factory 'adf-fin': timed out"
    )
    messages = _task_turn(tagged, name="list_pipeline_runs")
    forwarded = _forwarded(
        _FakeModelRequest(messages=messages), _middleware(("list_pipeline_runs",))
    )

    content = forwarded.messages[-1].content
    assert "[adf-agent]" not in content
    assert content == (
        "NOTE: 'PL_X' is not in factory 'adf-fin'; using 'pl_x' from 'adf-risk'.\n"
        "1 run(s) (newest first) in factory 'adf-risk' for 'pl_x'\n"
        "  - runId=r1 | pl_x | Succeeded | [open run](<https://adf.azure.com/r1>)\n"
        "ERROR querying runs in factory 'adf-fin': timed out"
        f"\n\n{keep.KEEP_REQUIREMENT}"
    )
    # View-only: the stored result keeps the tool's own text, tags included.
    assert messages[-1].content == tagged


def test_write_tools_are_never_patched_even_when_passed_in() -> None:
    """Rule 9 reports a refused change by its outcome alone: no keep-every-line."""

    middleware = AdfResultKeepMiddleware(
        (*READ_TOOLS, "manage_trigger_states", "rerun_failed_pipeline")
    )
    assert middleware.tool_names == frozenset(READ_TOOLS)
    for name in keep.WRITE_TOOL_NAMES:
        request = _FakeModelRequest(messages=_task_turn("tr_a was not enabled.", name=name))
        assert _forwarded(request, middleware) is request


def test_other_tools_are_left_alone() -> None:
    request = _FakeModelRequest(
        messages=[
            HumanMessage(content='User asked: "What is the SLA for pl_x?"'),
            AIMessage(
                content="",
                tool_calls=[_call("k1", "ai_search_tool"), _call("f1", "read_file")],
            ),
            ToolMessage(content="[1] SLA document …", tool_call_id="k1"),
            ToolMessage(content="file body", tool_call_id="f1"),
        ]
    )
    assert _forwarded(request) is request


def test_parallel_results_patch_only_the_read_tools() -> None:
    request = _FakeModelRequest(
        messages=[
            HumanMessage(content='User asked: "Is the SLA for pl_x realistic?"'),
            AIMessage(
                content="",
                tool_calls=[
                    _call("k1", "ai_search_tool"),
                    _call("a1", "get_pipeline_runtime_estimate"),
                ],
            ),
            ToolMessage(content="[1] SLA document …", tool_call_id="k1"),
            ToolMessage(content=RESULT, tool_call_id="a1"),
        ]
    )
    forwarded = _forwarded(request)
    assert forwarded.messages[2].content == "[1] SLA document …"
    assert forwarded.messages[3].content.endswith(keep.KEEP_REQUIREMENT)


def test_a_failed_empty_or_non_text_result_is_left_alone() -> None:
    for messages in (
        _task_turn("ValueError: boom", status="error"),
        _task_turn(""),
        _task_turn("   \n"),
        _task_turn("[adf-agent] \n"),
        _task_turn([{"type": "text", "text": RESULT}]),
    ):
        request = _FakeModelRequest(messages=messages)
        assert _forwarded(request) is request


def test_only_the_current_task_is_patched() -> None:
    earlier = _task_turn()
    earlier.append(AIMessage(content="pl_x is not currently running …"))

    # A later message with no ADF call since: the earlier result is history.
    request = _FakeModelRequest(messages=[*earlier, HumanMessage(content="thanks")])
    assert _forwarded(request) is request

    # A later message with a new call: only the new result carries it.
    request = _FakeModelRequest(
        messages=[
            *earlier,
            HumanMessage(content='User asked: "And pl_y?"'),
            AIMessage(content="", tool_calls=[_call("c2", "get_pipeline_runtime_estimate")]),
            ToolMessage(content="second result", tool_call_id="c2"),
        ]
    )
    forwarded = _forwarded(request)
    assert forwarded.messages[2].content == RESULT
    assert forwarded.messages[-1].content == f"second result\n\n{keep.KEEP_REQUIREMENT}"


def test_every_read_result_of_the_task_is_patched_once() -> None:
    """Two read calls in one task, then a model call after a search."""

    request = _FakeModelRequest(
        messages=[
            HumanMessage(content='User asked: "Which pipeline loads LOANSYS?"'),
            AIMessage(content="", tool_calls=[_call("a1", "discover_pipelines_by_source_system")]),
            ToolMessage(content="first", tool_call_id="a1"),
            AIMessage(content="", tool_calls=[_call("a2", "get_pipeline_runtime_estimate")]),
            ToolMessage(content="second", tool_call_id="a2"),
            AIMessage(content="", tool_calls=[_call("k1", "ai_search_tool")]),
            ToolMessage(content="[1] KB …", tool_call_id="k1"),
        ]
    )
    forwarded = _forwarded(request)
    contents = [m.content for m in forwarded.messages]
    assert contents[2] == f"first\n\n{keep.KEEP_REQUIREMENT}"
    assert contents[4] == f"second\n\n{keep.KEEP_REQUIREMENT}"
    assert contents[6] == "[1] KB …"
    assert sum(c.count("KEEP REQUIREMENT") for c in contents) == 2


def test_no_human_message_or_no_messages() -> None:
    for messages in ([], [HumanMessage(content="hi")], _task_turn()[1:]):
        request = _FakeModelRequest(messages=messages)
        forwarded = _forwarded(request)
        if messages and isinstance(messages[0], AIMessage):
            # No task message in view (e.g. summarized away): the whole view is
            # the task, and its read result is still patched.
            assert forwarded.messages[-1].content.endswith(keep.KEEP_REQUIREMENT)
        else:
            assert forwarded is request


def test_the_async_path_patches_the_same_way() -> None:
    seen: list[_FakeModelRequest] = []

    async def handler(request: _FakeModelRequest) -> str:
        seen.append(request)
        return "response"

    request = _FakeModelRequest(messages=_task_turn())
    assert asyncio.run(_middleware().awrap_model_call(request, handler)) == "response"
    assert seen[0].messages[-1].content == f"{UNTAGGED}\n\n{keep.KEEP_REQUIREMENT}"


# --- the copy check on the subagent's answer ----------------------------------

ROW_ID = "06b3a9f2-c8cf-11f1-8969-5e2d7c1b9a40"
LISTED = (
    "[adf-agent] Factory 'example-data-factory': 1 pipeline(s)\n"
    f"- pl_A1_NightlyCoreIngest | Failed | 2026-10-15 19:30:06 UTC | runId={ROW_ID}"
)
DOUBLED = LISTED.replace(ROW_ID, "06b3a9f2-c8cf-11f1-11f1-8969-5e2d7c1b9a40")
RERUN_ID = "9d41c7e0-c9a1-11f1-8a2b-5e2d7c1b9a40"
RERAN = f"Rerun of 'pl_A1_NightlyCoreIngest' started: new runId={RERUN_ID}"


def _answered(answer: Any, messages: list) -> AIMessage:
    """The subagent's answer as the middleware hands it on."""

    response = _middleware().wrap_model_call(
        _FakeModelRequest(messages=messages),
        lambda r: ModelResponse(result=[AIMessage(content=answer, id="answer-1")]),
    )
    (message,) = response.result
    return message


def test_a_run_id_the_subagent_changed_is_put_back() -> None:
    # SUP-06 (run 8's re-run): the subagent wrote the run's third group twice.
    message = _answered(DOUBLED, _task_turn(LISTED, name="list_pipelines"))
    assert message.content == LISTED
    assert message.id == "answer-1"
    # A write tool gets no KEEP REQUIREMENT, but its result is copied from too.
    turn = _task_turn(RERAN, name="rerun_failed_pipeline")
    assert _answered(RERAN.replace("-8a2b-", "-8969-"), turn).content == RERAN


def test_a_name_the_subagent_changed_is_put_back() -> None:
    # The reply's slip in QA-07, should the subagent make it: a separator written twice.
    slipped = LISTED.replace("pl_A1_NightlyCoreIngest", "pl_A1__NightlyCoreIngest")
    assert _answered(slipped, _task_turn(LISTED, name="list_pipelines")).content == LISTED
    # The user's spelling is theirs.
    turn = _task_turn(LISTED, name="list_pipelines")
    turn[0] = HumanMessage(content="Is pl_A1__NightlyCoreIngest running?")
    assert _answered(slipped, turn).content == slipped


def test_only_the_final_answer_is_checked_against_usable_results() -> None:
    calling = AIMessage(content=DOUBLED, tool_calls=[_call("c2", "list_pipeline_runs")])
    response = _middleware().wrap_model_call(
        _FakeModelRequest(messages=_task_turn(LISTED, name="list_pipelines")),
        lambda r: ModelResponse(result=[calling]),
    )
    assert response.result[0] is calling
    # A failed call is not copied from, and an earlier task's result is not this task's.
    failed = _task_turn(LISTED, name="list_pipelines", status="error")
    assert _answered(DOUBLED, failed).content == DOUBLED
    earlier = [*_task_turn(LISTED, name="list_pipelines"), AIMessage(content=LISTED)]
    later = [
        *earlier,
        HumanMessage(content="And pl_y?"),
        AIMessage(content="", tool_calls=[_call("c9", "get_pipeline_runtime_estimate")]),
        ToolMessage(content=RESULT, tool_call_id="c9", name="get_pipeline_runtime_estimate"),
    ]
    assert _answered(DOUBLED, later).content == DOUBLED


def test_the_async_path_checks_the_answer_too() -> None:
    async def handler(request: _FakeModelRequest) -> ModelResponse:
        return ModelResponse(result=[AIMessage(content=DOUBLED)])

    request = _FakeModelRequest(messages=_task_turn(LISTED, name="list_pipelines"))
    response = asyncio.run(_middleware().awrap_model_call(request, handler))
    assert response.result[0].content == LISTED


def test_the_adf_subagent_wires_it_over_its_read_tools() -> None:
    from v1.core.subagents import ADF_SUBAGENT
    from v1.core.tools import ADF_TOOLS

    wired = [m for m in ADF_SUBAGENT["middleware"] if isinstance(m, AdfResultKeepMiddleware)]
    assert len(wired) == 1
    names = {tool.name for tool in ADF_TOOLS}
    assert wired[0].tool_names == names - keep.WRITE_TOOL_NAMES
    # Every write tool the subagent carries is one the middleware leaves out.
    assert keep.WRITE_TOOL_NAMES <= names
    assert "ai_search_tool" not in wired[0].tool_names
    assert "get_pipeline_runtime_estimate" in wired[0].tool_names


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
