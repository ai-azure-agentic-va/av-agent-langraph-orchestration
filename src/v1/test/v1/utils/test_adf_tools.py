"""Offline tests for the ADF subagent tools.

The Azure management client is replaced with an in-memory fake, so the tests
cover the tool-facing behavior: factory alias resolution (default / named /
unknown / unset), the run-tree walk with its recursion budget, and the error
truncation helpers. No network, no credentials.

Runs standalone (``python test_adf_tools.py``) or under pytest.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import v1.core.tools.adf.tools as adf


# --- fakes -------------------------------------------------------------------


class _Settings:
    def __init__(self, mapping: dict, default: str | None = None) -> None:
        self.adf_factory_mapping = mapping
        self.adf_default_factory = default


_FIN = {"subscription_id": "sub-1", "resource_group": "rg-fin", "factory_name": "adf-fin"}
_RISK = {"subscription_id": "sub-2", "resource_group": "rg-risk", "factory_name": "adf-risk"}


@dataclass
class _InvokedBy:
    name: str
    invoked_by_type: str
    pipeline_run_id: str | None = None


@dataclass
class _Run:
    run_id: str
    pipeline_name: str
    status: str
    run_start: Any = None
    run_end: Any = None
    duration_in_ms: int = 1000
    message: str = ""
    invoked_by: Any = None


@dataclass
class _Activity:
    activity_name: str
    activity_type: str
    status: str
    error: dict | None = None
    output: Any = None


@dataclass
class _Named:
    name: str


class _Value:
    def __init__(self, value: list) -> None:
        self.value = value


class _FakeClient:
    """Mimics the aio DataFactoryManagementClient surface the tools touch."""

    def __init__(
        self,
        pipelines: list[str] | None = None,
        runs: dict[str, _Run] | None = None,
        activities: dict[str, list[_Activity]] | None = None,
    ) -> None:
        outer = self
        self._pipelines = pipelines or []
        self._runs = runs or {}
        self._activities = activities or {}

        class _Pipelines:
            def list_by_factory(self, rg: str, factory: str):
                async def _gen():
                    for name in outer._pipelines:
                        yield _Named(name)

                return _gen()

        class _PipelineRuns:
            async def get(self, rg: str, factory: str, run_id: str) -> _Run:
                try:
                    return outer._runs[run_id]
                except KeyError as missing:
                    raise RuntimeError(f"run {run_id} not found") from missing

            async def query_by_factory(self, rg: str, factory: str, filter_parameters=None):
                return _Value(list(outer._runs.values()))

        class _ActivityRuns:
            async def query_by_pipeline_run(
                self, rg: str, factory: str, run_id: str, filter_parameters=None
            ):
                return _Value(outer._activities.get(run_id, []))

        self.pipelines = _Pipelines()
        self.pipeline_runs = _PipelineRuns()
        self.activity_runs = _ActivityRuns()


def _patch(settings: _Settings, client: _FakeClient | None = None):
    """Patch settings + client factory on the tools module; restore."""

    saved_settings = adf.settings
    saved_client = adf._client
    adf.settings = settings

    async def _fake_client(subscription_id: str) -> _FakeClient:
        return client or _FakeClient()

    adf._client = _fake_client

    def restore() -> None:
        adf.settings = saved_settings
        adf._client = saved_client

    return restore


def _run(coro):
    return asyncio.run(coro)


# --- factory resolution -------------------------------------------------------


def test_default_factory_used_when_unset_and_single_mapping() -> None:
    restore = _patch(_Settings({"fin": _FIN}))
    try:
        assert adf._resolve_factory("") == ("fin", "sub-1", "rg-fin", "adf-fin")
    finally:
        restore()


def test_configured_default_wins_with_multiple_factories() -> None:
    restore = _patch(_Settings({"fin": _FIN, "risk": _RISK}, default="risk"))
    try:
        assert adf._resolve_factory("")[0] == "risk"
        assert adf._resolve_factory("fin")[0] == "fin"  # explicit alias still works
    finally:
        restore()


def test_multiple_factories_without_default_asks_for_alias() -> None:
    restore = _patch(_Settings({"fin": _FIN, "risk": _RISK}))
    try:
        result = _run(adf.list_pipelines.ainvoke({"factory": ""}))
        assert "no default is set" in result
        assert "fin" in result and "risk" in result
    finally:
        restore()


def test_unknown_alias_lists_available_factories() -> None:
    restore = _patch(_Settings({"fin": _FIN, "risk": _RISK}, default="fin"))
    try:
        result = _run(adf.list_pipelines.ainvoke({"factory": "nope"}))
        assert "Unknown factory 'nope'" in result
        assert "fin" in result and "risk" in result
    finally:
        restore()


def test_no_mapping_configured_is_reported() -> None:
    restore = _patch(_Settings({}))
    try:
        result = _run(adf.list_pipelines.ainvoke({"factory": ""}))
        assert "No Data Factory is configured" in result
    finally:
        restore()


def test_misconfigured_entry_names_missing_keys() -> None:
    restore = _patch(_Settings({"fin": {"subscription_id": "sub-1"}}))
    try:
        result = _run(adf.list_pipelines.ainvoke({"factory": "fin"}))
        assert "misconfigured" in result
        assert "resource_group" in result and "factory_name" in result
    finally:
        restore()


# --- list_factories / list_pipelines -----------------------------------------


def test_list_factories_marks_default() -> None:
    restore = _patch(_Settings({"fin": _FIN, "risk": _RISK}, default="fin"))
    try:
        result = _run(adf.list_factories.ainvoke({}))
        assert "fin: factory 'adf-fin'  (default)" in result
        assert "risk: factory 'adf-risk'" in result
    finally:
        restore()


def test_list_pipelines_names_factory_alias() -> None:
    client = _FakeClient(pipelines=["pl_orchestrator", "pl_load"])
    restore = _patch(_Settings({"fin": _FIN}), client)
    try:
        result = _run(adf.list_pipelines.ainvoke({}))
        assert "Factory 'fin' has 2 pipeline(s)" in result
        assert "pl_orchestrator" in result and "pl_load" in result
    finally:
        restore()


# --- run details / run tree ----------------------------------------------------


def test_run_details_requires_run_id() -> None:
    restore = _patch(_Settings({"fin": _FIN}))
    try:
        result = _run(adf.get_pipeline_run_details.ainvoke({"run_id": "  "}))
        assert "provide a pipeline run_id" in result
    finally:
        restore()


def test_run_tree_follows_failed_child_to_root_cause() -> None:
    runs = {
        "parent": _Run("parent", "pl_orchestrator", "Failed"),
        "child": _Run("child", "pl_load", "Failed"),
    }
    activities = {
        "parent": [
            _Activity(
                "Run pl_load",
                "ExecutePipeline",
                "Failed",
                error={"errorCode": "2200", "message": "child failed"},
                output={"pipelineRunId": "child"},
            ),
            _Activity("Notify", "WebActivity", "Succeeded"),
        ],
        "child": [
            _Activity(
                "Copy data",
                "Copy",
                "Failed",
                error={"errorCode": "2200", "message": "<html>Table not found</html>"},
            )
        ],
    }
    client = _FakeClient(runs=runs, activities=activities)
    restore = _patch(_Settings({"fin": _FIN}), client)
    try:
        result = _run(adf.get_pipeline_run_tree.ainvoke({"run_id": "parent"}))
        assert "pl_orchestrator (runId=parent) → Failed" in result
        assert "pl_load (runId=child) → Failed" in result
        assert "Copy data [Copy] → Failed" in result  # root cause reached
        assert "Table not found" in result and "<html>" not in result  # HTML stripped
    finally:
        restore()


def test_run_tree_climbs_from_child_to_root_and_counts_family() -> None:
    """A failed CHILD run must still yield the whole family.

    Mirrors the shape of the pl_L1_DailyMaster demo tree: the failure is a leaf,
    its ancestors failed only by propagation, and a sibling succeeded.
    """
    runs = {
        "root": _Run("root", "pl_L1_DailyMaster", "Failed", invoked_by=_InvokedBy("tr", "ScheduleTrigger")),
        "mid": _Run("mid", "pl_L2_Ingest", "Failed", invoked_by=_InvokedBy("Exec", "PipelineActivity", "root")),
        "leaf": _Run("leaf", "pl_L3_Copy", "Failed", invoked_by=_InvokedBy("Exec", "PipelineActivity", "mid")),
        "ok": _Run("ok", "pl_L2_Transform", "Succeeded", invoked_by=_InvokedBy("Exec", "PipelineActivity", "root")),
    }
    activities = {
        "root": [
            _Activity("Exec_Ingest", "ExecutePipeline", "Failed", output={"pipelineRunId": "mid"}),
            _Activity("Exec_Transform", "ExecutePipeline", "Succeeded", output={"pipelineRunId": "ok"}),
        ],
        "mid": [_Activity("Exec_Copy", "ExecutePipeline", "Failed", output={"pipelineRunId": "leaf"})],
        "leaf": [_Activity("CopyFile", "Copy", "Failed", error={"errorCode": "5001", "message": "source missing"})],
        "ok": [_Activity("Transform", "Wait", "Succeeded")],
    }
    client = _FakeClient(runs=runs, activities=activities)
    restore = _patch(_Settings({"fin": _FIN}), client)
    try:
        # asked about the LEAF, not the root — the tool must climb up first
        result = _run(adf.get_pipeline_run_tree.ainvoke({"run_id": "leaf"}))
        assert "is a CHILD" in result and "runId=root" in result
        assert "pl_L1_DailyMaster (runId=root) → Failed" in result  # climbed to the root
        assert "source missing" in result  # root cause still reached
        # the succeeded sibling branch is expanded, so the counts are real
        assert "pl_L2_Transform (runId=ok) → Succeeded" in result
        assert "family: 4 pipeline run(s) — 3 Failed, 1 Succeeded" in result
    finally:
        restore()


def test_run_tree_depth_cap_counts_pipeline_levels() -> None:
    """_TREE_MAX_DEPTH must mean N pipeline levels, not N indent steps."""
    runs = {str(i): _Run(str(i), f"pl_L{i}", "Failed") for i in range(6)}
    activities = {
        str(i): [_Activity("Exec", "ExecutePipeline", "Failed", output={"pipelineRunId": str(i + 1)})]
        for i in range(5)
    }
    activities["5"] = [_Activity("Fail", "Fail", "Failed", error={"message": "deepest"})]
    client = _FakeClient(runs=runs, activities=activities)
    restore = _patch(_Settings({"fin": _FIN}), client)
    saved = adf._TREE_MAX_DEPTH
    adf._TREE_MAX_DEPTH = 6  # 6 levels allowed -> all 6 runs must be reached
    try:
        result = _run(adf.get_pipeline_run_tree.ainvoke({"run_id": "0"}))
        assert "max depth" not in result
        assert "deepest" in result  # the 6th level was actually walked
        assert "family: 6 pipeline run(s) — 6 Failed" in result
    finally:
        adf._TREE_MAX_DEPTH = saved
        restore()


def test_run_tree_respects_run_budget() -> None:
    runs = {
        "parent": _Run("parent", "pl_orchestrator", "Failed"),
        "child": _Run("child", "pl_load", "Failed"),
    }
    activities = {
        "parent": [
            _Activity(
                "Run pl_load",
                "ExecutePipeline",
                "Failed",
                output={"pipelineRunId": "child"},
            )
        ]
    }
    client = _FakeClient(runs=runs, activities=activities)
    restore = _patch(_Settings({"fin": _FIN}), client)
    saved_budget = adf._TREE_MAX_RUNS
    adf._TREE_MAX_RUNS = 1  # only the parent fits
    try:
        result = _run(adf.get_pipeline_run_tree.ainvoke({"run_id": "parent"}))
        assert "run budget reached" in result
        assert "pl_load (runId=child)" not in result
    finally:
        adf._TREE_MAX_RUNS = saved_budget
        restore()


# --- helpers -------------------------------------------------------------------


def test_truncate_caps_and_marks() -> None:
    long_text = "word " * 400
    result = adf._truncate(long_text)
    assert len(result) <= adf._MAX_MSG + len(" …[truncated]")
    assert result.endswith("…[truncated]")


def test_clean_error_strips_html() -> None:
    assert adf._clean_error("<html><b>boom</b></html>") == "boom"


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
