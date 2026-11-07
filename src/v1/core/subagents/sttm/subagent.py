from __future__ import annotations

from v1.core.middlewares.azure_links import AzureLinksMiddleware
from v1.core.prompts import STTM_SUBAGENT_DESCRIPTION, STTM_SUBAGENT_PROMPT
from v1.core.tools.sttm import STTM_TOOLS

STTM_SUBAGENT = {
    "name": "sttm-agent",
    "description": STTM_SUBAGENT_DESCRIPTION,
    "system_prompt": STTM_SUBAGENT_PROMPT,
    "tools": STTM_TOOLS,
    # Its answer's Azure links are checked against its tools' results, as every
    # subagent's are.
    "middleware": [AzureLinksMiddleware(label="sttm-agent's answer")],
}
