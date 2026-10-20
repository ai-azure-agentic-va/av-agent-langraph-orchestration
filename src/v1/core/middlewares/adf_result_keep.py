"""View-only keep reminder on the ``adf-agent`` subagent's own tool results.

:mod:`v1.core.middlewares.subagent_relay` made the orchestrator pass an
``adf-agent`` answer through whole, and graded runs confirm it now does, word
for word. What was still missing in run 7 was gone one step earlier: the
subagent itself dropped lines its tools had printed while writing that answer —
a discovery row's Status (UC1-01) and its [open run] link (UC1-06), the GROUP B
heading (UC1-09), the 'Completion estimate: Not applicable' line (UC3-02/03),
the factories searched (UC8-10) and the closest-names line (UC2-08). The ADF
prompt's closing paragraph says to keep the tools' rows and labels verbatim,
far above the result it governs.

So for each of the subagent's model calls this middleware appends a short KEEP
REQUIREMENT to every result of a READ tool from the current task, right where
the model reads it. It is:

- **View-only.** Like the relay, it calls ``handler(request.override(...))``
  with patched copies, so the subagent's state and what reaches the orchestrator
  are untouched.
- **Narrow.** Only the tools it is given are patched. The write tools are left
  out on purpose: rule 9 of the ADF prompt (and its adf-write-refusals skill)
  reports a refused change by its outcome ALONE, and an instruction to keep
  every line must never sit beside one. ``ai_search_tool`` is not an ADF tool and is never passed in. A failed
  call (``status="error"``), an empty result and non-text content are skipped.
- **Untagged.** The patched copy drops the '[adf-agent]' tag that starts many
  tool lines. It labels the tool, not the finding, and once the subagent kept
  every line it copied a tagged block into an answer whole (SRCH-03, run 8).

The subagent copies those lines token by token, and once wrote a run ID with a
group twice (SUP-06, run 8's re-run), its link opening no run. So its final
answer also goes through :mod:`v1.core.middlewares.copy_check`, against every
result of the task, the write tools' included: a rerun's new run ID is copied
too.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Iterable

from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage, AnyMessage, ToolMessage

from v1.core.middlewares.copy_check import copy_checked, tool_call_ids
from v1.core.middlewares.subagent_relay import _turn_start

logger = logging.getLogger(__name__)

# The tools that change something. Their refusals are reported by outcome alone
# (ADF prompt rule 9), which a keep-every-line instruction would contradict.
WRITE_TOOL_NAMES = frozenset({"manage_trigger_states", "rerun_failed_pipeline"})

# Same form as the relay's and the tools' own ANSWER REQUIREMENT lines, none of
# which has reached a user in the graded runs. Each item it names is a line
# that went missing in run 7.
KEEP_REQUIREMENT = (
    "KEEP REQUIREMENT (an instruction to you, not text for the user): if your answer "
    "uses the result above, carry its lines into the answer as the tool wrote them, in "
    "their order. Keep every heading and group (GROUP A and GROUP B stay two separate "
    "groups), every row with all of its cells (Status, Last Run, start, end, runtime, "
    "factory, folder) and its [open run] link, and every NOTE, count and caveat, "
    "including the 'Completion estimate', 'Recommendation', factories-searched and "
    "closest-names lines. Change the layout only; never drop, merge or summarize a "
    "line. Leave out only an ANSWER REQUIREMENT and any sentence naming a tool or an "
    "argument (opened_at=, factory=) — say what it allows in plain words instead."
)

# The tools' label for their own text; the patched copy reads without it.
_TAG = re.compile(r"\[adf-agent\] ?")


def _read_call_ids(messages: list[AnyMessage], tool_names: frozenset[str]) -> set[str]:
    """Ids of the calls in ``messages`` made to one of ``tool_names``."""

    ids: set[str] = set()
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls or []:
            if call.get("name") in tool_names and call.get("id"):
                ids.add(call["id"])
    return ids


def _keep_view(messages: list[AnyMessage], tool_names: frozenset[str]) -> list[AnyMessage] | None:
    """``messages`` with the requirement on this task's read-tool results.

    ``None`` when nothing needs it, so the caller passes the request through
    untouched.
    """

    start = _turn_start(messages)
    ids = _read_call_ids(messages[start:], tool_names)
    if not ids:
        return None
    view = list(messages)
    patched = 0
    for index in range(start, len(messages)):
        message = messages[index]
        if (
            not isinstance(message, ToolMessage)
            or message.tool_call_id not in ids
            or message.status == "error"
            or not isinstance(message.content, str)
        ):
            continue
        content = _TAG.sub("", message.content).rstrip()
        if not content.strip():
            continue
        view[index] = message.model_copy(update={"content": f"{content}\n\n{KEEP_REQUIREMENT}"})
        patched += 1
    if not patched:
        return None
    logger.info("Keep requirement added to %d ADF tool result(s) in the model view", patched)
    return view


def _copy_checked(request: ModelRequest, response: ModelResponse) -> ModelResponse:
    """``response`` with the subagent's final answer checked against this task's results."""

    results = tool_call_ids(request.messages[_turn_start(request.messages) :])
    return copy_checked(request, response, results, "adf-agent's answer")


class AdfResultKeepMiddleware(AgentMiddleware):
    """Remind the ``adf-agent`` subagent, beside each read result, to keep its lines,
    and put back a run ID, run date or name its answer changed while copying them."""

    def __init__(self, tool_names: Iterable[str]) -> None:
        self.tool_names = frozenset(tool_names) - WRITE_TOOL_NAMES

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        view = _keep_view(request.messages, self.tool_names)
        response = handler(request if view is None else request.override(messages=view))
        return _copy_checked(request, response)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        view = _keep_view(request.messages, self.tool_names)
        response = await handler(request if view is None else request.override(messages=view))
        return _copy_checked(request, response)
