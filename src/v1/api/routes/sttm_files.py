"""Download endpoint for the workbooks the sttm-agent builds.

``GET /sttm/files/{file_id}`` serves a file the ``sttm_workbook`` tool kept in
the platform's store (:mod:`v1.core.tools.sttm.files`), to the signed-in caller
whose run built it, until it expires. The links the tool writes use the
frontend's path for this route (``STTM_DOWNLOAD_BASE_URL``): its ``/api`` proxy
forwards them here with the caller's token.

A caller the access-control file keeps from the sttm-agent, or from its
workbook tool, gets nothing, even for a file built before the change. That
refusal, and a malformed, unknown, expired or someone else's id, all get the
same 404, so no one can tell an id that exists from one that does not.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response
from langgraph.store.base import BaseStore

from v1.core.middlewares.access_control import TASK_TOOL_NAME, caller_access
from v1.core.subagents.sttm import STTM_SUBAGENT
from v1.core.tools.sttm.files import get_file, is_file_id
from v1.core.tools.sttm.tools import sttm_workbook
from v1.utils.group_routing import groups_from_request

logger = logging.getLogger(__name__)

NOT_FOUND = (
    "This download link has expired or is not yours. Ask the STTM agent to build the "
    "workbook again."
)
UNAVAILABLE = "Downloads are not available right now. Try again in a few minutes."
# A download is the caller's own file: no cache may keep it, and no browser may
# read it as anything but the type it is sent as.
_PRIVATE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def _platform_store() -> BaseStore | None:
    """The platform's store, which langgraph-api makes reachable from custom routes."""

    from langgraph.config import get_store

    try:
        return get_store()
    except (RuntimeError, KeyError, AttributeError):  # not served by langgraph-api
        return None


def _identity(request: Request) -> str | None:
    """The caller's identity, as the run that built their files saw it."""

    try:
        value = getattr(request.user, "identity", None)
    except (AssertionError, KeyError):  # no auth middleware set request.user
        return None
    return str(value) if value else None


def _blocked(request: Request) -> bool:
    """Whether the caller's access no longer reaches the workbook tool."""

    access = caller_access(groups_from_request(request))
    return (
        STTM_SUBAGENT["name"] in access.blocked_subagents
        or TASK_TOOL_NAME in access.blocked_tools
        or sttm_workbook.name in access.blocked_tools
    )


def _attachment(name: str) -> str:
    # The quoted form only for a name that needs it, as Starlette's FileResponse does.
    quoted = quote(name)
    if quoted != name:
        return f"attachment; filename*=utf-8''{quoted}"
    return f'attachment; filename="{name}"'


def _not_found(file_id: str, reason: str) -> PlainTextResponse:
    logger.info(
        "STTM download refused (%s): %s", reason, file_id if is_file_id(file_id) else "bad id"
    )
    return PlainTextResponse(NOT_FOUND, status_code=404, headers=_PRIVATE)


def create_sttm_files_router(
    store: Callable[[], BaseStore | None] = _platform_store,
) -> APIRouter:
    """Build the /sttm/files router; ``store`` gives the store to read files from.

    Authentication is enforced by LangGraph's auth middleware, which covers custom
    routes because ``http.enable_custom_route_auth`` is true in langgraph.json; it
    is also what sets ``request.user``, the caller this route serves.
    """

    router = APIRouter(prefix="/sttm/files", tags=["sttm"])

    @router.get("/{file_id}")
    async def download_sttm_file(file_id: str, request: Request) -> Response:
        owner = _identity(request)
        if owner is None:
            return _not_found(file_id, "no signed-in caller")
        if _blocked(request):
            return _not_found(file_id, "access")
        kept = store()
        if kept is None:
            logger.error("STTM download unavailable: this server has no store")
            return PlainTextResponse(UNAVAILABLE, status_code=503, headers=_PRIVATE)
        try:
            found = await get_file(kept, owner=owner, file_id=file_id, now=datetime.now(UTC))
        except Exception:  # the store is down
            logger.exception("STTM download unavailable: the store could not be read")
            return PlainTextResponse(UNAVAILABLE, status_code=503, headers=_PRIVATE)
        if found is None:
            return _not_found(file_id, "no such file for this caller, or expired")
        return Response(
            content=found.content,
            media_type=found.media_type,
            headers={**_PRIVATE, "Content-Disposition": _attachment(found.name)},
        )

    return router


__all__ = ["NOT_FOUND", "UNAVAILABLE", "create_sttm_files_router"]
