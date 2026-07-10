from __future__ import annotations

from v1.core.prompts import SERVICENOW_SUBAGENT_PROMPT
from v1.core.tools import (
    ai_search_tool,
    calculator,
    get_current_datetime,
    servicenow_get_ticket_detail,
    servicenow_list_tickets,
)


SERVICENOW_SUBAGENT = {
    "name": "servicenow-ticket-agent",
    "description": (
        "Use for ServiceNow ticket tasks: getting one ticket's details (summary or "
        "full card) and listing tickets by optional status."
    ),
    "system_prompt": SERVICENOW_SUBAGENT_PROMPT,
    "tools": [
        servicenow_get_ticket_detail,
        servicenow_list_tickets,
        ai_search_tool,
        get_current_datetime,
        calculator,
    ],
}