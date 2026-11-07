"""Starter-prompts endpoint.

Returns a list of suggested prompts the chat UI renders as one-click starters.
Each entry has a short ``label`` (button text) and the ``message`` that is sent
to the agent when the starter is selected.

The set is group-scoped: each group in the access-control file
(``v1.core.middlewares.access_control``) can carry its own ``starter_prompts``,
the same way it scopes the search index. The caller's groups come from the
authenticated principal that LangGraph stamps onto ``request.user``. A caller in
several groups gets the highest-priority group's prompts. Callers in no group
with prompts get an empty list — there is no built-in fallback.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel

from v1.core.middlewares.access_control import caller_access
from v1.utils.group_routing import groups_from_request

logger = logging.getLogger(__name__)


class StarterPrompt(BaseModel):
    label: str
    message: str


class StarterPromptsResponse(BaseModel):
    prompts: list[StarterPrompt]


def create_starter_prompts_router() -> APIRouter:
    """Build the /starter-prompts router.

    Authentication is enforced by LangGraph's auth middleware. Custom HTTP routes
    are only covered when ``http.enable_custom_route_auth`` is true in
    langgraph.json (it is); that flag is also what populates ``request.user`` so
    the group-scoped prompt routing below works — without it every caller gets an
    empty prompt list.
    """

    router = APIRouter(prefix="/starter-prompts", tags=["starter-prompts"])

    @router.get("")
    async def get_starter_prompts(request: Request) -> StarterPromptsResponse:
        access = caller_access(groups_from_request(request))
        logger.debug(
            "starter-prompts routing: matched_groups=%d prompt_count=%d",
            len(access.groups),
            len(access.starter_prompts),
        )
        return StarterPromptsResponse(
            prompts=[
                StarterPrompt(label=prompt.label, message=prompt.message)
                for prompt in access.starter_prompts
            ]
        )

    return router
