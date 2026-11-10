"""Tests for the app-identity Databricks ``WorkspaceClient`` factory and caller helpers.

``get_azure_credential``, ``get_config``, ``get_settings`` and ``_new_workspace_client``
are replaced through the module-level names in ``v1.utils.databricks_client``. No network.

Runs standalone (``python test_databricks_client.py``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

from azure.core.exceptions import ClientAuthenticationError
from databricks_test_support import captured_logs, platform_user, run_tests

import v1.utils.databricks_client as dc

SCOPE = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/.default"
HOST = "https://adb-1.azuredatabricks.net"
_TIMEOUTS = {"http_timeout_seconds": 50, "retry_timeout_seconds": 50}
_ALL_BUILT: list[dict] = []


# -- fakes ---------------------------------------------------------------------


class FakeCredential:
    """Stands in for the shared ``DefaultAzureCredential``; records each scope asked for."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.scopes: list[str] = []
        self.calls = 0

    def get_token(self, *scopes: str) -> SimpleNamespace:
        self.calls += 1
        self.scopes.extend(scopes)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(token="app-token", expires_on=0)


def _settings(**overrides: object) -> SimpleNamespace:
    values = {"databricks_sql_timeout_seconds": 50, "databricks_jobs_timeout_seconds": 30}
    return SimpleNamespace(**{**values, **overrides})


@contextlib.contextmanager
def _installed(
    credential: FakeCredential | None = None, settings: object = None
) -> Iterator[tuple[list[dict], FakeCredential]]:
    """Patch the credential, settings and client hooks; yield (built kwargs, credential)."""

    built: list[dict] = []
    cred = credential or FakeCredential()
    names = ("get_azure_credential", "get_settings", "_new_workspace_client")
    saved = {name: getattr(dc, name) for name in names}
    dc.get_azure_credential = lambda: cred
    dc.get_settings = lambda: settings or _settings()
    dc._new_workspace_client = lambda **kw: built.append(kw) or SimpleNamespace(kwargs=kw)
    try:
        yield built, cred
    finally:
        _ALL_BUILT.extend(built)
        for name, value in saved.items():
            setattr(dc, name, value)


def _build(host: str, *, credential: FakeCredential | None = None, settings: object = None) -> dict:
    """Run ``app_workspace_client(host)``; return the ``Config`` kwargs it built."""

    with _installed(credential, settings) as (built, _):
        client = asyncio.run(dc.app_workspace_client(host))
    assert len(built) == 1 and client.kwargs == built[0], built
    return built[0]


@contextlib.contextmanager
def _run_user(user: Any = None, *, outside_run: bool = False) -> Iterator[None]:
    """Make ``dc.get_config`` return a run config carrying ``user``."""

    def fake_get_config() -> dict:
        if outside_run:
            raise RuntimeError("Called get_config outside of a runnable context")
        return {"configurable": {"langgraph_auth_user": user}}

    saved = dc.get_config
    dc.get_config = fake_get_config
    try:
        yield
    finally:
        dc.get_config = saved


def _refused(call: Any) -> None:
    try:
        call()
    except dc.DatabricksAccessError:
        return
    raise AssertionError("expected DatabricksAccessError")


# -- client --------------------------------------------------------------------


def test_scope_is_the_azure_databricks_app() -> None:
    assert dc.DATABRICKS_SCOPE == SCOPE


def test_client_uses_app_token_and_timeouts() -> None:
    credential = FakeCredential()
    kwargs = _build(HOST, credential=credential)
    assert kwargs == {"host": HOST, "token": "app-token", "auth_type": "pat", **_TIMEOUTS}
    assert credential.scopes == [SCOPE]


def test_host_is_stripped() -> None:
    assert _build(f"  {HOST} ")["host"] == HOST


def test_sdk_timeouts_follow_the_longest_tool_timeout() -> None:
    settings = _settings(databricks_sql_timeout_seconds=20, databricks_jobs_timeout_seconds=45.5)
    kwargs = _build(HOST, settings=settings)
    assert kwargs["http_timeout_seconds"] == 46 and kwargs["retry_timeout_seconds"] == 46, kwargs


def test_each_client_asks_for_a_token() -> None:
    credential = FakeCredential()
    with _installed(credential) as (built, _):
        asyncio.run(dc.app_workspace_client(HOST))
        asyncio.run(dc.app_workspace_client(HOST))
    assert credential.calls == 2 and len(built) == 2


def test_token_is_fetched_off_the_event_loop() -> None:
    on_loop: list[bool] = []

    class LoopCheckingCredential(FakeCredential):
        def get_token(self, *scopes: str) -> SimpleNamespace:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                on_loop.append(False)
            else:
                on_loop.append(True)
            return super().get_token(*scopes)

    _build(HOST, credential=LoopCheckingCredential())
    assert on_loop == [False], on_loop


def test_credential_failure_propagates() -> None:
    credential = FakeCredential(error=ClientAuthenticationError("no az login"))
    with _installed(credential) as (built, _):
        try:
            asyncio.run(dc.app_workspace_client(HOST))
        except ClientAuthenticationError:
            pass
        else:
            raise AssertionError("expected ClientAuthenticationError")
    assert built == []


def test_blank_host_refused() -> None:
    for host in ("", "  ", None):
        credential = FakeCredential()
        with _installed(credential) as (built, _):
            try:
                asyncio.run(dc.app_workspace_client(host))  # type: ignore[arg-type]
            except ValueError:
                pass
            else:
                raise AssertionError(f"expected ValueError for {host!r}")
        assert built == [] and credential.calls == 0, host


def test_timeouts_reach_the_sdk_config() -> None:
    """The SDK takes the timeouts on ``Config``, which ``WorkspaceClient`` is built from."""

    from databricks.sdk.config import Config

    assert {"http_timeout_seconds", "retry_timeout_seconds"} <= {a.name for a in Config.attributes()}
    seen: dict = {}
    saved = (dc.Config, dc.WorkspaceClient)
    dc.Config = lambda **kw: seen.setdefault("config", kw) and "cfg"
    dc.WorkspaceClient = lambda **kw: seen.setdefault("client", kw)
    try:
        dc._new_workspace_client(host=HOST, token="t", auth_type="pat", **_TIMEOUTS)
    finally:
        dc.Config, dc.WorkspaceClient = saved
    assert seen["config"] == {"host": HOST, "token": "t", "auth_type": "pat", **_TIMEOUTS}
    assert seen["client"] == {"config": "cfg"}


# -- caller --------------------------------------------------------------------


def test_run_caller_reads_identity_and_oid() -> None:
    user = {"identity": "user:abc", "object_id": "oid-1"}
    for shape in (user, SimpleNamespace(**user)):
        with _run_user(shape):
            assert dc.run_caller() == dc.Caller("user:abc", "oid-1")
    with _run_user({"identity": "user:abc"}):
        assert dc.run_caller() == dc.Caller("user:abc", None)
    with _run_user({"identity": "user:abc", "object_id": "  "}):
        assert dc.run_caller() == dc.Caller("user:abc", None)


def test_run_caller_through_the_platform_user_proxy() -> None:
    """Through the platform's own normalization (langgraph_api ProxyUser), live and serialized."""

    from v1.utils.auth import AuthenticatedPrincipal

    principal = AuthenticatedPrincipal(subject="sub-a", auth_mode="jwt", object_id="oid-a")
    proxy = platform_user(principal.to_langgraph_user())
    for user in (proxy, proxy.model_dump()):
        with _run_user(user):
            assert dc.run_caller() == dc.Caller(principal.identity, "oid-a")
    proxy = platform_user(identity="user:b", object_id="oid-b")
    with _run_user(proxy):
        assert dc.run_caller() == dc.Caller("user:b", "oid-b")


def test_run_caller_without_identity_refused() -> None:
    with _run_user(outside_run=True):
        _refused(dc.run_caller)
    for user in (None, {}, {"object_id": "oid-1"}, {"identity": "  "}, {"identity": 7}):
        with _run_user(user):
            _refused(dc.run_caller)


def test_request_caller_reads_mapping_and_attributes() -> None:
    user = {"identity": "user:abc", "object_id": "oid-1"}
    for shape in (user, SimpleNamespace(**user), platform_user(user)):
        assert dc.request_caller(shape) == dc.Caller("user:abc", "oid-1")
    assert dc.request_caller({"identity": "user:abc"}) == dc.Caller("user:abc", None)


def test_request_caller_without_identity_refused() -> None:
    class Exploding:
        def __getattr__(self, name: str) -> Any:
            raise RuntimeError("no auth backend")

    for user in (None, {}, SimpleNamespace(), {"identity": ""}, Exploding()):
        _refused(lambda user=user: dc.request_caller(user))


# -- payloads and audit ------------------------------------------------------------


def test_payloads() -> None:
    assert dc.SIGN_IN_MESSAGE == "Couldn't tell who you are. Sign in again and resend your message."
    assert json.loads(dc.access_error_payload()) == {"status": "error", "message": dc.SIGN_IN_MESSAGE}
    assert json.loads(dc.client_error_payload()) == {
        "status": "error",
        "message": dc.CLIENT_UNAVAILABLE_MESSAGE,
    }
    assert dc.CLIENT_UNAVAILABLE_MESSAGE == (
        "Databricks is unavailable right now. Try again in a few minutes."
    )
    assert str(dc.DatabricksAccessError()) == ""


def test_removed_names_are_gone() -> None:
    for name in (
        "workspace_client_for",
        "user_workspace_client",
        "_run_user",
        "resolve_token",
        "access_message",
    ):
        assert not hasattr(dc, name), name
    assert not [name for name in vars(dc) if name.startswith("ACCESS_")]


def test_audit_log_line() -> None:
    with captured_logs("v1.utils.databricks_client") as logs:
        dc.audit_log(dc.Caller("user:abc", None), workspace="dev", action="run_sql", detail="SELECT 1")
        dc.audit_log(dc.Caller("user:abc", "oid-1"), workspace="dev", action="list_jobs", detail="-")
    assert logs.at(logging.INFO) == [
        "databricks_call identity=user:abc oid=- workspace=dev action=run_sql detail=SELECT 1",
        "databricks_call identity=user:abc oid=oid-1 workspace=dev action=list_jobs detail=-",
    ], logs.messages


def test_audit_log_escapes_control_characters() -> None:
    """A detail (user- or model-steerable) can't start a forged line of its own."""

    caller = dc.Caller("user:abc", "oid-1")
    with captured_logs("v1.utils.databricks_client", fmt="%(message)s") as logs:
        forged = "\n databricks_call identity=forged"
        dc.audit_log(caller, workspace="dev", action="run_sql", detail=forged)
        dc.audit_log(
            caller,
            workspace="dev\r\nx",
            action="run_sql\x00",
            detail="SELECT 1\r\t\x1b[31m\x7f\x85\u2028",
        )
    assert len(logs.messages) == 2, logs.messages
    assert logs.getvalue().count("\n") == 2, logs.getvalue()
    assert logs.messages == [
        (
            "databricks_call identity=user:abc oid=oid-1 workspace=dev action=run_sql"
            " detail=\\n databricks_call identity=forged"
        ),
        (
            "databricks_call identity=user:abc oid=oid-1 workspace=dev\\r\\nx action=run_sql"
            "\\x00 detail=SELECT 1\\r\\t\\x1b[31m\\x7f\\x85\\u2028"
        ),
    ], logs.messages


def test_audit_log_caps_the_detail() -> None:
    with captured_logs("v1.utils.databricks_client") as logs:
        dc.audit_log(dc.Caller("user:abc", None), workspace="dev", action="a", detail="x" * 5000)
        dc.audit_log(dc.Caller("user:abc", None), workspace="dev", action="a", detail="y" * 2000)
    long, exact = (m.split(" detail=", 1)[1] for m in logs.messages)
    assert long == "x" * 2000 + "…", len(long)
    assert exact == "y" * 2000, len(exact)


def test_zz_every_client_used_the_app_token() -> None:
    """Every client any test built had the app's token and a host; nothing else."""

    assert _ALL_BUILT, "earlier tests should have built clients"
    for kwargs in _ALL_BUILT:
        assert set(kwargs) == {"host", "token", "auth_type", *_TIMEOUTS}, kwargs
        assert kwargs["token"] == "app-token" and kwargs["auth_type"] == "pat", kwargs
        assert isinstance(kwargs["host"], str) and kwargs["host"].strip(), kwargs


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
