"""Regression tests for the Graph group cache (PROD_DEPLOYMENT_TODO §3).

GRAPHGROUPS-LOCK: the global lock must not be held across the blocking Graph
fetch (so distinct users don't serialize), and the cache must be bounded
(LRU cap + TTL sweep) so it can't grow without limit.

Runs standalone (``python test_graph_groups.py``) or under pytest.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import urllib.error

import v1.utils.graph_groups as gg


def _jwt_with_claims(claims: dict) -> str:
    """Build an unsigned JWT-shaped token whose payload carries ``claims``."""

    def seg(obj) -> str:
        raw = json.dumps(obj).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return f"{seg({'alg': 'none'})}.{seg(claims)}.sig"


def _patch_fetch(fn):
    """Swap _fetch_groups, returning a restore callable; also clears the cache."""

    original = gg._fetch_groups
    gg._fetch_groups = fn
    gg._cache.clear()

    def restore() -> None:
        gg._fetch_groups = original
        gg._cache.clear()

    return restore


def test_distinct_oids_do_not_serialize_on_the_fetch() -> None:
    # Both threads must be inside _fetch_groups at the same time. If the lock were
    # held across the fetch (the bug), the second thread could not enter until the
    # first returned and the barrier would time out -> empty results.
    barrier = threading.Barrier(2, timeout=3)

    def fake_fetch(oid, timeout):
        barrier.wait()
        return (gg.GraphGroup(id=f"g-{oid}", display_name=None),)

    restore = _patch_fetch(fake_fetch)
    try:
        results: dict[str, tuple] = {}

        def run(oid: str) -> None:
            results[oid] = gg.resolve_groups_via_graph(oid)

        threads = [threading.Thread(target=run, args=(o,)) for o in ("oid-A", "oid-B")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)

        assert all(not thread.is_alive() for thread in threads), "fetch serialized (deadlock)"
        assert results["oid-A"][0].id == "g-oid-A"
        assert results["oid-B"][0].id == "g-oid-B"
    finally:
        restore()


def test_second_call_for_same_oid_is_cached() -> None:
    calls = {"n": 0}

    def fake_fetch(oid, timeout):
        calls["n"] += 1
        return (gg.GraphGroup(id="g1", display_name="G1"),)

    restore = _patch_fetch(fake_fetch)
    try:
        first = gg.resolve_groups_via_graph("oid-x")
        second = gg.resolve_groups_via_graph("oid-x")
        assert calls["n"] == 1  # second served from cache
        assert first == second
    finally:
        restore()


def test_lru_cap_evicts_least_recently_used() -> None:
    prev = os.environ.get("GRAPH_GROUPS_CACHE_MAX_ENTRIES")
    os.environ["GRAPH_GROUPS_CACHE_MAX_ENTRIES"] = "2"

    def fake_fetch(oid, timeout):
        return (gg.GraphGroup(id=f"g-{oid}", display_name=None),)

    restore = _patch_fetch(fake_fetch)
    try:
        gg.resolve_groups_via_graph("a")
        gg.resolve_groups_via_graph("b")
        gg.resolve_groups_via_graph("c")  # pushes the cache over the cap of 2
        assert len(gg._cache) == 2
        assert "a" not in gg._cache  # oldest evicted
        assert "b" in gg._cache and "c" in gg._cache
    finally:
        restore()
        if prev is None:
            os.environ.pop("GRAPH_GROUPS_CACHE_MAX_ENTRIES", None)
        else:
            os.environ["GRAPH_GROUPS_CACHE_MAX_ENTRIES"] = prev


def test_expired_entries_are_refetched_and_swept() -> None:
    prev = os.environ.get("GRAPH_GROUPS_CACHE_SECONDS")
    os.environ["GRAPH_GROUPS_CACHE_SECONDS"] = "0"  # entries expire immediately
    calls = {"n": 0}

    def fake_fetch(oid, timeout):
        calls["n"] += 1
        return (gg.GraphGroup(id="g1", display_name=None),)

    restore = _patch_fetch(fake_fetch)
    try:
        gg.resolve_groups_via_graph("oid-y")  # fetch #1, stored already-expired
        gg.resolve_groups_via_graph("oid-y")  # expired -> fetch #2
        assert calls["n"] == 2

        # A fresh write sweeps other expired entries: oid-y (expired) is gone
        # after a normal-TTL write for a different oid.
        os.environ["GRAPH_GROUPS_CACHE_SECONDS"] = "900"
        gg.resolve_groups_via_graph("oid-z")
        assert "oid-y" not in gg._cache
        assert "oid-z" in gg._cache
    finally:
        restore()
        if prev is None:
            os.environ.pop("GRAPH_GROUPS_CACHE_SECONDS", None)
        else:
            os.environ["GRAPH_GROUPS_CACHE_SECONDS"] = prev


def test_decode_token_identity_extracts_app_only_diagnostics() -> None:
    # The app-only Graph token whose identity we want surfaced in container logs.
    token = _jwt_with_claims(
        {
            "idtyp": "app",
            "appid": "11111111-2222-3333-4444-555555555555",
            "app_displayname": "agent-backend",
            "aud": "https://graph.microsoft.com",
            "oid": "66666666-7777-8888-9999-000000000000",
            "roles": ["GroupMember.Read.All"],
            "iat": 123,  # noise that must not leak into the diagnostic dict
        }
    )
    identity = gg._decode_token_identity(token)
    assert identity == {
        "idtyp": "app",
        "appid": "11111111-2222-3333-4444-555555555555",
        "app_displayname": "agent-backend",
        "aud": "https://graph.microsoft.com",
        "oid": "66666666-7777-8888-9999-000000000000",
        "roles": ["GroupMember.Read.All"],
        "scp": None,
    }


def test_decode_token_identity_is_safe_on_garbage() -> None:
    # Opaque (non-JWT) tokens and malformed payloads must never raise.
    assert gg._decode_token_identity("not-a-jwt") == {}
    assert gg._decode_token_identity("a.!!!notbase64!!!.c") == {}


def test_http_error_returns_empty_best_effort() -> None:
    # A 403 (missing Graph app role) must degrade to empty groups, never raise.
    def fake_fetch(oid, timeout):
        raise urllib.error.HTTPError(
            url="https://graph", code=403, msg="Forbidden", hdrs=None, fp=None
        )

    restore = _patch_fetch(fake_fetch)
    try:
        assert gg.resolve_groups_via_graph("oid-403") == ()
        assert "oid-403" not in gg._cache  # failures are not cached
    finally:
        restore()


def test_generic_error_returns_empty_best_effort() -> None:
    def fake_fetch(oid, timeout):
        raise RuntimeError("no managed identity available")

    restore = _patch_fetch(fake_fetch)
    try:
        assert gg.resolve_groups_via_graph("oid-boom") == ()
        assert "oid-boom" not in gg._cache
    finally:
        restore()


def _set_obo_env(monkeypatch_env: dict) -> callable:
    """Set OBO env vars from a dict; return a restore callable."""

    saved = {key: os.environ.get(key) for key in monkeypatch_env}
    for key, value in monkeypatch_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    def restore() -> None:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    return restore


def test_obo_configured_requires_all_three_credentials() -> None:
    restore = _set_obo_env(
        {
            "AGENT_AUTH_GROUPS_OBO": "true",
            "ENTRA_TENANT_ID": "tenant-1",
            "ENTRA_CLIENT_ID": "client-1",
            "ENTRA_CLIENT_SECRET": "secret-1",
            "AZURE_KEY_VAULT_URI": "",  # force env fallback in resolve_env_secret
        }
    )
    try:
        assert gg.obo_configured() is True
    finally:
        restore()

    # Missing the secret => not configured (can't act as the user without a cred).
    restore = _set_obo_env(
        {
            "AGENT_AUTH_GROUPS_OBO": "true",
            "ENTRA_TENANT_ID": "tenant-1",
            "ENTRA_CLIENT_ID": "client-1",
            "ENTRA_CLIENT_SECRET": None,
        }
    )
    try:
        assert gg.obo_configured() is False
    finally:
        restore()

    # Feature flag off => not configured even with full creds.
    restore = _set_obo_env(
        {
            "AGENT_AUTH_GROUPS_OBO": "false",
            "ENTRA_TENANT_ID": "tenant-1",
            "ENTRA_CLIENT_ID": "client-1",
            "ENTRA_CLIENT_SECRET": "secret-1",
        }
    )
    try:
        assert gg.obo_configured() is False
    finally:
        restore()


def test_obo_path_used_when_assertion_and_config_present() -> None:
    seen: dict[str, str] = {}

    def fake_obo_fetch(user_assertion, timeout):
        seen["assertion"] = user_assertion
        return (gg.GraphGroup(id="g-obo", display_name="Via OBO"),)

    def fake_app_fetch(oid, timeout):  # should not be called
        seen["app"] = oid
        return (gg.GraphGroup(id="g-app", display_name=None),)

    obo_env = _set_obo_env(
        {
            "AGENT_AUTH_GROUPS_OBO": "true",
            "ENTRA_TENANT_ID": "tenant-1",
            "ENTRA_CLIENT_ID": "client-1",
            "ENTRA_CLIENT_SECRET": "secret-1",
            "AZURE_KEY_VAULT_URI": "",
        }
    )
    orig_obo, orig_app = gg._fetch_groups_obo, gg._fetch_groups
    gg._fetch_groups_obo = fake_obo_fetch
    gg._fetch_groups = fake_app_fetch
    gg._cache.clear()
    try:
        result = gg.resolve_groups_via_graph("oid-1", user_assertion="raw.user.token")
        assert result == (gg.GraphGroup(id="g-obo", display_name="Via OBO"),)
        assert seen == {"assertion": "raw.user.token"}  # app-only path untouched
    finally:
        gg._fetch_groups_obo, gg._fetch_groups = orig_obo, orig_app
        gg._cache.clear()
        obo_env()


def test_app_only_path_used_when_obo_not_configured() -> None:
    called = {"app": 0}

    def fake_app_fetch(oid, timeout):
        called["app"] += 1
        return (gg.GraphGroup(id="g-app", display_name=None),)

    obo_env = _set_obo_env({"ENTRA_CLIENT_SECRET": None})  # OBO unconfigured
    restore = _patch_fetch(fake_app_fetch)
    try:
        result = gg.resolve_groups_via_graph("oid-2", user_assertion="raw.user.token")
        assert result == (gg.GraphGroup(id="g-app", display_name=None),)
        assert called["app"] == 1  # fell back to app-only despite an assertion
    finally:
        restore()
        obo_env()


def _main() -> int:
    checks = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for check in checks:
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - standalone runner reports all
            failures += 1
            print(f"FAIL {check.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {check.__name__}")
    print(f"\n{len(checks) - failures}/{len(checks)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
