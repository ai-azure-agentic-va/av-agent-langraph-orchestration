from __future__ import annotations

from v1.core.prompts import ADLS_SUBAGENT_PROMPT
from v1.core.tools import ADLS_TOOLS


ADLS_SUBAGENT = {
    "name": "adls-agent",
    "description": (
        "Azure Data Lake Storage agent. Use for anything about datasets and their "
        "files in the data lake: which datasets are configured, a dataset's expected "
        "file path, its expected file arrival SLA, its expected file metadata "
        "(source system, dataset, file name, ingestion frequency), the data quality "
        "rules configured for it, and which files actually landed (with size and "
        "last-modified time) — the file expectations a Production Support Engineer "
        "needs during incident investigation. Also owns the enterprise DQ rules "
        "configuration: given just a dataset/table name from a ServiceNow DQ ticket "
        "(e.g. speedpay_check_analytics) it reads that table's dq_rules_config rows "
        "(timeliness/completeness rules, time target, expected path) and can then "
        "check whether the expected file actually landed."
    ),
    "system_prompt": ADLS_SUBAGENT_PROMPT,
    "tools": ADLS_TOOLS,
}
