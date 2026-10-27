"""Regression tests for the view-only keep requirement on ``adf-agent``'s tool results.

The subagent dropped lines its tools had printed while writing its answer (run 7:
a row's Status and [open run] link, the GROUP B heading, the 'Completion
estimate' line, the factories searched, the closest names), so each of its model
calls now sees a KEEP REQUIREMENT beside the current task's READ-tool results.
Only that call's view changes: persisted messages, earlier turns, the write
tools, other tools, and failed or empty results are left exactly as they were.

The subagent's final answer also goes through the copy check: a run ID it
changed while copying a result (SUP-06, run 8's re-run), a run's date changed on
its row (SUP-02) and a name with a separator written twice (QA-07) are put back,
and nothing else is touched.

Runs standalone (``python test_adf_result_keep.py``) or under pytest.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Any

from langchain.agents.middleware import ExtendedModelResponse, ModelResponse
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


# Shaped like the fixtures' run IDs: most share the ending of the host that timed
# them, and one is random.
RANDOM = "7c1e9a40-2b6d-4f3a-9e21-5d8c0b7a6f14"
DAILY = "0f5e2a10-c7f0-11f1-a9a3-7a1b2c3d4e5f"
FAILED = "3a9d1e22-c8cf-11f1-84a8-7a1b2c3d4e5f"


def _link(run_id: str, subscription: str = "<sub>") -> str:
    return (
        f"[open run](<https://adf.azure.com/en/monitoring/pipelineruns/{run_id}"
        f"?factory=/subscriptions/{subscription}/resourceGroups/<rg>/providers/"
        "Microsoft.DataFactory/factories/example-data-factory>)"
    )


LISTING = (
    "Factory example-data-factory: 3 ACTIVE pipeline(s), newest run first:\n\n"
    f"- pl_A2_LoadCoreTables — Mid-level parent. | last run 2026-10-15 19:31:40 UTC "
    f"(Failed) | {_link(RANDOM)}\n"
    f"- pl-100-00-ex-surveyco-daily-ingest_data — SurveyCo ingest. | last run "
    f"**2026-10-14 17:01:30 UTC** (Succeeded) | {_link(DAILY)}\n"
    f"- pl_UC2_Recovery — Recovery fixture. | last run 2026-10-10 09:12:05 UTC "
    f"(Failed) | {_link(FAILED)}"
)


def _listed(result: str = LISTING) -> list:
    return _task_turn(result, name="list_pipelines")


def _copied(answer: Any, messages: list | None = None) -> AIMessage:
    """The answer as the middleware hands it on, for a task whose result is LISTING."""

    return _answered(answer, messages if messages is not None else _listed())


def test_a_run_id_given_another_runs_ending_is_put_back() -> None:
    # The reply's slips in SUP-16 (run 4), SUP-06 (run 8) and SUP-02 (its
    # re-run), should the subagent make them: this random ID written with the
    # fixtures' common ending, so its link opened no run.
    slipped = LISTING.replace(RANDOM, "7c1e9a40-2b6d-4f3a-9e21-5d8c-7a1b2c3d4e5f")
    message = _copied(slipped)
    assert message.content == LISTING
    assert message.id == "answer-1"
    # UC2-07 (run 3): another run's fourth group.
    assert (
        _copied(LISTING.replace(FAILED, "3a9d1e22-c8cf-11f1-baba-7a1b2c3d4e5f")).content == LISTING
    )
    # A group written twice, or dropped.
    assert (
        _copied(LISTING.replace(FAILED, "3a9d1e22-c8cf-11f1-11f1-84a8-7a1b2c3d4e5f")).content
        == LISTING
    )
    assert _copied(LISTING.replace(FAILED, "3a9d1e22-c8cf-11f1-7a1b2c3d4e5f")).content == LISTING
    # Written in capitals, a known ID is still known; a slip is put back as the result writes it.
    assert _copied(LISTING.replace(DAILY, DAILY.upper())).content == LISTING.replace(
        DAILY, DAILY.upper()
    )
    assert (
        _copied(LISTING.replace(FAILED, "3A9D1E22-C8CF-11F1-BABA-7A1B2C3D4E5F")).content == LISTING
    )


def test_a_run_date_changed_on_its_row_is_put_back() -> None:
    # SUP-02 (run 8): 2026-10-14 became 2026-10-10 on its row; the bold stays.
    assert _copied(LISTING.replace("2026-10-14 17:01:30", "2026-10-10 17:01:30")).content == LISTING
    row = f"Latest: 2026-10-10T17:01:30Z (Succeeded) {_link(DAILY)}"
    assert _copied(row).content == f"Latest: 2026-10-14T17:01:30Z (Succeeded) {_link(DAILY)}"
    # Both slips on one row: the ID first, then the date it carries.
    both = LISTING.replace(DAILY, "0f5e2a10-c7f0-11f1-a9a3-7a1b2c3d4e00").replace(
        "2026-10-14 17:01:30", "2026-10-11 17:01:30"
    )
    assert _copied(both).content == LISTING


def test_what_the_user_or_the_model_wrote_is_left_alone() -> None:
    typed = "3a9d1e22-c8cf-11f1-84a8-000000000000"
    turn = _listed()
    turn[0] = HumanMessage(
        content=f'User asked: "Validate 00000000-0000-0000-0000-000000000000 and {typed}; '
        'the ticket says it ran 2026-10-10 17:01:30 UTC"'
    )
    kept = [
        # IDs and a time the user typed, though they look like slips.
        f"Run {typed} and 00000000-0000-0000-0000-000000000000 were not found.",
        f"The ticket's 2026-10-10 17:01:30 UTC is not {DAILY}'s 2026-10-14 17:01:30 UTC.",
        # An ID cut short on purpose, and one with a word joined on.
        "Latest run 0f5e2a10-c7f0-11f1-a9a3-7a1b… succeeded.",
        f"Its log is {FAILED}-details.txt.",
        # Times of the model's own: a window on a line with no run ID, another
        # time zone, and a next run beside the row's own time.
        "Searched 2026-10-15 19:15:00 to 2026-10-15 20:15:00 UTC.",
        f"{DAILY} ran 2026-10-14 13:01:30 EDT.",
        f"{DAILY} ran 2026-10-14 17:01:30 UTC; next 2026-10-15 17:01:30 UTC.",
        f"{DAILY} is due again 2026-10-15 18:00:00 UTC.",
        # The user's time on the run's row, without the row's own time.
        f"For {DAILY} the ticket says 2026-10-10 17:01:30 UTC.",
        # Another run that only shares a first group.
        "Run 7c1e9a40-0000-4000-8000-000000000000 was not found.",
    ]
    for answer in kept:
        assert _copied(answer, turn).content == answer, answer

    # Two runs in the result share the first three groups: no way to tell.
    twin = "3a9d1e22-c8cf-11f1-0000-111111111111"
    result = f"{LISTING}\n- pl_twin | last run 2026-10-10 09:12:05 UTC | {_link(twin)}"
    slipped = f"Failed run 3a9d1e22-c8cf-11f1-baba-7a1b2c3d4e5f {_link(FAILED)}"
    assert _copied(slipped, _listed(result)).content == slipped
    # The result has the run at two times with the same clock: no way to tell.
    result = f"{LISTING}\n- earlier: {DAILY} 2026-10-13 17:01:30 UTC"
    row = f"{DAILY} last ran 2026-10-12 17:01:30 UTC"
    assert _copied(row, _listed(result)).content == row


def test_the_subscription_in_a_link_is_not_a_run() -> None:
    # Real links carry the subscription's ID in their factory path. Read as a
    # run, it would tie every linked row together and lend pl_B pl_A's time.
    subscription = "5b3e1f00-9a2c-4d7e-8f10-2c4b6a8e0d13"
    result = (
        f"- pl_A | last run 2026-10-15 19:31:40 UTC | {_link(RANDOM, subscription)}\n"
        f"- pl_B | {_link(DAILY, subscription)}"
    )
    row = f"pl_B ran 2026-10-12 19:31:40 UTC | {_link(DAILY, subscription)}"
    assert _copied(row, _listed(result)).content == row
    # The rows' own slips are still put back.
    slipped = f"pl_A ran 2026-10-11 19:31:40 UTC | {_link(RANDOM[:-4] + '0000', subscription)}"
    fixed = f"pl_A ran 2026-10-15 19:31:40 UTC | {_link(RANDOM, subscription)}"
    assert _copied(slipped, _listed(result)).content == fixed


NAME = "pl-100-00-ex-surveyco-daily-ingest_data"


def _monitor(label: str) -> str:
    return (
        f"in ADF Studio: [open Monitor filtered to '{label}'](<https://adf.azure.com/en/monitoring/"
        "pipelineruns?factory=/subscriptions/<sub>/resourceGroups/<rg>/providers/"
        f"Microsoft.DataFactory/factories/example-data-factory&filter.pipelinename={NAME}>)"
    )


def test_a_name_with_a_separator_written_twice_is_put_back() -> None:
    # QA-07 (run 3 and run 8's re-run): the Monitor link labelled with a hyphen
    # written twice; the link itself kept the name.
    result = f"{LISTING}\n{_monitor(NAME)}"
    slipped = f"{LISTING}\n{_monitor('pl-100-00-ex--surveyco-daily-ingest_data')}"
    assert _copied(slipped, _listed(result)).content == result
    # Any run of either separator, and one the result writes twice written once.
    assert _copied(LISTING.replace("pl_UC2_Recovery", "pl__UC2___Recovery")).content == LISTING
    doubled = LISTING.replace("pl_UC2_Recovery", "pl__UC2_Recovery")
    assert _copied(LISTING, _listed(doubled)).content == doubled
    # A run ID with a hyphen written twice, and then the date changed on its row.
    slipped = LISTING.replace(DAILY, DAILY.replace("-", "--", 1)).replace(
        "2026-10-14 17:01:30", "2026-10-11 17:01:30"
    )
    assert _copied(slipped).content == LISTING


def test_a_name_changed_any_other_way_is_left_alone() -> None:
    result = f"{LISTING}\nRe-check pl_UC2_Recovery once pl_twin_a and pl__twin_a are fixed."
    turn = _listed(result)
    turn[0] = HumanMessage(
        content='User asked: "Did pl-100-00-ex--surveyco-daily-ingest_data run today?"'
    )
    kept = [
        # The user's spelling, though it reads like the slip.
        "No pipeline is named pl-100-00-ex--surveyco-daily-ingest_data.",
        # A letter or a digit changed, its case, or '-' written for '_'.
        "pl-100-01-ex-surveyco-daily-ingest_data has not run.",
        "PL_UC2_RECOVERY failed; re-check it.",
        "pl-UC2-Recovery failed.",
        # Two names in the result read the same once squeezed: no way to tell.
        "pl___twin_a is fixed.",
        # Words of the model's own, with a dash written as two hyphens.
        "The run--not the pipeline--failed; a follow-up re-run is due.",
        # Part of a longer token: a name is read whole or not at all.
        "Its output is pl__UC2_Recovery_.csv.",
    ]
    for answer in kept:
        assert _copied(answer, turn).content == answer, answer


def test_every_response_shape_and_text_block_is_checked() -> None:
    slipped = LISTING.replace(RANDOM, "7c1e9a40-2b6d-4f3a-9e21-5d8c-7a1b2c3d4e5f")
    middleware = _middleware()

    def through(response: Any) -> Any:
        return middleware.wrap_model_call(_FakeModelRequest(messages=_listed()), lambda r: response)

    assert through(AIMessage(content=slipped)).content == LISTING
    extended = through(
        ExtendedModelResponse(model_response=ModelResponse(result=[AIMessage(content=slipped)]))
    )
    assert extended.model_response.result[0].content == LISTING

    reasoning = {"type": "reasoning", "summary": [f"saw {slipped[:40]}"]}
    blocks = [reasoning, {"type": "text", "text": slipped}, "plain"]
    fixed = _copied(blocks).content
    assert fixed == [reasoning, {"type": "text", "text": LISTING}, "plain"]

    clean = AIMessage(content=LISTING)
    assert through(ModelResponse(result=[clean])).result[0] is clean


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
