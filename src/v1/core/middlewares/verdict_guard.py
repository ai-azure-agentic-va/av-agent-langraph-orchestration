"""Deterministic backstop for the no-verdict contract.

The DQ timeliness process — not this assistant — rules on-time/late and
complete/incomplete (meeting notes, Jul 23). The orchestrator prompt says so,
but under pressure ("answer strictly yes or no") the model still emits a bare
verdict on some runs. This guard replaces any such answer with the
facts-plus-deferral format, every run.
"""

from __future__ import annotations

import re
from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

# Fires only when BOTH hold: a timeliness/completeness question, and a short
# answer led by yes/no ("No", "Yes.", "No it was late" — all verdicts).
_TOPIC_TERMS = (
    "late", "on time", "on-time", "sla", "time target", "arriv",
    "complete", "missed", "deadline", "due",
)
_MAX_VERDICT_WORDS = 4
_FACT_LINE = re.compile(r"time target|lastModified|expected arrival", re.IGNORECASE)

_DEFERRAL = (
    "I can't answer that with a bare yes or no: the on-time/late "
    "(or complete/incomplete) determination is made by the DQ timeliness "
    "process, not by this assistant.\n\n{facts}\n\n"
    "Compare the configured target with the actual arrival above, or check "
    "the DQ evaluation for the official ruling."
)
_NO_FACTS = (
    "Ask me for the dataset's configured time target and the actual file "
    "arrival and I will retrieve both so you can compare them."
)


def _text(content: Any) -> str:
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict)).strip()
    return str(content or "")


def _is_bare_verdict(answer: str) -> bool:
    core = re.split(r"want to explore further", answer, flags=re.IGNORECASE)[0]
    words = re.findall(r"[A-Za-z']+", core)
    return bool(words) and words[0].lower() in ("yes", "no") and len(words) <= _MAX_VERDICT_WORDS


def _evidence(messages: list) -> str:
    """Quote target/arrival lines from the newest adls-agent tool output."""
    for msg in reversed(messages):
        text = _text(msg.content) if isinstance(msg, ToolMessage) else ""
        if "[adls-agent]" in text:
            lines = [ln.strip() for ln in text.splitlines() if _FACT_LINE.search(ln)]
            if lines:
                return "From the latest lookup:\n" + "\n".join(f"- {ln}" for ln in lines[:6])
    return _NO_FACTS


class VerdictGuardMiddleware(AgentMiddleware):
    """Replace a bare yes/no timeliness verdict with facts plus deferral."""

    async def aafter_agent(self, state: AgentState, runtime) -> dict[str, Any] | None:
        messages = state.get("messages", []) or []
        question, final = "", None
        for msg in reversed(messages):
            if final is None and isinstance(msg, AIMessage) and _text(msg.content):
                final = msg
            if isinstance(msg, HumanMessage):
                question = _text(msg.content).lower()
                break
        if (
            final is None
            or not any(term in question for term in _TOPIC_TERMS)
            or not _is_bare_verdict(_text(final.content))
        ):
            return None
        # Same id -> the messages reducer replaces the verdict instead of appending.
        replacement = _DEFERRAL.format(facts=_evidence(messages))
        return {"messages": [AIMessage(content=replacement, id=final.id)]}
