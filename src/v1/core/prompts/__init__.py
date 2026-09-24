from v1.core.config import get_settings
from v1.core.prompts.adf import ADF_SUBAGENT_DESCRIPTION, ADF_SUBAGENT_PROMPT
from v1.core.prompts.orchestrator import (
    ADF_ROUTING_BLOCK,
    BASE_SYSTEM_PROMPT,
    URL_READER_BLOCK,
)
from v1.core.prompts.servicenow import SERVICENOW_SUBAGENT_PROMPT

_settings = get_settings()

# The orchestrator prompt this deployment runs: the base instructions plus one
# routing block per configured capability, appended in lockstep with the matching
# subagent in ``v1.core.subagents.ENABLED_SUBAGENTS`` (or the read_url tool in
# ``v1.core.agent``).
SYSTEM_PROMPT = "\n\n".join(
    [
        BASE_SYSTEM_PROMPT,
        *([ADF_ROUTING_BLOCK] if _settings.adf_enabled else []),
        *([URL_READER_BLOCK] if _settings.url_reader_enabled else []),
    ]
)

__all__ = [
    "ADF_ROUTING_BLOCK",
    "ADF_SUBAGENT_DESCRIPTION",
    "ADF_SUBAGENT_PROMPT",
    "BASE_SYSTEM_PROMPT",
    "URL_READER_BLOCK",
    "SYSTEM_PROMPT",
    "SERVICENOW_SUBAGENT_PROMPT",
]
