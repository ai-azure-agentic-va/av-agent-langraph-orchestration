"""Regression tests for the view-only relay requirement on ``adf-agent`` answers.

The orchestrator re-titled an ``adf-agent`` answer and dropped its opening lines
(UC1-07, run 5), so the model call that writes the reply now sees a RELAY
REQUIREMENT beside each of the current turn's ``adf-agent`` answers. Only that
call's view changes: persisted messages, earlier turns, other subagents and
tools, and a blocked or failed ``task`` call are left exactly as they were.

The same middleware runs :mod:`v1.core.middlewares.copy_check` on the final
reply's copy of those answers: a run ID given another run's ending, a run's date
changed on its row (UC2-07, SUP-16, SUP-06, SUP-02), a name with a hyphen
written twice (QA-07) and a follow-up suggestion naming a trigger as the user
typed it (QA-18) are put back, and nothing else is touched.

Runs standalone (``python test_subagent_relay.py``) or under pytest.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from langchain.agents.middleware import ExtendedModelResponse, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import v1.core.middlewares.subagent_relay as relay
from v1.core.middlewares import SubagentRelayMiddleware

ANSWER = (
    "Source system taken from the view name: LOANSYS.\n\n"
    "5 active candidate pipelines (ran in the last 30 days) …"
)


# --- lightweight stand-in so the test stays offline (no real ModelRequest) ---


@dataclass
class _FakeModelRequest:
    messages: list

    def override(self, **overrides: Any) -> _FakeModelRequest:
        return replace(self, **overrides)


def _task(call_id: str, subagent_type: str) -> dict:
    return {
        "name": relay.TASK_TOOL_NAME,
        "args": {"description": "User asked: which pipeline…", "subagent_type": subagent_type},
        "id": call_id,
        "type": "tool_call",
    }


def _forwarded(request: _FakeModelRequest) -> _FakeModelRequest:
    seen: list[_FakeModelRequest] = []
    SubagentRelayMiddleware().wrap_model_call(request, lambda r: seen.append(r) or "response")
    return seen[0]


def _adf_turn(content: Any = ANSWER, **tool_kwargs: Any) -> list:
    return [
        HumanMessage(content="Which pipeline populates LOANSYS_ACCOUNTS_VW?"),
        AIMessage(content="", tool_calls=[_task("c1", relay.ADF_SUBAGENT_NAME)]),
        ToolMessage(content=content, tool_call_id="c1", **tool_kwargs),
    ]


def test_this_turns_adf_answer_carries_the_requirement() -> None:
    messages = _adf_turn()
    request = _FakeModelRequest(messages=messages)
    forwarded = _forwarded(request)

    patched = forwarded.messages[-1]
    assert patched.content == f"{ANSWER}\n\n{relay.RELAY_REQUIREMENT}"
    assert patched.tool_call_id == "c1" and patched.status == "success"
    assert forwarded.messages[:2] == messages[:2]
    # View-only: the persisted message and the request's own list are untouched.
    assert messages[-1].content == ANSWER
    assert request.messages is messages and len(messages) == 3


def test_the_requirement_names_what_went_missing() -> None:
    text = relay.RELAY_REQUIREMENT
    assert text.startswith("RELAY REQUIREMENT (an instruction to you, not text for the user)")
    assert "its first line, then every heading, group, row, note, count and link" in text
    assert "Never open with an answer, title or summary of your own" in text
    assert "this answer still goes in whole, as its own part" in text
    assert "A refused change is still reported by its outcome alone." in text
    # Run 6: a ServiceNow verdict opened UC8-09, and follow-ups renamed a
    # pipeline by the user's folder path (SRCH-03) or offered to diagnose
    # runs that had not failed (UC8-07).
    assert "a result that disagrees with it is given under its own source" in text
    assert "never as a verdict opening the reply" in text
    assert "name each factory, pipeline and trigger exactly as this answer names it" in text
    assert "never a shorthand, spelling or folder path the user typed" in text
    assert "never offer to diagnose a failure this answer does not show" in text
    # Run 7: QA-18 offered the user's TRIG_FX_… spelling; UC2-02 offered to
    # validate a run that had succeeded, UC2-03 to count the rejected run.
    assert "never offer to validate the recovery of a run that did not fail" in text
    assert "or to count a run the answer rejected as a recovery" in text


def test_other_subagents_and_tools_are_left_alone() -> None:
    request = _FakeModelRequest(
        messages=[
            HumanMessage(content="Show INC0003609 and search the KB for the error"),
            AIMessage(
                content="",
                tool_calls=[
                    _task("s1", "servicenow-ticket-agent"),
                    {"name": "ai_search_tool", "args": {"query": "x"}, "id": "k1"},
                ],
            ),
            ToolMessage(content="INC0003609 …", tool_call_id="s1"),
            ToolMessage(content="[1] KB article …", tool_call_id="k1"),
        ]
    )
    assert _forwarded(request) is request


def test_parallel_results_patch_only_the_adf_answer() -> None:
    request = _FakeModelRequest(
        messages=[
            HumanMessage(content="Investigate INC0003609"),
            AIMessage(
                content="",
                tool_calls=[
                    _task("s1", "servicenow-ticket-agent"),
                    _task("a1", relay.ADF_SUBAGENT_NAME),
                ],
            ),
            ToolMessage(content="INC0003609 …", tool_call_id="s1"),
            ToolMessage(content=ANSWER, tool_call_id="a1"),
        ]
    )
    forwarded = _forwarded(request)
    assert forwarded.messages[2].content == "INC0003609 …"
    assert forwarded.messages[3].content.endswith(relay.RELAY_REQUIREMENT)


def test_a_blocked_failed_or_empty_call_is_left_alone() -> None:
    blocked = "Data Factory pipeline lookup is not available for your access."
    for messages in (
        _adf_turn(blocked, status="error"),
        _adf_turn(""),
        _adf_turn("   \n"),
        _adf_turn([{"type": "text", "text": ANSWER}]),
    ):
        request = _FakeModelRequest(messages=messages)
        assert _forwarded(request) is request


def test_only_the_current_turn_is_patched() -> None:
    earlier = _adf_turn()
    earlier.append(AIMessage(content="Here is what adf-agent found …"))

    # A later turn that never called adf-agent: the earlier answer is history.
    request = _FakeModelRequest(messages=[*earlier, HumanMessage(content="thanks")])
    assert _forwarded(request) is request

    # A later turn that called it again: only the new answer carries it.
    request = _FakeModelRequest(
        messages=[
            *earlier,
            HumanMessage(content="And for LOANSYS_CREDIT_HISTORY_VW?"),
            AIMessage(content="", tool_calls=[_task("c2", relay.ADF_SUBAGENT_NAME)]),
            ToolMessage(content="second answer", tool_call_id="c2"),
        ]
    )
    forwarded = _forwarded(request)
    assert forwarded.messages[2].content == ANSWER
    assert forwarded.messages[-1].content == f"second answer\n\n{relay.RELAY_REQUIREMENT}"


def test_every_adf_answer_of_the_turn_is_patched_once() -> None:
    """Two adf-agent calls in one turn, then a model call after a third tool."""

    request = _FakeModelRequest(
        messages=[
            HumanMessage(content="Compare the two factories"),
            AIMessage(content="", tool_calls=[_task("a1", relay.ADF_SUBAGENT_NAME)]),
            ToolMessage(content="first", tool_call_id="a1"),
            AIMessage(content="", tool_calls=[_task("a2", relay.ADF_SUBAGENT_NAME)]),
            ToolMessage(content="second", tool_call_id="a2"),
            AIMessage(content="", tool_calls=[{"name": "ai_search_tool", "args": {}, "id": "k1"}]),
            ToolMessage(content="[1] KB …", tool_call_id="k1"),
        ]
    )
    forwarded = _forwarded(request)
    contents = [m.content for m in forwarded.messages]
    assert contents[2] == f"first\n\n{relay.RELAY_REQUIREMENT}"
    assert contents[4] == f"second\n\n{relay.RELAY_REQUIREMENT}"
    assert contents[6] == "[1] KB …"
    assert sum(c.count("RELAY REQUIREMENT") for c in contents) == 2


def test_no_human_message_or_no_messages() -> None:
    for messages in ([], [HumanMessage(content="hi")], _adf_turn()[1:]):
        request = _FakeModelRequest(messages=messages)
        forwarded = _forwarded(request)
        if messages and isinstance(messages[0], AIMessage):
            # No user message in view (e.g. summarized away): the whole view is
            # the turn, and its adf-agent answer is still patched.
            assert forwarded.messages[-1].content.endswith(relay.RELAY_REQUIREMENT)
        else:
            assert forwarded is request


def test_the_async_path_patches_the_same_way() -> None:
    seen: list[_FakeModelRequest] = []

    async def handler(request: _FakeModelRequest) -> str:
        seen.append(request)
        return "response"

    request = _FakeModelRequest(messages=_adf_turn())
    assert asyncio.run(SubagentRelayMiddleware().awrap_model_call(request, handler)) == "response"
    assert seen[0].messages[-1].content == f"{ANSWER}\n\n{relay.RELAY_REQUIREMENT}"


# --- the copy check -----------------------------------------------------------

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


def _replied(reply: Any, messages: list | None = None) -> AIMessage:
    """The reply as the middleware hands it on, for a turn whose answer is LISTING."""

    request = _FakeModelRequest(messages=messages if messages is not None else _adf_turn(LISTING))
    response = SubagentRelayMiddleware().wrap_model_call(
        request, lambda r: ModelResponse(result=[AIMessage(content=reply, id="reply-1")])
    )
    (message,) = response.result
    return message


def test_a_run_id_given_another_runs_ending_is_put_back() -> None:
    # SUP-16 (run 4), SUP-06 (run 8) and SUP-02 (its re-run) all wrote this
    # random ID with the fixtures' common ending, so its link opened no run.
    slipped = LISTING.replace(RANDOM, "7c1e9a40-2b6d-4f3a-9e21-5d8c-7a1b2c3d4e5f")
    message = _replied(slipped)
    assert message.content == LISTING
    assert message.id == "reply-1"
    # UC2-07 (run 3): another run's fourth group.
    assert _replied(LISTING.replace(FAILED, "3a9d1e22-c8cf-11f1-baba-7a1b2c3d4e5f")).content == LISTING
    # A group written twice (the subagent's slip in SUP-06's re-run), or dropped.
    assert _replied(LISTING.replace(FAILED, "3a9d1e22-c8cf-11f1-11f1-84a8-7a1b2c3d4e5f")).content == LISTING
    assert _replied(LISTING.replace(FAILED, "3a9d1e22-c8cf-11f1-7a1b2c3d4e5f")).content == LISTING
    # Written in capitals, a known ID is still known; a slip is put back as written in the answer.
    assert _replied(LISTING.replace(DAILY, DAILY.upper())).content == LISTING.replace(DAILY, DAILY.upper())
    assert _replied(LISTING.replace(FAILED, "3A9D1E22-C8CF-11F1-BABA-7A1B2C3D4E5F")).content == LISTING


def test_a_run_date_changed_on_its_row_is_put_back() -> None:
    # SUP-02 (run 8): 2026-10-14 became 2026-10-10 on its row; the bold stays.
    assert _replied(LISTING.replace("2026-10-14 17:01:30", "2026-10-10 17:01:30")).content == LISTING
    row = f"Latest: 2026-10-10T17:01:30Z (Succeeded) {_link(DAILY)}"
    assert _replied(row).content == f"Latest: 2026-10-14T17:01:30Z (Succeeded) {_link(DAILY)}"
    # Both slips on one row: the ID first, then the date it carries.
    both = LISTING.replace(DAILY, "0f5e2a10-c7f0-11f1-a9a3-7a1b2c3d4e00").replace(
        "2026-10-14 17:01:30", "2026-10-11 17:01:30"
    )
    assert _replied(both).content == LISTING


def test_what_the_user_or_the_model_wrote_is_left_alone() -> None:
    typed = "3a9d1e22-c8cf-11f1-84a8-000000000000"
    turn = _adf_turn(LISTING)
    turn[0] = HumanMessage(
        content=f"Validate 00000000-0000-0000-0000-000000000000 and {typed}; "
        "the ticket says it ran 2026-10-10 17:01:30 UTC"
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
    for reply in kept:
        assert _replied(reply, turn).content == reply, reply

    # Two runs in the answer share the first three groups: no way to tell.
    twin = "3a9d1e22-c8cf-11f1-0000-111111111111"
    answer = f"{LISTING}\n- pl_twin | last run 2026-10-10 09:12:05 UTC | {_link(twin)}"
    slipped = f"Failed run 3a9d1e22-c8cf-11f1-baba-7a1b2c3d4e5f {_link(FAILED)}"
    assert _replied(slipped, _adf_turn(answer)).content == slipped
    # The answer has the run at two times with the same clock: no way to tell.
    answer = f"{LISTING}\n- earlier: {DAILY} 2026-10-13 17:01:30 UTC"
    row = f"{DAILY} last ran 2026-10-12 17:01:30 UTC"
    assert _replied(row, _adf_turn(answer)).content == row


def test_the_subscription_in_a_link_is_not_a_run() -> None:
    # Real links carry the subscription's ID in their factory path. Read as a
    # run, it would tie every linked row together and lend pl_B pl_A's time.
    subscription = "5b3e1f00-9a2c-4d7e-8f10-2c4b6a8e0d13"
    answer = (
        f"- pl_A | last run 2026-10-15 19:31:40 UTC | {_link(RANDOM, subscription)}\n"
        f"- pl_B | {_link(DAILY, subscription)}"
    )
    row = f"pl_B ran 2026-10-12 19:31:40 UTC | {_link(DAILY, subscription)}"
    assert _replied(row, _adf_turn(answer)).content == row
    # The rows' own slips are still put back.
    slipped = f"pl_A ran 2026-10-11 19:31:40 UTC | {_link(RANDOM[:-4] + '0000', subscription)}"
    fixed = f"pl_A ran 2026-10-15 19:31:40 UTC | {_link(RANDOM, subscription)}"
    assert _replied(slipped, _adf_turn(answer)).content == fixed


NAME = "pl-100-00-ex-surveyco-daily-ingest_data"


def _monitor(label: str) -> str:
    return (
        f"in ADF Studio: [open Monitor filtered to '{label}'](<https://adf.azure.com/en/monitoring/"
        "pipelineruns?factory=/subscriptions/<sub>/resourceGroups/<rg>/providers/"
        f"Microsoft.DataFactory/factories/example-data-factory&filter.pipelinename={NAME}>)"
    )


def test_a_name_with_a_separator_written_twice_is_put_back() -> None:
    # QA-07 (run 3 and run 8's re-run): the reply labelled the Monitor link with
    # a hyphen written twice; the link itself kept the name.
    answer = f"{LISTING}\n{_monitor(NAME)}"
    slipped = f"{LISTING}\n{_monitor('pl-100-00-ex--surveyco-daily-ingest_data')}"
    assert _replied(slipped, _adf_turn(answer)).content == answer
    # Any run of either separator, and one the answer writes twice written once.
    assert _replied(LISTING.replace("pl_UC2_Recovery", "pl__UC2___Recovery")).content == LISTING
    doubled = LISTING.replace("pl_UC2_Recovery", "pl__UC2_Recovery")
    assert _replied(LISTING, _adf_turn(doubled)).content == doubled
    # A run ID with a hyphen written twice, and then the date changed on its row.
    slipped = LISTING.replace(DAILY, DAILY.replace("-", "--", 1)).replace(
        "2026-10-14 17:01:30", "2026-10-11 17:01:30"
    )
    assert _replied(slipped).content == LISTING


def test_a_name_changed_any_other_way_is_left_alone() -> None:
    answer = f"{LISTING}\nRe-check pl_UC2_Recovery once pl_twin_a and pl__twin_a are fixed."
    turn = _adf_turn(answer)
    turn[0] = HumanMessage(content="Did pl-100-00-ex--surveyco-daily-ingest_data run today?")
    kept = [
        # The user's spelling, though it reads like the slip.
        "No pipeline is named pl-100-00-ex--surveyco-daily-ingest_data.",
        # A letter or a digit changed, its case, or '-' written for '_'.
        "pl-100-01-ex-surveyco-daily-ingest_data has not run.",
        "PL_UC2_RECOVERY failed; re-check it.",
        "pl-UC2-Recovery failed.",
        # Two names in the answer read the same once squeezed: no way to tell.
        "pl___twin_a is fixed.",
        # Words of the model's own, with a dash written as two hyphens.
        "The run--not the pipeline--failed; a follow-up re-run is due.",
        # Part of a longer token: a name is read whole or not at all.
        "Its output is pl__UC2_Recovery_.csv.",
    ]
    for reply in kept:
        assert _replied(reply, turn).content == reply, reply


TRIGGER = "trig-fx-60-00-ex-ops-daily-ingest_data"
TYPED = "TRIG_FX_60_00_EX_OPS_DAILY_INGEST_DATA"


def _follow_ups(*bullets: str, heading: str = "## Want to explore further?") -> str:
    return "\n".join([heading, *(f"- {bullet}" for bullet in bullets)])


def _typed_turn(answer: str, question: str = f"Enable trigger {TYPED}") -> list:
    turn = _adf_turn(answer)
    turn[0] = HumanMessage(content=question)
    return turn


def test_a_follow_up_names_a_trigger_as_the_answer_does() -> None:
    # QA-18 (all three write runs): the suggestions offered the user's spelling
    # of a trigger the answer named as published.
    answer = f"{TRIGGER} was not enabled."
    asked = [
        f"Show me the current state of trigger {TYPED}",
        "List all enabled and disabled triggers in factory fileShare-to-ADF",
        f"Show me the most recent runs started by trigger {TYPED.lower()}",
    ]
    named = [bullet.replace(TYPED, TRIGGER).replace(TYPED.lower(), TRIGGER) for bullet in asked]
    reply = f"{answer}\n\n{_follow_ups(*asked)}"
    assert _replied(reply, _typed_turn(answer)).content == f"{answer}\n\n{_follow_ups(*named)}"
    # The answer quoting the user's spelling too leaves one published name.
    noted = f"{answer}\nNOTE: '{TYPED}' matched trigger '{TRIGGER}' (separator-insensitive)."
    reply = f"{noted}\n\n{_follow_ups(*asked)}"
    assert _replied(reply, _typed_turn(noted)).content == f"{noted}\n\n{_follow_ups(*named)}"
    # The heading as the UI reads it, in bold or at another level.
    for heading in ("**Want to explore further?**", "### Want to explore further", "Want to explore further:"):
        reply = f"{answer}\n\n{_follow_ups(*asked, heading=heading)}"
        fixed = f"{answer}\n\n{_follow_ups(*named, heading=heading)}"
        assert _replied(reply, _typed_turn(answer)).content == fixed, heading
    # Text ahead of the first bullet is passed over, as the UI passes it over.
    reply = f"{answer}\n\n## Want to explore further?\nYou could ask:\n\n- {asked[0]}"
    fixed = f"{answer}\n\n## Want to explore further?\nYou could ask:\n\n- {named[0]}"
    assert _replied(reply, _typed_turn(answer)).content == fixed


def test_only_the_follow_ups_the_ui_shows_are_renamed() -> None:
    answer = f"{TRIGGER} was not enabled."
    asked = f"Show me the current state of trigger {TYPED}"
    kept = [
        # The answer's own text: it can quote the user, as a NOTE does.
        f"No trigger is named exactly '{TYPED}'.\n\n{_follow_ups('List the triggers')}",
        # No suggestions, a fourth bullet, a bullet under the next heading or
        # past text that is not one: the UI shows none of those.
        f"{answer}\n- {asked}",
        f"{answer}\n\n{_follow_ups('a', 'b', 'c', asked)}",
        f"{answer}\n\n{_follow_ups('a')}\n## Sources\n- {asked}",
        f"{answer}\n\n## Want to explore further?\n## Sources\n- {asked}",
        f"{answer}\n\n{_follow_ups('a')}\nThat is all.\n- {asked}",
    ]
    for reply in kept:
        assert _replied(reply, _typed_turn(answer)).content == reply, reply
    # A name the answer does not hold, two it holds that read the same, the
    # user's spelling as the only one it holds, and a spelling of the model's
    # own: none is a name as the user typed it.
    for other, question, bullet in [
        (answer, "Enable PL_60_00_EX_OPS", "Show me the runs of pipeline PL_60_00_EX_OPS"),
        (f"{answer}\ntr-a and tr_a were not enabled.", "Enable TR_A", "Enable TR_A in factory adf-fin"),
        (f"No trigger is named exactly '{TYPED}'.", f"Enable trigger {TYPED}", asked),
        (answer, "Which triggers are disabled?", asked),
    ]:
        reply = f"{other}\n\n{_follow_ups(bullet)}"
        assert _replied(reply, _typed_turn(other, question)).content == reply, bullet


def test_a_capital_opening_a_suggestion_is_not_a_spelling() -> None:
    # Replays of runs 3-8 renamed 'Re-run …' to the answer's 're-run' before
    # the rule took only the user's spellings; a user can type one too.
    answer = "pl_x failed at 09:00 and was re-run at 10:00."
    reply = f"{answer}\n\n{_follow_ups('Re-run pl_x for today')}"
    assert _replied(reply, _typed_turn(answer, "Re-run pl_x?")).content == reply
    # Any other change of case is the user's spelling: 'TR_A' for 'tr_a'.
    answer = "tr_a was not enabled."
    reply = f"{answer}\n\n{_follow_ups('Show the state of TR_A', 'Show the state of Tr_A')}"
    fixed = f"{answer}\n\n{_follow_ups('Show the state of tr_a', 'Show the state of tr_a')}"
    assert _replied(reply, _typed_turn(answer, "Enable TR_A")).content == fixed


def test_only_a_final_reply_in_an_adf_turn_is_checked() -> None:
    slipped = LISTING.replace(RANDOM, "7c1e9a40-2b6d-4f3a-9e21-5d8c-7a1b2c3d4e5f")
    request = _FakeModelRequest(messages=_adf_turn(LISTING))
    calling = AIMessage(content=slipped, tool_calls=[_task("c2", relay.ADF_SUBAGENT_NAME)])
    response = SubagentRelayMiddleware().wrap_model_call(
        request, lambda r: ModelResponse(result=[calling])
    )
    assert response.result[0] is calling

    # This turn asked only ServiceNow; the run ID came from an earlier turn.
    earlier = [*_adf_turn(LISTING), AIMessage(content=LISTING)]
    later = [
        *earlier,
        HumanMessage(content="Show INC0003609"),
        AIMessage(content="", tool_calls=[_task("s1", "servicenow-ticket-agent")]),
        ToolMessage(content="INC0003609 …", tool_call_id="s1"),
    ]
    assert _replied(slipped, later).content == slipped
    # This turn asked adf-agent something else; the run ID is the earlier answer's.
    again = [
        *earlier,
        HumanMessage(content="Is pl_x running?"),
        AIMessage(content="", tool_calls=[_task("c2", relay.ADF_SUBAGENT_NAME)]),
        ToolMessage(content=ANSWER, tool_call_id="c2"),
    ]
    assert _replied(slipped, again).content == slipped
    renamed = LISTING.replace("pl_UC2_Recovery", "pl__UC2_Recovery")
    assert _replied(renamed, again).content == renamed
    # A blocked adf-agent call has no answer to check against.
    blocked = _adf_turn("Data Factory pipeline lookup is not available.", status="error")
    assert _replied(slipped, blocked).content == slipped


def test_every_response_shape_and_text_block_is_checked() -> None:
    slipped = LISTING.replace(RANDOM, "7c1e9a40-2b6d-4f3a-9e21-5d8c-7a1b2c3d4e5f")
    middleware = SubagentRelayMiddleware()

    def through(response: Any) -> Any:
        return middleware.wrap_model_call(_FakeModelRequest(messages=_adf_turn(LISTING)), lambda r: response)

    assert through(AIMessage(content=slipped)).content == LISTING
    extended = through(ExtendedModelResponse(model_response=ModelResponse(result=[AIMessage(content=slipped)])))
    assert extended.model_response.result[0].content == LISTING

    reasoning = {"type": "reasoning", "summary": [f"saw {slipped[:40]}"]}
    blocks = [reasoning, {"type": "text", "text": slipped}, "plain"]
    fixed = _replied(blocks).content
    assert fixed == [reasoning, {"type": "text", "text": LISTING}, "plain"]

    clean = AIMessage(content=LISTING)
    assert through(ModelResponse(result=[clean])).result[0] is clean


def test_the_async_path_checks_the_copy_too() -> None:
    slipped = LISTING.replace("2026-10-14 17:01:30", "2026-10-10 17:01:30")

    async def handler(request: _FakeModelRequest) -> ModelResponse:
        return ModelResponse(result=[AIMessage(content=slipped)])

    request = _FakeModelRequest(messages=_adf_turn(LISTING))
    response = asyncio.run(SubagentRelayMiddleware().awrap_model_call(request, handler))
    assert response.result[0].content == LISTING


def test_the_orchestrator_wires_it_beside_the_access_gate() -> None:
    """Read from source: importing v1.core.agent builds the whole agent."""

    source = (Path(relay.__file__).parents[1] / "agent.py").read_text()
    assert "from v1.core.middlewares.subagent_relay import SubagentRelayMiddleware" in source
    access = source.index("SubagentAccessMiddleware(),")
    wired = source.index("SubagentRelayMiddleware(),")
    editing = source.index("ContextEditingMiddleware(")
    assert access < wired < editing


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
