"""Starter-prompts endpoint.

Returns a list of suggested prompts the chat UI renders as one-click starters.
Each entry has a short ``label`` (button text) and the ``message`` that is sent
to the agent when the starter is selected.

The set is group-scoped: ``TENANT_GROUP_STARTER_PROMPTS_MAPPING`` maps an Entra
group object-id (or display name) to its own prompt list, mirroring how
``TENANT_GROUP_INDEX_MAPPING`` scopes the search index. The caller's groups come
from the authenticated principal that LangGraph stamps onto ``request.user``.
Callers matching no mapped group get an empty list — there is no built-in
fallback, so starter prompts are shown only to members of a mapped group.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel

from v1.core.config import get_settings
from v1.utils.group_routing import groups_from_request, resolve_for_groups

logger = logging.getLogger(__name__)
settings = get_settings()


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
        groups = groups_from_request(request)
        prompts = resolve_for_groups(
            settings.tenant_group_starter_prompts_mapping, groups, []
        )
        logger.debug(
            "starter-prompts routing: group_count=%d prompt_count=%d (groups_mapped=%d)",
            len(groups),
            len(prompts),
            len(settings.tenant_group_starter_prompts_mapping),
        )
        return StarterPromptsResponse(
            prompts=[StarterPrompt(**prompt) for prompt in prompts]
        )

    return router
