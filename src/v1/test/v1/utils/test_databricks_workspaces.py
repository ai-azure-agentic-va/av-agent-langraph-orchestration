"""The Databricks workspace validator and the chat-workspace resolver.

Every workspace write goes through ``validate_entry``: a bad field is named, never
widened (a malformed allowlist or group list is refused, not read as open). A chat's
alias resolves to a workspace the caller can see, or is refused. A missing alias
resolves to the legacy workspace, else the caller's default (flagged so callers drop
the saved tables). Never another workspace.

Runs standalone (``python test_databricks_workspaces.py``) or under pytest.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from databricks_test_support import patched_env, run_tests

import v1.core.databricks_workspaces as dw
from v1.core.config import Settings, get_settings
from v1.core.databricks_workspaces import (
    ALIAS_PATTERN,
    WORKSPACE_UNAVAILABLE_MESSAGE,
    Workspace,
    WorkspaceUnavailable,
    check_warehouse_id,
    validate_entry,
    workspace_fields,
)
from v1.core.workspace_registry.snapshot import get_snapshot


def _ws(host: str = "https://adb-h.azuredatabricks.net", warehouse: str = "w", **extra: Any) -> dict[str, Any]:
    return {"host": host, "sql_warehouse_id": warehouse, **extra}


def _valid(alias: str, entry: dict[str, Any]) -> Workspace:
    result = validate_entry(alias, entry)
    assert isinstance(result, Workspace), (alias, result)
    return result


@contextmanager
def _registry(entries: dict[str, dict[str, Any]], legacy: str | None = None) -> Iterator[None]:
    """Point the process's snapshot at ``entries`` (in order) and the legacy setting."""

    with patched_env({"DATABRICKS_LEGACY_WORKSPACE": None}):
        settings = Settings(_env_file=None, DATABRICKS_LEGACY_WORKSPACE=legacy)
    dw.get_settings = lambda: settings
    get_snapshot().replace(tuple(_valid(alias, entry) for alias, entry in entries.items()))
    try:
        yield
    finally:
        dw.get_settings = get_settings
        get_snapshot().replace(())


THREE = {
    "claims": _ws(label="Invoices · prod", groups=["CLAIMS-ANALYSTS", "gid-claims"]),
    "shared": _ws(groups=["*"]),
    "eng": _ws(jobs_enabled=False, groups=["DATA-ENG"]),
}


def _raises_unavailable(fn: Any, *args: Any) -> None:
    try:
        result = fn(*args)
    except WorkspaceUnavailable as exc:
        assert str(exc) == WORKSPACE_UNAVAILABLE_MESSAGE, str(exc)
    else:
        raise AssertionError(f"{fn.__name__}{args!r} returned {result!r}")


def test_message_and_legacy_alias():
    assert WORKSPACE_UNAVAILABLE_MESSAGE == (
        "This chat's Databricks workspace isn't available to you. Pick another in the workspace menu."
    )
    assert dw.LEGACY_ALIAS == "default"


def test_alias_rule():
    for bad in ("Invoices", "a_b", "default", "", "a" * 65, " claims", "claims ", "a.b", 7, None):
        assert validate_entry(bad, _ws()) == "alias", bad
    for good in ("claims-prod", "a", "a" * 64, "0-9", "-"):
        assert isinstance(validate_entry(good, _ws()), Workspace), good
    assert ALIAS_PATTERN.fullmatch("claims-prod") and not ALIAS_PATTERN.fullmatch("Invoices")


def test_validate_entry_fields():
    zeta = _valid("zeta", _ws(host="https://adb-z.azuredatabricks.net", warehouse=" wz ", label=" Zeta ", data_allowlist="main.sales"))
    assert (zeta.alias, zeta.label, zeta.host, zeta.sql_warehouse_id) == ("zeta", "Zeta", "https://adb-z.azuredatabricks.net", "wz")
    assert zeta.data_allowlist == "main.sales" and zeta.scope.entries == (("main", "sales"),)
    assert zeta.jobs_enabled is True and zeta.groups == () and zeta.everyone
    alpha = _valid("alpha", _ws(jobs_enabled=False, groups=["G1", "G2"]))
    assert alpha.label == "alpha"
    assert alpha.data_allowlist is None and alpha.scope.entries == ()  # no allowlist = open scope
    assert alpha.jobs_enabled is False
    assert alpha.groups == ("G1", "G2") and not alpha.everyone
    # null is the default, as for the other optional fields.
    assert _valid("nulled", _ws(jobs_enabled=None)).jobs_enabled is True


def test_validate_entry_names_the_first_bad_field():
    cases = {
        "entry": ("x", [1], None),
        "host": ({"sql_warehouse_id": "w"}, {"host": "http://adb-1.azuredatabricks.net"}),
        "sql_warehouse_id": ({"host": "https://adb-h.azuredatabricks.net"}, _ws(warehouse="  "), _ws(warehouse=7)),
        "data_allowlist": (_ws(data_allowlist="a.b.c.d"), _ws(data_allowlist=" , "), _ws(data_allowlist=""), _ws(data_allowlist=["a"])),
        "label": (_ws(label="  "), _ws(label=7)),
        "jobs_enabled": (_ws(jobs_enabled="true"), _ws(jobs_enabled=1)),
    }
    for field, entries in cases.items():
        for entry in entries:
            assert validate_entry("a", entry) == field, (field, entry)
    assert validate_entry("Bad", "not an object") == "alias"  # the alias is checked first


def test_warehouse_id_is_a_url_safe_token():
    # It goes into the warehouses API path: no slash, dot, space or other character.
    for bad in ("../x", "a/b", "", "a b", "wh.1", "wh?x=1", "wh%2F", "x" * 65):
        assert validate_entry("a", _ws(warehouse=bad)) == "sql_warehouse_id", bad
        assert check_warehouse_id(bad) is None, bad
    for good in ("0123456789abcdef", "wh-1", "A" * 64, " 1a2b3c4d5e6f7a8b "):
        assert _valid("a", _ws(warehouse=good)).sql_warehouse_id == good.strip(), good
        assert check_warehouse_id(good) == good.strip(), good


def test_malformed_groups_are_refused_never_everyone():
    for bad in ("", 0, False, {}, [" "], [""], "*", [1]):
        assert validate_entry("a", _ws(groups=bad)) == "groups", bad
    for everyone in ([], None):
        assert _valid("a", _ws(groups=everyone)).everyone, everyone
    assert _valid("a", _ws()).everyone
    mixed = _valid("a", _ws(groups=["A", " "]))
    assert mixed.groups == ("A",) and not mixed.everyone  # blank items alongside real ones are ignored


def test_host_must_be_an_azure_databricks_https_url():
    good = (
        "https://adb-1.2.azuredatabricks.net",
        "https://adb-1.2.azuredatabricks.net/",
        "HTTPS://ADB-1.2.AzureDatabricks.NET",
        " https://adb-1.2.azuredatabricks.net ",
    )
    bad = (
        "http://adb-1.2.azuredatabricks.net",
        "adb-1.2.azuredatabricks.net",
        "https://example.com",
        "https://azuredatabricks.net",
        "https://adb-1.azuredatabricks.net.evil.com",
        "https://x.azuredatabricks.net.evil.com",
        "https://evil.com/adb-1.azuredatabricks.net",
        "https://adb-1.azuredatabricks.net/api",
        "https://adb-1.azuredatabricks.net?x=1",
        "https://adb-1.azuredatabricks.net#x",
        "https://user@adb-1.azuredatabricks.net",
        "https://adb-1.azuredatabricks.net:8443",
        "https://",
        7,
    )
    for host in good:
        assert isinstance(validate_entry("a", _ws(host=host)), Workspace), host
    assert _valid("a", _ws(host=good[3])).host == "https://adb-1.2.azuredatabricks.net"  # stripped
    for host in bad:
        assert validate_entry("a", _ws(host=host)) == "host", host
        assert dw.check_host(host) is None, host


def test_validate_entry_round_trips():
    entries = (
        _ws(),
        _ws(host="https://ADB-1.azuredatabricks.net/", label="Invoices · prod", data_allowlist="main.sales, hr", jobs_enabled=False, groups=["G1", "*"]),
        _ws(groups=["A", " "], label=" spaced ", warehouse=" w2 "),
    )
    for entry in entries:
        ws = _valid("claims-prod", entry)
        fields = workspace_fields(ws)
        assert set(fields) == {"label", "host", "sql_warehouse_id", "data_allowlist", "jobs_enabled", "groups"}, fields
        assert isinstance(fields["groups"], list)
        assert validate_entry("claims-prod", fields) == ws, (fields, ws)
    assert workspace_fields(_valid("a", _ws()))["data_allowlist"] is None


def test_visibility_by_group_id_or_name_and_star():
    def aliases(groups: list[str]) -> list[str]:
        return [w.alias for w in dw.visible_workspaces(groups)]

    with _registry(THREE):
        assert aliases(["CLAIMS-ANALYSTS"]) == ["claims", "shared"]
        assert aliases(["gid-claims"]) == ["claims", "shared"]
        assert aliases(["DATA-ENG", "gid-claims"]) == ["claims", "shared", "eng"]
        assert aliases([]) == ["shared"]
        assert aliases(["claims-analysts"]) == ["shared"]  # exact match only
    with _registry({"eng": _ws(groups=["DATA-ENG"])}):
        assert dw.visible_workspaces(["OTHER"]) == ()


def test_resolve_workspace():
    # (legacy, groups, alias) -> (workspace, legacy_fallback); None: WorkspaceUnavailable.
    cases = [
        (None, ["DATA-ENG"], "eng", ("eng", False)),
        (None, ["DATA-ENG"], "shared", ("shared", False)),
        (None, ["DATA-ENG"], "claims", None),  # hidden
        (None, ["DATA-ENG"], "nope", None),  # unknown
        (None, ["DATA-ENG"], "Eng", None),  # aliases are exact
        # A missing alias (or "default") is the legacy workspace...
        ("eng", ["DATA-ENG"], None, ("eng", False)),
        ("eng", ["DATA-ENG"], "default", ("eng", False)),
        ("eng", ["DATA-ENG"], " default ", ("eng", False)),
        # ...else the caller's default, flagged.
        (None, ["DATA-ENG", "gid-claims"], None, ("claims", True)),
        (None, ["DATA-ENG", "gid-claims"], "default", ("claims", True)),
        (None, ["DATA-ENG"], None, ("shared", True)),
        # A hidden or unknown legacy workspace is refused, never substituted.
        ("claims", ["DATA-ENG"], None, None),
        ("claims", ["DATA-ENG"], "default", None),
        ("gone", ["DATA-ENG"], None, None),
        # A blank alias was sent: refused, never read as missing.
        *((legacy, ["DATA-ENG"], blank, None) for legacy in (None, "shared") for blank in ("", " \t")),
    ]
    for legacy, groups, alias, expected in cases:
        with _registry(THREE, legacy=legacy):
            if expected is None:
                _raises_unavailable(dw.resolve_workspace, groups, alias)
            else:
                ws, fallback = dw.resolve_workspace(groups, alias)
                assert (ws.alias, fallback) == expected, (legacy, groups, alias, ws.alias, fallback)
    with _registry({}):
        _raises_unavailable(dw.resolve_workspace, ["DATA-ENG"], None)
    with _registry({"eng": _ws(groups=["DATA-ENG"])}):
        for alias in (None, "default"):
            _raises_unavailable(dw.resolve_workspace, ["OTHER"], alias)


def test_canonical_alias_one_rule_for_chat_and_tables():
    with _registry(THREE, legacy=" eng "):
        assert dw.legacy_alias() == "eng"
        assert dw.canonical_alias(None) == "eng"
        assert dw.canonical_alias("default") == "eng"
        assert dw.canonical_alias(" claims ") == "claims"
        assert dw.canonical_alias("anything") == "anything"  # visibility is checked later
        for bad in ("", "  ", 7, ["eng"], {"alias": "eng"}):
            _raises_unavailable(dw.canonical_alias, bad)
    with _registry(THREE):
        assert dw.legacy_alias() is None
        assert dw.canonical_alias(None) is None
        assert dw.canonical_alias("default") is None


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
