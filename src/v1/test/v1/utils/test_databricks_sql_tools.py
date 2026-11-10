"""Tests for the Databricks SQL tools (ported from databricks-agent ``test_tools.py``).

The plain functions (``search``, ``describe``, ``run_query``) run against a fake
``StatementClient``. The LangChain tools run with ``app_workspace_client`` and
``run_workspace`` replaced through ``v1.core.tools.databricks.sql.tools``, and the caller
given as the run's ``langgraph_auth_user``. No network.
Runs standalone (``python test_databricks_sql_tools.py``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from types import SimpleNamespace

from databricks.sdk.service.sql import StatementState
from azure.core.exceptions import ClientAuthenticationError
from databricks_test_support import FakeStatementClient, patched_env, platform_user, run_tests
from databricks_test_support import captured_logs as _captured_logs

from v1.utils import databricks_client

import v1.core.tools.databricks.run_workspace as run_workspace_module
import v1.core.tools.databricks.sql.selection as sql_selection
import v1.core.tools.databricks.sql.tools as sql_tools
from v1.core.config import Settings
from v1.core.databricks_workspaces import WORKSPACE_UNAVAILABLE_MESSAGE, Workspace
from v1.core.tools.databricks import build_databricks_tools
from v1.core.tools.databricks.channel import open_channel
from v1.core.tools.databricks.sql.scope import DataScope
from v1.core.tools.databricks.sql.statements import StatementClient, StatementResult
from v1.core.tools.databricks.sql.tools import (
    CHOOSE_DESCRIPTION,
    CHOOSE_TABLES,
    DEFAULT_CHOICE_QUESTION,
    DESCRIBING,
    METRIC_VIEW_HINT,
    RUN_SQL_DESCRIPTION,
    RUNNING,
    SEARCHING,
    WAITING,
    build_sql_tools,
    choose,
    default_statement_client_factory,
    describe,
    keywords_of,
    run_query,
    search,
)
from v1.utils.databricks_client import CLIENT_UNAVAILABLE_MESSAGE, SIGN_IN_MESSAGE

OPEN = DataScope.parse("")
TAXI = DataScope.parse("samples.nyctaxi")
# Row caps for run_query tests that don't exercise the model/UI split.
CAPS = {"max_rows_to_model": 2, "max_rows_to_ui": 2}
TRIPS_INFO = StatementResult(
    "completed",
    ["table_type", "comment", "column_name", "full_data_type", "comment"],
    [
        ["MANAGED", "Taxi trips", "fare_amount", "double", "Fare in USD"],
        ["MANAGED", "Taxi trips", "pickup_zip", "int", None],
    ],
)
TOOL_NAMES = ["search_tables", "describe_table", "run_sql", CHOOSE_TABLES]
# One call per tool that needs a client.
CLIENT_CALLS = [
    ("search_tables", {"keywords": "taxi"}),
    ("describe_table", {"full_name": "samples.nyctaxi.trips"}),
    ("run_sql", {"query": "SELECT * FROM samples.nyctaxi.trips"}),
]
ALL_CALLS = [
    *CLIENT_CALLS,
    (CHOOSE_TABLES, {"candidates": ["samples.nyctaxi.trips", "samples.nyctaxi.zones"]}),
]


def ok(rows, columns=("c",)):
    return StatementResult("completed", list(columns), rows)


def numbers(count: int, truncated: bool = False) -> StatementResult:
    return StatementResult("completed", ["n"], [[str(i)] for i in range(count)], truncated)


def _json(coro) -> dict:
    return json.loads(asyncio.run(coro))


def _settings(**overrides: object) -> SimpleNamespace:
    values = {
        "databricks_enabled": True,
        # Not a Settings field: the run workspace's allowlist (see ``_Tools``).
        "databricks_data_allowlist": None,
        "databricks_sql_timeout_seconds": 50,
        "databricks_jobs_timeout_seconds": 30,
        "databricks_sql_poll_interval_seconds": 1,
        "databricks_sql_max_rows_to_model": 50,
        "databricks_sql_max_rows_to_ui": 200,
        "databricks_legacy_workspace": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@contextlib.contextmanager
def _on_workspace(allowlist: str | None = None):
    """Resolve every run to workspace ``main`` scoped by ``allowlist`` (no fallback)."""
    workspace = Workspace(
        alias="main",
        label="Databricks",
        host="https://ws",
        sql_warehouse_id="wh-1",
        data_allowlist=allowlist,
        scope=DataScope.parse(allowlist),
        jobs_enabled=True,
        groups=(),
    )
    saved = sql_tools.run_workspace
    sql_tools.run_workspace = lambda: (workspace, False)
    try:
        yield workspace
    finally:
        sql_tools.run_workspace = saved


CALLER = {"identity": "user:a", "object_id": "o1"}
LABEL = "Databricks"  # the label of ``_on_workspace``'s workspace
CLIENT_UNAVAILABLE = {"status": "error", "message": CLIENT_UNAVAILABLE_MESSAGE}


class _Tools:
    """Built SQL tools wired to a fake client; replaces ``app_workspace_client``, runs as
    ``user`` (``None``: outside a run, so no caller) and resolves the run to
    ``_on_workspace`` with the settings' allowlist."""

    def __init__(
        self,
        *,
        fail_with: Exception | None = None,
        user: dict | None = CALLER,
        **settings: object,
    ) -> None:
        self.fake = FakeStatementClient()
        self.workspace = object()
        self.factory_args: list[object] = []
        self.warehouse_ids: list[str] = []
        self.query_tags: list[dict | None] = []
        self.client_hosts: list[str] = []

        async def fake_app_workspace_client(host: str) -> object:
            self.client_hosts.append(host)
            if fail_with is not None:
                raise fail_with
            return self.workspace

        def factory(
            workspace_client: object, warehouse_id: str, query_tags: dict | None = None
        ) -> FakeStatementClient:
            self.factory_args.append(workspace_client)
            self.warehouse_ids.append(warehouse_id)
            self.query_tags.append(query_tags)
            return self.fake

        self._saved = sql_tools.app_workspace_client, databricks_client.get_config
        sql_tools.app_workspace_client = fake_app_workspace_client
        if user is not None:
            auth_user = platform_user(dict(user))
            databricks_client.get_config = lambda: {
                "configurable": {"langgraph_auth_user": auth_user}
            }
        self._workspace = _on_workspace(_settings(**settings).databricks_data_allowlist)
        self.run_workspace = self._workspace.__enter__()
        self.by_name = {t.name: t for t in build_sql_tools(_settings(**settings), factory)}

    def call(self, name: str, args: dict) -> dict:
        return _json(self.by_name[name].ainvoke(args))

    def untouched(self) -> bool:
        """No client was requested and nothing ran."""
        return not self.client_hosts and not self.fake.calls

    def __enter__(self) -> "_Tools":
        return self

    def __exit__(self, *exc: object) -> None:
        sql_tools.app_workspace_client, databricks_client.get_config = self._saved
        self._workspace.__exit__(None, None, None)


# -- search ---------------------------------------------------------------------


def test_search_maps_rows_and_quotes_names() -> None:
    fake_sql = FakeStatementClient(
        ok(
            [
                ["samples", "nyctaxi", "trips", "MANAGED", "Taxi trips"],
                ["taxi-raw-records", "default", "taxi_tripdata", "MANAGED", None],
            ]
        )
    )
    statuses: list[str] = []
    out = _json(search(fake_sql, "taxi trips", scope=OPEN, on_status=statuses.append))
    assert out == {
        "status": "completed",
        "tables": [
            {"full_name": "samples.nyctaxi.trips", "type": "MANAGED", "comment": "Taxi trips"},
            {
                "full_name": "`taxi-raw-records`.default.taxi_tripdata",
                "type": "MANAGED",
                "comment": None,
            },
        ],
    }
    assert statuses == [SEARCHING]  # "Running query…" is only for run_sql
    call = fake_sql.calls[0]
    assert call["parameters"] == {"k0": "%taxi%", "k1": "%trips%"}
    assert "system.information_schema.tables" in call["sql"]
    assert "table_schema <> 'information_schema'" in call["sql"]
    assert call["max_rows"] == 20


def test_search_query_shape() -> None:
    # Keywords are parameters, never SQL.
    fake_sql = FakeStatementClient(ok([]), ok([]), ok([]))
    asyncio.run(search(fake_sql, "o'brien; DROP", scope=OPEN))
    call = fake_sql.calls[0]
    assert "brien" not in call["sql"] and "DROP" not in call["sql"]
    assert call["parameters"] == {"k0": "%o'brien;%", "k1": "%drop%"}
    # The allowlist becomes a parameterized filter.
    asyncio.run(search(fake_sql, "taxi", scope=TAXI))
    call = fake_sql.calls[1]
    assert "table_catalog = :s0_table_catalog AND table_schema = :s0_table_schema" in call["sql"]
    assert call["parameters"] == {
        "s0_table_catalog": "samples",
        "s0_table_schema": "nyctaxi",
        "k0": "%taxi%",
    }
    # The limit is capped.
    asyncio.run(search(fake_sql, "taxi", scope=OPEN, limit=500))
    assert fake_sql.calls[2]["max_rows"] == 50 and "LIMIT 50" in fake_sql.calls[2]["sql"]


def test_keyword_wildcards_match_literally() -> None:
    # "_" is LIKE's one-character wildcard; "\" is Databricks' default LIKE escape.
    fake_sql = FakeStatementClient(ok([]))
    asyncio.run(search(fake_sql, r"order_items a\b", scope=OPEN))
    assert fake_sql.calls[0]["parameters"] == {"k0": r"%order\_items%", "k1": r"%a\\b%"}


def test_keywords_are_cleaned_and_capped() -> None:
    assert keywords_of("Taxi, FARES 100% a") == ["taxi", "fares", "100"]
    assert len(keywords_of(" ".join(f"w{i}" for i in range(20)))) == 8
    fake_sql = FakeStatementClient()
    assert _json(search(fake_sql, " , x ", scope=OPEN))["status"] == "rejected"
    assert not fake_sql.calls


def test_search_reports_warehouse_start_and_relays_errors() -> None:
    fake_sql = FakeStatementClient(
        StatementResult("permission_denied", message="no CAN USE"),
        states=(StatementState.PENDING, StatementState.RUNNING),
    )
    statuses: list[str] = []
    out = _json(search(fake_sql, "taxi", scope=OPEN, on_status=statuses.append))
    assert statuses == [SEARCHING, WAITING]
    assert out["status"] == "permission_denied" and "workspace admin" in out["message"]
    assert "CAN USE" not in out["message"]


# -- describe -------------------------------------------------------------------


def test_describe_returns_columns_and_samples() -> None:
    fake_sql = FakeStatementClient(TRIPS_INFO, ok([["12.5", "10001"]], ("fare_amount", "zip")))
    statuses: list[str] = []
    out = _json(
        describe(fake_sql, "`Samples`.NycTaxi.trips", scope=TAXI, on_status=statuses.append)
    )
    assert out == {
        "status": "completed",
        "full_name": "samples.nyctaxi.trips",
        "type": "MANAGED",
        "comment": "Taxi trips",
        "columns": [
            {"name": "fare_amount", "type": "double", "comment": "Fare in USD"},
            {"name": "pickup_zip", "type": "int", "comment": None},
        ],
        "sample_rows": [["12.5", "10001"]],
    }
    assert fake_sql.calls[0]["parameters"] == {
        "catalog": "samples",
        "schema": "nyctaxi",
        "table": "trips",
    }
    assert fake_sql.calls[1]["sql"] == "SELECT * FROM samples.nyctaxi.trips LIMIT 3"
    assert statuses == [DESCRIBING]


def test_describe_metric_view_skips_samples() -> None:
    fake_sql = FakeStatementClient(
        ok([["METRIC_VIEW", "Invoices", "Total Invoices", "bigint", "Count of claims"]])
    )
    out = _json(describe(fake_sql, "m.s.claims_mv", scope=OPEN))
    assert out["how_to_query"] == METRIC_VIEW_HINT
    assert "sample_rows" not in out and len(fake_sql.calls) == 1


def test_describe_rejects_bad_names_without_running() -> None:
    cases = [(name, OPEN, "catalog.schema.table") for name in ("trips", "x.y", "a.b.c.d", "`bad")]
    for name, scope, text in [*cases, ("samples.tpch.orders", TAXI, "outside")]:
        fake_sql = FakeStatementClient()
        out = _json(describe(fake_sql, name, scope=scope))
        assert out["status"] == "rejected" and text in out["message"], (name, out)
        assert not fake_sql.calls, name


def test_describe_unknown_or_unreadable_table() -> None:
    # information_schema can list tables the user has no USE CATALOG on.
    unreadable = StatementResult("permission_or_not_found", message="[INSUFFICIENT_PERMISSIONS]")
    for results in ([ok([])], [TRIPS_INFO, unreadable]):
        out = _json(describe(FakeStatementClient(*results), "samples.nyctaxi.t", scope=OPEN))
        assert out["status"] == "permission_or_not_found", out


# -- run_query ------------------------------------------------------------------


def test_run_query_caps_model_rows_and_returns_the_sql() -> None:
    fake_sql = FakeStatementClient(numbers(5, truncated=True))
    statuses: list[str] = []
    out = _json(
        run_query(
            fake_sql,
            "SELECT n FROM samples.nyctaxi.trips",
            scope=OPEN,
            **CAPS,
            on_status=statuses.append,
        )
    )
    assert out == {
        "status": "completed",
        "sql": "SELECT\n  n\nFROM samples.nyctaxi.trips",
        "columns": ["n"],
        "rows": [["0"], ["1"]],
        "row_count": 2,
        "truncated": True,
    }
    assert fake_sql.calls[0]["max_rows"] == 2
    assert statuses == [RUNNING]


def test_run_query_runs_exactly_the_checked_sql() -> None:
    fake_sql = FakeStatementClient(numbers(0))
    query = "select n from samples.nyctaxi.trips -- busiest zones"
    out = _json(run_query(fake_sql, query, scope=OPEN, **CAPS))
    ran = fake_sql.calls[0]["sql"]
    assert ran == "SELECT\n  n\nFROM samples.nyctaxi.trips /* busiest zones */"
    assert out["sql"] == ran


def test_run_query_truncation_and_ui_result() -> None:
    # (rows, warehouse truncated) -> model (row_count, truncated), UI (row_count, truncated)
    cases = [
        ((3, False), (2, True), (3, False)),
        ((3, True), (2, True), (3, True)),
        ((1, False), (1, False), (1, False)),
        ((0, False), (0, False), (0, False)),
    ]
    for (count, truncated), model, ui in cases:
        results: list[dict] = []
        out = _json(
            run_query(
                FakeStatementClient(numbers(count, truncated)),
                "SELECT 1",
                scope=OPEN,
                max_rows_to_model=2,
                max_rows_to_ui=5,
                on_result=results.append,
            )
        )
        [shown] = results
        assert (out["row_count"], out["truncated"]) == model, (count, truncated, out)
        assert (shown["row_count"], shown["truncated"]) == ui, (count, truncated, shown)
        assert len(shown["rows"]) == count and shown["columns"] == ["n"]


def test_run_query_ui_rows_capped_separately() -> None:
    fake_sql = FakeStatementClient(numbers(250))
    results: list[dict] = []
    model = _json(
        run_query(
            fake_sql,
            "SELECT n FROM samples.nyctaxi.trips",
            scope=OPEN,
            max_rows_to_model=50,
            max_rows_to_ui=200,
            on_result=results.append,
        )
    )
    assert model["row_count"] == 50 and len(model["rows"]) == 50
    assert model["truncated"] is True
    assert fake_sql.calls[0]["max_rows"] == 200
    [ui] = results
    assert set(ui) == {"sql", "columns", "rows", "row_count", "truncated"}
    assert ui["sql"] == model["sql"] and ui["columns"] == ["n"]
    assert len(ui["rows"]) == 200 and ui["row_count"] == 200
    assert ui["truncated"] is True


def test_run_query_rejects_without_executing() -> None:
    results: list[dict] = []
    for query, reason in (
        ("DROP TABLE samples.nyctaxi.trips", "read-only"),
        ("SELECT * FROM samples.tpch.orders", "outside"),
    ):
        fake_sql = FakeStatementClient()
        out = _json(run_query(fake_sql, query, scope=TAXI, **CAPS, on_result=results.append))
        assert out["status"] == "rejected" and reason in out["message"], out
        assert not fake_sql.calls
    assert results == []


def test_run_query_status_texts() -> None:
    fake_sql = FakeStatementClient(states=(StatementState.PENDING, StatementState.RUNNING))
    statuses: list[str] = []
    asyncio.run(run_query(fake_sql, "SELECT 1", scope=OPEN, **CAPS, on_status=statuses.append))
    assert statuses == [WAITING, RUNNING]


def test_run_query_error_messages() -> None:
    cases = [
        ("timeout", "narrower"),
        ("failed", "UNRESOLVED_COLUMN"),
        ("permission_or_not_found", "doesn't have access"),
        ("permission_denied", "workspace admin"),
    ]
    results: list[dict] = []
    for status, text in cases:
        fake_sql = FakeStatementClient(StatementResult(status, message="[UNRESOLVED_COLUMN] x"))
        payload = _json(
            run_query(fake_sql, "SELECT 1", scope=OPEN, **CAPS, on_result=results.append)
        )
        assert payload["status"] == status and text in payload["message"], (status, payload)
        assert "sql" not in payload, (status, payload)
    assert results == []  # no UI result unless the query completed


# -- choose -----------------------------------------------------------------------


def _choose(candidates, question="", *, scope=OPEN, selection_active=False):
    content, choices = choose(candidates, question, scope=scope, selection_active=selection_active)
    return json.loads(content), choices


def test_choose_dedupes_and_formats() -> None:
    out, choices = _choose(["A.B.C", "a.b.c", "a.b.d"], "Which sales table?")
    assert out["status"] == "waiting_for_user", out
    assert choices == {"question": "Which sales table?", "tables": ["a.b.c", "a.b.d"]}
    out, choices = _choose([f"a.b.t{i}" for i in range(6)])
    assert out["status"] == "waiting_for_user" and len(choices["tables"]) == 6


def test_choose_rejections() -> None:
    cases = [
        (["a.b.c"], OPEN, False, "between 2 and 6"),
        ([f"a.b.t{i}" for i in range(7)], OPEN, False, "between 2 and 6"),
        (["a.b.c", "A.B.C"], OPEN, False, "between 2 and 6"),
        (["samples.nyctaxi.trips", "samples.tpch.orders"], TAXI, False, "outside"),
        (["samples.nyctaxi.trips", "trips"], TAXI, False, "catalog.schema.table"),
        (["a.b.c", "a.b.d"], OPEN, True, "already selected"),
    ]
    for candidates, scope, selection_active, text in cases:
        out, choices = _choose(candidates, scope=scope, selection_active=selection_active)
        assert out["status"] == "rejected" and text in out["message"], (candidates, out)
        assert choices is None


def test_choose_question() -> None:
    assert DEFAULT_CHOICE_QUESTION == "Which table should I use?"
    for question in ("", "   "):
        assert _choose(["a.b.c", "a.b.d"], question)[1]["question"] == DEFAULT_CHOICE_QUESTION
    # Capped at 200 characters.
    _, choices = _choose(["a.b.c", "a.b.d"], "  " + "q" * 250 + "  ")
    assert choices["question"] == "q" * 199 + "…"
    exact = "w" * 200
    assert _choose(["a.b.c", "a.b.d"], exact)[1]["question"] == exact


def test_descriptions() -> None:
    lowered = CHOOSE_DESCRIPTION.lower()
    assert "ambiguous" in lowered and "selected" in lowered
    assert "do not answer the question yet" in lowered
    # The SQL reaches the user from the task artifact, never from the written answer.
    assert "Show the user the SQL" not in RUN_SQL_DESCRIPTION
    assert "UI" not in RUN_SQL_DESCRIPTION


# -- the LangChain tools ----------------------------------------------------------


def test_tool_list_ends_with_choose_tables() -> None:
    with _Tools() as tools:
        assert list(tools.by_name) == TOOL_NAMES
        assert all(t.response_format == "content" for t in tools.by_name.values())


def test_run_sql_returns_checked_sql_and_capped_rows() -> None:
    with _Tools() as tools, open_channel(None) as channel:
        tools.fake.results = [numbers(60)]
        out = tools.call("run_sql", {"query": "select * from samples.nyctaxi.trips"})
        assert out["sql"] == "SELECT\n  *\nFROM samples.nyctaxi.trips", out["sql"]
        assert len(out["rows"]) == 50 and out["truncated"] is True
        assert tools.fake.calls[0]["sql"] == out["sql"]
        assert tools.fake.calls[0]["max_rows"] == 200
        assert tools.factory_args == [tools.workspace]
        assert len(channel.results) == 1 and channel.results[0]["sql"] == out["sql"]


def test_run_sql_uses_the_configured_row_caps() -> None:
    caps = {"databricks_sql_max_rows_to_model": 3, "databricks_sql_max_rows_to_ui": 4}
    with _Tools(**caps) as tools, open_channel(None) as channel:
        tools.fake.results = [numbers(5)]
        out = tools.call("run_sql", {"query": "SELECT n FROM samples.nyctaxi.trips"})
        assert len(out["rows"]) == 3 and tools.fake.calls[0]["max_rows"] == 4
        assert len(channel.results[0]["rows"]) == 4 and channel.results[0]["truncated"] is True
        # Rejected or failed queries record nothing.
        assert tools.call("run_sql", {"query": "DROP TABLE x"})["status"] == "rejected"
        tools.fake.results = [StatementResult("failed", message="boom")]
        tools.call("run_sql", {"query": "SELECT n FROM samples.nyctaxi.trips"})
        assert len(channel.results) == 1 and len(tools.fake.calls) == 2


def test_run_sql_uses_app_client_and_tags() -> None:
    with _Tools() as tools:
        assert tools.call("run_sql", {"query": "SELECT 1"})["status"] == "completed"
        assert tools.client_hosts == ["https://ws"]
        assert tools.factory_args == [tools.workspace] and tools.warehouse_ids == ["wh-1"]
        [tags] = tools.query_tags
        assert tags["user_oid"] == "o1" and tags["source"] == "agent", tags
        assert tags["workspace"] == "main", tags
    # No object ID: still tagged, without user_oid.
    with _Tools(user={"identity": "user:b"}) as tools:
        tools.call("run_sql", {"query": "SELECT 1"})
        assert "user_oid" not in tools.query_tags[0], tools.query_tags


def test_run_sql_logs_audit_line() -> None:
    with _Tools() as tools, _captured_logs(databricks_client.__name__) as logs:
        tools.call("run_sql", {"query": "SELECT 1"})
    [line] = logs.messages
    assert line.startswith("databricks_call identity=user:a oid=o1 workspace=main "), line
    assert "action=run_sql detail=SELECT 1" in line, line


def test_tools_audit_once_per_call_with_what_was_asked() -> None:
    with _Tools() as tools, _captured_logs(databricks_client.__name__) as logs:
        tools.fake.results = [ok([])]
        tools.call("search_tables", {"keywords": "taxi fares"})
        tools.fake.results = [TRIPS_INFO, ok([["1", "2"]])]
        tools.call("describe_table", {"full_name": "samples.nyctaxi.trips"})
        # choose_tables builds no client, so it logs nothing.
        tools.call(CHOOSE_TABLES, ALL_CALLS[-1][1])
    assert [m.split(" action=", 1)[1] for m in logs.messages] == [
        "search_tables detail=taxi fares",
        "describe_table detail=samples.nyctaxi.trips",
    ], logs.messages


def test_credential_failure_is_unavailable_payload() -> None:
    error = ClientAuthenticationError("token request failed: secret-detail")
    with (
        _Tools(fail_with=error) as tools,
        _captured_logs(sql_tools.__name__, databricks_client.__name__) as logs,
    ):
        for name, args in CLIENT_CALLS:
            out = tools.call(name, args)
            assert out == {**CLIENT_UNAVAILABLE, "workspace": LABEL}, (name, out)
        assert tools.factory_args == [] and tools.fake.calls == []
    captured = logs.getvalue()
    assert "ClientAuthenticationError" in captured and "secret-detail" not in captured, captured
    # No client, no audit line.
    assert "databricks_call" not in captured, captured


def test_tools_return_unavailable_when_the_client_cannot_be_built() -> None:
    error = ValueError("host metadata https://h?token=dapi-secret")
    with _Tools(fail_with=error) as tools, _captured_logs(sql_tools.__name__) as logs:
        for name, args in CLIENT_CALLS:
            out = tools.call(name, args)
            assert out == {**CLIENT_UNAVAILABLE, "workspace": LABEL}, (name, out)
        assert tools.factory_args == [] and tools.fake.calls == []
    captured = logs.getvalue()
    assert "ValueError" in captured and "dapi-secret" not in captured, captured


def test_no_identity_is_sign_in_payload() -> None:
    # Outside a run there is no langgraph_auth_user, so no caller: no client is built.
    with _Tools(user=None) as tools, _captured_logs(databricks_client.__name__) as logs:
        for name, args in CLIENT_CALLS:
            out = tools.call(name, args)
            expected = {"status": "error", "workspace": LABEL, "message": SIGN_IN_MESSAGE}
            assert out == expected, (name, out)
        assert tools.untouched() and tools.factory_args == []
    assert logs.messages == []


def test_run_sql_returns_failed_payload_on_network_errors() -> None:
    """Through the real StatementClient: the SDK's TimeoutError / ConnectionError don't escape."""

    import requests

    from v1.core.tools.databricks.sql.statements import UNREACHABLE_MESSAGE

    class FailingAPI:
        def __init__(self, error: Exception) -> None:
            self.error = error

        def execute_statement(self, **kwargs: object) -> None:
            raise self.error

    for error in (TimeoutError("timed out"), requests.ConnectionError("refused")):
        with _Tools() as tools:
            tools.workspace = SimpleNamespace(statement_execution=FailingAPI(error))
            [run_sql] = [t for t in build_sql_tools(_settings()) if t.name == "run_sql"]
            out = _json(run_sql.ainvoke({"query": "SELECT 1"}))
        assert out["status"] == "failed" and UNREACHABLE_MESSAGE in out["message"], (error, out)


def test_run_sql_access_errors_are_a_fixed_no_retry_message() -> None:
    """Through the real StatementClient: a 401 or 403 from the warehouse is one fixed
    message, with none of Databricks' text and no invitation to retry."""

    from databricks.sdk.errors import PermissionDenied, Unauthenticated

    class FailingAPI:
        def __init__(self, error: Exception) -> None:
            self.error = error

        def execute_statement(self, **kwargs: object) -> None:
            raise self.error

    for error in (PermissionDenied("SDK-TEXT no CAN USE"), Unauthenticated("SDK-TEXT bad token")):
        with _Tools() as tools:
            tools.workspace = SimpleNamespace(statement_execution=FailingAPI(error))
            [run_sql] = [t for t in build_sql_tools(_settings()) if t.name == "run_sql"]
            out = _json(run_sql.ainvoke({"query": "SELECT 1"}))
        assert out == {
            "status": "permission_denied",
            "workspace": LABEL,
            "message": sql_tools.ERROR_MESSAGES["permission_denied"],
        }, (error, out)
        assert "SDK-TEXT" not in out["message"], (error, out)
        assert "try again" not in out["message"].lower(), (error, out)
        assert "do not retry" in out["message"], (error, out)


def test_tools_apply_the_configured_allowlist() -> None:
    with _Tools(databricks_data_allowlist="samples.nyctaxi") as tools:
        out = tools.call("run_sql", {"query": "SELECT * FROM samples.tpch.orders"})
        assert out["status"] == "rejected" and "outside" in out["message"], out
        out = tools.call("describe_table", {"full_name": "samples.tpch.orders"})
        assert out["status"] == "rejected" and "outside" in out["message"], out
        tools.fake.results = [ok([])]
        tools.call("search_tables", {"keywords": "taxi"})
        assert tools.fake.calls[0]["parameters"]["s0_table_catalog"] == "samples"
        assert len(tools.fake.calls) == 1


@contextlib.contextmanager
def _run_selection(tables: object, tables_workspace: str = "main"):
    """Run the tools as if ``configurable["databricks_tables"]`` were ``tables``."""

    saved = sql_selection.get_config
    configurable = {"databricks_tables": tables, "databricks_tables_workspace": tables_workspace}
    sql_selection.get_config = lambda: {"configurable": configurable}
    try:
        yield
    finally:
        sql_selection.get_config = saved


def test_tools_reject_an_invalid_selection() -> None:
    with (
        _Tools(databricks_data_allowlist="samples.nyctaxi") as tools,
        _run_selection(["other.s.t"]),
        open_channel(None) as channel,
    ):
        for name, args in ALL_CALLS:
            out = tools.call(name, args)
            assert out["status"] == "rejected", (name, out)
            assert out["message"].startswith("The tables selected for this chat can't be used")
            assert "other.s.t" in out["message"], out
            assert out["workspace"] == LABEL, out
        assert tools.untouched() and channel.choices is None


def test_tools_are_narrowed_to_the_selection() -> None:
    with (
        _Tools(databricks_data_allowlist="samples.nyctaxi") as tools,
        _run_selection(["samples.nyctaxi.trips"]),
        open_channel(None) as channel,
    ):
        out = tools.call("run_sql", {"query": "SELECT * FROM samples.nyctaxi.zones"})
        assert out["status"] == "rejected" and "outside" in out["message"], out
        out = tools.call("run_sql", {"query": "SELECT * FROM samples.nyctaxi.trips"})
        assert out["status"] == "completed", out
        tools.fake.results = [ok([])]
        tools.call("search_tables", {"keywords": "taxi"})
        assert tools.fake.calls[-1]["parameters"]["s0_table_name"] == "trips"
        # A selection is in use, so no choice is offered.
        out = tools.call(CHOOSE_TABLES, ALL_CALLS[-1][1])
        assert out["status"] == "rejected" and "already selected" in out["message"], out
        assert channel.choices is None


def test_choose_tool_records_choices_in_channel() -> None:
    args = ALL_CALLS[-1][1]
    with _Tools(databricks_data_allowlist="samples.nyctaxi") as tools:
        with open_channel(None) as channel:
            out = tools.call(CHOOSE_TABLES, args)
            assert out["status"] == "waiting_for_user", out
            assert channel.choices == {
                "question": DEFAULT_CHOICE_QUESTION,
                "tables": ["samples.nyctaxi.trips", "samples.nyctaxi.zones"],
            }
        with open_channel(None) as channel:
            bad = {"candidates": ["samples.nyctaxi.trips", "samples.tpch.orders"]}
            assert tools.call(CHOOSE_TABLES, bad)["status"] == "rejected"
            assert channel.choices is None
        # Without a channel the tool still answers.
        assert tools.call(CHOOSE_TABLES, args)["status"] == "waiting_for_user"
        assert tools.untouched()


def test_choose_tables_reads_the_selection_once() -> None:
    # Tables picked in another workspace: ignored, so a choice can be offered.
    with (
        _captured_logs(sql_selection.__name__) as logs,
        _run_selection(["samples.nyctaxi.trips"], "sales"),
        _Tools(databricks_data_allowlist="samples.nyctaxi") as tools,
    ):
        assert tools.call(CHOOSE_TABLES, ALL_CALLS[-1][1])["status"] == "waiting_for_user"
    assert logs.getvalue().count("databricks_selection_ignored") == 1, logs.getvalue()


def test_tools_name_the_chat_workspace() -> None:
    """Every answer after the chat's workspace resolved carries its label."""
    with _Tools() as tools:
        tools.fake.results = [ok([["samples", "nyctaxi", "trips", "MANAGED", None]])]
        answers = [tools.call("search_tables", {"keywords": "taxi"})]
        tools.fake.results = [TRIPS_INFO, ok([["1", "2"]])]
        answers.append(tools.call("describe_table", {"full_name": "samples.nyctaxi.trips"}))
        answers.append(tools.call("run_sql", {"query": "SELECT 1"}))
        answers.append(tools.call(CHOOSE_TABLES, ALL_CALLS[-1][1]))
        assert [a["status"] for a in answers] == ["completed"] * 3 + ["waiting_for_user"]
        # A guard rejection is still an answer from the chat's workspace.
        answers.append(tools.call("run_sql", {"query": "DROP TABLE x"}))
        assert answers[-1]["status"] == "rejected", answers[-1]
        for answer in answers:
            assert answer["workspace"] == LABEL, answer
            assert list(answer)[:2] == ["status", "workspace"], answer
    with _Tools(fail_with=ValueError("x")) as tools:
        out = tools.call("run_sql", {"query": "SELECT 1"})
        assert out == {**CLIENT_UNAVAILABLE, "workspace": LABEL}, out


def test_search_and_describe_tools_run_as_the_app() -> None:
    with _Tools() as tools:
        tools.fake.results = [ok([["samples", "nyctaxi", "trips", "MANAGED", None]])]
        out = tools.call("search_tables", {"keywords": "taxi", "limit": 5})
        assert out["tables"][0]["full_name"] == "samples.nyctaxi.trips"
        assert tools.fake.calls[0]["max_rows"] == 5
        tools.fake.results = [TRIPS_INFO, ok([["1", "2"]])]
        out = tools.call("describe_table", {"full_name": "samples.nyctaxi.trips"})
        assert out["status"] == "completed" and out["sample_rows"] == [["1", "2"]]
        assert tools.client_hosts == [tools.run_workspace.host] * 2


def test_default_factory_uses_the_given_warehouse() -> None:
    workspace = SimpleNamespace(statement_execution=object())
    settings = _settings(databricks_sql_timeout_seconds=7, databricks_sql_poll_interval_seconds=2)
    client = default_statement_client_factory(settings)(workspace, "wh-claims")
    assert isinstance(client, StatementClient)
    assert (client._warehouse_id, client._timeout_s, client._poll_interval_s) == ("wh-claims", 7, 2)


def test_build_databricks_tools() -> None:
    expected = [*TOOL_NAMES, "find_failed_runs", "explain_run", "list_workspaces"]
    assert build_databricks_tools(_settings(databricks_enabled=False)) == []
    assert [t.name for t in build_databricks_tools(_settings())] == expected
    # Real Settings: DATABRICKS_ENABLED with local dev auth is all it takes; no workspace is
    # needed (workspaces come from the registry at runtime).
    env = {"AGENT_AUTH_MODE": "local", "APP_ENV": "local", "DATABRICKS_ENABLED": "true"}
    saved = {key: os.environ.get(key) for key in env}
    try:
        os.environ.update(env)
        names = [t.name for t in build_databricks_tools(Settings(_env_file=None))]
        assert names == expected, names
        os.environ["DATABRICKS_ENABLED"] = "false"
        assert build_databricks_tools(Settings(_env_file=None)) == []
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


TWO_WORKSPACES = {
    "claims": {
        "label": "Invoices",
        "host": "https://adb-claims.azuredatabricks.net",
        "sql_warehouse_id": "wh-claims",
        "data_allowlist": "samples.nyctaxi",
    },
    "sales": {
        "host": "https://adb-sales.azuredatabricks.net",
        "sql_warehouse_id": "wh-sales",
        "data_allowlist": "samples.tpch",
        "groups": ["SALES"],
    },
}


@contextlib.contextmanager
def _registry_run(configurable: dict, groups: tuple[str, ...] = ()):
    """Tools resolving through the real resolver: ``TWO_WORKSPACES`` is the registry and
    the run sends ``configurable`` for a caller in ``groups``."""

    import v1.core.databricks_workspaces as dw
    import v1.core.tools.databricks.run_workspace as rw
    from v1.core.workspace_registry.snapshot import get_snapshot

    with patched_env({"DATABRICKS_LEGACY_WORKSPACE": None}):
        settings = Settings(_env_file=None)
    registry = tuple(dw.validate_entry(alias, entry) for alias, entry in TWO_WORKSPACES.items())
    assert all(isinstance(ws, dw.Workspace) for ws in registry), registry
    saved = (dw.get_settings, rw.get_config, rw.groups_from_config, sql_selection.get_config)
    dw.get_settings = lambda: settings
    get_snapshot().replace(registry)
    rw.get_config = sql_selection.get_config = lambda: {"configurable": configurable}
    rw.groups_from_config = lambda: groups
    # Inside ``_Tools``: it restores ``run_workspace`` on exit.
    sql_tools.run_workspace = run_workspace_module.run_workspace  # the real resolver
    try:
        yield
    finally:
        dw.get_settings, rw.get_config, rw.groups_from_config, sql_selection.get_config = saved
        get_snapshot().replace(())


def test_sql_uses_workspace_warehouse_and_allowlist() -> None:
    nyc = "SELECT * FROM samples.nyctaxi.trips"
    tpch = "SELECT * FROM samples.tpch.orders"
    cases = [("claims", (), nyc, tpch), ("sales", ("SALES",), tpch, nyc)]
    for alias, groups, allowed, outside in cases:
        with _Tools() as tools, _registry_run({"databricks_workspace": alias}, groups):
            assert tools.call("run_sql", {"query": allowed})["status"] == "completed"
            out = tools.call("run_sql", {"query": outside})
            assert out["status"] == "rejected" and "outside" in out["message"], (alias, out)
            out = tools.call("describe_table", {"full_name": outside.split()[-1]})
            assert out["status"] == "rejected" and "outside" in out["message"], (alias, out)
            # Each call builds the app's client before its own checks.
            hosts = tools.client_hosts
            assert hosts == [f"https://adb-{alias}.azuredatabricks.net"] * 3, hosts
            assert tools.warehouse_ids == [f"wh-{alias}"] * 3
            assert [t["workspace"] for t in tools.query_tags] == [alias] * 3


def test_sql_tools_unavailable_workspace_payload() -> None:
    # Removed from the config, hidden from the caller, malformed: refused, never the default.
    for configurable in (
        {"databricks_workspace": "gone"},
        {"databricks_workspace": "sales"},
        {"databricks_workspace": ["claims"]},
    ):
        with _Tools() as tools, _registry_run(configurable):
            for name, args in ALL_CALLS:
                out = tools.call(name, args)
                expected = {"status": "error", "message": WORKSPACE_UNAVAILABLE_MESSAGE}
                assert out == expected, (configurable, name, out)
                assert "workspace" not in out, out
            assert tools.untouched()


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
