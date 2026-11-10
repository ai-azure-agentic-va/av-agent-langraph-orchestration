"""Tests for Databricks name parsing and the data allowlist (ported from ``test_scope.py``).

Pure: no I/O. Runs standalone (``python test_databricks_sql_scope.py``).
"""

from __future__ import annotations

from databricks_test_support import run_tests

from v1.core.tools.databricks.sql.scope import DataScope, format_name, split_name

SPLIT_CASES = [
    ("samples.nyctaxi.trips", ("samples", "nyctaxi", "trips")),
    ("Samples.NycTaxi.Trips", ("samples", "nyctaxi", "trips")),
    (
        "`taxi-raw-records`.default.taxi_tripdata",
        ("taxi-raw-records", "default", "taxi_tripdata"),
    ),
    ("`a.b`.c", ("a.b", "c")),
    ("`we``ird`", ("we`ird",)),
    (" samples . nyctaxi ", ("samples", "nyctaxi")),
]


def _raises_value_error(fn, *args, match: str | None = None) -> None:
    try:
        fn(*args)
    except ValueError as exc:
        if match is not None:
            assert match in str(exc), (args, str(exc))
    else:
        raise AssertionError(f"{fn.__name__}{args!r} did not raise ValueError")


def test_split_name() -> None:
    for name, parts in SPLIT_CASES:
        assert split_name(name) == parts, (name, split_name(name))


def test_split_name_rejects_malformed() -> None:
    for name in ["a..b", "", "`a.b", "a."]:
        _raises_value_error(split_name, name)


def test_format_name_quotes_only_when_needed() -> None:
    assert format_name("samples", "nyctaxi", "trips") == "samples.nyctaxi.trips"
    assert format_name("taxi-raw-records", "default", "t") == "`taxi-raw-records`.default.t"
    assert format_name("we`ird") == "`we``ird`"
    assert format_name("2024_sales") == "`2024_sales`"


def test_empty_allowlist_is_open() -> None:
    for raw in (None, "", "   "):
        scope = DataScope.parse(raw)
        assert scope.is_open, raw
        assert scope.describe() == "OPEN", raw
        assert scope.covers("any", "thing", "here"), raw
        assert scope.search_filter() == ("TRUE", {}), raw


def test_bad_allowlists_are_rejected() -> None:
    # Blank entries never mean open scope.
    _raises_value_error(DataScope.parse, " , ,", match="no entries")
    _raises_value_error(DataScope.parse, "a.b.c.d", match="more than 3 parts")


def test_covers_catalog_schema_and_table_entries() -> None:
    scope = DataScope.parse(
        "member_orders_uc, samples.nyctaxi, `taxi-raw-records`.default.taxi_tripdata,"
    )
    assert scope.describe() == "allowlist (3 entries)"
    assert scope.covers("member_orders_uc", "any", "table")
    assert scope.covers("SAMPLES", "NycTaxi", "trips")
    assert not scope.covers("samples", "tpch", "orders")
    assert scope.covers("taxi-raw-records", "default", "taxi_tripdata")
    assert not scope.covers("taxi-raw-records", "default", "other")


def test_search_filter_is_parameterized() -> None:
    sql, params = DataScope.parse("samples.nyctaxi,main").search_filter()
    assert sql == (
        "((table_catalog = :s0_table_catalog AND table_schema = :s0_table_schema)"
        " OR (table_catalog = :s1_table_catalog))"
    )
    assert params == {
        "s0_table_catalog": "samples",
        "s0_table_schema": "nyctaxi",
        "s1_table_catalog": "main",
    }


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
