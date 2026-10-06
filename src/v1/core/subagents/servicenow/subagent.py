from __future__ import annotations

from v1.core.middlewares.user_context import SUBAGENT_GUIDANCE, UserContextMiddleware
from v1.core.prompts import SERVICENOW_SUBAGENT_PROMPT
from v1.core.tools import (
    ai_search_tool,
    calculator,
    get_current_datetime,
    servicenow_find_similar_resolutions,
    servicenow_get_ticket_detail,
    servicenow_list_tickets,
    servicenow_search_knowledge,
)

SERVICENOW_SUBAGENT = {
    "name": "servicenow-ticket-agent",
    "description": (
        "Use for ALL ServiceNow tasks. Incidents are its default: getting one "
        "ticket's details (summary or full card) and listing/searching tickets by "
        "optional status. That includes WHO worked on something — \"which "
        "engineer worked on <X>\", \"what is <person> working on\" — whatever <X> "
        "names: an incident search, never `ai_search_tool`, and never out of "
        "scope. It also searches ServiceNow's own knowledge articles "
        "(KB…) whenever the task is a PROCEDURE ask — the steps to do something, "
        "how to run or perform it, the process for it, or a KB number — whether "
        "or not the task text names an article."
    ),
    "system_prompt": SERVICENOW_SUBAGENT_PROMPT,
    "tools": [
        servicenow_get_ticket_detail,
        servicenow_list_tickets,
        servicenow_find_similar_resolutions,
        servicenow_search_knowledge,
        ai_search_tool,
        get_current_datetime,
        calculator,
    ],
    # The task text is all a subagent otherwise sees, so "my incidents" needs the
    # signed-in user's name to resolve (see the FIRST PERSON people rule).
    "middleware": [UserContextMiddleware(guidance=SUBAGENT_GUIDANCE)],
}
