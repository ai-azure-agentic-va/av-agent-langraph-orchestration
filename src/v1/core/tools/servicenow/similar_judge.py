"""Picks which past closed incidents are similar to the parent incident.

Used by ``servicenow_find_similar_resolutions`` for every family: the tool fetches the
candidates (newest first, a batch at a time) and ONE call to the app's own chat model
reads the parent and the batch and returns the numbers of the similar ones. The model
can only return numbers it was sent.

Two meanings of "similar":
- same PROBLEM (every family but 'application'): the same failure, on any server, job
  or dataset — the error-pattern match support teams asked for;
- same TOPIC ('application'): any problem with the same feature, document or step of
  the application, whatever went wrong.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Mapping, Sequence
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

# Usually ~2 s, but the shared deployment has spiked to 20-30 s (measured 2026-10-14).
_TIMEOUT_SECONDS = 60
# Per-incident caps: a pasted log cannot blow up the call (50 cards ≈ 10-15K tokens).
_DESCRIPTION_CHARS = 800
_NOTES_CHARS = 400
_URL_RE = re.compile(r"https?://\S+")
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

_REPLY = (
    'Reply with JSON only: {"matches": ["<incident number>", ...]}, most similar '
    "first, an empty list when none match."
)
# Measured on mixed pools (2026-10-11): 18/18 first picks right on reworded tickets.
# Without the "different server, job …" sentence it kept 1 of 7 'Memory limit reached
# on <job>' twins; with it, 7 of 7.
_SAME_PROBLEM = (
    "You match ServiceNow incidents. Given a NEW incident and a list of PAST closed "
    "incidents, pick the past incidents that describe the SAME problem: the same failure "
    "or root cause, judged by meaning, not by shared words. The same failure on a "
    "different server, job, dataset, report or application IS a match, and so is "
    "different wording for the same problem; shared words about a different problem is "
    "NOT. " + _REPLY
)
# The topic, not the root cause: the stakeholders' own examples pair a wrong letter
# with letters not generated, sent twice or not received. Measured on such a batch, a
# "same failure or root cause" instruction kept 1 of 5 of them; naming the kinds of
# failure that still count as the same topic kept 4-5. Not used for a monitoring
# alert, which is matched by its title instead (tools._MONITORING_ALERT_RE): by topic,
# every alert type on one app counted as one.
_SAME_TOPIC = (
    "You match ServiceNow incidents of ONE application. Given a NEW incident and PAST "
    "closed incidents of the same application, pick every past incident about the same "
    "TOPIC as the new one (the same feature, screen, document type, queue or process "
    "step), whatever went wrong with it: not generated, wrong one produced, duplicated, "
    "not delivered, an error shown, data missing. The specific failure and root cause "
    "do NOT need to match, and a ticket that does not name the application can still "
    "match. Include a ticket when in doubt; leave out only tickets about a different "
    "topic. " + _REPLY
)


def _clean(value: Any, limit: int) -> str:
    # Alert text carries links and literal "\n" runs: tokens, no meaning.
    text = _URL_RE.sub(" ", str(value or "")).replace("\\n", " ")
    return " ".join(text.split())[:limit]


def _card(ticket: Mapping[str, Any], *, with_resolution: bool) -> str:
    parts = [
        f"Short description: {_clean(ticket.get('short_description'), 300)}",
        f"Configuration item: {ticket.get('configuration_item') or 'n/a'}",
        f"Description: {_clean(ticket.get('description'), _DESCRIPTION_CHARS)}",
    ]
    # Same problem: how it was fixed says what the failure was. Same topic: it pulls
    # the judgement toward matching root causes, which that meaning must not.
    if with_resolution and ticket.get("close_notes"):
        parts.append(f"Resolution: {_clean(ticket['close_notes'], _NOTES_CHARS)}")
    return " | ".join(parts)


@lru_cache(maxsize=1)
def _model() -> Any:
    # Lazy import: the agent module imports these tools.
    from v1.core.agent import get_azure_chat_model

    # The reply is a list of up to 50 numbers (~600 tokens); the rest is headroom for a
    # deployment that reasons first, so the list is never cut off mid-way.
    return get_azure_chat_model().model_copy(update={"max_tokens": 2000})


async def pick_similar(
    parent: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]], *, same_topic: bool
) -> list[int] | None:
    """Indexes into ``candidates`` of the similar ones, most similar first ([] = none),
    or None when the call failed."""

    if not candidates:
        return []
    index_of = {str(t["ticket_number"]).upper(): i for i, t in enumerate(candidates)}

    def card(ticket: Mapping[str, Any]) -> str:
        return _card(ticket, with_resolution=not same_topic)

    prompt = f"NEW INCIDENT:\n{card(parent)}\n\nPAST INCIDENTS:\n" + "\n".join(
        f"[{t['ticket_number']}] {card(t)}" for t in candidates
    )
    started = time.monotonic()
    try:
        reply = await asyncio.wait_for(
            _model().ainvoke([("system", _SAME_TOPIC if same_topic else _SAME_PROBLEM), ("human", prompt)]),
            _TIMEOUT_SECONDS,
        )
        # .text, not str(content): a reply can arrive as a list of parts, whose str()
        # is a Python repr that no JSON parser reads.
        matches = json.loads(_JSON_RE.search(reply.text).group(0))["matches"]
    except Exception as exc:
        logger.warning(
            f"servicenow.similar_judge_failed parent={parent.get('ticket_number')} "
            f"sent={len(candidates)} error_type={type(exc).__name__} "
            f"elapsed_ms={int((time.monotonic() - started) * 1000)}",
            exc_info=True,
        )
        return None
    picked: list[int] = []
    for match in matches:
        index = index_of.get(str(match).strip().upper())
        if index is not None and index not in picked:
            picked.append(index)
    logger.info(
        f"servicenow.similar_judge parent={parent.get('ticket_number')} "
        f"meaning={'same_topic' if same_topic else 'same_problem'} sent={len(candidates)} "
        f"kept={len(picked)} elapsed_ms={int((time.monotonic() - started) * 1000)}"
    )
    return picked
