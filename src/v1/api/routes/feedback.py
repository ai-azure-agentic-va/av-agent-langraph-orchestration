"""LangSmith feedback endpoint.

Accepts a ``run_id`` returned by the chat endpoint, along with a binary score
(0 = negative, 1 = positive) and an optional free-text comment, then writes the
feedback to LangSmith so it appears under Monitoring → Feedback.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import anyio.to_thread
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

if TYPE_CHECKING:
    from langsmith import Client

logger = logging.getLogger(__name__)

_lsm: Client | None = None


def _get_client() -> Client:
    """Return a LangSmith Client, creating it on first call."""

    global _lsm
    if _lsm is None:
        from langsmith import Client

        _lsm = Client()
    return _lsm


class FeedbackPayload(BaseModel):
    run_id: str
    score: int  # 0 = thumbs down | 1 = thumbs up
    comment: str | None = None


def create_feedback_router() -> APIRouter:
    """Build the /feedback router.

    Authentication is enforced by LangGraph's auth middleware. Custom HTTP routes
    are only covered when ``http.enable_custom_route_auth`` is true in
    langgraph.json (it is) — without that flag the @auth.authenticate handler runs
    on built-in resource routes only, leaving this endpoint open.
    """

    router = APIRouter(prefix="/feedback", tags=["feedback"])

    @router.post("")
    async def submit_feedback(
        payload: FeedbackPayload,
    ) -> dict[str, str]:
        if payload.score not in (0, 1):
            raise HTTPException(status_code=422, detail="score must be 0 or 1")

        try:
            await anyio.to_thread.run_sync(
                lambda: _get_client().create_feedback(
                    run_id=payload.run_id,
                    key="user_feedback",
                    score=payload.score,
                    comment=payload.comment,
                )
            )
        except Exception as exc:
            logger.error(
                "Error creating feedback for run %s",
                payload.run_id,
                exc_info=True,
            )
            raise HTTPException(
                status_code=500,
                detail="feedback submission failed",
            ) from exc

        return {"status": "ok", "run_id": payload.run_id}

    return router