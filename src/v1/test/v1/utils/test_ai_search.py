"""Regression tests for the AI Search request timeout (PROD_DEPLOYMENT_TODO §1).

AISEARCH-3: the cached ``SearchClient`` must be built with an explicit
connect/read timeout so a hung upstream cannot pin its ``to_thread`` worker for
the azure-core default of 300s.

Runs standalone (``python test_ai_search.py``) or under pytest.
"""

from __future__ import annotations

import os

from azure.core.credentials import AzureKeyCredential

import v1.core.tools.ai_search.ai_search as ais
from v1.core.config import Settings


def test_timeout_config_default_and_override() -> None:
    prev = os.environ.pop("AZURE_SEARCH_TIMEOUT_SECONDS", None)
    try:
        assert Settings(_env_file=None).azure_search_timeout_seconds == 30.0
        os.environ["AZURE_SEARCH_TIMEOUT_SECONDS"] = "5"
        assert Settings(_env_file=None).azure_search_timeout_seconds == 5.0
    finally:
        os.environ.pop("AZURE_SEARCH_TIMEOUT_SECONDS", None)
        if prev is not None:
            os.environ["AZURE_SEARCH_TIMEOUT_SECONDS"] = prev


def test_search_client_built_with_timeout() -> None:
    captured: dict = {}

    class FakeSearchClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def close(self):
            pass

    orig_client = ais.SearchClient
    orig_cred = ais._search_credential
    orig_cache = dict(ais._search_clients)
    orig_timeout = ais.settings.azure_search_timeout_seconds
    orig_endpoint = ais.settings.azure_search_endpoint
    try:
        ais.SearchClient = FakeSearchClient
        ais._search_credential = lambda: AzureKeyCredential("fake")
        ais._search_clients.clear()
        ais.settings.azure_search_timeout_seconds = 12.5
        ais.settings.azure_search_endpoint = "https://example.search.windows.net"

        client = ais._get_search_client("idx-1")

        assert isinstance(client, FakeSearchClient)
        assert captured["index_name"] == "idx-1"
        assert captured["connection_timeout"] == 12.5
        assert captured["read_timeout"] == 12.5
        # Cached: a second call reuses the client and does not rebuild.
        captured.clear()
        assert ais._get_search_client("idx-1") is client
        assert captured == {}
    finally:
        ais.SearchClient = orig_client
        ais._search_credential = orig_cred
        ais._search_clients.clear()
        ais._search_clients.update(orig_cache)
        ais.settings.azure_search_timeout_seconds = orig_timeout
        ais.settings.azure_search_endpoint = orig_endpoint


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
