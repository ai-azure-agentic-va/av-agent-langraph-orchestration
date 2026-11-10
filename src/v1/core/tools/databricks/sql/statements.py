"""Async wrapper around the Statement Execution API, run as the app and tagged with the caller.

The SDK is synchronous, so every call runs in a worker thread. We poll ourselves (instead of
waiting server-side) so state changes can be streamed to the UI, and so a timeout or a client
disconnect cancels the statement on the warehouse.

Databricks refusing the app (403, e.g. no CAN USE on the warehouse) or its token (401), at
submit time, while polling or as the statement's own error, is ``permission_denied`` with no
message: Databricks' text never reaches the model. Table-level refusals keep their own
``permission_or_not_found`` status.

A transport failure (the SDK raises ``TimeoutError`` or ``requests.ConnectionError`` once its
retries run out) becomes a ``failed`` result with a generic message; only the exception class is
logged, since its text can carry URLs and request detail.

Each statement carries ``query_tags`` naming the caller, so the warehouse's query history
attributes app-identity queries to a user. A warehouse that rejects the tags gets the statement
once more without them: attribution must never stop a query from running.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from databricks.sdk.errors import BadRequest, DatabricksError, PermissionDenied, Unauthenticated
from databricks.sdk.service.sql import (
    Disposition,
    Format,
    QueryTag,
    ServiceErrorCode,
    StatementParameterListItem,
    StatementResponse,
    StatementState,
)

from v1.utils.databricks_client import Caller

_ACTIVE = (StatementState.PENDING, StatementState.RUNNING)
# Unity Catalog reports "no such table" and "no access" alike, so they share one status.
_NOT_FOUND_OR_DENIED = (
    "TABLE_OR_VIEW_NOT_FOUND",
    "SCHEMA_NOT_FOUND",
    "CATALOG_NOT_FOUND",
    "INSUFFICIENT_PERMISSIONS",
    "PERMISSION_DENIED",
)
# Databricks refused the app or its token (as opposed to a table it can't read).
_ACCESS_ERRORS = (PermissionDenied, Unauthenticated)
MAX_MESSAGE_CHARS = 1000
UNREACHABLE_MESSAGE = "Databricks didn't respond. Try again in a few minutes."

logger = logging.getLogger(__name__)

Status = Literal["completed", "failed", "permission_or_not_found", "permission_denied", "timeout"]

APP_TAG = "langgraph-agent"


def caller_tags(
    caller: Caller, *, workspace: str, source: Literal["agent", "api"]
) -> dict[str, str]:
    """The ``query_tags`` that attribute a statement to ``caller``; ``user_oid`` is left out
    when the caller has no object ID."""
    tags = {"app": APP_TAG, "source": source, "workspace": workspace}
    if caller.object_id is not None:
        tags["user_oid"] = caller.object_id
    return tags


@dataclass
class StatementResult:
    status: Status
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    truncated: bool = False
    message: str | None = None


def _message(text: str) -> str:
    return text.strip()[:MAX_MESSAGE_CHARS]


def _unreachable(exc: Exception, step: str) -> StatementResult:
    logger.warning("databricks_statement_unreachable step=%s error=%s", step, type(exc).__name__)
    return StatementResult("failed", message=UNREACHABLE_MESSAGE)


def _access_denied(exc: Exception) -> StatementResult:
    """The app or its token was refused; only the class is logged (the text can carry
    request detail), and nothing of it is kept on the result."""

    logger.warning("databricks_statement_access_denied error=%s", type(exc).__name__)
    return StatementResult("permission_denied")


def _tags_rejected(exc: BadRequest) -> bool:
    """Whether the warehouse refused the request because of its query tags. Covers
    ``InvalidParameterValue``, a ``BadRequest`` subclass."""
    return "query_tag" in str(exc).lower()


def _to_result(response: StatementResponse) -> StatementResult:
    status = response.status
    if status.state != StatementState.SUCCEEDED:
        if status.error and status.error.error_code == ServiceErrorCode.UNAUTHENTICATED:
            return StatementResult("permission_denied")
        error = status.error.message if status.error and status.error.message else None
        message = _message(error or status.state.value)
        if any(marker in message for marker in _NOT_FOUND_OR_DENIED):
            return StatementResult("permission_or_not_found", message=message)
        return StatementResult("failed", message=message)
    manifest = response.manifest
    schema_columns = manifest.schema.columns if manifest and manifest.schema else None
    rows = (response.result.data_array or []) if response.result else []
    return StatementResult(
        "completed",
        columns=[c.name for c in schema_columns or []],
        rows=rows,
        truncated=bool(manifest and manifest.truncated),
    )


class StatementClient:
    def __init__(
        self,
        workspace_client: Any,
        warehouse_id: str,
        *,
        timeout_s: float,
        poll_interval_s: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        query_tags: Mapping[str, str] | None = None,
    ):
        self._api = workspace_client.statement_execution
        self._warehouse_id = warehouse_id
        self._query_tags = (
            [QueryTag(key=k, value=v) for k, v in query_tags.items()] if query_tags else None
        )
        self._timeout_s = timeout_s
        self._poll_interval_s = poll_interval_s
        self._sleep = sleep
        self._clock = clock

    async def execute(
        self,
        sql: str,
        *,
        parameters: dict[str, str] | None = None,
        max_rows: int,
        on_state: Callable[[StatementState], None] | None = None,
    ) -> StatementResult:
        params = [
            StatementParameterListItem(name=k, value=v) for k, v in (parameters or {}).items()
        ]
        request = {
            "statement": sql,
            "warehouse_id": self._warehouse_id,
            "wait_timeout": "0s",
            "disposition": Disposition.INLINE,
            "format": Format.JSON_ARRAY,
            "row_limit": max_rows,
            "parameters": params or None,
        }
        try:
            try:
                response = await asyncio.to_thread(
                    self._api.execute_statement, **request, query_tags=self._query_tags
                )
            except BadRequest as exc:
                if not (self._query_tags and _tags_rejected(exc)):
                    raise
                logger.warning("databricks_query_tags_rejected warehouse=%s", self._warehouse_id)
                response = await asyncio.to_thread(
                    self._api.execute_statement, **request, query_tags=None
                )
        except _ACCESS_ERRORS as exc:  # e.g. no CAN USE on the warehouse, or a refused token
            return _access_denied(exc)
        except DatabricksError as exc:
            return StatementResult("failed", message=_message(str(exc)))
        except Exception as exc:  # noqa: BLE001 - transport errors must not end the chat turn
            return _unreachable(exc, "execute")

        statement_id = response.statement_id
        deadline = self._clock() + self._timeout_s
        last_state = None
        try:
            while (state := response.status.state) in _ACTIVE:
                if state != last_state and on_state:
                    on_state(state)
                last_state = state
                if self._clock() >= deadline:
                    await asyncio.to_thread(self._cancel_quietly, statement_id)
                    return StatementResult(
                        "timeout", message=f"no result after {self._timeout_s:g} seconds"
                    )
                await self._sleep(self._poll_interval_s)
                response = await asyncio.to_thread(self._api.get_statement, statement_id)
        except asyncio.CancelledError:
            # The client went away: stop the query instead of leaving it running. Fire and
            # forget in a worker thread: the SDK call can block for its whole retry window.
            asyncio.get_running_loop().run_in_executor(None, self._cancel_quietly, statement_id)
            raise
        except Exception as exc:  # noqa: BLE001 - transport errors must not end the chat turn
            await asyncio.to_thread(self._cancel_quietly, statement_id)
            if isinstance(exc, _ACCESS_ERRORS):
                return _access_denied(exc)
            if isinstance(exc, DatabricksError):
                return StatementResult("failed", message=_message(str(exc)))
            return _unreachable(exc, "poll")
        return _to_result(response)

    def _cancel_quietly(self, statement_id: str) -> None:
        try:
            self._api.cancel_execution(statement_id)
        except Exception:  # noqa: BLE001, S110 - best effort; the warehouse stops abandoned ones too
            pass
