from __future__ import annotations

from v1.core.prompts import ADLS_SUBAGENT_DESCRIPTION, ADLS_SUBAGENT_PROMPT
from v1.core.tools import ADLS_TOOLS


ADLS_SUBAGENT = {
    "name": "adls-agent",
    "description": ADLS_SUBAGENT_DESCRIPTION,
    "system_prompt": ADLS_SUBAGENT_PROMPT,
    "tools": ADLS_TOOLS,
}
