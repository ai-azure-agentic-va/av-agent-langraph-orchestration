"""Regression tests for the agent step ceiling (PROD_DEPLOYMENT_TODO §3).

CONFIG-MAXSTEPS / AGENT-RECURSION: ``agent_max_steps`` must actually cap the
parent loop. ``_build_agent_sync`` wires it as the graph's ``recursion_limit``;
this confirms the value is set and that LangGraph enforces a configured limit
without one being passed at invoke time.

Runs standalone (``python test_agent_recursion.py``) or under pytest.
"""

from __future__ import annotations

from typing import TypedDict

from langgraph.errors import GraphRecursionError
from langgraph.graph import START, StateGraph
from langgraph.pregel import Pregel


class _State(TypedDict):
    x: int


def _looping_graph() -> Pregel:
    """A graph that loops forever, so only a recursion_limit can stop it."""

    builder = StateGraph(_State)
    builder.add_node("inc", lambda state: {"x": state["x"] + 1})
    builder.add_edge(START, "inc")
    builder.add_edge("inc", "inc")
    return builder.compile()


def test_configured_recursion_limit_is_enforced() -> None:
    # No recursion_limit passed at invoke time — it must come from the config
    # baked in by with_config, which is exactly how _build_agent_sync sets it.
    graph = _looping_graph().with_config({"recursion_limit": 4})
    try:
        graph.invoke({"x": 0})
    except GraphRecursionError:
        return
    raise AssertionError("expected GraphRecursionError with a configured limit")


def test_build_agent_sets_recursion_limit_from_config() -> None:
    import v1.core.agent as agent_mod

    tiny = _looping_graph()
    saved = {
        "create_deep_agent": agent_mod.create_deep_agent,
        "get_azure_chat_model": agent_mod.get_azure_chat_model,
        "build_backend": agent_mod.build_backend,
        "ensure": agent_mod._ensure_harness_profiles_registered,
    }
    try:
        agent_mod.create_deep_agent = lambda **kwargs: tiny
        agent_mod.get_azure_chat_model = lambda: None
        agent_mod.build_backend = lambda: None
        agent_mod._ensure_harness_profiles_registered = lambda: None

        built = agent_mod._build_agent_sync(checkpointer=None)

        # Still a Pregel (with_config returns a copy, not a RunnableBinding), so
        # the platform's downstream checkpointer/store injection keeps working.
        assert isinstance(built, Pregel)
        assert built.config["recursion_limit"] == agent_mod.settings.agent_max_steps
    finally:
        agent_mod.create_deep_agent = saved["create_deep_agent"]
        agent_mod.get_azure_chat_model = saved["get_azure_chat_model"]
        agent_mod.build_backend = saved["build_backend"]
        agent_mod._ensure_harness_profiles_registered = saved["ensure"]


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
