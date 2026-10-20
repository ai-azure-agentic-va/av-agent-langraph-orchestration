"""View-only relay reminder on this turn's ``adf-agent`` answers.

The orchestrator prompt already says to present an ``adf-agent`` answer
faithfully (FAITHFUL COVERS ROWS, NOT JUST FIELDS). Those rules sit far above
the answer they govern, and graded runs kept finding the
reply re-titled with a one-line answer of the model's own and the subagent's
opening lines gone: UC1-07 (run 5) lost both 'Source system taken from the view
name: LOANSYS' and the '5 active candidate pipelines' count under a new 'The
pipeline that populates … is:' heading.

So for each model call this middleware appends a short RELAY REQUIREMENT to
every ``adf-agent`` result of the current turn, right where the model reads the
answer. It is:

- **View-only.** It calls ``handler(request.override(messages=...))`` with copies
  of the patched messages, so ``state["messages"]``, the thread history and
  what the UI shows are untouched, and a later turn's request carries the
  answer without it.
- **Narrow.** Only a ``task`` call whose ``subagent_type`` is ``adf-agent`` is
  patched. A blocked or failed call (``status="error"``, e.g. the refusal
  :mod:`v1.core.middlewares.subagent_access` returns), an empty result, and
  every other subagent's and tool's result are left alone.

The reply still copies the answer out token by token, and can change a run ID,
a run's date or a name while doing so, and its follow-up suggestions can name a
trigger as the user typed it. So the same middleware hands the final reply to
:mod:`v1.core.middlewares.copy_check`, which puts those back from this turn's
``adf-agent`` answers.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage

from v1.core.middlewares.copy_check import copy_checked
from v1.core.middlewares.subagent_access import ADF_SUBAGENT_NAME, TASK_TOOL_NAME

logger = logging.getLogger(__name__)

# Addressed to the model and marked as such: the tools' ANSWER REQUIREMENT lines
# use the same form, and none has reached a user in the graded runs. The last
# two sentences answer run 6: a ServiceNow 'Yes.' opened UC8-09 ahead of this
# answer, and follow-ups offered 'pl-100-01' by the user's folder path (SRCH-03)
# or a diagnosis of runs that had not failed (UC8-07). Run 7 added triggers to
# the names (QA-18 offered the user's TRIG_FX_… spelling) and the recovery
# offers: validating a run that had succeeded (UC2-02), and counting the run the
# answer rejected anyway (UC2-03).
RELAY_REQUIREMENT = (
    "RELAY REQUIREMENT (an instruction to you, not text for the user): the text "
    "above is adf-agent's answer. Carry it into your reply as written — its first "
    "line, then every heading, group, row, note, count and link, in its order — and "
    "add only your follow-up suggestions after it. Never open with an answer, title "
    "or summary of your own, and never drop a line, merge a group or fold rows into "
    "a sentence. When your reply also covers other results, this answer still goes "
    "in whole, as its own part, and a result that disagrees with it is given under "
    "its own source — never as a verdict opening the reply. A refused change is "
    "still reported by its outcome alone. In your follow-up suggestions, name each "
    "factory, pipeline and trigger exactly as this answer names it — never a shorthand, "
    "spelling or folder path the user typed — never offer to diagnose a failure this "
    "answer does not show, and never offer to validate the recovery of a run that did "
    "not fail or to count a run the answer rejected as a recovery."
)


def _turn_start(messages: list[AnyMessage]) -> int:
    """Index of the first message after the latest ``HumanMessage`` (0 if none)."""

    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], HumanMessage):
            return index + 1
    return 0


def _adf_call_ids(messages: list[AnyMessage]) -> set[str]:
    """Ids of the ``task`` calls in ``messages`` that went to ``adf-agent``.

    deepagents' ``task`` result carries no tool name, so its target is read off
    the call it answers.
    """

    ids: set[str] = set()
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls or []:
            args = call.get("args") or {}
            if (
                call.get("name") == TASK_TOOL_NAME
                and args.get("subagent_type") == ADF_SUBAGENT_NAME
                and call.get("id")
            ):
                ids.add(call["id"])
    return ids


def _relay_view(messages: list[AnyMessage]) -> list[AnyMessage] | None:
    """``messages`` with the requirement on this turn's ``adf-agent`` answers.

    ``None`` when nothing needs it, so the caller passes the request through
    untouched.
    """

    start = _turn_start(messages)
    ids = _adf_call_ids(messages[start:])
    if not ids:
        return None
    view = list(messages)
    patched = 0
    for index in range(start, len(messages)):
        message = messages[index]
        # deepagents always hands the subagent's answer back as text.
        if (
            not isinstance(message, ToolMessage)
            or message.tool_call_id not in ids
            or message.status == "error"
            or not isinstance(message.content, str)
            or not message.content.strip()
        ):
            continue
        view[index] = message.model_copy(
            update={"content": f"{message.content.rstrip()}\n\n{RELAY_REQUIREMENT}"}
        )
        patched += 1
    if not patched:
        return None
    logger.info("Relay requirement added to %d adf-agent answer(s) in the model view", patched)
    return view


def _copy_checked(request: ModelRequest, response: ModelResponse) -> ModelResponse:
    """``response`` with its final reply checked against this turn's adf-agent answers."""

    answers = _adf_call_ids(request.messages[_turn_start(request.messages) :])
    return copy_checked(request, response, answers, "the reply")


class SubagentRelayMiddleware(AgentMiddleware):
    """Remind the orchestrator, beside the answer, to relay ``adf-agent`` whole,
    and put back a run ID, run date or name the reply changed while copying it."""

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        view = _relay_view(request.messages)
        if view is None:
            return handler(request)
        return _copy_checked(request, handler(request.override(messages=view)))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        view = _relay_view(request.messages)
        if view is None:
            return await handler(request)
        return _copy_checked(request, await handler(request.override(messages=view)))
