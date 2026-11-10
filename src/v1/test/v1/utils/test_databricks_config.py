"""Settings for the databricks-agent: when it switches on, and what switches it off.

``DATABRICKS_ENABLED`` switches it on. It authenticates as the app (the shared Azure
credential), so nothing else is required: ``databricks_config_problem`` stays ``None``.
Workspaces are not settings: their rules are tested in ``test_databricks_workspaces``.

Runs standalone (``python test_databricks_config.py``) or under pytest.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from types import SimpleNamespace
from typing import Any

from databricks_test_support import patched_env, run_tests
from pydantic import ValidationError

import v1.core.config as config
import v1.core.databricks_workspaces as dw
from v1.core.config import (
    RETIRED_DATABRICKS_SETTINGS,
    Settings,
    admin_object_ids,
    ignored_admin_entries,
    retired_databricks_settings,
)
from v1.core.workspace_registry.snapshot import get_snapshot

SWITCH = "DATABRICKS_ENABLED"
ADMIN_ID = "3a5c7e90-1b2d-4f6a-8c0e-2d4f6a8c0e1b"

_ENV_KEYS = (
    "AGENT_AUTH_MODE",
    "APP_ENV",
    "AZURE_KEY_VAULT_URI",
    "DATABRICKS_ENABLED",
    "DATABRICKS_WORKSPACES_SEED_FILE",
    "DATABRICKS_ADMIN_GROUPS",
    "DATABRICKS_REGISTRY_POLL_SECONDS",
    "DATABRICKS_LEGACY_WORKSPACE",
    *RETIRED_DATABRICKS_SETTINGS,
    "DATABRICKS_SQL_MAX_ROWS_TO_MODEL",
    "DATABRICKS_SQL_MAX_ROWS_TO_UI",
    "REDIS_URI",
    # Former OBO prerequisites: Databricks no longer reads them.
    "AGENT_AUTH_GROUPS_OBO",
    "AGENT_AUTH_TENANT",
    "AGENT_AUTH_CLIENT_ID",
    "ENTRA_TENANT_ID",
    "ENTRA_CLIENT_ID",
    "ENTRA_CLIENT_SECRET",
)


def _with_env(env: dict[str, str], build: Any) -> Any:
    """Run ``build()`` with exactly ``env`` set for the Databricks-related keys."""

    with patched_env({**dict.fromkeys(_ENV_KEYS), **env}):
        return build()


def _plain(**env: str) -> Settings:
    return _with_env(env, lambda: Settings(_env_file=None))


def _settings(**env: str) -> Any:
    """Settings built from ``env`` (local dev auth unless overridden), with the
    Databricks properties evaluated while that env is still in place."""

    def build() -> SimpleNamespace:
        s = Settings(_env_file=None)
        return SimpleNamespace(
            databricks_enabled=s.databricks_enabled,
            databricks_config_problem=s.databricks_config_problem,
            unconfigured_capabilities=s.unconfigured_capabilities,
        )

    return _with_env({"AGENT_AUTH_MODE": "local", "APP_ENV": "local", **env}, build)


def _ws(alias: str, *groups: str) -> dw.Workspace:
    ws = dw.validate_entry(
        alias,
        {"host": "https://adb-1.azuredatabricks.net", "sql_warehouse_id": "w", "groups": list(groups)},
    )
    assert isinstance(ws, dw.Workspace), ws
    return ws


def test_defaults():
    s = _plain()
    assert s.databricks_switch is False
    assert s.databricks_enabled is False and s.databricks_config_problem is None
    assert s.databricks_workspaces_seed_file is None
    assert s.databricks_admin_groups == []
    assert s.databricks_registry_poll_seconds == 30
    assert s.databricks_legacy_workspace is None
    assert not hasattr(s, "databricks_config_profile")
    assert s.databricks_sql_timeout_seconds == 50
    assert s.databricks_sql_poll_interval_seconds == 1
    assert s.databricks_sql_max_rows_to_model == 50
    assert s.databricks_sql_max_rows_to_ui == 200
    assert s.databricks_jobs_timeout_seconds == 30
    assert s.databricks_explain_timeout_seconds == 60
    assert s.redis_uri is None
    for name in RETIRED_DATABRICKS_SETTINGS:
        assert not hasattr(s, name.lower()), name


def test_config_problem():
    # env -> enabled; nothing but the switch matters, in local dev or deployed.
    cases = [
        ({SWITCH: "true"}, True),
        ({"AGENT_AUTH_MODE": "jwt", "APP_ENV": "dev", SWITCH: "true"}, True),
        ({"AGENT_AUTH_MODE": "jwt", "APP_ENV": "dev", SWITCH: "true", "AGENT_AUTH_GROUPS_OBO": "false"}, True),
        # Switched off: the retired settings switch nothing on.
        ({"AGENT_AUTH_MODE": "jwt"}, False),
        ({"DATABRICKS_HOST": "https://adb-1.azuredatabricks.net", "DATABRICKS_WORKSPACES": "{}"}, False),
    ]
    for env, enabled in cases:
        s = _settings(**env)
        assert s.databricks_config_problem is None, (env, s.databricks_config_problem)
        assert s.databricks_enabled is enabled, env
        assert (("databricks-agent", SWITCH) in s.unconfigured_capabilities) is not enabled, env


def test_switched_on_needs_no_redis_key_or_obo():
    s = _settings(
        AGENT_AUTH_MODE="jwt", APP_ENV="dev", DATABRICKS_ENABLED="true", REDIS_URI="", ENTRA_CLIENT_SECRET=""
    )
    assert s.databricks_config_problem is None and s.databricks_enabled is True


def test_token_key_and_profile_are_retired():
    assert {"DATABRICKS_TOKEN_ENCRYPTION_KEY", "DATABRICKS_CONFIG_PROFILE"} <= set(
        RETIRED_DATABRICKS_SETTINGS
    )
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)  # no .env here
        try:
            env = {"DATABRICKS_TOKEN_ENCRYPTION_KEY": "k", "DATABRICKS_CONFIG_PROFILE": "work"}
            named = _with_env(env, retired_databricks_settings)
            assert named == ("DATABRICKS_TOKEN_ENCRYPTION_KEY", "DATABRICKS_CONFIG_PROFILE"), named
        finally:
            os.chdir(cwd)


def test_registry_settings_and_bounds():
    s = _plain(
        DATABRICKS_WORKSPACES_SEED_FILE="/etc/agent/workspaces.json",
        DATABRICKS_REGISTRY_POLL_SECONDS="5",
        DATABRICKS_LEGACY_WORKSPACE="claims",
    )
    assert s.databricks_workspaces_seed_file == "/etc/agent/workspaces.json"
    assert s.databricks_registry_poll_seconds == 5
    assert s.databricks_legacy_workspace == "claims"
    for key in (
        "DATABRICKS_REGISTRY_POLL_SECONDS",
        "DATABRICKS_SQL_MAX_ROWS_TO_MODEL",
        "DATABRICKS_SQL_MAX_ROWS_TO_UI",
    ):
        for bad in ("0", "-1"):
            try:
                _plain(**{key: bad})
            except ValidationError as exc:
                assert key in str(exc), exc
            else:
                raise AssertionError(f"{key}={bad} was accepted")
        assert _plain(**{key: "1"}) is not None


def test_retired_settings_are_named():
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)  # no .env here yet
        try:
            named = _with_env(
                {"DATABRICKS_HOST": "https://x.azuredatabricks.net", "DATABRICKS_WORKSPACES": "{}"},
                retired_databricks_settings,
            )
            assert named == ("DATABRICKS_WORKSPACES", "DATABRICKS_HOST"), named
            blank = {"DATABRICKS_HOST": "  ", "DATABRICKS_JOBS_ENABLED": ""}
            assert _with_env(blank, retired_databricks_settings) == ()
            assert _with_env({}, retired_databricks_settings) == ()
            # The .env file counts too, in the same order; blank there is not named either.
            with open(".env", "w", encoding="utf-8") as env_file:
                env_file.write("DATABRICKS_JOBS_ENABLED=false\nDATABRICKS_DATA_ALLOWLIST=\n")
            named = _with_env({"DATABRICKS_SQL_WAREHOUSE_ID": "w"}, retired_databricks_settings)
            assert named == ("DATABRICKS_SQL_WAREHOUSE_ID", "DATABRICKS_JOBS_ENABLED"), named
        finally:
            os.chdir(cwd)


def test_registration_is_decided_once():
    """Roster, routing, gate and sign-in all read one cached decision."""

    reads = []
    original = Settings.databricks_enabled
    Settings.databricks_enabled = property(lambda _self: reads.append(1) or True)
    config.databricks_registered.cache_clear()
    try:
        assert config.databricks_registered() is True
        assert config.databricks_registered() is True
        assert len(reads) == 1, reads
    finally:
        Settings.databricks_enabled = original
        config.databricks_registered.cache_clear()


def test_databricks_local_dev():
    cases = [
        ({"AGENT_AUTH_MODE": " Dev ", "APP_ENV": "local"}, True),
        ({"AGENT_AUTH_MODE": "mock", "APP_ENV": "local"}, True),
        ({"AGENT_AUTH_MODE": "LOCAL", "APP_ENV": "local"}, True),
        ({"AGENT_AUTH_MODE": "jwt", "APP_ENV": "local"}, False),
        ({"AGENT_AUTH_MODE": "dev", "APP_ENV": "dev"}, False),
        ({"AGENT_AUTH_MODE": "dev"}, False),
        ({}, False),
    ]
    for env, expected in cases:
        assert _with_env(env, config.databricks_local_dev) is expected, env


def test_admin_object_ids_ignore_names():
    entries = ["Databricks Admins", f" {ADMIN_ID.upper()} ", "", " ", "not-a-guid-1234"]
    assert admin_object_ids(entries) == frozenset({ADMIN_ID})
    assert ignored_admin_entries(entries) == 2
    assert admin_object_ids([]) == frozenset() and ignored_admin_entries([]) == 0


@contextlib.contextmanager
def _registered(registered: bool = True):
    """``databricks_registered`` forced for the block; restored after."""

    original = config.databricks_registered
    config.databricks_registered = lambda: registered
    try:
        yield
    finally:
        config.databricks_registered = original


def test_databricks_group_keys():
    """Admin object IDs (lowercased; a display name grants nothing) and every workspace's
    groups except ``"*"``; none while the databricks-agent is off."""

    get_snapshot().replace((_ws("a", "CLAIMS", "gid-1"), _ws("b", "*")))
    env = {"DATABRICKS_ADMIN_GROUPS": f"ADM-NAME,{ADMIN_ID.upper()}"}
    try:
        with _registered():
            keys = _with_env(env, _fresh(dw.databricks_group_keys))
        with _registered(False):
            off = _with_env(env, _fresh(dw.databricks_group_keys))
    finally:
        get_snapshot().replace(())
    assert {"CLAIMS", "gid-1", ADMIN_ID} == keys, keys
    assert off == frozenset(), off


def _fresh(read: Any) -> Any:
    """``read`` with the settings re-read from the env in place; the cache cleared after."""

    def build() -> Any:
        config.get_settings.cache_clear()
        try:
            return read()
        finally:
            config.get_settings.cache_clear()

    return build


def test_a_group_named_after_a_workspace_group_id_is_not_exported():
    """A Graph group whose display name is another group's object ID never passes for it:
    anyone can create one. Its real ID and an honest name still match."""

    from v1.utils.auth import _databricks_groups
    from v1.utils.graph_groups import GraphGroup

    spoof = GraphGroup(id="11111111-2222-4333-8444-555555555555", display_name=ADMIN_ID.upper())
    named = GraphGroup(id="66666666-7777-4888-8999-000000000000", display_name="CLAIMS")
    get_snapshot().replace((_ws("a", "CLAIMS", ADMIN_ID),))
    try:
        with _registered():
            assert _databricks_groups([], [spoof]) == set()
            assert _databricks_groups([], [named]) == {"CLAIMS"}
            assert _databricks_groups([ADMIN_ID.upper()]) == {ADMIN_ID}
    finally:
        get_snapshot().replace(())


def test_new_workspace_group_is_exported_after_refresh():
    """Sign-in's group filter follows the live snapshot: a workspace added after startup
    exports its group without a restart (the Settings object is the cached one)."""

    from v1.utils.auth import _mapped_groups

    get_snapshot().replace(())
    try:
        with _registered():
            assert "NEW-GRP" not in dw.databricks_group_keys()
            assert _mapped_groups(["NEW-GRP", "UNMAPPED"]) == ()
            get_snapshot().replace((_ws("new", "NEW-GRP"),))
            assert "NEW-GRP" in dw.databricks_group_keys()
            # Matched case-insensitively, exported in the workspace's form.
            assert _mapped_groups(["new-grp", "UNMAPPED"]) == ("NEW-GRP",)
    finally:
        get_snapshot().replace(())


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
