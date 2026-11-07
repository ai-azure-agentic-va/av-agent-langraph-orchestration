from __future__ import annotations

from v1.core.middlewares.azure_links import AzureLinksMiddleware
from v1.core.middlewares.user_context import SUBAGENT_GUIDANCE, UserContextMiddleware
from v1.core.prompts import SERVICENOW_SUBAGENT_DESCRIPTION, SERVICENOW_SUBAGENT_PROMPT
from v1.core.tools import (
    ai_search_tool,
    calculator,
    get_current_datetime,
    servicenow_find_similar_resolutions,
    servicenow_get_ticket_detail,
    servicenow_list_change_requests,
    servicenow_list_tickets,
    servicenow_search_knowledge,
)

SERVICENOW_SUBAGENT = {
    "name": "servicenow-ticket-agent",
    "description": SERVICENOW_SUBAGENT_DESCRIPTION,
    "system_prompt": SERVICENOW_SUBAGENT_PROMPT,
    "tools": [
        servicenow_get_ticket_detail,
        servicenow_list_tickets,
        servicenow_find_similar_resolutions,
        servicenow_search_knowledge,
        servicenow_list_change_requests,
        ai_search_tool,
        get_current_datetime,
        calculator,
    ],
    # The task text is all a subagent otherwise sees, so "my incidents" needs the
    # signed-in user's name to resolve (see the FIRST PERSON people rule). A
    # ticket's alert link is kept as the ticket holds it; an Azure link the
    # answer builds from a ticket's fields is checked like every subagent's.
    "middleware": [
        AzureLinksMiddleware(label="servicenow-ticket-agent's answer"),
        UserContextMiddleware(guidance=SUBAGENT_GUIDANCE),
    ],
}
