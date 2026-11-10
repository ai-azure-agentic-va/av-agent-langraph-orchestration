"""Tests for the Databricks task channel (``v1.core.tools.databricks.channel``).

No LangGraph run is needed: the channel's writer is a plain list append, and
``get_stream_writer`` is replaced on the channel module where a fallback is tested.
Runs standalone (``python test_databricks_channel.py``).
"""

from __future__ import annotations

import asyncio
import contextlib

from databricks_test_support import run_tests

import v1.core.tools.databricks.channel as channel_module
from v1.core.tools.databricks.channel import (
    TaskChannel,
    current_channel,
    emit_status,
    open_channel,
    record_choices,
    record_result,
)

RESULT = {"sql": "SELECT 1", "columns": ["n"], "rows": [["1"]], "row_count": 1, "truncated": False}


@contextlib.contextmanager
def _stream_writer(get_writer):
    """Replace ``get_stream_writer`` on the channel module for the block."""
    saved = channel_module.get_stream_writer
    channel_module.get_stream_writer = get_writer
    try:
        yield
    finally:
        channel_module.get_stream_writer = saved


def test_channel_collects_results_and_status() -> None:
    sent: list[dict] = []
    with open_channel(sent.append) as channel:
        assert isinstance(channel, TaskChannel) and current_channel() is channel
        record_result(RESULT)
        record_choices({"question": "Which table should I use?", "options": []})
        emit_status("Running query…")
    assert channel.results == [RESULT]
    assert channel.choices == {"question": "Which table should I use?", "options": []}
    assert sent == [{"type": "status", "text": "Running query…"}]
    assert current_channel() is None


def test_no_channel_is_noop() -> None:
    assert current_channel() is None
    record_result(RESULT)
    record_choices({"question": "x"})
    emit_status("Running query…")  # outside a run: nothing to write to, never raises
    assert current_channel() is None


def test_emit_status_falls_back_to_the_stream_writer() -> None:
    sent: list[dict] = []
    with _stream_writer(lambda: sent.append):
        emit_status("Searching tables…")
        with open_channel(None):
            emit_status("Reading table schema…")
    assert sent == [
        {"type": "status", "text": "Searching tables…"},
        {"type": "status", "text": "Reading table schema…"},
    ]


def test_emit_status_never_raises() -> None:
    def broken(_: dict) -> None:
        raise ValueError("stream closed")

    with open_channel(broken):
        emit_status("Running query…")

    def no_run() -> None:
        raise RuntimeError("Called get_config outside of a runnable context")

    with _stream_writer(no_run):
        emit_status("Running query…")


def test_channel_is_reset_after_an_error() -> None:
    try:
        with open_channel(None):
            raise ValueError("boom")
    except ValueError:
        pass
    assert current_channel() is None


def test_channels_isolated_across_tasks() -> None:
    async def task_call(name: str) -> TaskChannel:
        with open_channel(None) as channel:
            await asyncio.sleep(0)
            record_result({"sql": name})
            await asyncio.sleep(0)
            # A child task (as a tool call inside the subagent) sees the same channel.
            await asyncio.create_task(_record_in_child(name))
            return channel

    async def both() -> list[TaskChannel]:
        return await asyncio.gather(task_call("a"), task_call("b"))

    first, second = asyncio.run(both())
    assert first.results == [{"sql": "a"}, {"sql": "a-child"}]
    assert second.results == [{"sql": "b"}, {"sql": "b-child"}]


async def _record_in_child(name: str) -> None:
    await asyncio.sleep(0)
    record_result({"sql": f"{name}-child"})


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
