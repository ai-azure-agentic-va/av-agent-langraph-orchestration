"""Tests for the read-only SQL guard (ported from databricks-agent ``test_guard.py``).

Pure: no I/O. Runs standalone (``python test_databricks_sql_guard.py``).
"""

from __future__ import annotations

import sqlglot
from databricks_test_support import run_tests
from sqlglot import exp

from v1.core.tools.databricks.sql import guard
from v1.core.tools.databricks.sql.guard import SqlRejected, check_sql, checked_sql
from v1.core.tools.databricks.sql.scope import DataScope

OPEN = DataScope.parse("")
TAXI = DataScope.parse("samples.nyctaxi")
HYPHEN = DataScope.parse("`taxi-raw-records`.default")

ALLOWED = [
    (
        OPEN,
        "SELECT pickup_zip, avg(fare_amount) AS avg_fare FROM samples.nyctaxi.trips GROUP BY ALL ORDER BY 2 DESC LIMIT 10",
    ),
    (OPEN, "WITH t AS (SELECT * FROM samples.nyctaxi.trips) SELECT count(*) FROM t"),
    (OPEN, "SELECT 1 UNION ALL SELECT 2"),
    (
        OPEN,
        "SELECT pickup_zip FROM samples.nyctaxi.trips WHERE pickup_zip IN (SELECT pickup_zip FROM samples.nyctaxi.trips)",
    ),
    (
        OPEN,
        "SELECT `Line of Business`, MEASURE(`Total Invoices`) FROM `metric-demo`.s.mv GROUP BY ALL",
    ),
    (OPEN, "select * from SAMPLES.NYCTAXI.TRIPS limit 5;"),
    (
        OPEN,
        "SELECT * FROM samples.nyctaxi.trips QUALIFY row_number() OVER (ORDER BY fare_amount DESC) = 1",
    ),
    (OPEN, "SELECT * FROM trips"),
    (
        OPEN,
        "SELECT date_trunc('MONTH', tpep_pickup_datetime) AS m, count(*) FROM samples.nyctaxi.trips GROUP BY 1",
    ),
    (OPEN, "SELECT * FROM samples.nyctaxi.trips LIMIT 3"),
    (TAXI, "SELECT count(*) FROM samples.nyctaxi.trips"),
    (TAXI, "WITH t AS (SELECT * FROM samples.nyctaxi.trips) SELECT * FROM t"),
    (TAXI, "SELECT * FROM SAMPLES.NycTaxi.TRIPS"),
    (HYPHEN, "SELECT * FROM `taxi-raw-records`.default.taxi_tripdata"),
    (
        TAXI,
        "WITH a AS (SELECT * FROM samples.nyctaxi.trips), b AS (SELECT * FROM a) SELECT * FROM b",
    ),
    (
        TAXI,
        "WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r WHERE n < 3) SELECT * FROM r",
    ),
    (TAXI, "SELECT samples.nyctaxi.myudf(fare_amount) FROM samples.nyctaxi.trips"),
    (OPEN, "SELECT other.s.myudf(1)"),
]

REJECTED = [
    (OPEN, "DROP TABLE a.b.c", "read-only"),
    (OPEN, "DELETE FROM a.b.c", "read-only"),
    (OPEN, "INSERT INTO a.b.c VALUES (1)", "read-only"),
    (OPEN, "UPDATE a.b.c SET x = 1", "read-only"),
    (
        OPEN,
        "MERGE INTO a.b.c t USING d.e.f s ON t.id = s.id WHEN MATCHED THEN DELETE",
        "read-only",
    ),
    (OPEN, "CREATE TEMPORARY VIEW v AS SELECT 1", "read-only"),
    (OPEN, "SHOW TABLES", "read-only"),
    (OPEN, "DESCRIBE TABLE a.b.c", "read-only"),
    (OPEN, "EXPLAIN SELECT 1", "read-only"),
    (OPEN, "SET x = 1", "read-only"),
    (OPEN, "USE CATALOG c", "read-only"),
    (OPEN, "OPTIMIZE a.b.c", "read-only"),
    (OPEN, "CACHE TABLE a.b.c", "read-only"),
    (OPEN, "VALUES (1)", "read-only"),
    (OPEN, "GRANT SELECT ON TABLE a.b.c TO `x`", "read-only"),
    (OPEN, "TRUNCATE TABLE a.b.c", "read-only"),
    (OPEN, "COPY INTO a.b.c FROM '/x'", "read-only"),
    (OPEN, "SELECT 1; DROP TABLE x", "exactly one"),
    (OPEN, "SELECT 1; SELECT 2", "exactly one"),
    (OPEN, "", "exactly one"),
    (OPEN, "   ;  ", "exactly one"),
    (OPEN, "SELECT * FROM read_files('/Volumes/x')", "read_files"),
    (OPEN, "SELECT http_request(conn => 'c', method => 'GET', path => '/')", "http_request"),
    (OPEN, "SELECT AI_QUERY('ep', 'hi')", "ai_query"),
    (OPEN, "SELECT * FROM IDENTIFIER('a.b.c')", "identifier"),
    (OPEN, "SELECT * FROM range(10)", "table-valued"),
    (OPEN, "SELECT upper(x) FROM (SELECT ai_gen('p') AS x)", "ai_gen"),
    (OPEN, "SELECT 'unterminated", "could not be parsed"),
    (OPEN, "SELECT * FROM (", "could not be parsed"),
    (TAXI, "SELECT * FROM samples.tpch.orders", "outside"),
    (TAXI, "SELECT * FROM trips", "fully qualified"),
    (TAXI, "SELECT * FROM nyctaxi.trips", "fully qualified"),
    (TAXI, "WITH t AS (SELECT * FROM samples.tpch.orders) SELECT * FROM t", "outside"),
    (TAXI, "WITH trips AS (SELECT 1) SELECT * FROM samples.tpch.trips", "outside"),
    (
        TAXI,
        "SELECT * FROM samples.nyctaxi.trips WHERE pickup_zip IN (SELECT 1 FROM main.x.y)",
        "outside",
    ),
    (TAXI, "SELECT * FROM samples.nyctaxi.trips a JOIN main.x.y b ON a.k = b.k", "outside"),
    (TAXI, "SELECT * FROM IDENTIFIER('samples.tpch.orders')", "identifier"),
    # A CTE only hides tables in its own scope.
    (
        TAXI,
        "SELECT * FROM orders WHERE EXISTS (WITH orders AS (SELECT 1) SELECT * FROM orders)",
        "fully qualified",
    ),
    (
        TAXI,
        "SELECT * FROM (WITH orders AS (SELECT 1 AS a) SELECT * FROM orders) x CROSS JOIN orders",
        "fully qualified",
    ),
    (TAXI, "WITH orders AS (SELECT * FROM orders) SELECT * FROM orders", "fully qualified"),
    # User-defined functions outside the allowlist can read other tables.
    (TAXI, "SELECT other.s.myudf(1) FROM samples.nyctaxi.trips", "outside"),
    (TAXI, "SELECT s.myudf(1) FROM samples.nyctaxi.trips", "fully qualified"),
    # Path-based file reads are external reads, like read_files.
    (OPEN, "SELECT * FROM json.`/Volumes/a/b/c.json`", "by path"),
    (OPEN, "SELECT * FROM delta.`s3://bucket/path`", "by path"),
    (OPEN, "SELECT * FROM parquet.`/tmp/x`", "by path"),
    (TAXI, "SELECT * FROM csv.`/Volumes/a/b/c.csv`", "by path"),
    # Secrets and JVM calls.
    (OPEN, "SELECT secret('scope', 'key')", "secret"),
    (OPEN, "SELECT try_secret('scope', 'key')", "try_secret"),
    (OPEN, "SELECT reflect('java.lang.System', 'getenv', 'HOME')", "reflect"),
    (OPEN, "SELECT java_method('java.lang.System', 'getenv', 'HOME')", "java_method"),
]


def test_allows_read_only_queries() -> None:
    for scope, sql in ALLOWED:
        assert check_sql(sql, scope) is None, (scope.describe(), sql, check_sql(sql, scope))
        # What actually runs is the regenerated SQL, so it must pass the same checks.
        regenerated = checked_sql(sql, scope)
        assert check_sql(regenerated, scope) is None, (scope.describe(), sql, regenerated)


def test_rejects_everything_else() -> None:
    for scope, sql, reason in REJECTED:
        result = check_sql(sql, scope)
        assert result is not None and reason in result, (scope.describe(), sql, reason, result)


def test_outside_message_names_the_table() -> None:
    assert check_sql("SELECT * FROM Samples.TPCH.Orders", TAXI) == (
        "samples.tpch.orders is outside the data this assistant may use"
    )


class FutureQuery(exp.Expression, exp.Query):
    """A query type a later sqlglot release might add."""


class FutureWrite(exp.Expression, exp.DML):
    """A write statement type a later sqlglot release might add."""


def _check_parsed_as(tree: exp.Expression) -> str | None:
    """``check_sql("SELECT 1")`` with ``sqlglot.parse`` replaced to return ``tree``."""

    original = guard.sqlglot.parse
    guard.sqlglot.parse = lambda sql, read: [tree]
    try:
        return check_sql("SELECT 1", OPEN)
    finally:
        guard.sqlglot.parse = original


def test_unknown_query_and_write_types_are_rejected() -> None:
    # A write type is rejected anywhere in the query, not only at the root.
    query = sqlglot.parse_one("SELECT 1", read="databricks")
    query.set("where", exp.Where(this=FutureWrite()))
    for tree in (FutureQuery(), query):
        assert _check_parsed_as(tree) == "only read-only SELECT queries are allowed", tree


def test_checked_sql() -> None:
    # Regenerated from the checked query.
    assert checked_sql("select n from samples.nyctaxi.trips; ", OPEN) == (
        "SELECT\n  n\nFROM samples.nyctaxi.trips"
    )
    try:
        checked_sql("DELETE FROM samples.nyctaxi.trips", OPEN)
    except SqlRejected as rejected:
        assert "only read-only SELECT queries are allowed" in str(rejected), str(rejected)
    else:
        raise AssertionError("checked_sql did not raise SqlRejected")


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
