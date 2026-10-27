"""Replaces a bare on-time/late verdict on an ADLS lookup with the facts; the DQ process decides."""

from __future__ import annotations

import logging
import re
from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

logger = logging.getLogger(__name__)

ADLS_SUBAGENT_NAME = "adls-agent"

# Whole words only, so "latest" is not "late" and "translate" is not "sla".
_TIMELINESS_QUESTION = re.compile(
    r"\b(late|on[- ]time|sla|time target|arriv\w*|(in)?complete(ness)?|missed|deadline|due)\b",
    re.IGNORECASE,
)
_MAX_VERDICT_WORDS = 4
_VERDICT_WORD = re.compile(r"\b(yes|no|late|on[- ]time|missed|met)\b", re.IGNORECASE)
_NON_ADLS_TOOLS = frozenset({"ai_search_tool", "read_url"})
_FACT_LINE = re.compile(r"sla given|arrived|lastModified", re.IGNORECASE)

_DEFERRAL = (
    "I can't answer that with a bare yes or no: the on-time/late "
    "(or complete/incomplete) determination is made by the DQ timeliness "
    "process, not by this assistant.\n\n{facts}\n\n"
    "Compare the SLA with the actual arrival above, or check the DQ "
    "evaluation for the official ruling."
)
_NO_FACTS = (
    "Give me the landing path and the SLA and I will retrieve the actual file "
    "arrival so you can compare them."
)


def _text(content: Any) -> str:
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict)).strip()
    return str(content or "")


def _is_bare_verdict(answer: str) -> bool:
    core = re.split(r"want to explore further", answer, flags=re.IGNORECASE)[0]
    words = re.findall(r"[A-Za-z']+", core)
    return 0 < len(words) <= _MAX_VERDICT_WORDS and bool(_VERDICT_WORD.search(core))


def _current_turn(messages: list) -> tuple[str, list]:
    """The latest user question and the messages after it."""
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], HumanMessage):
            return _text(messages[index].content), messages[index + 1 :]
    return "", messages


def _is_adls_call(call: dict) -> bool:
    return (call.get("args") or {}).get("subagent_type") == ADLS_SUBAGENT_NAME


def _rests_on_adls(messages: list, turn: list) -> bool:
    """Whether the answer is based on an ADLS lookup."""
    calls = [call for msg in turn if isinstance(msg, AIMessage) for call in msg.tool_calls]
    if any(_is_adls_call(call) for call in calls):
        return True
    if any(call["name"] == "task" or call["name"] in _NON_ADLS_TOOLS for call in calls):
        return False
    return bool(_adls_results(messages))


def _adls_results(messages: list) -> list[str]:
    """ADLS lookup results, newest first: adls-agent `task` results and raw tool output."""
    adls_calls = {
        call["id"]
        for msg in messages
        if isinstance(msg, AIMessage)
        for call in msg.tool_calls
        if _is_adls_call(call)
    }
    results = []
    for msg in reversed(messages):
        if not isinstance(msg, ToolMessage):
            continue
        text = _text(msg.content)
        if msg.tool_call_id in adls_calls or "[adls-agent]" in text:
            results.append(text)
    return results


def _evidence(messages: list) -> str:
    """Quote the SLA and arrival lines of the newest ADLS lookup."""
    for result in _adls_results(messages):
        lines = [line.strip() for line in result.splitlines() if _FACT_LINE.search(line)]
        if lines:
            return "From the latest lookup:\n" + "\n".join(f"- {line}" for line in lines[:6])
    return _NO_FACTS


def _replace_bare_verdict(state: AgentState) -> dict[str, Any] | None:
    messages = state.get("messages", []) or []
    question, turn = _current_turn(messages)
    answer = next(
        (msg for msg in reversed(turn) if isinstance(msg, AIMessage) and _text(msg.content)), None
    )
    if (
        answer is None
        or not _TIMELINESS_QUESTION.search(question)
        or not _is_bare_verdict(_text(answer.content))
        or not _rests_on_adls(messages, turn)
    ):
        return None
    facts = _evidence(messages)
    logger.info(
        "Verdict guard replaced a bare %r answer to a timeliness question (%s)",
        _text(answer.content)[:40],
        "quoting the latest lookup" if facts != _NO_FACTS else "no lookup to quote",
    )
    # Same id -> the messages reducer replaces the verdict instead of appending.
    return {"messages": [AIMessage(content=_DEFERRAL.format(facts=facts), id=answer.id)]}


class AdlsVerdictGuardMiddleware(AgentMiddleware):
    """Replace a bare yes/no timeliness verdict with facts plus deferral."""

    def after_agent(self, state: AgentState, runtime: Any) -> dict[str, Any] | None:
        return _replace_bare_verdict(state)

    async def aafter_agent(self, state: AgentState, runtime: Any) -> dict[str, Any] | None:
        return _replace_bare_verdict(state)
