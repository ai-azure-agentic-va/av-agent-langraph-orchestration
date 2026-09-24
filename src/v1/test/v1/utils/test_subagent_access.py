"""Regression tests for the per-group subagent access gate.

For a caller in ``ADF_DISABLED_GROUPS`` (e.g. an external ``FIN-APP-EXT``
caller) the orchestrator must lose exactly the disabled subagent: a restriction
note is appended to the system prompt and any stray ``task`` call to a disabled
``subagent_type`` is hard-blocked. The ``task`` delegation tool itself is NEVER
stripped — ServiceNow is always registered and never gated, so delegation always
has a legitimate target and removing ``task`` would take ServiceNow down with
the restricted subagent. Internal callers are untouched.

ServiceNow is deliberately NOT gated here (incidents and KB articles are scoped
inside ServiceNow itself), so the servicenow-ticket-agent must stay reachable
for every caller — the last three tests pin that.

Runs standalone (``python test_subagent_access.py``) or under pytest.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from langchain_core.messages import SystemMessage, ToolMessage

import v1.core.middlewares.subagent_access as sa
from v1.core.subagents.servicenow.subagent import SERVICENOW_SUBAGENT


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
        adf_disabled: list[str] | None = None,
        adf_factories: dict | None = None,
    ) -> None:
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
        _Settings(adf_disabled=["FIN-APP-EXT"]),
        caller_groups=("FIN-APP-INT",),
    )
    try:
        assert sa._disabled_subagents_for_caller() == frozenset()
    finally:
        restore()


def test_gate_matches_its_own_groups() -> None:
    restore = _patch(
        _Settings(adf_disabled=["FIN-NO-ADF"]),
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


def test_adf_disabled_keeps_task_and_notes_adf() -> None:
    restore = _patch(
        _Settings(adf_disabled=["FIN-NO-ADF"]),
        caller_groups=("FIN-NO-ADF",),
    )
    try:
        forwarded = _forwarded_request(_request_with_task())
        tool_names = [t.name for t in forwarded.tools]
        # ServiceNow delegation must survive an ADF restriction: `task` is the
        # only way to reach the (ungated) ServiceNow subagent.
        assert sa.TASK_TOOL_NAME in tool_names
        assert "ai_search_tool" in tool_names
        text = forwarded.system_message.text
        assert "ACCESS RESTRICTION" in text
        assert "Azure Data Factory is NOT available" in text
    finally:
        restore()


def test_unregistered_adf_leaves_the_prompt_untouched() -> None:
    # No factories configured: the caller is in ADF_DISABLED_GROUPS but there is
    # no ADF subagent to restrict, so no note about a capability that never
    # existed in this deployment.
    restore = _patch(
        _Settings(adf_disabled=["FIN-APP-EXT"], adf_factories={}),
        caller_groups=("FIN-APP-EXT",),
    )
    try:
        original = _request_with_task()
        forwarded = _forwarded_request(original)
        assert forwarded is original
        assert sa.TASK_TOOL_NAME in [t.name for t in forwarded.tools]
        assert "ACCESS RESTRICTION" not in (forwarded.system_message.text or "")
    finally:
        restore()


def test_internal_caller_keeps_request_untouched() -> None:
    restore = _patch(
        _Settings(adf_disabled=["FIN-NO-ADF"]),
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


def test_disabled_adf_task_call_is_blocked() -> None:
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
    finally:
        restore()


def test_internal_caller_task_calls_run() -> None:
    restore = _patch(
        _Settings(adf_disabled=["FIN-NO-ADF"]),
        caller_groups=("FIN-APP-INT",),
    )
    try:
        result = sa.SubagentAccessMiddleware().wrap_tool_call(
            _task_call(sa.ADF_SUBAGENT_NAME, "call_4"),
            lambda _req: "delegated",
        )
        assert result == "delegated"
    finally:
        restore()


def test_non_task_tool_calls_are_never_blocked() -> None:
    restore = _patch(
        _Settings(adf_disabled=["FIN-APP-EXT"]),
        caller_groups=("FIN-APP-EXT",),
    )
    try:
        request = _FakeToolCallRequest(
            tool_call={"name": "ai_search_tool", "args": {"query": "x"}, "id": "call_8"}
        )
        result = sa.SubagentAccessMiddleware().wrap_tool_call(request, lambda _req: "searched")
        assert result == "searched"
    finally:
        restore()


# --- ServiceNow is not gated here -------------------------------------------


def test_servicenow_delegation_is_never_blocked() -> None:
    """No gate covers ServiceNow: even a restricted caller may delegate to it."""

    restore = _patch(
        _Settings(adf_disabled=["FIN-APP-EXT"]),
        caller_groups=("FIN-APP-EXT",),
    )
    try:
        assert sa._disabled_subagents_for_caller() == frozenset({sa.ADF_SUBAGENT_NAME})
        result = sa.SubagentAccessMiddleware().wrap_tool_call(
            _task_call(SERVICENOW_SUBAGENT["name"], "call_7"),
            lambda _req: "delegated",
        )
        assert result == "delegated"
        text = _forwarded_request(_request_with_task()).system_message.text
        assert "ServiceNow" not in text
    finally:
        restore()


def test_task_tool_survives_so_servicenow_stays_reachable() -> None:
    """The gated ADF caller must still be SHOWN `task` — ServiceNow needs it.

    ADF is the only gate, so "every gated subagent is disabled" is true for any
    ADF-restricted caller. Dropping `task` there would silently remove the
    ServiceNow subagent, which is deliberately ungated.
    """

    restore = _patch(
        _Settings(adf_disabled=["FIN-APP-EXT"]),
        caller_groups=("FIN-APP-EXT",),
    )
    try:
        forwarded = _forwarded_request(_request_with_task())
        assert sa.TASK_TOOL_NAME in [t.name for t in forwarded.tools]
    finally:
        restore()


def test_knowledge_tool_lives_on_the_servicenow_subagent() -> None:
    """The KB tool is ServiceNow-specific: it belongs to the subagent, not the orchestrator."""

    assert "servicenow_search_knowledge" in {
        tool.name for tool in SERVICENOW_SUBAGENT["tools"]
    }


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
