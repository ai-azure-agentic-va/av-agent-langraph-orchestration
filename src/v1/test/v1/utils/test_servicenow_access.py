"""Regression tests for the per-group ServiceNow access gate.

SERVICENOW-EXT: for a caller in ``SERVICENOW_DISABLED_GROUPS`` (e.g. an
external ``FIN-APP-EXT`` caller) the orchestrator must lose the ServiceNow
subagent — the ``task`` delegation tool is stripped, a restriction note is
appended to the system prompt, and any stray ``task`` -> ``servicenow-ticket-agent``
call is hard-blocked. Internal callers are untouched.

Runs standalone (``python test_servicenow_access.py``) or under pytest.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from langchain_core.messages import SystemMessage, ToolMessage

import v1.core.middlewares.servicenow_access as sa


# --- lightweight stand-ins so the test stays offline (no real ModelRequest) ---


@dataclass
class _FakeTool:
    name: str


@dataclass
class _FakeModelRequest:
    tools: list
    system_message: SystemMessage | None

    def override(self, **overrides: Any) -> "_FakeModelRequest":
        return replace(self, **overrides)


@dataclass
class _FakeToolCallRequest:
    tool_call: dict


class _Settings:
    def __init__(self, disabled: list[str]) -> None:
        self.servicenow_disabled_groups = disabled


def _patch(disabled_groups: list[str], caller_groups: tuple[str, ...]):
    """Patch settings + groups_from_config on the middleware module; restore."""

    saved_settings = sa.settings
    saved_groups = sa.groups_from_config
    sa.settings = _Settings(disabled_groups)
    sa.groups_from_config = lambda: caller_groups

    def restore() -> None:
        sa.settings = saved_settings
        sa.groups_from_config = saved_groups

    return restore


def _request_with_task() -> _FakeModelRequest:
    return _FakeModelRequest(
        tools=[_FakeTool("ai_search_tool"), _FakeTool(sa.TASK_TOOL_NAME)],
        system_message=SystemMessage(content="You are the orchestrator."),
    )


# --- disabled-for-caller resolution -----------------------------------------


def test_not_disabled_when_no_groups_configured() -> None:
    restore = _patch(disabled_groups=[], caller_groups=("FIN-APP-EXT",))
    try:
        assert sa._servicenow_disabled_for_caller() is False
    finally:
        restore()


def test_not_disabled_when_caller_not_in_group() -> None:
    restore = _patch(
        disabled_groups=["FIN-APP-EXT"],
        caller_groups=("FIN-APP-INT",),
    )
    try:
        assert sa._servicenow_disabled_for_caller() is False
    finally:
        restore()


def test_disabled_when_caller_in_group() -> None:
    restore = _patch(
        disabled_groups=["FIN-APP-EXT"],
        caller_groups=("other", "FIN-APP-EXT"),
    )
    try:
        assert sa._servicenow_disabled_for_caller() is True
    finally:
        restore()


# --- model-call gating ------------------------------------------------------


def test_external_caller_loses_task_tool_and_gets_restriction_note() -> None:
    restore = _patch(
        disabled_groups=["FIN-APP-EXT"],
        caller_groups=("FIN-APP-EXT",),
    )
    try:
        seen: dict[str, _FakeModelRequest] = {}

        def handler(request: _FakeModelRequest) -> str:
            seen["request"] = request
            return "ok"

        result = sa.ServiceNowAccessMiddleware().wrap_model_call(
            _request_with_task(), handler
        )

        assert result == "ok"
        forwarded = seen["request"]
        tool_names = [t.name for t in forwarded.tools]
        assert sa.TASK_TOOL_NAME not in tool_names  # task delegation removed
        assert "ai_search_tool" in tool_names  # KB search preserved
        assert "not available" in forwarded.system_message.text.lower()
        assert "ACCESS RESTRICTION" in forwarded.system_message.text
    finally:
        restore()


def test_internal_caller_keeps_task_tool_untouched() -> None:
    restore = _patch(
        disabled_groups=["FIN-APP-EXT"],
        caller_groups=("FIN-APP-INT",),
    )
    try:
        original = _request_with_task()
        seen: dict[str, _FakeModelRequest] = {}

        def handler(request: _FakeModelRequest) -> str:
            seen["request"] = request
            return "ok"

        sa.ServiceNowAccessMiddleware().wrap_model_call(original, handler)

        forwarded = seen["request"]
        assert forwarded is original  # passed through unmodified
        assert sa.TASK_TOOL_NAME in [t.name for t in forwarded.tools]
        assert "ACCESS RESTRICTION" not in (forwarded.system_message.text or "")
    finally:
        restore()


# --- tool-call hard block (defense-in-depth) --------------------------------


def test_external_caller_servicenow_task_call_is_blocked() -> None:
    restore = _patch(
        disabled_groups=["FIN-APP-EXT"],
        caller_groups=("FIN-APP-EXT",),
    )
    try:
        request = _FakeToolCallRequest(
            tool_call={
                "name": sa.TASK_TOOL_NAME,
                "args": {"subagent_type": sa.SERVICENOW_SUBAGENT_NAME},
                "id": "call_1",
            }
        )

        def handler(_req: Any) -> str:
            raise AssertionError("handler must not run for a blocked ServiceNow call")

        result = sa.ServiceNowAccessMiddleware().wrap_tool_call(request, handler)

        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert result.tool_call_id == "call_1"
        assert "not available" in result.content.lower()
    finally:
        restore()


def test_internal_caller_servicenow_task_call_runs() -> None:
    restore = _patch(
        disabled_groups=["FIN-APP-EXT"],
        caller_groups=("FIN-APP-INT",),
    )
    try:
        request = _FakeToolCallRequest(
            tool_call={
                "name": sa.TASK_TOOL_NAME,
                "args": {"subagent_type": sa.SERVICENOW_SUBAGENT_NAME},
                "id": "call_2",
            }
        )

        def handler(_req: Any) -> str:
            return "delegated"

        result = sa.ServiceNowAccessMiddleware().wrap_tool_call(request, handler)
        assert result == "delegated"
    finally:
        restore()


def _main() -> int:
    checks = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for check in checks:
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - standalone runner reports all
            failures += 1
            print(f"FAIL {check.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {check.__name__}")
    print(f"\n{len(checks) - failures}/{len(checks)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
