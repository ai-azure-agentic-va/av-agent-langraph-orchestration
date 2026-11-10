from __future__ import annotations

from v1.core.prompts import STTM_SUBAGENT_DESCRIPTION
from v1.core.subagents.sttm.runnable import NAME, STTM_RUNNABLE
from v1.core.tools.sttm import STTM_TOOLS

STTM_SUBAGENT = {
    "name": NAME,
    "description": STTM_SUBAGENT_DESCRIPTION,
    # A fork is handed the conversation, from which it reads the analyst's
    # message itself; its graph has the stack of a declarative subagent
    # (runnable.py).
    "runnable": STTM_RUNNABLE,
    "mode": "fork",
    # deepagents does not read a compiled subagent's tools; the access-control
    # file's names do.
    "tools": STTM_TOOLS,
}
