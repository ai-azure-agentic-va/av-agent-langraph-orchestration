"""Azure Key Vault secret resolver with a 5-minute in-memory cache.

Use ``resolve_env_secret("ENV_NAME")``: it reads ``ENV_NAME_SECRET_NAME`` (or the
dash-cased ``ENV_NAME``) from Key Vault when ``AZURE_KEY_VAULT_URI`` is set, and
otherwise falls back to the local env value. Auth uses ``DefaultAzureCredential``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

try:  # pragma: no cover - exercised only when the optional packages exist.
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient
except Exception:  # pragma: no cover - keep import safe on minimal installs.
    DefaultAzureCredential = None  # type: ignore[assignment,misc]
    SecretClient = None  # type: ignore[assignment,misc]

try:  # pragma: no cover - exercised only when the optional packages exist.
    from azure.identity.aio import DefaultAzureCredential as AsyncDefaultAzureCredential
    from azure.keyvault.secrets.aio import SecretClient as AsyncSecretClient
except Exception:  # pragma: no cover - keep import safe on minimal installs.
    AsyncDefaultAzureCredential = None  # type: ignore[assignment,misc]
    AsyncSecretClient = None  # type: ignore[assignment,misc]


logger = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 300


@dataclass(frozen=True)
class _CacheEntry:
    value: str | None
    expires_at: float


class KeyVault:
    """Tiny Key Vault facade with caching.

    The class is safe to instantiate even when ``azure-keyvault-secrets`` is not
    installed: every ``get`` then short-circuits to ``default``.
    """

    def __init__(
        self,
        vault_uri: str | None = None,
        *,
        credential: Any | None = None,
        client: Any | None = None,
        async_credential: Any | None = None,
        async_client: Any | None = None,
        cache_ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> None:
        if vault_uri is None:
            vault_uri = os.getenv("AZURE_KEY_VAULT_URI") or ""
        self._vault_uri = vault_uri
        self._cache_ttl_seconds = max(0, cache_ttl_seconds)
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()
        self._credential = credential
        self._client = client
        self._async_credential = async_credential
        self._async_client = async_client

    @property
    def enabled(self) -> bool:
        """True when a vault URI is configured and the SDK is importable."""

        return bool(self._vault_uri) and (self._client is not None or SecretClient is not None)

    @property
    def async_enabled(self) -> bool:
        """True when a vault URI is configured and the async SDK is importable."""

        return bool(self._vault_uri) and (
            self._async_client is not None or AsyncSecretClient is not None
        )

    def get(self, name: str, default: str | None = None) -> str | None:
        """Return the secret value for ``name`` or ``default`` if unavailable."""

        if not self.enabled:
            return default

        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(name)
            if cached is not None and cached.expires_at > now:
                return cached.value if cached.value is not None else default

        try:
            value = self._fetch(name)
        except Exception as exc:  # pragma: no cover - network path.
            logger.warning("Key Vault secret fetch failed for %s: %s", name, exc)
            return default

        expires_at = time.monotonic() + self._cache_ttl_seconds
        with self._lock:
            self._cache[name] = _CacheEntry(value=value, expires_at=expires_at)
        return value if value is not None else default

    async def aget(self, name: str, default: str | None = None) -> str | None:
        """Async sibling of :meth:`get` — never blocks the event loop.

        Uses the async Key Vault SDK when available; otherwise it offloads the
        synchronous fetch to a worker thread. The in-memory cache is shared with
        :meth:`get`, so a value resolved on either path serves the other.
        """

        if not self.async_enabled:
            # No async SDK: run the (possibly blocking) sync path off-loop so the
            # event loop keeps serving other requests during the network call.
            return await asyncio.to_thread(self.get, name, default)

        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(name)
            if cached is not None and cached.expires_at > now:
                return cached.value if cached.value is not None else default

        try:
            value = await self._afetch(name)
        except Exception as exc:  # pragma: no cover - network path.
            logger.warning("Key Vault secret fetch failed for %s: %s", name, exc)
            return default

        expires_at = time.monotonic() + self._cache_ttl_seconds
        with self._lock:
            self._cache[name] = _CacheEntry(value=value, expires_at=expires_at)
        return value if value is not None else default

    def invalidate(self, name: str | None = None) -> None:
        """Drop one or all cached entries (handy for tests / hot reloads)."""

        with self._lock:
            if name is None:
                self._cache.clear()
            else:
                self._cache.pop(name, None)

    # -- internals -----------------------------------------------------------

    def _fetch(self, name: str) -> str | None:
        client = self._get_client()
        secret = client.get_secret(name)
        value = getattr(secret, "value", None)
        return str(value) if value is not None else None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if SecretClient is None or DefaultAzureCredential is None:  # pragma: no cover
            raise RuntimeError("azure-keyvault-secrets / azure-identity are not installed")
        if self._credential is None:
            self._credential = DefaultAzureCredential()
        self._client = SecretClient(vault_url=self._vault_uri, credential=self._credential)
        return self._client

    async def _afetch(self, name: str) -> str | None:
        client = self._aget_client()
        secret = await client.get_secret(name)
        value = getattr(secret, "value", None)
        return str(value) if value is not None else None

    def _aget_client(self) -> Any:
        if self._async_client is not None:
            return self._async_client
        if AsyncSecretClient is None or AsyncDefaultAzureCredential is None:  # pragma: no cover
            raise RuntimeError(
                "azure-keyvault-secrets / azure-identity (aio) are not installed"
            )
        # Build the credential + client once; guard with the lock so concurrent
        # first-time callers don't construct (and leak) duplicate clients.
        with self._lock:
            if self._async_client is None:
                if self._async_credential is None:
                    self._async_credential = AsyncDefaultAzureCredential()
                self._async_client = AsyncSecretClient(
                    vault_url=self._vault_uri, credential=self._async_credential
                )
        return self._async_client

    async def aclose(self) -> None:
        """Close the async client + credential (idempotent)."""

        client, credential = self._async_client, self._async_credential
        self._async_client = None
        self._async_credential = None
        if client is not None:
            await client.close()
        if credential is not None:
            await credential.close()


def default_secret_name(env_name: str) -> str:
    """Return the default Key Vault secret name for an environment variable."""

    return env_name.strip().lower().replace("_", "-")


def secret_name_for_env(env_name: str) -> str:
    """Return explicit or conventional Key Vault secret name for ``env_name``."""

    override = os.getenv(f"{env_name}_SECRET_NAME")
    if override and override.strip():
        return override.strip()
    return default_secret_name(env_name)


# Module-level KeyVault singleton so DefaultAzureCredential + SecretClient
# are built once per process instead of once per secret read.
_default_kv_lock = threading.Lock()
_default_kv: KeyVault | None = None


def _get_default_kv() -> KeyVault:
    global _default_kv
    if _default_kv is not None:
        return _default_kv
    with _default_kv_lock:
        if _default_kv is None:
            _default_kv = KeyVault()
    return _default_kv


def resolve_env_secret(
    env_name: str,
    default: str | None = None,
    *,
    vault: KeyVault | None = None,
) -> str | None:
    """Resolve a secret from Key Vault, falling back to local env/default.

    Local env fallback keeps tests and local development lightweight. In
    production, set ``AZURE_KEY_VAULT_URI`` and preferably ``*_SECRET_NAME``
    variables so the secret material never needs to live in app settings.
    """

    fallback = os.getenv(env_name)
    if fallback is None:
        fallback = default
    return (vault or _get_default_kv()).get(secret_name_for_env(env_name), default=fallback)


async def aresolve_env_secret(
    env_name: str,
    default: str | None = None,
    *,
    vault: KeyVault | None = None,
) -> str | None:
    """Async sibling of :func:`resolve_env_secret` — safe to call on the event loop.

    Resolves the secret via the async Key Vault SDK (falling back to the local
    env value / ``default``) without blocking the loop. Use this from coroutines;
    use :func:`resolve_env_secret` from synchronous code.
    """

    fallback = os.getenv(env_name)
    if fallback is None:
        fallback = default
    return await (vault or _get_default_kv()).aget(
        secret_name_for_env(env_name), default=fallback
    )


async def aclose_default_kv() -> None:
    """Close the process-wide KeyVault's async client/credential, if any."""

    if _default_kv is not None:
        await _default_kv.aclose()


__all__ = [
    "KeyVault",
    "aclose_default_kv",
    "aresolve_env_secret",
    "default_secret_name",
    "resolve_env_secret",
    "secret_name_for_env",
]
