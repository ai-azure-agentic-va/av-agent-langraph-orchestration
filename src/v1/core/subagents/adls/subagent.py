from __future__ import annotations

from v1.core.middlewares.azure_links import AzureLinksMiddleware
from v1.core.middlewares.tool_markdown import ToolMarkdownMiddleware
from v1.core.prompts import ADLS_SUBAGENT_DESCRIPTION, ADLS_SUBAGENT_PROMPT
from v1.core.tools import ADLS_TOOLS

ADLS_SUBAGENT = {
    "name": "adls-agent",
    "description": ADLS_SUBAGENT_DESCRIPTION,
    "system_prompt": ADLS_SUBAGENT_PROMPT,
    "tools": ADLS_TOOLS,
    # The file-arrival result reaches the model as markdown, so the facts it
    # returns verbatim render one per line, not as one paragraph. Its answer's
    # Azure links are checked against its tools' results, as every subagent's are.
    "middleware": [
        AzureLinksMiddleware(label="adls-agent's answer"),
        ToolMarkdownMiddleware(tool.name for tool in ADLS_TOOLS),
    ],
}
