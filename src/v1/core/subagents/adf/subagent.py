from __future__ import annotations

from v1.core.prompts import ADF_SUBAGENT_DESCRIPTION, ADF_SUBAGENT_PROMPT
from v1.core.tools import ADF_TOOLS


ADF_SUBAGENT = {
    "name": "adf-agent",
    "description": ADF_SUBAGENT_DESCRIPTION,
    "system_prompt": ADF_SUBAGENT_PROMPT,
    "tools": ADF_TOOLS,
}
