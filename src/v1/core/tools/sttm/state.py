"""The reducer for the state channels that hold a conversation's STTM sessions.

Sessions live in their own channel, ``sttm_sessions``, not in the ``files`` the
filesystem tools read and write, so only the STTM tools can change them. The
layout steps of each session's workbooks live beside them, in ``sttm_layouts``:
a layout is not part of the session, so changing it neither needs an editable
session nor makes its reviews out of date. The agent's state schema declares
both (:class:`v1.core.agent.ChatAgentState`), and deepagents builds each
subagent on that schema: the orchestrator keeps them between turns, and the
sttm-agent receives them with each task and hands back what its tools changed.
"""

from __future__ import annotations


def merge_by_revision(existing: dict | None, new: dict | None) -> dict:
    """Reducer for an STTM channel: per session, the entry with the higher revision wins.

    A returning ``task`` hands back every entry it was given, changed or not, so
    with parallel tasks a plain "new wins" merge would let one task's stale copy
    undo another's change. Each change raises an entry's ``revision``; a tie goes
    to the newer write.
    """

    merged = dict(existing or {})
    for session_id, entry in (new or {}).items():
        current = merged.get(session_id)
        if current is None or entry.get("revision", 0) >= current.get("revision", 0):
            merged[session_id] = entry
    return merged


# The sessions' reducer, by the name the state schema has always used.
merge_sessions = merge_by_revision

__all__ = ["merge_by_revision", "merge_sessions"]
