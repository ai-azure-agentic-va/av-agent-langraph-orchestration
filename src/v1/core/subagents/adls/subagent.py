from __future__ import annotations

from v1.core.prompts import ADLS_SUBAGENT_PROMPT
from v1.core.tools import (
    get_data_quality_rules,
    get_dataset_config,
    list_dataset_files,
    list_datasets,
)


ADLS_SUBAGENT = {
    "name": "adls-agent",
    "description": (
        "Azure Data Lake Storage agent. Use for anything about datasets and their "
        "files in the data lake: which datasets are configured, a dataset's expected "
        "file path, its expected file arrival SLA, its expected file metadata "
        "(source system, dataset, file name, ingestion frequency), the data quality "
        "rules configured for it, and which files actually landed (with size and "
        "last-modified time) — the file expectations a Production Support Engineer "
        "needs during incident investigation."
    ),
    "system_prompt": ADLS_SUBAGENT_PROMPT,
    "tools": [
        list_datasets,
        get_dataset_config,
        get_data_quality_rules,
        list_dataset_files,
    ],
}
