"""Tests for the per-run table selection (``v1.core.tools.databricks.sql.selection``).

``get_config`` is replaced on the selection module, so no LangGraph run is needed.
Runs standalone (``python test_databricks_sql_selection.py``).
"""

from __future__ import annotations

import contextlib

from databricks_test_support import captured_logs, run_tests

import v1.core.databricks_workspaces as dw
from v1.core.databricks_workspaces import Workspace
from v1.core.tools.databricks.sql import selection
from v1.core.tools.databricks.sql.scope import DataScope
from v1.core.tools.databricks.sql.selection import (
    MAX_NAME_CHARS,
    MAX_SELECTED,
    SELECTION_KEY,
    TABLES_WORKSPACE_KEY,
    SelectionInvalid,
    narrowed_scope,
    run_scope,
    run_selection,
    selection_dropped,
)

OPEN = DataScope.parse("")
TAXI = DataScope.parse("samples.nyctaxi")


def _raises_invalid(selected: list[str], configured: DataScope = TAXI) -> str:
    try:
        narrowed_scope(selected, configured)
    except SelectionInvalid as exc:
        return str(exc)
    raise AssertionError(f"{selected!r} was accepted")


@contextlib.contextmanager
def _config(config: object = None, *, outside_run: bool = False):
    def fake_get_config() -> object:
        if outside_run:
            raise RuntimeError("Called get_config outside of a runnable context")
        return config

    saved = selection.get_config
    selection.get_config = fake_get_config
    try:
        yield
    finally:
        selection.get_config = saved


def test_empty_selection_keeps_configured_scope() -> None:
    assert narrowed_scope([], TAXI) is TAXI
    assert narrowed_scope([], OPEN) is OPEN


def test_selection_narrows_to_selected_tables() -> None:
    scope = narrowed_scope(["samples.nyctaxi.trips"], TAXI)
    assert scope.entries == (("samples", "nyctaxi", "trips"),)
    assert not scope.covers("samples", "nyctaxi", "zones")


def test_open_scope_accepts_any_full_name() -> None:
    scope = narrowed_scope(["other.s.t", "`my-cat`.s.t"], OPEN)
    assert scope.entries == (("other", "s", "t"), ("my-cat", "s", "t"))


def test_invalid_selections_are_rejected() -> None:
    assert (MAX_SELECTED, MAX_NAME_CHARS) == (20, 800)
    names = [f"samples.nyctaxi.t{i}" for i in range(MAX_SELECTED + 1)]
    long_name = "samples.nyctaxi." + "t" * (MAX_NAME_CHARS - len("samples.nyctaxi.") + 1)
    cases = [
        (["other.s.t"], TAXI, "other.s.t is outside the data this assistant may use"),
        (names, OPEN, "at most 20"),
        (["samples.nyctaxi"], TAXI, "full catalog.schema.table name"),
        (["samples.nyctaxi.trips\n"], TAXI, "printable"),
        ([long_name], OPEN, "800"),
    ]
    for selected, configured, text in cases:
        message = _raises_invalid(selected, configured)
        assert text in message, (selected, message)
        # Unsafe names are never echoed back.
        assert "\n" not in message and long_name not in message, message
    assert len(narrowed_scope(names[:MAX_SELECTED], OPEN).entries) == MAX_SELECTED


def test_dedupes_case_insensitively() -> None:
    scope = narrowed_scope(
        ["samples.nyctaxi.trips", "Samples.NYCTAXI.Trips", "samples.nyctaxi.zones"], TAXI
    )
    assert scope.entries == (("samples", "nyctaxi", "trips"), ("samples", "nyctaxi", "zones"))


def _ws(alias: str = "default", scope: DataScope = OPEN) -> Workspace:
    return Workspace(
        alias=alias,
        label=alias,
        host=f"https://{alias}",
        sql_warehouse_id="w",
        data_allowlist=None,
        scope=scope,
        jobs_enabled=True,
        groups=(),
    )


DEFAULT = _ws()
A = _ws("a", TAXI)
B = _ws("b", TAXI)  # same allowlist as A: a shared metastore


@contextlib.contextmanager
def _legacy(alias: str | None):
    saved = dw.legacy_alias
    dw.legacy_alias = lambda: alias
    try:
        yield
    finally:
        dw.legacy_alias = saved


@contextlib.contextmanager
def _logs():
    with captured_logs(selection.__name__) as capture:
        yield capture.messages


def _picked(tables: object, tables_workspace: object = None) -> dict:
    configurable: dict = {SELECTION_KEY: tables}
    if tables_workspace is not None:
        configurable[TABLES_WORKSPACE_KEY] = tables_workspace
    return {"configurable": configurable}


def test_run_selection_reads_configurable() -> None:
    assert SELECTION_KEY == "databricks_tables"
    assert TABLES_WORKSPACE_KEY == "databricks_tables_workspace"
    with _config(_picked(["a.b.c"], "default")), _legacy("default"):
        assert run_selection(DEFAULT, False) == ["a.b.c"]
        assert run_scope(DEFAULT, False)[1].entries == (("a", "b", "c"),)
        # The raw selection comes back with the scope (the SQL tools read both from here).
        assert run_scope(DEFAULT, False)[0] == ["a.b.c"]


def test_run_selection_is_empty_outside_a_run_or_when_absent() -> None:
    with _config(outside_run=True):
        assert run_selection(A, False) == []
        assert run_scope(A, False)[1] is A.scope
        assert not selection_dropped(A, False)
    for config in (None, {}, {"configurable": {}}, {"configurable": {SELECTION_KEY: None}}):
        with _config(config):
            assert run_selection(A, False) == [], config
            assert not selection_dropped(A, False), config
            assert not selection_dropped(A, True), config


def test_run_selection_rejects_non_list() -> None:
    for value in ("a.b.c", {"a.b.c": True}, ["a.b.c", 1]):
        with _config(_picked(value, "a")):
            try:
                run_selection(A, False)
            except SelectionInvalid:
                continue
            raise AssertionError(f"{value!r} was accepted")


def test_selection_applies_when_tables_workspace_matches() -> None:
    with _config(_picked(["samples.nyctaxi.trips"], "a")), _logs() as messages:
        assert run_selection(A, False) == ["samples.nyctaxi.trips"]
        assert run_scope(A, False)[1].entries == (("samples", "nyctaxi", "trips"),)
        assert not selection_dropped(A, False)
    assert messages == [], messages


def test_selection_ignored_when_tables_workspace_differs_even_if_in_allowlist() -> None:
    """Picked in A, chat now on B; the names pass B's allowlist too."""

    with _config(_picked(["samples.nyctaxi.trips"], "a")), _logs() as messages:
        assert run_selection(B, False) == []
        assert run_scope(B, False)[1] is B.scope
        assert selection_dropped(B, False)
    assert messages and all(
        m == "databricks_selection_ignored reason=other_workspace" for m in messages
    ), messages
    # A tables_workspace that isn't a string never matches.
    with _config(_picked(["samples.nyctaxi.trips"], 7)):
        assert run_selection(A, False) == [] and selection_dropped(A, False)


def test_missing_tables_workspace_treated_as_legacy() -> None:
    picked = _picked(["samples.nyctaxi.trips"])  # an old UI sends no tables workspace
    with _config(picked), _legacy("default"):
        assert run_selection(DEFAULT, False) == ["samples.nyctaxi.trips"]
        assert not selection_dropped(DEFAULT, False)
        assert run_selection(A, False) == [] and selection_dropped(A, False)
    with _config(picked), _legacy("a"):
        assert run_selection(A, False) == ["samples.nyctaxi.trips"]
        assert run_selection(DEFAULT, False) == []
    # A blank tables workspace was sent: it never matches (not read as missing).
    for blank in ("", "  "):
        with _config(_picked(["samples.nyctaxi.trips"], blank)), _legacy("a"):
            assert run_selection(A, False) == [] and selection_dropped(A, False), blank
    # No legacy workspace: the tables belong to no workspace.
    with _config(picked), _legacy(None):
        assert run_selection(DEFAULT, False) == [] and selection_dropped(DEFAULT, False)


def test_default_tables_workspace_follows_the_legacy_alias() -> None:
    """Migration: a chat saved as "default" (workspace and tables) with
    DATABRICKS_LEGACY_WORKSPACE=a resolves to a, and its tables still apply there."""

    picked = _picked(["samples.nyctaxi.trips"], "default")
    with _config(picked), _legacy("a"), _logs() as messages:
        assert run_selection(A, False) == ["samples.nyctaxi.trips"]
        assert not selection_dropped(A, False)
        assert run_selection(B, False) == [] and selection_dropped(B, False)
    assert messages == ["databricks_selection_ignored reason=other_workspace"], messages
    # Padded aliases compare as the resolver reads them.
    with _config(_picked(["samples.nyctaxi.trips"], " a ")), _legacy(None):
        assert run_selection(A, False) == ["samples.nyctaxi.trips"]


def test_legacy_fallback_drops_tables() -> None:
    """Saved tables, no alias, no legacy workspace: the default stands in."""

    for picked in (_picked(["samples.nyctaxi.trips"]), _picked(["samples.nyctaxi.trips"], "a")):
        with _config(picked), _legacy("a"), _logs() as messages:
            assert run_selection(A, True) == []
            assert run_scope(A, True)[1] is A.scope
            assert selection_dropped(A, True)
        assert messages and all(
            m == "databricks_selection_ignored reason=legacy_fallback" for m in messages
        ), messages
    # Nothing was picked: nothing is dropped.
    with _config(_picked([], "a")):
        assert run_selection(A, True) == [] and not selection_dropped(A, True)


def test_scope_uses_workspace_allowlist() -> None:
    with _config({"configurable": {}}):
        assert run_scope(A, False)[1] is A.scope
        assert run_scope(DEFAULT, False)[1] is DEFAULT.scope
    # The same names, checked against each workspace's own allowlist.
    with _config(_picked(["other.s.t"], "default")), _legacy("default"):
        assert run_scope(DEFAULT, False)[1].entries == (("other", "s", "t"),)
    with _config(_picked(["other.s.t"], "a")):
        try:
            run_scope(A, False)
        except SelectionInvalid:
            return
    raise AssertionError("a selection outside the workspace allowlist was accepted")


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
