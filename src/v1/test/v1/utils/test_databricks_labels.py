"""``with_workspace`` (``v1.core.tools.databricks.labels``): names the workspace in a tool
payload.

Runs standalone (``python test_databricks_labels.py``).
"""

from __future__ import annotations

import json

from databricks_test_support import run_tests

from v1.core.tools.databricks.labels import with_workspace


def test_adds_the_workspace_after_status() -> None:
    payload = json.dumps({"status": "completed", "rows": [1], "when": "x"})
    out = with_workspace(payload, "Dev")
    assert json.loads(out) == {"status": "completed", "rows": [1], "when": "x", "workspace": "Dev"}
    assert list(json.loads(out)) == ["status", "workspace", "rows", "when"], out


def test_without_status_adds_the_workspace() -> None:
    out = json.loads(with_workspace(json.dumps({"a": 1}), "Dev"))
    assert out == {"a": 1, "workspace": "Dev"}, out


def test_keeps_an_existing_workspace() -> None:
    payload = json.dumps({"status": "succeeded", "workspace": "Prod"})
    assert with_workspace(payload, "Dev") == payload


def test_serialises_like_the_tools() -> None:
    payload = json.dumps({"status": "error", "message": "é"}, default=str)
    out = with_workspace(payload, "Invoices · prod")
    assert out == json.dumps(
        {"status": "error", "workspace": "Invoices · prod", "message": "é"}, default=str
    ), out


def test_non_json_and_non_objects_are_unchanged() -> None:
    for payload in ("not json", "", "[1, 2]", '"text"', "3", "null"):
        assert with_workspace(payload, "Dev") == payload, payload


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
