"""Regression tests for the Azure link check on final answers (QA #5).

On a generic pipeline, the link in the final answer opened ADF Studio's start
page (directory, subscription and factory pickers) instead of the failed run.
The ADF tools build every link from the factory the run was found in; a link a
model writes itself can drop the ``factory=``, take a ticket's Resource Group
GUID for the group's name, or open an ID that is no pipeline run (a generic
run's Triggered By).

So each ADF Studio link, and each Azure portal link that names a resource, in a
final answer is the run's own link, one the conversation holds, a held link put
back, or it is removed; text glued to a bare one is set apart, and an answer
left with nothing says that the link was left out. Every other link is left as
written. These build the held results with the ADF tools' own link helpers.

Runs standalone (``python test_azure_links.py``) or under pytest.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from langchain.agents.middleware import ExtendedModelResponse, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from test_patch_tool_calls import _stand_in_model, _user

import v1.core.agent as agent_mod
import v1.core.middlewares.azure_links as links
import v1.core.tools.adf.tools as adf_tools
from v1.core.middlewares import AzureLinksMiddleware
from v1.core.middlewares.subagent_access import TASK_TOOL_NAME
from v1.core.tools.ai_search import ai_search as ai_search_mod

SUB = "00000000-0000-4000-8000-000000000000"
RG = "rg-data-dev"
FACTORY = "df-sample"
OTHER_FACTORY = "df-archive"
GENERIC = "pl_generic_copy"
PARENT_PIPELINE = "pl_parent_orders"
CHILD = "3a9d1e22-c8cf-11f1-84a8-7a1b2c3d4e5f"
PARENT = "5b2c7d10-c8cf-11f1-84a8-7a1b2c3d4e5f"
# What ADF Monitor shows as a generic run's Triggered By: the parent's Execute
# Pipeline activity run, not a pipeline run.
TRIGGERED_BY = "9e8d7c6b-1111-4a2b-8c3d-0e1f2a3b4c5d"
# Another run of the generic pipeline, printed in a row of its own.
OTHER_RUN = "7c4e9f30-c8cf-11f1-84a8-7a1b2c3d4e5f"
# A ticket's 'Resource Group:' field: an ID, not the group's name.
TICKET_RG = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
ALERT = (
    "https://portal.azure.com/#view/Microsoft_Azure_Monitoring_Alerts/"
    f"AlertDetails.ReactView/alertId~/%2Fsubscriptions%2F{SUB}%2Fproviders%2F"
    "Microsoft.AlertsManagement%2Falerts%2Fc0ffee00-1234-4abc-8def-0123456789ab"
)


# --- lightweight stand-ins so the test stays offline (no real ModelRequest) ---


@dataclass
class _FakeModelRequest:
    messages: list

    def override(self, **overrides: Any) -> _FakeModelRequest:
        return replace(self, **overrides)


class _ParentRuns:
    async def get(self, rg: str, factory_name: str, run_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            status="Failed",
            run_start=datetime(2026, 10, 14, 4, 0, 5, tzinfo=UTC),
            parameters={"FileName": "orders.csv"},
            pipeline_name=PARENT_PIPELINE,
        )


def _child_run() -> SimpleNamespace:
    return SimpleNamespace(
        run_id=CHILD,
        pipeline_name=GENERIC,
        status="Failed",
        invoked_by=SimpleNamespace(
            name=TRIGGERED_BY,
            id=TRIGGERED_BY,
            invoked_by_type="PipelineActivity",
            pipeline_name=PARENT_PIPELINE,
            pipeline_run_id=PARENT,
        ),
    )


def _url(run_id: str, rg: str = RG, factory: str = FACTORY) -> str:
    return adf_tools._run_url(SUB, rg, factory, run_id)


def _recovery(rg: str = RG) -> str:
    """A recovery check of the generic run: its link, its parent's lines (with the
    parent run's link) and the Monitor footer, as the ADF tools print them."""

    client = SimpleNamespace(pipeline_runs=_ParentRuns())
    parent = asyncio.run(adf_tools._parent_run_lines(client, SUB, rg, FACTORY, _child_run()))
    return "\n".join(
        [
            f"[adf-agent] Recovery check for '{GENERIC}' in factory '{FACTORY}'",
            f"  Failed run      : {CHILD} {adf_tools._run_link(SUB, rg, FACTORY, CHILD)}",
            *parent,
            "  Verdict         : NOT RECOVERED",
            *adf_tools._monitor_footer(SUB, rg, FACTORY, pipeline_name=GENERIC),
        ]
    )


def _listing(factory: str = FACTORY) -> str:
    """A run listing's row: the parent appears as a parentRunId, unlinked."""

    return (
        f"[adf-agent] Runs of '{GENERIC}' in factory '{factory}':\n"
        f"  - runId={CHILD} | {GENERIC} | Failed | start=2026-10-14 04:02:11 UTC | "
        f"5120 ms | triggeredBy={adf_tools._invoked_text(_child_run())} | "
        f"{adf_tools._run_link(SUB, RG, factory, CHILD)}"
    )


def _card(*between: str) -> str:
    """get_pipeline_run_details' card for the generic run: its link on the header,
    its parentRunId in the triggeredBy field below it."""

    run = SimpleNamespace(
        **vars(_child_run()),
        run_start=None,
        run_end=None,
        duration_in_ms=5120,
        parameters={},
        message="",
    )
    return "\n".join(
        [
            (
                f"[adf-agent] Run {CHILD} (factory {FACTORY}) "
                f"{adf_tools._run_link(SUB, RG, FACTORY, CHILD)}"
            ),
            *between,
            *adf_tools._run_fields(run),
        ]
    )


def _ticket(description: str) -> str:
    """A ServiceNow ticket as its ToolMessage holds it: the tool's dict, as JSON."""

    return json.dumps(
        {
            "number": "INC0007001",
            "short_description": f"ADF pipeline {GENERIC} failed",
            "description": description,
        }
    )


TICKET = _ticket(
    f"Azure Monitor alert fired.\nSubscription ID: {SUB}\nResource Group: {TICKET_RG}\n"
    f"Alert: {ALERT}\n"
)


def _task(answer: str, subagent_type: str = "adf-agent") -> list:
    """The orchestrator's conversation: a subagent's answer as its task result."""

    call = {
        "name": TASK_TOOL_NAME,
        "args": {"description": "Check the run", "subagent_type": subagent_type},
        "id": "call_task",
        "type": "tool_call",
    }
    return [
        HumanMessage(content=f"Did {GENERIC} recover?"),
        AIMessage(content="", tool_calls=[call]),
        ToolMessage(content=answer, tool_call_id="call_task", name=TASK_TOOL_NAME),
    ]


def _turn(*results: str, name: str = "validate_pipeline_recovery", task: str = "") -> list:
    """A subagent's task and its tool calls, one result each."""

    messages: list = [HumanMessage(content=task or f"Did {GENERIC}'s failed run recover?")]
    for index, result in enumerate(results):
        call = {"name": name, "args": {}, "id": f"call_{index}", "type": "tool_call"}
        messages += [
            AIMessage(content="", tool_calls=[call]),
            ToolMessage(content=result, tool_call_id=f"call_{index}", name=name),
        ]
    return messages


def _through(
    message: AIMessage, messages: list, *, whole_conversation: bool = False, label: str = "it"
) -> AIMessage:
    """``message`` as the middleware hands it on, as a model call's answer."""

    middleware = AzureLinksMiddleware(whole_conversation=whole_conversation, label=label)
    response = middleware.wrap_model_call(
        _FakeModelRequest(messages=messages), lambda r: ModelResponse(result=[message])
    )
    (checked,) = response.result
    return checked


def _checked(answer: Any, messages: list, **kwargs: Any) -> AIMessage:
    return _through(AIMessage(content=answer, id="answer-1"), messages, **kwargs)


def test_a_link_a_tool_returned_is_kept_as_it_is() -> None:
    turn = _turn(_recovery())
    message = AIMessage(content=f"It failed: {adf_tools._run_link(SUB, RG, FACTORY, CHILD)}")
    assert _through(message, turn) is message

    # Read through markdown escapes, '&amp;', percent-encoding and the host's case.
    monitor = adf_tools._factory_monitor_url(SUB, RG, FACTORY, GENERIC)
    for written in (
        monitor.replace("_", "\\_"),
        monitor.replace("&", "&amp;"),
        monitor.replace("/subscriptions/", "%2Fsubscriptions%2F"),
        monitor.replace("https://adf.azure.com", "HTTPS://ADF.Azure.com"),
        f"{_url(CHILD)}/",
    ):
        answer = f"Runs: [open Monitor](<{written}>)"
        assert _checked(answer, turn).content == answer, written


def test_a_run_link_without_its_factory_opens_the_run_again() -> None:
    turn = _turn(_recovery())
    bare = f"https://adf.azure.com/en/monitoring/pipelineruns/{CHILD}"
    message = _checked(f"Failed run: [open run]({bare})", turn)
    assert message.content == f"Failed run: [open run](<{_url(CHILD)}>)"
    assert message.id == "answer-1"
    # A bare URL is swapped where it stands, the sentence round it kept.
    assert _checked(f"See {bare}.", turn).content == f"See {_url(CHILD)}."
    assert _checked(f"See <{bare}>.", turn).content == f"See <{_url(CHILD)}>."


def test_a_run_link_built_from_the_tickets_resource_group_opens_the_run_again() -> None:
    turn = _turn(_recovery(), TICKET)
    wrong = _url(CHILD, rg=TICKET_RG)
    assert _checked(f"[open run](<{wrong}>)", turn).content == f"[open run](<{_url(CHILD)}>)"
    # A link whose words were its URL gets the run's link as its words too.
    right = _url(CHILD)
    assert _checked(f"[{wrong}]({wrong})", turn).content == f"[{right}](<{right}>)"
    # The run ID with a group slipped is still that run (copy_check's slip).
    slipped = _url(CHILD.replace("-84a8-", "-84a8-84a8-"), rg=TICKET_RG)
    assert _checked(f"[open run](<{slipped}>)", turn).content == f"[open run](<{right}>)"


def test_a_link_to_the_triggered_by_guid_is_removed() -> None:
    turn = _turn(_recovery())
    answer = (
        f"- **Failed run:** {CHILD} [open run](<{_url(CHILD)}>)\n"
        f"- **Triggered By:** {TRIGGERED_BY} [open run](<{_url(TRIGGERED_BY)}>)\n"
        f"- **Parent pipeline:** {PARENT_PIPELINE}"
    )
    assert _checked(answer, turn).content == (
        f"- **Failed run:** {CHILD} [open run](<{_url(CHILD)}>)\n"
        f"- **Triggered By:** {TRIGGERED_BY}\n"
        f"- **Parent pipeline:** {PARENT_PIPELINE}"
    )
    # Words that said more than "open this" stay, unlinked.
    worded = f"Started by [the parent's activity run](<{_url(TRIGGERED_BY)}>)."
    assert _checked(worded, turn).content == "Started by the parent's activity run."


def test_the_parent_run_opens_in_its_childs_factory() -> None:
    kept = f"{PARENT_PIPELINE} failed too.\nParent run: [open run](<{_url(PARENT)}>)"
    wrong_factory = kept.replace(_url(PARENT), _url(PARENT, factory=OTHER_FACTORY))
    wrong_group = kept.replace(_url(PARENT), _url(PARENT, rg=TICKET_RG))

    # The recovery check links the parent run itself.
    direct = _turn(_recovery())
    assert _checked(kept, direct).content == kept
    assert _checked(wrong_factory, direct).content == kept

    # A run listing names the parent by its parentRunId alone: its link is the
    # row's link with the parent's ID, Execute Pipeline never crossing factories.
    derived = _turn(_listing(), name="list_pipeline_runs")
    assert _checked(kept, derived).content == kept
    assert _checked(wrong_factory, derived).content == kept
    assert _checked(wrong_group, derived).content == kept

    # Rows that put the same parent in two factories cannot both be right.
    split = _turn(_listing(), _listing(OTHER_FACTORY), name="list_pipeline_runs")
    assert _checked(kept, split).content == f"{PARENT_PIPELINE} failed too."


def test_a_studio_page_opens_a_factory_a_tool_linked_or_goes() -> None:
    turn = _turn(_recovery())
    monitor = adf_tools._factory_monitor_url(SUB, RG, FACTORY)
    # The factory's unfiltered Monitor tab: a page of a factory a tool linked.
    kept = f"Not recovered. [open Monitor](<{monitor}>)"
    assert _checked(kept, turn).content == kept

    # Without a factory, or with one built from the ticket, Studio opens its pickers.
    no_factory = "Not recovered. [open Monitor](https://adf.azure.com/en/monitoring/pipelineruns)"
    assert _checked(no_factory, turn).content == "Not recovered."
    ticket_factory = (
        f"https://adf.azure.com/en/home?factory=/subscriptions/{SUB}/resourceGroups/"
        f"{TICKET_RG}/providers/Microsoft.DataFactory/factories/{FACTORY}"
    )
    assert _checked(f"Check [ADF Studio]({ticket_factory}).", turn).content == "Check ADF Studio."

    # Studio's start page opens nothing in particular, so it is left as written.
    for start in (
        "https://adf.azure.com",
        "https://adf.azure.com/",
        "https://adf.azure.com/en/home",
    ):
        answer = f"Open ADF Studio ({start}) and pick the factory."
        assert _checked(answer, turn).content == answer, start


def test_a_tickets_portal_link_is_kept_or_put_back() -> None:
    turn = _turn(TICKET, name="servicenow_get_ticket_detail")
    for answer in (
        f"The alert: [open alert](<{ALERT}>)",
        f"The alert: `{ALERT}`",
        f"The alert: {ALERT}.",
        "Sign in at https://portal.azure.com and open the alert.",
    ):
        message = AIMessage(content=answer)
        assert _through(message, turn) is message, answer

    # A GUID's characters slipped, the case changed or the end cut off: the
    # ticket's link put back.
    for slipped in (
        ALERT.replace("c0ffee00-1234", "c0ffee0-1234"),
        ALERT.replace("AlertDetails", "alertdetails"),
    ):
        fixed = _checked(f"The alert: [open alert](<{slipped}>)", turn).content
        assert fixed == f"The alert: [open alert](<{ALERT}>)", slipped
    assert _checked(f"The alert: {ALERT[:-4]}", turn).content == f"The alert: {ALERT}"
    assert _checked(f"The alert: `{ALERT[:-4]}`", turn).content == f"The alert: `{ALERT}`"

    # A resource link built from the ticket's fields is no link the ticket holds.
    built = (
        f"https://portal.azure.com/#@example.onmicrosoft.com/resource/subscriptions/{SUB}/"
        f"resourceGroups/{TICKET_RG}/providers/Microsoft.DataFactory/factories/{FACTORY}"
    )
    answer = f"The pipeline failed.\nThe factory: [open in the portal]({built})"
    assert _checked(answer, turn).content == "The pipeline failed."
    assert _checked(f"The pipeline failed. `{built}`", turn).content == "The pipeline failed."


def test_a_link_in_a_tickets_html_is_read_as_the_browser_reads_it() -> None:
    # '&quot;' closes the href, as '"' would: the link held is the alert itself.
    turn = _turn(_ticket(f"Alert: <a href=&quot;{ALERT}&quot;>open</a>"), name="get_ticket")
    message = AIMessage(content=f"The alert: [open alert](<{ALERT}>)")
    assert _through(message, turn) is message
    # A result that is not JSON can carry its line breaks as '\n'.
    escaped = _turn(f"[ticket] Alert: {ALERT}\\nResource Group: {TICKET_RG}", name="get_ticket")
    assert _through(message, escaped) is message


def test_other_links_are_left_as_written() -> None:
    turn = _turn(_recovery())
    answer = (
        "See [KB0010001](https://example.service-now.com/kb_view.do?sysparm_article=KB0010001), "
        "[the runbook](https://kb.example.test/generic-pipelines), "
        "https://learn.microsoft.com/azure/data-factory/monitor-visually and "
        "https://management.azure.com/subscriptions/x."
    )
    message = AIMessage(content=answer)
    assert _through(message, turn) is message
    assert _through(message, []) is message


def test_a_subagent_holds_its_tools_results_only() -> None:
    # The task text is the orchestrator's writing: a link it put there does not
    # come back as the subagent's finding.
    pasted = _url(PARENT, factory=OTHER_FACTORY)
    turn = _turn(TICKET, name="servicenow_get_ticket_detail", task=f"Check this run: {pasted}")
    answer = f"The parent run is {PARENT} ([open run](<{pasted}>))."
    assert _checked(answer, turn).content == f"The parent run is {PARENT}."

    # The orchestrator holds what the user wrote.
    conversation = [HumanMessage(content=f"Why did this fail? {pasted}")]
    message = AIMessage(content=answer)
    assert _through(message, conversation, whole_conversation=True) is message


def test_a_general_purpose_answer_is_not_held() -> None:
    from deepagents.middleware.subagents import GENERAL_PURPOSE_SUBAGENT

    assert GENERAL_PURPOSE_SUBAGENT["name"] in links.UNCHECKED_SUBAGENTS

    result = f"Failed run [open run](<{_url(CHILD)}>)"
    answer = f"It did not recover.\n[open run](<{_url(CHILD)}>)"
    checked = _checked(answer, _task(result), whole_conversation=True)
    assert checked.content == answer
    unchecked = _checked(answer, _task(result, "general-purpose"), whole_conversation=True)
    assert unchecked.content == "It did not recover."


def test_a_resource_group_with_parentheses_keeps_its_link() -> None:
    rg = "rg-data(dev)"
    turn = _turn(_recovery(rg))
    link = _url(CHILD, rg=rg)
    for answer in (
        f"[open run](<{link}>)",
        f"[open run]({link})",
        f"See {link}.",
        f"Failed (see {link}).",
    ):
        message = AIMessage(content=answer)
        assert _through(message, turn) is message, answer
    wrong = _url(CHILD, rg=rg, factory=OTHER_FACTORY)
    assert _checked(f"Failed (see {wrong}).", turn).content == f"Failed (see {link})."
    assert _checked(f"[open run]({wrong})", turn).content == f"[open run](<{link}>)"


def test_what_a_removed_link_leaves_is_tidied() -> None:
    turn = _turn(_recovery())
    bogus = _url(TRIGGERED_BY)
    for written, left in (
        (f"The activity run failed ({bogus}).", "The activity run failed."),
        (f"Activity run — {bogus}", "Activity run"),
        (f"Runs: [open run](<{_url(CHILD)}>), [open run](<{bogus}>)", None),
        (f"Done.\n- {bogus}\n- Verdict: NOT RECOVERED", "Done.\n- Verdict: NOT RECOVERED"),
        (f"Done.\nLink: {bogus}.", "Done."),
        (f"Done.\n- **Activity run:** [open run](<{bogus}>)", "Done."),
        (f"| {GENERIC} | Failed | {bogus} |", f"| {GENERIC} | Failed | |"),
        (f"Activity [{bogus}] failed.", "Activity failed."),
        (f"Activity run **{bogus}** failed.", "Activity run failed."),
        (f"1. [open run](<{bogus}>) — Failed\n2. Succeeded", "1. Failed\n2. Succeeded"),
        (f"Runs:\n  - [open run](<{bogus}>) for {GENERIC}", f"Runs:\n  - for {GENERIC}"),
        (f"- {GENERIC} (Failed): [open run](<{bogus}>)", f"- {GENERIC} (Failed):"),
        (f"Done.\nNote: see [the link](<{bogus}>)", "Done."),
        (f"Activity run:[open run](<{bogus}>) failed", "Activity run: failed"),
        # Words that name something stay, link words alone do not.
        (f"Started by [{TRIGGERED_BY}](<{bogus}>).", f"Started by {TRIGGERED_BY}."),
        (f"[run {TRIGGERED_BY} at {bogus}](<{bogus}>)", f"run {TRIGGERED_BY} at"),
        # An answer that was nothing but links says that the link was left out.
        (f"Activity run: [open run](<{bogus}>)", links.LEFT_OUT),
        (f"Here it is: <{bogus}>", links.LEFT_OUT),
        (bogus, links.LEFT_OUT),
    ):
        expected = left if left is not None else f"Runs: [open run](<{_url(CHILD)}>)"
        assert _checked(written, turn).content == expected, written


def test_an_escaped_link_in_a_subagents_answer_is_held() -> None:
    # A subagent's answer reaches the orchestrator as the model wrote it, markdown
    # escapes and all: 'rg\_data\_dev', 'rg-data\(dev\)'.
    for rg in ("rg_data_dev", "rg-data(dev)"):
        link = _url(CHILD, rg=rg)
        escaped = re.sub(r"([_()])", r"\\\1", link)
        conversation = _task(f"Failed run: [open run](<{escaped}>)")
        for answer in (
            f"[open run](<{link}>)",
            f"[open run](<{escaped}>)",
            f"See {link}.",
            f"See {escaped}.",
        ):
            message = AIMessage(content=answer)
            assert _through(message, conversation, whole_conversation=True) is message, answer
    alert = ALERT.replace("_", "\\_")
    conversation = _task(f"The alert: {alert}", "servicenow-ticket-agent")
    for answer in (f"The alert: [open alert](<{ALERT}>)", f"The alert: {alert}"):
        message = AIMessage(content=answer)
        assert _through(message, conversation, whole_conversation=True) is message, answer


def test_an_earlier_answer_is_held_once_summarization_drops_its_result() -> None:
    link = f"[open run](<{_url(CHILD)}>)"
    # The summary that stands for the older turns is a model's writing that
    # nothing checked: a link only it holds is not held.
    summary = HumanMessage(
        content=f"Summary so far: {GENERIC} failed; its parent run is {_url(PARENT)}",
        additional_kwargs={"lc_source": "summarization"},
    )
    conversation = [
        summary,
        AIMessage(content=f"{GENERIC} did not recover. {link}"),
        HumanMessage(content="Send me that link again."),
    ]
    message = AIMessage(content=f"Here it is: {link}")
    assert _through(message, conversation, whole_conversation=True) is message
    answer = f"The parent run: [open run](<{_url(PARENT)}>)"
    assert _checked(answer, conversation, whole_conversation=True).content == links.LEFT_OUT
    # A subagent holds its tools' results only.
    assert _checked(message.content, conversation).content == links.LEFT_OUT


def test_a_tools_link_for_a_run_wins_over_a_pasted_one() -> None:
    pasted = _url(CHILD, factory=OTHER_FACTORY)
    conversation = [
        HumanMessage(content=f"Why did this run fail? {pasted}"),
        *_turn(_recovery())[1:],
    ]
    answer = f"It failed: [open run](<{pasted}>)"
    checked = _checked(answer, conversation, whole_conversation=True)
    assert checked.content == f"It failed: [open run](<{_url(CHILD)}>)"


def test_a_link_to_another_resource_is_not_put_back() -> None:
    # One name apart ('df-sales-prd' for 'df-sales-dev') is another resource.
    portal = (
        f"https://portal.azure.com/#@example.onmicrosoft.com/resource/subscriptions/{SUB}/"
        f"resourceGroups/{RG}/providers/Microsoft.DataFactory/factories/df-sales-dev/overview"
    )
    monitor = adf_tools._factory_monitor_url(SUB, RG, "df-sales-dev")
    turn = _turn(f"Factory: {portal}\nRuns: [open Monitor](<{monitor}>)", name="get_factory")
    for written in (
        portal.replace("df-sales-dev", "df-sales-prd"),
        monitor.replace("df-sales-dev", "df-sales-qa"),
    ):
        answer = f"The pipeline failed.\nFactory: [open in Azure]({written})"
        assert _checked(answer, turn).content == "The pipeline failed.", written


def test_a_parent_run_id_below_its_childs_link_takes_that_link() -> None:
    kept = f"{PARENT_PIPELINE} failed too.\nParent run: [open run](<{_url(PARENT)}>)"
    wrong = kept.replace(_url(PARENT), _url(PARENT, rg=TICKET_RG))
    card = _turn(_card(), name="get_pipeline_run_details")
    assert _checked(wrong, card).content == kept

    # A blank line ends the card the link heads; a row that prints a run of its
    # own is that run's.
    blank = _turn(_card(""), name="get_pipeline_run_details")
    row = (
        f"  - runId={OTHER_RUN} | {GENERIC} | Failed | "
        f"triggeredBy={adf_tools._invoked_text(_child_run())}"
    )
    rows = _turn(f"{_card().splitlines()[0]}\n{row}", name="list_pipeline_runs")
    for messages in (blank, rows):
        assert _checked(wrong, messages).content == f"{PARENT_PIPELINE} failed too."


def test_text_glued_to_a_bare_link_is_set_apart() -> None:
    # The renderer runs a bare URL on to the next space: '…/df-sample—it' would
    # open no factory.
    turn = _turn(_recovery())
    link = _url(CHILD)
    for written, expected in (
        (f"{{see {link}}}", f"{{see {link} }}"),
        (f"See {link}—it failed.", f"See {link} —it failed."),
        (f"See {link}….", f"See {link} …."),
        (f"See {link}\u200b.", f"See {link} \u200b."),
        (f"See {_url(CHILD, rg=TICKET_RG)}—it failed.", f"See {link} —it failed."),
        (f"See {_url(TRIGGERED_BY)}—it failed.", "See —it failed."),
    ):
        assert _checked(written, turn).content == expected, written
    # Code is not rendered as a link, and a link it does not check is left as written.
    for answer in (f"`{link}—it`", "See https://kb.example.test/generic—it failed."):
        message = AIMessage(content=answer)
        assert _through(message, turn) is message, answer


def test_tidying_a_long_line_takes_linear_time() -> None:
    bogus = _url(TRIGGERED_BY)
    gap = " \t" * 25_000
    for answer in (
        f"Failed{gap}x [open run](<{bogus}>)",
        f"Failed{gap}x ({bogus})",
        f"Failed ({gap}x) {bogus}",
    ):
        start = time.perf_counter()
        _checked(answer, _turn(_recovery()))
        assert time.perf_counter() - start < 1.0, answer[:20]


def test_a_deeply_nested_result_is_read_without_recursing() -> None:
    wrong = f"[open run](<{_url(CHILD, rg=TICKET_RG)}>)"
    held = json.dumps(f"Failed run [open run](<{_url(CHILD)}>)")
    for depth in (900, 5_000, 100_000):
        turn = _turn("[" * depth + held + "]" * depth, name="get_ticket")
        assert _checked(wrong, turn).content == f"[open run](<{_url(CHILD)}>)", depth


def test_a_check_that_errors_leaves_the_answer_as_it_was() -> None:
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]

    def broken(*args: Any) -> Any:
        raise RuntimeError("boom")

    saved = links._link_sources
    links._link_sources = broken
    links.logger.addHandler(handler)
    try:
        message = AIMessage(content=f"[open run](<{_url(TRIGGERED_BY)}>)")
        assert _through(message, _turn(_recovery()), label="adf-agent's answer") is message
    finally:
        links._link_sources = saved
        links.logger.removeHandler(handler)
    (record,) = records
    assert "adf-agent's answer" in record.getMessage()
    assert "failing open" in record.getMessage()
    assert record.exc_info is not None


def test_every_response_shape_and_text_block_is_checked() -> None:
    turn = _turn(_recovery())
    wrong = f"Failed run: [open run](<{_url(CHILD, rg=TICKET_RG)}>)"
    right = f"Failed run: [open run](<{_url(CHILD)}>)"
    middleware = AzureLinksMiddleware()

    def through(response: Any) -> Any:
        return middleware.wrap_model_call(_FakeModelRequest(messages=turn), lambda r: response)

    assert through(AIMessage(content=wrong)).content == right
    extended = through(
        ExtendedModelResponse(model_response=ModelResponse(result=[AIMessage(content=wrong)]))
    )
    assert extended.model_response.result[0].content == right

    reasoning = {"type": "reasoning", "summary": [f"saw {wrong}"]}
    blocks = [reasoning, {"type": "text", "text": wrong}, "plain", wrong]
    fixed = through(ModelResponse(result=[AIMessage(content=blocks)])).result[0].content
    assert fixed == [reasoning, {"type": "text", "text": right}, "plain", right]

    # A message that calls tools is no answer.
    call = {"name": TASK_TOOL_NAME, "args": {}, "id": "call_next", "type": "tool_call"}
    calling = AIMessage(content=wrong, tool_calls=[call])
    assert through(ModelResponse(result=[calling])).result[0] is calling


def test_the_async_path_checks_the_answer_too() -> None:
    async def handler(request: _FakeModelRequest) -> ModelResponse:
        return ModelResponse(result=[AIMessage(content=f"[open run]({_url(CHILD, rg=TICKET_RG)})")])

    request = _FakeModelRequest(messages=_turn(_recovery()))
    response = asyncio.run(AzureLinksMiddleware().awrap_model_call(request, handler))
    assert response.result[0].content == f"[open run](<{_url(CHILD)}>)"


def test_a_change_is_logged_without_the_subscription() -> None:
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    links.logger.addHandler(handler)
    try:
        answer = f"Not recovered. [open run](<{_url(TRIGGERED_BY)}>) [open run]({_url(CHILD, rg=TICKET_RG)})"
        _checked(answer, _turn(_recovery()), label="adf-agent's answer")
    finally:
        links.logger.removeHandler(handler)
    (record,) = records
    text = record.getMessage()
    assert "adf-agent's answer" in text
    assert f"removed adf.azure.com/en/monitoring/pipelineruns/{TRIGGERED_BY}" in text
    assert f"(factory {TICKET_RG}/{FACTORY}) -> " in text
    assert SUB not in text


def test_the_orchestrator_checks_its_answer_against_the_whole_conversation() -> None:
    captured: dict[str, Any] = {}
    saved = {
        name: getattr(agent_mod, name)
        for name in (
            "create_deep_agent",
            "get_azure_chat_model",
            "build_backend",
            "_ensure_harness_profiles_registered",
        )
    }
    try:
        agent_mod.create_deep_agent = lambda **kwargs: (
            captured.update(kwargs) or SimpleNamespace(with_config=lambda config: config)
        )
        agent_mod.get_azure_chat_model = lambda: None
        agent_mod.build_backend = lambda: None
        agent_mod._ensure_harness_profiles_registered = lambda: None
        agent_mod._build_agent_sync(checkpointer=None)
    finally:
        for name, value in saved.items():
            setattr(agent_mod, name, value)

    stack = captured["middleware"]
    kinds = [type(middleware).__name__ for middleware in stack]
    assert kinds.count("AzureLinksMiddleware") == 1
    checker = stack[kinds.index("AzureLinksMiddleware")]
    assert checker.whole_conversation is True
    # Outer of ContextEditing: a result it clears from the model's view still counts.
    assert kinds.index("SubagentAccessMiddleware") < kinds.index("AzureLinksMiddleware")
    assert kinds.index("AzureLinksMiddleware") < kinds.index("ContextEditingMiddleware")


def test_every_subagent_checks_its_own_answer() -> None:
    from v1.core.subagents import ADF_SUBAGENT, ADLS_SUBAGENT, SERVICENOW_SUBAGENT

    for subagent in (ADF_SUBAGENT, ADLS_SUBAGENT, SERVICENOW_SUBAGENT):
        wired = [m for m in subagent["middleware"] if isinstance(m, AzureLinksMiddleware)]
        assert len(wired) == 1, subagent["name"]
        assert wired[0].whole_conversation is False, subagent["name"]
    # In adf-agent: inner of Citations, outer of the copy check and the markdown
    # view, so it reads the copy-checked answer against the plain results.
    kinds = [type(m).__name__ for m in ADF_SUBAGENT["middleware"]]
    position = kinds.index("AzureLinksMiddleware")
    assert kinds.index("CitationsMiddleware") < position
    assert position < kinds.index("ToolMarkdownMiddleware")
    assert position < kinds.index("AdfResultKeepMiddleware")


RUNBOOK = "https://kb.example.test/generic-pipelines"


def _search_holding_a_run_link(
    query: str,
    top_k: int,
    index_name: str,
    semantic_configuration: str | None,
    documents: Any,
    section: str | None = None,
) -> str:
    """Stand-in for the Azure Search call: one document that links the run."""

    index = documents.assign(
        RUNBOOK, {"title": "Generic pipelines", "url": RUNBOOK, "source_key": RUNBOOK}
    )
    return f"[{index}] Generic pipelines\nLast failed run: [open run](<{_url(CHILD)}>)"


@contextmanager
def _agent(replies: list[AIMessage]) -> Iterator[Any]:
    # The model stays patched for the whole test: the safety classifier fetches it
    # on every turn.
    model = _stand_in_model(replies)
    saved = (agent_mod.get_azure_chat_model, ai_search_mod._run_search)
    agent_mod.get_azure_chat_model = lambda: model
    ai_search_mod._run_search = _search_holding_a_run_link
    try:
        yield agent_mod._build_agent_sync(InMemorySaver())
    finally:
        agent_mod.get_azure_chat_model, ai_search_mod._run_search = saved
    assert not model.replies, "the scripted turn did not run to the end"


def test_both_answers_are_checked_in_the_real_agent() -> None:
    """The subagent's answer is checked against its search before the orchestrator
    sees it, and the orchestrator's against the subagent's answer: the run's link
    is what both persist, however each model wrote it."""

    task_call = {
        "name": TASK_TOOL_NAME,
        "args": {
            "description": f"Find {GENERIC}'s failed run",
            "subagent_type": "servicenow-ticket-agent",
        },
        "id": "call_task",
    }
    search_call = {"name": "ai_search_tool", "args": {"query": "generic run"}, "id": "call_search"}
    replies = [
        AIMessage(content="", tool_calls=[task_call]),
        AIMessage(content="", tool_calls=[search_call]),
        AIMessage(
            content=f"The failed run: [open run](https://adf.azure.com/en/monitoring/pipelineruns/{CHILD})"
        ),
        AIMessage(content=f"It failed. [open run](<{_url(CHILD, rg=TICKET_RG)}>)"),
    ]
    config = {"configurable": {"thread_id": "azure-links", "run_id": str(uuid.uuid4())}}

    async def scenario(graph: Any) -> None:
        await graph.ainvoke(_user(f"Where is {GENERIC}'s failed run?"), config)
        messages = (await graph.aget_state(config)).values["messages"]
        task_result = next(
            m for m in messages if isinstance(m, ToolMessage) and m.tool_call_id == "call_task"
        )
        assert task_result.content == f"The failed run: [open run](<{_url(CHILD)}>)"
        assert messages[-1].content == f"It failed. [open run](<{_url(CHILD)}>)"

    with _agent(replies) as graph:
        asyncio.run(scenario(graph))


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
