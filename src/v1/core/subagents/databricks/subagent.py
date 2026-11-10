from __future__ import annotations

from v1.core.middlewares.databricks_selection import (
    SUBAGENT_SELECTION_NOTE,
    SUBAGENT_SELECTION_NOTE_HIDDEN,
    DatabricksSelectionMiddleware,
)
from v1.core.middlewares.user_context import SUBAGENT_GUIDANCE, UserContextMiddleware
from v1.core.prompts import DATABRICKS_SUBAGENT_DESCRIPTION, DATABRICKS_SUBAGENT_PROMPT
from v1.core.tools import DATABRICKS_TOOLS

# Every tool runs as the app (its Databricks grants, narrowed by the workspace's
# allowlist), not as the user, so Databricks does not scope "my tables" or "my
# job" to the user; the signed-in-user block tells the subagent who "my" is in
# the delegated task text. The selection note names the tables the user
# picked for this chat, so the subagent queries them instead of searching.
DATABRICKS_SUBAGENT = {
    "name": "databricks-agent",
    "description": DATABRICKS_SUBAGENT_DESCRIPTION,
    "system_prompt": DATABRICKS_SUBAGENT_PROMPT,
    "tools": DATABRICKS_TOOLS,
    "middleware": [
        UserContextMiddleware(guidance=SUBAGENT_GUIDANCE),
        DatabricksSelectionMiddleware(
            SUBAGENT_SELECTION_NOTE, hidden_note=SUBAGENT_SELECTION_NOTE_HIDDEN
        ),
    ],
}
