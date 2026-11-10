"""The ``list_workspaces`` tool (``v1.core.tools.databricks.workspaces``).

The registry is set through the process's snapshot; the run's ``configurable`` and the
caller's groups are replaced on the ``run_workspace`` and ``workspaces`` modules. Only the
caller's visible workspaces are listed, the chat's own is marked, and nothing calls
Databricks.

Runs standalone (``python test_databricks_workspace_tool.py``).
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

from databricks_test_support import patched_env, run_tests

import v1.core.databricks_workspaces as dw
import v1.core.tools.databricks.run_workspace as rw
import v1.core.tools.databricks.workspaces as wt
from v1.core.config import Settings
from v1.core.databricks_workspaces import WORKSPACE_UNAVAILABLE_MESSAGE, Workspace, validate_entry
from v1.core.tools.databricks.workspaces import (
    LIST_WORKSPACES,
    SWITCH_HINT,
    build_workspace_tools,
    list_workspaces_payload,
)
from v1.core.workspace_registry.snapshot import get_snapshot
from v1.utils import databricks_client

REGISTRY = {
    "dev": {
        "label": "Dev",
        "host": "https://adb-dev.azuredatabricks.net",
        "sql_warehouse_id": "w1",
        "groups": ["A"],
    },
    "hidden-b": {
        "label": "Hidden B",
        "host": "https://adb-hidden.azuredatabricks.net",
        "sql_warehouse_id": "w2",
        "groups": ["B"],
    },
    "shared": {
        "label": "Shared",
        "host": "https://adb-shared.azuredatabricks.net",
        "sql_warehouse_id": "w3",
        "data_allowlist": "main.sales",
        "jobs_enabled": False,
    },
}


def _workspace(alias: str, entry: dict[str, Any]) -> Workspace:
    ws = validate_entry(alias, entry)
    assert isinstance(ws, Workspace), (alias, ws)
    return ws


@contextlib.contextmanager
def _run(configurable: dict | None, groups: tuple[str, ...] = ("A",), registry=REGISTRY):
    """A run with ``configurable`` for a caller in ``groups`` over ``registry`` (in order)."""

    with patched_env({"DATABRICKS_LEGACY_WORKSPACE": None}):
        settings = Settings(_env_file=None)
    saved = (dw.get_settings, rw.get_config, rw.groups_from_config, wt.groups_from_config)
    dw.get_settings = lambda: settings
    get_snapshot().replace(tuple(_workspace(a, e) for a, e in registry.items()))
    rw.get_config = lambda: {"configurable": configurable}
    rw.groups_from_config = wt.groups_from_config = lambda: groups
    try:
        yield
    finally:
        dw.get_settings, rw.get_config, rw.groups_from_config, wt.groups_from_config = saved
        get_snapshot().replace(())


def test_lists_only_visible_workspaces_in_order() -> None:
    with _run({"databricks_workspace": "dev"}):
        p = json.loads(list_workspaces_payload())
    assert p["status"] == "ok", p
    assert [w["alias"] for w in p["workspaces"]] == ["dev", "shared"], p
    assert [w["label"] for w in p["workspaces"]] == ["Dev", "Shared"], p
    assert [w["jobs_enabled"] for w in p["workspaces"]] == [True, False], p
    assert "hidden" not in json.dumps(p).lower(), p


def test_marks_current_workspace() -> None:
    with _run({"databricks_workspace": "dev"}):
        p = json.loads(list_workspaces_payload())
    assert p["current"] == "dev" and [w["current"] for w in p["workspaces"]] == [True, False]
    with _run({"databricks_workspace": "shared"}):
        p = json.loads(list_workspaces_payload())
    assert p["current"] == "shared" and [w["current"] for w in p["workspaces"]] == [False, True]


def test_hidden_saved_alias_gives_null_current() -> None:
    for alias in ("hidden-b", "gone", 7):
        with _run({"databricks_workspace": alias}):
            p = json.loads(list_workspaces_payload())
        assert p["status"] == "ok" and p["current"] is None, (alias, p)
        assert [w["alias"] for w in p["workspaces"]] == ["dev", "shared"], (alias, p)
        assert not any(w["current"] for w in p["workspaces"]), (alias, p)
        assert "hidden" not in json.dumps(p).lower(), (alias, p)


def test_no_visible_workspace_is_unavailable_payload() -> None:
    registry = {"hidden-b": REGISTRY["hidden-b"]}
    with _run({"databricks_workspace": "hidden-b"}, ("A",), registry):
        p = json.loads(list_workspaces_payload())
    assert p == {"status": "error", "message": WORKSPACE_UNAVAILABLE_MESSAGE}, p
    with _run({}, ("A",), {}):
        p = json.loads(list_workspaces_payload())
    assert p == {"status": "error", "message": WORKSPACE_UNAVAILABLE_MESSAGE}, p


def test_data_open_or_limited_and_no_hosts() -> None:
    with _run({"databricks_workspace": "dev"}):
        p = json.loads(list_workspaces_payload())
    assert {"open", "limited"} == {w["data"] for w in p["workspaces"]}, p
    assert [w["data"] for w in p["workspaces"]] == ["open", "limited"], p
    text = json.dumps(p)
    assert "host" not in text and "warehouse" not in text, text
    assert "azuredatabricks" not in text and "main.sales" not in text, text
    for w in p["workspaces"]:
        assert set(w) == {"label", "alias", "current", "jobs_enabled", "data"}, w


def test_makes_no_databricks_call() -> None:
    async def refuse(host: str):
        raise AssertionError(f"Databricks called for {host}")

    saved = databricks_client.app_workspace_client
    databricks_client.app_workspace_client = refuse
    try:
        with _run({"databricks_workspace": "dev"}):
            [tool] = build_workspace_tools()
            p = json.loads(tool.invoke({}))
    finally:
        databricks_client.app_workspace_client = saved
    assert p["status"] == "ok" and p["current"] == "dev", p


def test_switch_hint_text() -> None:
    assert SWITCH_HINT == "To use another workspace, choose it in the chat's workspace picker."
    with _run({"databricks_workspace": "dev"}):
        p = json.loads(list_workspaces_payload())
    assert p["switch_hint"] == "To use another workspace, choose it in the chat's workspace picker."


def test_tool_takes_no_arguments() -> None:
    [tool] = build_workspace_tools()
    assert LIST_WORKSPACES == "list_workspaces" and tool.name == LIST_WORKSPACES
    assert not tool.args, tool.args


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
