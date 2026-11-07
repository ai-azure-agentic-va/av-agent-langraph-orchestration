from v1.core.config import get_settings
from v1.core.subagents.adf import ADF_SUBAGENT, close_adf_resources
from v1.core.subagents.adls import ADLS_SUBAGENT, close_adls_resources
from v1.core.subagents.servicenow import SERVICENOW_SUBAGENT, close_servicenow_resources
from v1.core.subagents.sttm import STTM_SUBAGENT

_settings = get_settings()

# The roster handed to ``create_deep_agent``. ServiceNow is always wired; ADF and
# ADLS join only where configured, and the sttm-agent only where it is turned on.
# Each one's description is all the orchestrator learns of it, so an unwired one
# is never mentioned.
ENABLED_SUBAGENTS = [
    SERVICENOW_SUBAGENT,
    *([ADF_SUBAGENT] if _settings.adf_enabled else []),
    *([ADLS_SUBAGENT] if _settings.adls_enabled else []),
    *([STTM_SUBAGENT] if _settings.sttm_enabled else []),
]

# Every subagent the backend has, wired here or not: the subagent names, and with
# their tools the tool names, an access-control file may block. One file serves
# deployments that configure different subagents.
ALL_SUBAGENTS = [SERVICENOW_SUBAGENT, ADF_SUBAGENT, ADLS_SUBAGENT, STTM_SUBAGENT]

__all__ = [
    "ADF_SUBAGENT",
    "ADLS_SUBAGENT",
    "ALL_SUBAGENTS",
    "ENABLED_SUBAGENTS",
    "SERVICENOW_SUBAGENT",
    "STTM_SUBAGENT",
    "close_adf_resources",
    "close_adls_resources",
    "close_servicenow_resources",
]
