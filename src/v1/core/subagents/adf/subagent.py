from __future__ import annotations

from v1.core.prompts import ADF_SUBAGENT_PROMPT
from v1.core.tools import ADF_TOOLS


ADF_SUBAGENT = {
    "name": "adf-agent",
    "description": (
        "Azure Data Factory agent. Use for anything about data pipelines (names often "
        "start with 'pl_') and their runs: listing the configured factories, listing "
        "pipelines, describing what a pipeline does and its structure/hierarchy (which "
        "child pipelines it invokes), listing recent runs, and diagnosing failures — "
        "including walking a hierarchical run's full parent→child run tree to find the "
        "root cause."
    ),
    "system_prompt": ADF_SUBAGENT_PROMPT,
    "tools": ADF_TOOLS,
}
