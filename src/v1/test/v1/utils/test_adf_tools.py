"""Offline tests for the ADF subagent tools.

The Azure management client is replaced with an in-memory fake, so the tests
cover the tool-facing behavior: factory alias resolution (default / named /
unknown / unset), the run-tree walk with its recursion budget, and the error
truncation helpers. No network, no credentials.

Runs standalone (``python test_adf_tools.py``) or under pytest.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    parameters: dict | None = None


@dataclass
class _Activity:
    activity_name: str
    activity_type: str
    status: str
    error: dict | None = None
    output: Any = None
    activity_run_id: str = "act-1"


@dataclass
class _Named:
    name: str


@dataclass
class _Definition:
    """What pipelines.get returns: the activity list of a pipeline definition.

    Activities are plain dicts keyed by their wire names, which is how the tools
    read them — the SDK's activity models are MutableMappings over the same ARM
    JSON, and container activities nest raw dicts either way.
    """

    activities: list


class _Value:
    def __init__(self, value: list, continuation_token: str | None = None) -> None:
        self.value = value
        self.continuation_token = continuation_token


class _FakeClient:
    """Mimics the aio DataFactoryManagementClient surface the tools touch."""

    def __init__(
        self,
        pipelines: list[str] | None = None,
        runs: dict[str, _Run] | None = None,
        activities: dict[str, list[_Activity]] | None = None,
        definitions: dict[str, _Definition] | None = None,
        run_pages: list[_Value] | None = None,
        fail_on: str = "",
    ) -> None:
        outer = self
        self._pipelines = pipelines or []
        self._runs = runs or {}
        self._activities = activities or {}
        self._definitions = definitions or {}
        self._run_pages = list(run_pages or [])
        self._fail_on = fail_on
        # Every RunFilterParameters the tools sent, in order — the assertion
        # seam for filters and date windows, since ADF itself does the filtering.
        self.run_queries: list[Any] = []

        def _maybe_fail(call: str) -> None:
            if outer._fail_on == call:
                raise RuntimeError(f"{call} denied: (403) AuthorizationFailed")

        class _Pipelines:
            def list_by_factory(self, rg: str, factory: str):
                async def _gen():
                    _maybe_fail("list_by_factory")
                    for name in outer._pipelines:
                        yield _Named(name)

                return _gen()

            async def get(self, rg: str, factory: str, pipeline_name: str) -> _Definition:
                _maybe_fail("pipelines.get")
                try:
                    return outer._definitions[pipeline_name]
                except KeyError as missing:
                    raise RuntimeError(f"pipeline {pipeline_name} not found") from missing

        class _PipelineRuns:
            async def get(self, rg: str, factory: str, run_id: str) -> _Run:
                try:
                    return outer._runs[run_id]
                except KeyError as missing:
                    raise RuntimeError(f"run {run_id} not found") from missing

            async def query_by_factory(self, rg: str, factory: str, filter_parameters=None):
                _maybe_fail("query_by_factory")
                outer.run_queries.append(filter_parameters)
                if not outer._run_pages:
                    return _Value(list(outer._runs.values()))
                # One page per query, repeating the last one for extra queries.
                page = min(len(outer.run_queries), len(outer._run_pages)) - 1
                return outer._run_pages[page]

        class _ActivityRuns:
            async def query_by_pipeline_run(
                self, rg: str, factory: str, run_id: str, filter_parameters=None
            ):
                _maybe_fail("activity_runs")
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


# --- list_pipelines ----------------------------------------------------------


def test_list_pipelines_names_factory_alias() -> None:
    client = _FakeClient(pipelines=["pl_orchestrator", "pl_load"])
    restore = _patch(_Settings({"fin": _FIN}), client)
    try:
        result = _run(adf.list_pipelines.ainvoke({}))
        assert "Factory 'fin' has 2 pipeline(s)" in result
        assert "pl_orchestrator" in result and "pl_load" in result
    finally:
        restore()


# --- list_pipeline_runs: the query sent to ADF --------------------------------


def _sole_query(client: _FakeClient):
    """The one RunFilterParameters the tool sent."""
    assert len(client.run_queries) == 1, f"expected 1 query, got {len(client.run_queries)}"
    return client.run_queries[0]


def test_run_filters_are_sent_for_pipeline_status_and_trigger() -> None:
    client = _FakeClient(run_pages=[_Value([])])
    restore = _patch(_Settings({"fin": _FIN}), client)
    try:
        result = _run(
            adf.list_pipeline_runs.ainvoke(
                {"pipeline_name": " pl_load ", "status": "failed", "trigger_name": "tr_2h"}
            )
        )
        sent = _sole_query(client)
        assert {(f.operand, tuple(f.values_property)) for f in sent.filters} == {
            ("PipelineName", ("pl_load",)),
            ("Status", ("Failed",)),  # a caller's casing is normalized for ADF
            ("TriggeredByName", ("tr_2h",)),
        }
        # the name echoed back matches the one queried, with no stray whitespace
        assert "pipeline 'pl_load'" in result
    finally:
        restore()


def test_no_criteria_means_no_filters_and_a_rolling_window() -> None:
    client = _FakeClient(run_pages=[_Value([])])
    restore = _patch(_Settings({"fin": _FIN}), client)
    try:
        _run(adf.list_pipeline_runs.ainvoke({"last_n_days": 3}))
        sent = _sole_query(client)
        assert sent.filters == []
        assert sent.last_updated_before - sent.last_updated_after == timedelta(days=3)
        assert (datetime.now(timezone.utc) - sent.last_updated_before) < timedelta(minutes=1)
        assert (sent.order_by[0].order_by, sent.order_by[0].order) == ("RunStart", "DESC")
    finally:
        restore()


def test_zero_days_is_clamped_to_a_one_day_window() -> None:
    """ADF always needs a bounded window, so 0 cannot mean "everything"."""
    client = _FakeClient(run_pages=[_Value([])])
    restore = _patch(_Settings({"fin": _FIN}), client)
    try:
        result = _run(adf.list_pipeline_runs.ainvoke({"last_n_days": 0}))
        sent = _sole_query(client)
        assert sent.last_updated_before - sent.last_updated_after == timedelta(days=1)
        assert "in the last 1 day(s)" in result
    finally:
        restore()


def test_named_date_range_covers_the_whole_end_day() -> None:
    client = _FakeClient(run_pages=[_Value([])])
    restore = _patch(_Settings({"fin": _FIN}), client)
    try:
        result = _run(
            adf.list_pipeline_runs.ainvoke({"start_date": "2026-07-10", "end_date": "2026-07-12"})
        )
        sent = _sole_query(client)
        assert sent.last_updated_after == datetime(2026, 7, 10, tzinfo=timezone.utc)
        # end_date is inclusive, so the window runs to the following midnight...
        assert sent.last_updated_before == datetime(2026, 7, 13, tzinfo=timezone.utc)
        # ...while the message still names the last day the user asked about
        assert "between 2026-07-10 and 2026-07-12" in result
    finally:
        restore()


def test_end_date_alone_counts_the_window_back_from_that_date() -> None:
    """Regression: anchoring the start to "now" emptied any window ending in the past.

    'runs up to Jul 12' left last_updated_after at now-7d, which is AFTER the
    end of the window, so ADF matched nothing and the tool reported "no runs" —
    a wrong answer rather than an error.
    """
    client = _FakeClient(run_pages=[_Value([])])
    restore = _patch(_Settings({"fin": _FIN}), client)
    try:
        result = _run(adf.list_pipeline_runs.ainvoke({"end_date": "2026-07-12", "last_n_days": 3}))
        sent = _sole_query(client)
        assert sent.last_updated_after == datetime(2026, 7, 10, tzinfo=timezone.utc)
        assert sent.last_updated_before == datetime(2026, 7, 13, tzinfo=timezone.utc)
        assert "between 2026-07-10 and 2026-07-12" in result
    finally:
        restore()


def test_reversed_date_range_is_reported_instead_of_no_runs() -> None:
    client = _FakeClient(run_pages=[_Value([])])
    restore = _patch(_Settings({"fin": _FIN}), client)
    try:
        result = _run(
            adf.list_pipeline_runs.ainvoke({"start_date": "2026-07-20", "end_date": "2026-07-10"})
        )
        assert "Empty date window" in result
        assert client.run_queries == []  # nothing was asked of ADF
    finally:
        restore()


def test_invalid_date_names_the_field_and_the_format() -> None:
    client = _FakeClient(run_pages=[_Value([])])
    restore = _patch(_Settings({"fin": _FIN}), client)
    try:
        result = _run(adf.list_pipeline_runs.ainvoke({"start_date": "10-07-2026"}))
        assert "Invalid start_date" in result and "YYYY-MM-DD" in result
        assert client.run_queries == []
    finally:
        restore()


def test_unknown_status_is_rejected_rather_than_matching_nothing() -> None:
    """A plausible-but-wrong status must not read as "this pipeline never failed"."""
    client = _FakeClient(run_pages=[_Value([])])
    restore = _patch(_Settings({"fin": _FIN}), client)
    try:
        result = _run(adf.list_pipeline_runs.ainvoke({"status": "Canceled"}))
        assert "Unknown status 'Canceled'" in result
        assert "Cancelled" in result  # the valid spelling is offered
        assert client.run_queries == []
    finally:
        restore()


# --- list_pipeline_runs: rendering -------------------------------------------


def test_run_listing_pages_the_window_and_counts_every_page() -> None:
    pages = [
        _Value([_Run("r1", "pl_load", "Failed")], continuation_token="page-2"),
        _Value([_Run("r2", "pl_load", "Succeeded")]),
    ]
    client = _FakeClient(run_pages=pages)
    restore = _patch(_Settings({"fin": _FIN}), client)
    try:
        result = _run(adf.list_pipeline_runs.ainvoke({"pipeline_name": "pl_load"}))
        assert len(client.run_queries) == 2  # the continuation token was followed
        assert "2 run(s) (newest first) in factory 'fin' for 'pl_load'" in result
        assert "runId=r1" in result and "runId=r2" in result
        assert "lower bounds" not in result  # the window was fully paged
    finally:
        restore()


def test_run_listing_marks_counts_as_lower_bounds_when_pages_run_out() -> None:
    client = _FakeClient(run_pages=[_Value([_Run("r1", "pl_load", "Failed")], "more")])
    restore = _patch(_Settings({"fin": _FIN}), client)
    saved = adf._RUNS_MAX_PAGES
    adf._RUNS_MAX_PAGES = 1
    try:
        result = _run(adf.list_pipeline_runs.ainvoke({}))
        assert "1+ run(s)" in result
        assert "lower bounds" in result
    finally:
        adf._RUNS_MAX_PAGES = saved
        restore()


def test_capped_listing_still_reports_totals_for_the_whole_window() -> None:
    runs = [
        _Run("r1", "pl_load", "Failed"),
        _Run("r2", "pl_load", "Failed"),
        _Run("r3", "pl_report", "Succeeded"),
    ]
    client = _FakeClient(run_pages=[_Value(runs)])
    restore = _patch(_Settings({"fin": _FIN}), client)
    saved = adf._MAX_RUN_ROWS
    adf._MAX_RUN_ROWS = 2
    try:
        result = _run(adf.list_pipeline_runs.ainvoke({}))
        assert "3 run(s)" in result
        assert "pl_load | Failed: 2" in result
        assert "pl_report | Succeeded: 1" in result
        assert "showing the newest 2 runs" in result
        assert "runId=r3" not in result  # beyond the row cap, but still counted
    finally:
        adf._MAX_RUN_ROWS = saved
        restore()


def test_run_rows_carry_the_trigger_and_parent_run_id() -> None:
    runs = [
        _Run("r1", "pl_load", "Failed", invoked_by=_InvokedBy("tr_nightly", "ScheduleTrigger")),
        _Run("r2", "pl_copy", "Failed", invoked_by=_InvokedBy("Exec", "PipelineActivity", "r1")),
    ]
    client = _FakeClient(run_pages=[_Value(runs)])
    restore = _patch(_Settings({"fin": _FIN}), client)
    try:
        result = _run(adf.list_pipeline_runs.ainvoke({}))
        assert "triggeredBy=tr_nightly (ScheduleTrigger)" in result
        assert "parentRunId=r1" in result  # the only link back up a hierarchy
    finally:
        restore()


def test_no_runs_in_a_rolling_window_names_the_pipeline_and_window() -> None:
    client = _FakeClient(run_pages=[_Value([])])
    restore = _patch(_Settings({"fin": _FIN}), client)
    try:
        result = _run(
            adf.list_pipeline_runs.ainvoke({"pipeline_name": "pl_load", "last_n_days": 5})
        )
        assert "No runs for pipeline 'pl_load' in factory 'fin' in the last 5 day(s)." in result
    finally:
        restore()


def test_run_query_error_is_returned_as_text() -> None:
    client = _FakeClient(fail_on="query_by_factory")
    restore = _patch(_Settings({"fin": _FIN}), client)
    try:
        result = _run(adf.list_pipeline_runs.ainvoke({}))
        assert "ERROR querying runs in factory 'fin'" in result
        assert "AuthorizationFailed" in result
    finally:
        restore()


def test_list_pipelines_error_is_returned_as_text() -> None:
    client = _FakeClient(fail_on="list_by_factory")
    restore = _patch(_Settings({"fin": _FIN}), client)
    try:
        result = _run(adf.list_pipelines.ainvoke({}))
        assert "ERROR listing pipelines in factory 'fin'" in result
    finally:
        restore()


def test_list_pipelines_reports_an_empty_factory() -> None:
    restore = _patch(_Settings({"fin": _FIN}), _FakeClient(pipelines=[]))
    try:
        assert "has no pipelines" in _run(adf.list_pipelines.ainvoke({}))
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


def test_run_details_renders_every_activity_with_errors_and_output() -> None:
    runs = {
        "r1": _Run(
            "r1",
            "pl_load",
            "Failed",
            invoked_by=_InvokedBy("tr_nightly", "ScheduleTrigger"),
            message="<html>Activity Copy data failed</html>",
            parameters={"business_date": "2026-07-12"},
        )
    }
    activities = {
        "r1": [
            _Activity(
                "Copy data",
                "Copy",
                "Failed",
                error={"errorCode": "2200", "message": "<b>Table not found</b>"},
                activity_run_id="act-9",
            ),
            _Activity(
                "Notify",
                "WebActivity",
                "Succeeded",
                output={"status": "sent"},
                activity_run_id="act-10",
            ),
        ]
    }
    client = _FakeClient(runs=runs, activities=activities)
    restore = _patch(_Settings({"fin": _FIN}), client)
    try:
        result = _run(adf.get_pipeline_run_details.ainvoke({"run_id": " r1 "}))
        assert "pipeline    : pl_load" in result
        assert "status      : Failed" in result
        assert "triggeredBy : tr_nightly (ScheduleTrigger)" in result
        assert "business_date" in result  # run parameters are surfaced
        assert "message     : Activity Copy data failed" in result  # HTML stripped
        assert "activities (2):" in result
        assert "• Copy data [Copy] → Failed (activityRunId=act-9)" in result
        assert "error 2200: Table not found" in result and "<b>" not in result
        assert "output: " in result and "sent" in result
    finally:
        restore()


def test_run_details_reports_a_run_with_no_activities() -> None:
    client = _FakeClient(runs={"r1": _Run("r1", "pl_load", "Queued")}, activities={})
    restore = _patch(_Settings({"fin": _FIN}), client)
    try:
        result = _run(adf.get_pipeline_run_details.ainvoke({"run_id": "r1"}))
        assert "activities: (none reported)" in result
    finally:
        restore()


def test_run_details_keeps_the_run_when_activity_query_fails() -> None:
    """A denied activity query must not lose the run status already fetched."""
    client = _FakeClient(runs={"r1": _Run("r1", "pl_load", "Failed")}, fail_on="activity_runs")
    restore = _patch(_Settings({"fin": _FIN}), client)
    try:
        result = _run(adf.get_pipeline_run_details.ainvoke({"run_id": "r1"}))
        assert "status      : Failed" in result
        assert "activities: ERROR querying activity runs" in result
    finally:
        restore()


def test_run_details_reports_an_unknown_run_id() -> None:
    client = _FakeClient(runs={})
    restore = _patch(_Settings({"fin": _FIN}), client)
    try:
        result = _run(adf.get_pipeline_run_details.ainvoke({"run_id": "nope"}))
        assert "ERROR fetching run 'nope' in factory 'fin'" in result
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
    def _child_of(parent: str) -> _InvokedBy:
        return _InvokedBy("Exec", "PipelineActivity", parent)

    runs = {
        "root": _Run(
            "root", "pl_L1_DailyMaster", "Failed", invoked_by=_InvokedBy("tr", "ScheduleTrigger")
        ),
        "mid": _Run("mid", "pl_L2_Ingest", "Failed", invoked_by=_child_of("root")),
        "leaf": _Run("leaf", "pl_L3_Copy", "Failed", invoked_by=_child_of("mid")),
        "ok": _Run("ok", "pl_L2_Transform", "Succeeded", invoked_by=_child_of("root")),
    }
    activities = {
        "root": [
            _Activity("Exec_Ingest", "ExecutePipeline", "Failed", output={"pipelineRunId": "mid"}),
            _Activity(
                "Exec_Transform", "ExecutePipeline", "Succeeded", output={"pipelineRunId": "ok"}
            ),
        ],
        "mid": [
            _Activity("Exec_Copy", "ExecutePipeline", "Failed", output={"pipelineRunId": "leaf"})
        ],
        "leaf": [
            _Activity(
                "CopyFile",
                "Copy",
                "Failed",
                error={"errorCode": "5001", "message": "source missing"},
            )
        ],
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
        str(i): [
            _Activity("Exec", "ExecutePipeline", "Failed", output={"pipelineRunId": str(i + 1)})
        ]
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


# --- get_pipeline_structure ----------------------------------------------------


def test_structure_shows_invoked_children_and_nested_containers() -> None:
    definition = _Definition(
        activities=[
            {
                "name": "Exec_Ingest",
                "type": "ExecutePipeline",
                "typeProperties": {
                    "pipeline": {"referenceName": "pl_L2_Ingest", "type": "PipelineReference"}
                },
            },
            {
                "name": "Loop_Files",
                "type": "ForEach",
                "typeProperties": {
                    "activities": [{"name": "Copy_File", "type": "Copy", "typeProperties": {}}]
                },
            },
            {
                "name": "Gate",
                "type": "IfCondition",
                "typeProperties": {
                    "ifTrueActivities": [{"name": "Proceed", "type": "Wait"}],
                    "ifFalseActivities": [{"name": "Halt", "type": "Fail"}],
                },
            },
        ]
    )
    client = _FakeClient(definitions={"pl_L1_DailyMaster": definition})
    restore = _patch(_Settings({"fin": _FIN}), client)
    try:
        result = _run(
            adf.get_pipeline_structure.ainvoke({"pipeline_name": " pl_L1_DailyMaster "})
        )
        assert "Structure of 'pl_L1_DailyMaster' (factory 'fin')" in result
        assert "  - Exec_Ingest [ExecutePipeline] → invokes pl_L2_Ingest" in result
        assert "  - Loop_Files [ForEach]" in result
        assert "    - Copy_File [Copy]" in result  # nested one level deeper
        assert "    - Proceed [Wait]" in result
        assert "    - Halt [Fail]" in result  # both branches of the condition
    finally:
        restore()


def test_structure_requires_a_pipeline_name() -> None:
    restore = _patch(_Settings({"fin": _FIN}), _FakeClient())
    try:
        result = _run(adf.get_pipeline_structure.ainvoke({"pipeline_name": "  "}))
        assert "provide a pipeline name" in result
    finally:
        restore()


def test_structure_reports_a_pipeline_with_no_activities() -> None:
    client = _FakeClient(definitions={"pl_empty": _Definition(activities=[])})
    restore = _patch(_Settings({"fin": _FIN}), client)
    try:
        result = _run(adf.get_pipeline_structure.ainvoke({"pipeline_name": "pl_empty"}))
        assert "has no activities" in result
    finally:
        restore()


def test_structure_reports_an_unknown_pipeline() -> None:
    client = _FakeClient(definitions={})
    restore = _patch(_Settings({"fin": _FIN}), client)
    try:
        result = _run(adf.get_pipeline_structure.ainvoke({"pipeline_name": "pl_nope"}))
        assert "ERROR fetching pipeline 'pl_nope' in factory 'fin'" in result
    finally:
        restore()


# --- wiring -------------------------------------------------------------------


def test_subagent_exposes_every_tool_under_the_gated_name() -> None:
    """The subagent's name must match the access gate's, or the gate silently
    stops protecting it; its tool list must cover all five capabilities."""
    from v1.core.middlewares.subagent_access import ADF_SUBAGENT_NAME
    from v1.core.subagents import ADF_SUBAGENT

    assert ADF_SUBAGENT["name"] == ADF_SUBAGENT_NAME
    assert {tool.name for tool in ADF_SUBAGENT["tools"]} == {
        "list_pipelines",
        "list_pipeline_runs",
        "get_pipeline_run_details",
        "get_pipeline_run_tree",
        "get_pipeline_structure",
    }


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
