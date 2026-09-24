from v1.core.config import get_settings
from v1.core.subagents.adf import ADF_SUBAGENT, close_adf_resources
from v1.core.subagents.servicenow import SERVICENOW_SUBAGENT, close_servicenow_resources

# The roster handed to ``create_deep_agent``. ServiceNow is always wired; ADF
# joins only where configured, in lockstep with its routing block in
# ``v1.core.prompts.SYSTEM_PROMPT``.
ENABLED_SUBAGENTS = [
    SERVICENOW_SUBAGENT,
    *([ADF_SUBAGENT] if get_settings().adf_enabled else []),
]

__all__ = [
    "ADF_SUBAGENT",
    "ENABLED_SUBAGENTS",
    "SERVICENOW_SUBAGENT",
    "close_adf_resources",
    "close_servicenow_resources",
]
