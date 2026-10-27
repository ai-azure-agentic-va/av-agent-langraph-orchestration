from v1.core.config import get_settings
from v1.core.subagents.adf import ADF_SUBAGENT, close_adf_resources
from v1.core.subagents.adls import ADLS_SUBAGENT, close_adls_resources
from v1.core.subagents.servicenow import SERVICENOW_SUBAGENT, close_servicenow_resources

_settings = get_settings()

# The roster handed to ``create_deep_agent``. ServiceNow is always wired; ADF and
# ADLS join only where configured, in lockstep with their routing blocks in
# ``v1.core.prompts.SYSTEM_PROMPT``.
ENABLED_SUBAGENTS = [
    SERVICENOW_SUBAGENT,
    *([ADF_SUBAGENT] if _settings.adf_enabled else []),
    *([ADLS_SUBAGENT] if _settings.adls_enabled else []),
]

__all__ = [
    "ADF_SUBAGENT",
    "ADLS_SUBAGENT",
    "ENABLED_SUBAGENTS",
    "SERVICENOW_SUBAGENT",
    "close_adf_resources",
    "close_adls_resources",
    "close_servicenow_resources",
]
