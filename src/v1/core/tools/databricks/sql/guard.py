"""Checks model-written SQL before it reaches the warehouse. Pure: no I/O.

Unity Catalog grants to the app are the real boundary (every statement runs as the app).
This is defence in depth: exactly one read-only query, no risky built-in functions, and every
table inside the allowlist when one is configured. Anything that can't be parsed is rejected.
"""

import logging

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError
from sqlglot.optimizer.scope import traverse_scope

from v1.core.tools.databricks.sql.scope import OUTSIDE_SCOPE, DataScope, format_name, split_name

# sqlglot warns when it falls back to a generic Command (e.g. SHOW); those are rejected anyway.
logging.getLogger("sqlglot").setLevel(logging.ERROR)

# External reads, network calls, paid AI calls, secrets, JVM calls, and IDENTIFIER() (dynamic
# table names would bypass the allowlist check).
BLOCKED_FUNCTIONS = frozenset(
    (
        "read_files read_kafka read_kinesis read_pubsub read_pulsar http_request remote_query "
        "ai_query ai_gen ai_classify ai_extract ai_summarize ai_translate ai_fix_grammar ai_mask "
        "ai_analyze_sentiment ai_similarity ai_parse_document ai_forecast vector_search "
        "identifier secret try_secret reflect java_method"
    ).split()
)

# `json.`/path`` style reads: the "schema" is a file format and the "table" is a path.
_FILE_FORMATS = frozenset(
    {"avro", "binaryfile", "csv", "delta", "json", "orc", "parquet", "text", "xml"}
)

# The only statement types allowed. A query type added in a later sqlglot release is rejected.
_READ_ONLY_ROOTS = (exp.Select, exp.SetOperation, exp.Subquery)

# Rejected anywhere in the query. DML and DDL are sqlglot's bases for writes, so write types
# added in later releases are caught too.
_NOT_READ_ONLY = (exp.DML, exp.DDL) + tuple(
    getattr(exp, name)
    for name in (
        "Alter Cache Command Commit Copy Create Delete Describe Drop Grant Insert LoadData Merge "
        "Pragma Revoke Rollback Set Transaction TruncateTable Uncache Update Use"
    ).split()
    if hasattr(exp, name)  # class names vary slightly across sqlglot releases
)


class SqlRejected(ValueError):
    """The query may not run. The message is a reason the model can act on."""


def checked_sql(sql: str, scope: DataScope) -> str:
    """The SQL to run, regenerated from the parse tree that passed every check, so the warehouse
    runs exactly what was checked rather than text its own parser might read differently."""
    query = _parse(sql)
    if reason := _reason(query, scope):
        raise SqlRejected(reason)
    try:
        # RAISE: never let the generator drop or rewrite something it can't express.
        return query.sql(dialect="databricks", pretty=True, unsupported_level=ErrorLevel.RAISE)
    except SqlglotError:
        raise SqlRejected("the query could not be analysed; simplify it") from None


def check_sql(sql: str, scope: DataScope) -> str | None:
    """Return None if the query may run, otherwise a reason the model can act on."""
    try:
        return _reason(_parse(sql), scope)
    except SqlRejected as rejected:
        return str(rejected)


def _parse(sql: str) -> exp.Expression:
    try:
        statements = [s for s in sqlglot.parse(sql, read="databricks") if s is not None]
    except SqlglotError:
        raise SqlRejected(
            "the query could not be parsed; write one standard Databricks SQL SELECT statement"
        ) from None
    if len(statements) != 1:
        raise SqlRejected("send exactly one SQL statement")
    return statements[0]


def _reason(query: exp.Expression, scope: DataScope) -> str | None:
    if not isinstance(query, _READ_ONLY_ROOTS) or query.find(*_NOT_READ_ONLY):
        return "only read-only SELECT queries are allowed"
    for func in query.find_all(exp.Func):
        if (name := _func_name(func).lower()) in BLOCKED_FUNCTIONS:
            return f"the function {name}() is not allowed"
    tables = list(query.find_all(exp.Table))
    for table in tables:
        if not isinstance(table.this, exp.Identifier):
            return "table-valued functions are not allowed; query tables directly"
        if _is_path_read(table):
            return "reading files by path is not allowed; query tables directly"
    if scope.is_open:
        return None
    return _check_allowlist(query, tables, scope)


def _func_name(func: exp.Func) -> str:
    return func.name if isinstance(func, exp.Anonymous) else func.sql_name()


def _is_path_read(table: exp.Table) -> bool:
    name = table.name
    return (
        (not table.catalog and table.db.lower() in _FILE_FORMATS)
        or name.startswith("/")
        or "://" in name
    )


def _check_allowlist(query: exp.Query, tables: list[exp.Table], scope: DataScope) -> str | None:
    try:
        scopes = list(traverse_scope(query))
    except Exception:  # noqa: BLE001 - any failure to resolve scopes must fail closed
        return "the query could not be analysed; simplify it"
    # A CTE name only hides a table inside the scopes where that CTE is visible: in
    # `SELECT * FROM orders WHERE EXISTS (WITH orders AS ... )` the outer `orders` is a real table.
    reached: set[int] = set()
    for query_scope in scopes:
        visible_ctes = {name.lower() for name in query_scope.cte_sources}
        for table in query_scope.tables:
            reached.add(id(table))
            if not table.catalog and not table.db and table.name.lower() in visible_ctes:
                continue
            if reason := _table_reason(table, scope):
                return reason
    if any(id(table) not in reached for table in tables):
        return "the query could not be analysed; simplify it"
    for dot in query.find_all(exp.Dot):
        # e.g. catalog.schema.my_udf(...)
        if isinstance(dot.expression, exp.Func) and (reason := _function_reason(dot, scope)):
            return reason
    return None


def _table_reason(table: exp.Table, scope: DataScope) -> str | None:
    if not (table.catalog and table.db):
        return f"use the fully qualified catalog.schema.table name instead of {table.name}"
    return _outside_reason(scope, table.catalog, table.db, table.name)


def _function_reason(dot: exp.Dot, scope: DataScope) -> str | None:
    # A user-defined function can read any table its owner can, so it must be in scope too.
    function_name = _func_name(dot.expression)
    try:
        qualifier = split_name(dot.this.sql(dialect="databricks"))
    except ValueError:
        return "the query could not be analysed; simplify it"
    if len(qualifier) != 2:
        return f"use the fully qualified catalog.schema.function name for {function_name}"
    return _outside_reason(scope, *qualifier, function_name)


def _outside_reason(scope: DataScope, *parts: str) -> str | None:
    parts = tuple(part.lower() for part in parts)
    return None if scope.covers(*parts) else OUTSIDE_SCOPE.format(format_name(*parts))
