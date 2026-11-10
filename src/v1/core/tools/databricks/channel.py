"""A per-task-call channel carrying progress and UI results out of the Databricks subagent.

The orchestrator opens a channel around each ``task`` call to the subagent, handing it its own
stream writer. The subagent's tools then send status lines through that writer and record query
results and table choices on the channel, so the orchestrator can attach them to the ``task``
result without the subagent's inner messages reaching the main chat.

The channel lives in a ``ContextVar``: asyncio copies the context into child tasks, so the tools
of one ``task`` call see that call's channel, and parallel calls never see each other's.
"""

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from langgraph.config import get_stream_writer

# ``get_stream_writer`` is looked up on this module at call time so tests can replace it.

logger = logging.getLogger(__name__)

StreamWriter = Callable[[dict], None]


@dataclass
class TaskChannel:
    """What one ``task`` call collects: where status goes, the UI results and a table choice."""

    writer: StreamWriter | None
    results: list[dict] = field(default_factory=list)
    choices: dict | None = None


_channel: ContextVar[TaskChannel | None] = ContextVar("databricks_task_channel", default=None)


def current_channel() -> TaskChannel | None:
    """The channel of the ``task`` call this code runs under, if any."""
    return _channel.get()


@contextmanager
def open_channel(writer: StreamWriter | None) -> Iterator[TaskChannel]:
    """Open a channel for the duration of the block; it is closed even when the block raises."""
    channel = TaskChannel(writer=writer)
    token = _channel.set(channel)
    try:
        yield channel
    finally:
        _channel.reset(token)


def emit_status(text: str) -> None:
    """Send a status line to the UI; a no-op outside a run. Never raises."""
    channel = current_channel()
    try:
        writer = (channel and channel.writer) or get_stream_writer()
        writer({"type": "status", "text": text})
    except Exception as exc:  # noqa: BLE001 - status is best-effort; never fail a tool over it
        # Outside a LangGraph run ``get_stream_writer`` raises; that is the expected no-op case.
        logger.debug("databricks_status_not_sent error=%s", type(exc).__name__)


def record_result(result: dict) -> None:
    """Keep a query result for the UI on the open channel; a no-op without one."""
    if (channel := current_channel()) is not None:
        channel.results.append(result)


def record_choices(choices: dict) -> None:
    """Keep the table choice for the UI on the open channel; a no-op without one."""
    if (channel := current_channel()) is not None:
        channel.choices = choices
