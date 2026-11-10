"""The run's Databricks workspace (``v1.core.tools.databricks.run_workspace``).

The workspaces are set through the process's registry snapshot and the legacy setting on
``v1.core.databricks_workspaces``; the run's ``configurable`` and caller groups are
replaced on the ``run_workspace`` module. A saved alias the caller can no longer see is
refused, never redirected to another workspace.

Runs standalone (``python test_databricks_run_workspace.py``) or under pytest.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

from databricks_test_support import patched_env, run_tests

import v1.core.databricks_workspaces as dw
import v1.core.tools.databricks.run_workspace as rw
from v1.core.config import Settings
from v1.core.databricks_workspaces import (
    WORKSPACE_UNAVAILABLE_MESSAGE,
    Workspace,
    WorkspaceUnavailable,
    validate_entry,
)
from v1.core.tools.databricks.run_workspace import (
    WORKSPACE_KEY,
    run_workspace,
    workspace_unavailable_payload,
)
from v1.core.workspace_registry.snapshot import get_snapshot

TWO = {
    "claims": {"host": "https://adb-claims.azuredatabricks.net", "sql_warehouse_id": "w1", "groups": ["CLAIMS"]},
    "shared": {"host": "https://adb-shared.azuredatabricks.net", "sql_warehouse_id": "w2"},
}


def _settings(legacy: str | None) -> Settings:
    with patched_env({"DATABRICKS_LEGACY_WORKSPACE": None}):
        return Settings(_env_file=None, DATABRICKS_LEGACY_WORKSPACE=legacy)


def _workspace(alias: str, entry: dict[str, Any]) -> Workspace:
    ws = validate_entry(alias, entry)
    assert isinstance(ws, Workspace), (alias, ws)
    return ws


@contextlib.contextmanager
def _run(
    configurable: dict | None,
    groups: tuple[str, ...] = (),
    *,
    outside_run: bool = False,
    workspaces: dict[str, dict[str, Any]] = TWO,
    legacy: str | None = None,
):
    """A run with ``configurable`` and caller ``groups``, over ``workspaces`` (in order)."""

    settings = _settings(legacy)

    def fake_get_config() -> object:
        if outside_run:
            raise RuntimeError("Called get_config outside of a runnable context")
        return {"configurable": configurable}

    saved = (dw.get_settings, rw.get_config, rw.groups_from_config)
    dw.get_settings = lambda: settings
    get_snapshot().replace(tuple(_workspace(a, e) for a, e in workspaces.items()))
    rw.get_config = fake_get_config
    rw.groups_from_config = lambda: groups
    try:
        yield
    finally:
        dw.get_settings, rw.get_config, rw.groups_from_config = saved
        get_snapshot().replace(())


def _refused() -> None:
    try:
        result = run_workspace()
    except WorkspaceUnavailable as exc:
        assert str(exc) == WORKSPACE_UNAVAILABLE_MESSAGE, str(exc)
    else:
        raise AssertionError(f"expected WorkspaceUnavailable, got {result!r}")


def test_run_workspace_resolves_configurable_alias() -> None:
    assert WORKSPACE_KEY == "databricks_workspace"
    with _run({WORKSPACE_KEY: "claims"}, ("CLAIMS",)):
        workspace, fallback = run_workspace()
        assert (workspace.alias, workspace.host, fallback) == ("claims", "https://adb-claims.azuredatabricks.net", False)
    with _run({WORKSPACE_KEY: "shared"}, ("CLAIMS",)):
        workspace, fallback = run_workspace()
        assert (workspace.alias, fallback) == ("shared", False)
    # No alias and no legacy workspace: the caller's default, flagged as a fallback.
    for configurable in ({}, {WORKSPACE_KEY: None}, None):
        with _run(configurable, ("CLAIMS",)):
            workspace, fallback = run_workspace()
            assert (workspace.alias, fallback) == ("claims", True), configurable
    # DATABRICKS_LEGACY_WORKSPACE: a missing alias is the legacy workspace, not a fallback.
    with _run({}, ("CLAIMS",), legacy="shared"):
        workspace, fallback = run_workspace()
        assert (workspace.alias, workspace.host, fallback) == ("shared", "https://adb-shared.azuredatabricks.net", False)


def test_run_workspace_stale_alias_refused() -> None:
    """A saved alias the caller can't see is refused, never substituted."""

    # Visible workspaces exist ("shared"), but the chat's "claims" is hidden now.
    with _run({WORKSPACE_KEY: "claims"}, ("OTHER",)):
        _refused()
    # Removed from the config.
    with _run({WORKSPACE_KEY: "gone"}, ("CLAIMS",)):
        _refused()
    # A malformed or blank alias is refused, not read as missing (which would fall back).
    for alias in (7, ["shared"], {"alias": "shared"}, "", "   "):
        with _run({WORKSPACE_KEY: alias}, ("CLAIMS",)):
            _refused()
    # Outside a run: no alias, no groups; a group-only registry is refused.
    only_claims = {"claims": TWO["claims"]}
    with _run(None, outside_run=True, workspaces=only_claims):
        _refused()


def test_workspace_unavailable_payload() -> None:
    assert json.loads(workspace_unavailable_payload()) == {
        "status": "error",
        "message": WORKSPACE_UNAVAILABLE_MESSAGE,
    }


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
