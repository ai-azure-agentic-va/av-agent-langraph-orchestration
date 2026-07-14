"""Regression tests for the per-group subagent access gate.

For a caller in ``SERVICENOW_DISABLED_GROUPS`` / ``ADF_DISABLED_GROUPS`` (e.g.
an external ``FIN-APP-EXT`` caller) the orchestrator must lose exactly the
disabled subagent(s): a restriction note is appended to the system prompt and
any stray ``task`` call to a disabled ``subagent_type`` is hard-blocked. The
``task`` delegation tool itself is stripped ONLY when every registered
subagent is disabled — with one subagent still allowed, ``task`` must survive
so that delegation keeps working. Internal callers are untouched.

Runs standalone (``python test_subagent_access.py``) or under pytest.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from langchain_core.messages import SystemMessage, ToolMessage

import v1.core.middlewares.subagent_access as sa


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
    def __init__(
        self,
        servicenow_disabled: list[str] | None = None,
        adf_disabled: list[str] | None = None,
        adf_factories: dict | None = None,
    ) -> None:
        self.servicenow_disabled_groups = servicenow_disabled or []
        self.adf_disabled_groups = adf_disabled or []
        self.adf_factory_mapping = adf_factories if adf_factories is not None else {"fin": {}}


def _patch(settings: _Settings, caller_groups: tuple[str, ...]):
    """Patch settings + groups_from_config on the middleware module; restore."""

    saved_settings = sa.settings
    saved_groups = sa.groups_from_config
    sa.settings = settings
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


def _task_call(subagent_type: str, call_id: str) -> _FakeToolCallRequest:
    return _FakeToolCallRequest(
        tool_call={
            "name": sa.TASK_TOOL_NAME,
            "args": {"subagent_type": subagent_type},
            "id": call_id,
        }
    )


def _forwarded_request(request: _FakeModelRequest) -> _FakeModelRequest:
    seen: dict[str, _FakeModelRequest] = {}

    def handler(req: _FakeModelRequest) -> str:
        seen["request"] = req
        return "ok"

    result = sa.SubagentAccessMiddleware().wrap_model_call(request, handler)
    assert result == "ok"
    return seen["request"]


# --- disabled-for-caller resolution -----------------------------------------


def test_nothing_disabled_when_no_groups_configured() -> None:
    restore = _patch(_Settings(), caller_groups=("FIN-APP-EXT",))
    try:
        assert sa._disabled_subagents_for_caller() == frozenset()
    finally:
        restore()


def test_nothing_disabled_when_caller_not_in_groups() -> None:
    restore = _patch(
        _Settings(servicenow_disabled=["FIN-APP-EXT"], adf_disabled=["FIN-APP-EXT"]),
        caller_groups=("FIN-APP-INT",),
    )
    try:
        assert sa._disabled_subagents_for_caller() == frozenset()
    finally:
        restore()


def test_each_gate_matches_its_own_groups() -> None:
    restore = _patch(
        _Settings(servicenow_disabled=["FIN-APP-EXT"], adf_disabled=["FIN-NO-ADF"]),
        caller_groups=("other", "FIN-NO-ADF"),
    )
    try:
        assert sa._disabled_subagents_for_caller() == frozenset({sa.ADF_SUBAGENT_NAME})
    finally:
        restore()


def test_unregistered_adf_gate_is_ignored() -> None:
    # No factories configured -> the ADF subagent is not wired, so its gate
    # must not fire even for a caller in ADF_DISABLED_GROUPS.
    restore = _patch(
        _Settings(adf_disabled=["FIN-APP-EXT"], adf_factories={}),
        caller_groups=("FIN-APP-EXT",),
    )
    try:
        assert sa._disabled_subagents_for_caller() == frozenset()
    finally:
        restore()


# --- model-call gating ------------------------------------------------------


def test_servicenow_only_disabled_keeps_task_and_notes_servicenow() -> None:
    restore = _patch(
        _Settings(servicenow_disabled=["FIN-APP-EXT"]),
        caller_groups=("FIN-APP-EXT",),
    )
    try:
        forwarded = _forwarded_request(_request_with_task())
        tool_names = [t.name for t in forwarded.tools]
        assert sa.TASK_TOOL_NAME in tool_names  # ADF delegation must survive
        assert "ai_search_tool" in tool_names
        text = forwarded.system_message.text
        assert "ACCESS RESTRICTION" in text
        assert "ServiceNow is NOT available" in text
        assert "Data Factory is NOT available" not in text
        assert sa.TASK_TOOL_REMOVED_NOTE not in text
    finally:
        restore()


def test_adf_only_disabled_keeps_task_and_notes_adf() -> None:
    restore = _patch(
        _Settings(adf_disabled=["FIN-NO-ADF"]),
        caller_groups=("FIN-NO-ADF",),
    )
    try:
        forwarded = _forwarded_request(_request_with_task())
        assert sa.TASK_TOOL_NAME in [t.name for t in forwarded.tools]
        text = forwarded.system_message.text
        assert "Azure Data Factory is NOT available" in text
        assert "ServiceNow is NOT available" not in text
    finally:
        restore()


def test_all_subagents_disabled_drops_task_tool() -> None:
    restore = _patch(
        _Settings(servicenow_disabled=["FIN-APP-EXT"], adf_disabled=["FIN-APP-EXT"]),
        caller_groups=("FIN-APP-EXT",),
    )
    try:
        forwarded = _forwarded_request(_request_with_task())
        tool_names = [t.name for t in forwarded.tools]
        assert sa.TASK_TOOL_NAME not in tool_names  # no allowed target remains
        assert "ai_search_tool" in tool_names  # KB search preserved
        text = forwarded.system_message.text
        assert "ServiceNow is NOT available" in text
        assert "Azure Data Factory is NOT available" in text
        assert sa.TASK_TOOL_REMOVED_NOTE in text
    finally:
        restore()


def test_servicenow_disabled_without_adf_registered_drops_task_tool() -> None:
    # Pre-ADF behavior preserved: ServiceNow is the only registered subagent,
    # so disabling it removes the task tool entirely.
    restore = _patch(
        _Settings(servicenow_disabled=["FIN-APP-EXT"], adf_factories={}),
        caller_groups=("FIN-APP-EXT",),
    )
    try:
        forwarded = _forwarded_request(_request_with_task())
        assert sa.TASK_TOOL_NAME not in [t.name for t in forwarded.tools]
        assert sa.TASK_TOOL_REMOVED_NOTE in forwarded.system_message.text
    finally:
        restore()


def test_internal_caller_keeps_request_untouched() -> None:
    restore = _patch(
        _Settings(servicenow_disabled=["FIN-APP-EXT"], adf_disabled=["FIN-NO-ADF"]),
        caller_groups=("FIN-APP-INT",),
    )
    try:
        original = _request_with_task()
        forwarded = _forwarded_request(original)
        assert forwarded is original  # passed through unmodified
        assert sa.TASK_TOOL_NAME in [t.name for t in forwarded.tools]
        assert "ACCESS RESTRICTION" not in (forwarded.system_message.text or "")
    finally:
        restore()


# --- tool-call hard block (defense-in-depth) --------------------------------


def test_disabled_servicenow_task_call_is_blocked() -> None:
    restore = _patch(
        _Settings(servicenow_disabled=["FIN-APP-EXT"]),
        caller_groups=("FIN-APP-EXT",),
    )
    try:
        request = _task_call(sa.SERVICENOW_SUBAGENT_NAME, "call_1")

        def handler(_req: Any) -> str:
            raise AssertionError("handler must not run for a blocked ServiceNow call")

        result = sa.SubagentAccessMiddleware().wrap_tool_call(request, handler)

        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert result.tool_call_id == "call_1"
        assert "not available" in result.content.lower()
    finally:
        restore()


def test_disabled_adf_task_call_is_blocked_but_servicenow_runs() -> None:
    restore = _patch(
        _Settings(adf_disabled=["FIN-NO-ADF"]),
        caller_groups=("FIN-NO-ADF",),
    )
    try:
        blocked = sa.SubagentAccessMiddleware().wrap_tool_call(
            _task_call(sa.ADF_SUBAGENT_NAME, "call_2"),
            lambda _req: "must not run",
        )
        assert isinstance(blocked, ToolMessage)
        assert blocked.status == "error"
        assert "data factory" in blocked.content.lower()

        allowed = sa.SubagentAccessMiddleware().wrap_tool_call(
            _task_call(sa.SERVICENOW_SUBAGENT_NAME, "call_3"),
            lambda _req: "delegated",
        )
        assert allowed == "delegated"
    finally:
        restore()


def test_internal_caller_task_calls_run() -> None:
    restore = _patch(
        _Settings(servicenow_disabled=["FIN-APP-EXT"], adf_disabled=["FIN-NO-ADF"]),
        caller_groups=("FIN-APP-INT",),
    )
    try:
        for subagent in (sa.SERVICENOW_SUBAGENT_NAME, sa.ADF_SUBAGENT_NAME):
            result = sa.SubagentAccessMiddleware().wrap_tool_call(
                _task_call(subagent, "call_4"),
                lambda _req: "delegated",
            )
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
