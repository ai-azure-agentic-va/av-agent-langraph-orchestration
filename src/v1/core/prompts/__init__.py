from v1.core.prompts.adf import ADF_SUBAGENT_DESCRIPTION, ADF_SUBAGENT_PROMPT
from v1.core.prompts.adls import ADLS_SUBAGENT_DESCRIPTION, ADLS_SUBAGENT_PROMPT
from v1.core.prompts.databricks import (
    DATABRICKS_SUBAGENT_DESCRIPTION,
    DATABRICKS_SUBAGENT_PROMPT,
)
from v1.core.prompts.orchestrator import BASE_SYSTEM_PROMPT
from v1.core.prompts.servicenow import (
    SERVICENOW_SUBAGENT_DESCRIPTION,
    SERVICENOW_SUBAGENT_PROMPT,
)
from v1.core.prompts.sttm import STTM_SUBAGENT_DESCRIPTION, STTM_SUBAGENT_PROMPT

# The orchestrator prompt is the same everywhere: it names no capability. Each
# one reaches the model only through its own description (a subagent's line in
# the ``task`` tool, a direct tool's docstring), so what is unwired or blocked
# for a caller is simply absent.
SYSTEM_PROMPT = BASE_SYSTEM_PROMPT

__all__ = [
    "ADF_SUBAGENT_DESCRIPTION",
    "ADF_SUBAGENT_PROMPT",
    "ADLS_SUBAGENT_DESCRIPTION",
    "ADLS_SUBAGENT_PROMPT",
    "BASE_SYSTEM_PROMPT",
    "DATABRICKS_SUBAGENT_DESCRIPTION",
    "DATABRICKS_SUBAGENT_PROMPT",
    "SYSTEM_PROMPT",
    "SERVICENOW_SUBAGENT_DESCRIPTION",
    "SERVICENOW_SUBAGENT_PROMPT",
    "STTM_SUBAGENT_DESCRIPTION",
    "STTM_SUBAGENT_PROMPT",
]
