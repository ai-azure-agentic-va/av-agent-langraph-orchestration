"""Tests for the async Azure Key Vault resolution path.

Validates that ``KeyVault.aget`` / ``aresolve_env_secret`` resolve secrets
without blocking the event loop: when the async SDK is available they use the
async client; otherwise they fall back to the synchronous fetch off-loop. The
in-memory cache is shared with the synchronous path.

Driven through ``asyncio.run`` so no pytest-asyncio plugin is required.
"""

from __future__ import annotations

import asyncio

from v1.utils.azure_key_vault import KeyVault, aresolve_env_secret


class _FakeSecret:
    def __init__(self, value: str) -> None:
        self.value = value


class _FakeAsyncClient:
    """Minimal stand-in for ``azure.keyvault.secrets.aio.SecretClient``."""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values
        self.calls: list[str] = []
        self.closed = False

    async def get_secret(self, name: str) -> _FakeSecret:
        self.calls.append(name)
        return _FakeSecret(self._values[name])

    async def close(self) -> None:
        self.closed = True


def test_aget_uses_async_client_and_caches() -> None:
    async def _run() -> None:
        client = _FakeAsyncClient({"my-secret": "from-vault"})
        vault = KeyVault("https://vault.example", async_client=client)

        assert vault.async_enabled is True
        # First read hits the async client; second is served from the cache.
        assert await vault.aget("my-secret") == "from-vault"
        assert await vault.aget("my-secret") == "from-vault"
        assert client.calls == ["my-secret"]

        # The cache is shared with the synchronous path.
        assert vault.get("my-secret") == "from-vault"
        assert client.calls == ["my-secret"]

    asyncio.run(_run())


def test_aget_returns_default_on_fetch_error() -> None:
    async def _run() -> None:
        class _Boom:
            async def get_secret(self, name: str):  # noqa: ANN001
                raise RuntimeError("kv down")

        vault = KeyVault("https://vault.example", async_client=_Boom())
        assert await vault.aget("missing", default="fallback") == "fallback"

    asyncio.run(_run())


def test_aget_falls_back_to_sync_when_async_sdk_absent() -> None:
    async def _run() -> None:
        # No vault URI configured -> async_enabled is False -> offloads the sync
        # ``get`` to a thread, which short-circuits to the default.
        vault = KeyVault("")
        assert vault.async_enabled is False
        assert await vault.aget("anything", default="local-value") == "local-value"

    asyncio.run(_run())


def test_aclose_closes_async_client() -> None:
    async def _run() -> None:
        client = _FakeAsyncClient({"s": "v"})
        vault = KeyVault("https://vault.example", async_client=client)
        await vault.aget("s")
        await vault.aclose()
        assert client.closed is True
        # Idempotent: a second close is a no-op.
        await vault.aclose()

    asyncio.run(_run())


def test_aresolve_env_secret_prefers_vault_over_env(monkeypatch) -> None:  # noqa: ANN001
    async def _run() -> None:
        monkeypatch.setenv("WIDGET_TOKEN", "env-value")
        client = _FakeAsyncClient({"widget-token": "vault-value"})
        vault = KeyVault("https://vault.example", async_client=client)
        # Default secret name is the dash-cased env var name.
        assert await aresolve_env_secret("WIDGET_TOKEN", vault=vault) == "vault-value"

    asyncio.run(_run())


def test_aresolve_env_secret_falls_back_to_env(monkeypatch) -> None:  # noqa: ANN001
    async def _run() -> None:
        monkeypatch.setenv("WIDGET_TOKEN", "env-value")
        vault = KeyVault("")  # disabled -> use the local env value
        assert await aresolve_env_secret("WIDGET_TOKEN", vault=vault) == "env-value"

    asyncio.run(_run())


if __name__ == "__main__":
    test_aget_uses_async_client_and_caches()
    test_aget_returns_default_on_fetch_error()
    test_aget_falls_back_to_sync_when_async_sdk_absent()
    test_aclose_closes_async_client()
    print("ok")
