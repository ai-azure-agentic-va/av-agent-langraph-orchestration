"""Managed-identity helpers for Azure OpenAI and Azure AI Search.

When ``AZURE_USE_MANAGED_IDENTITY=true`` the app authenticates with
``DefaultAzureCredential`` instead of static API keys. ``DefaultAzureCredential``
chains a managed identity (in Azure), environment service-principal vars, and the
developer's ``az login`` session (locally), so the same code path works in both
places.

- Azure OpenAI clients (chat + embeddings) take an ``azure_ad_token_provider``
  callable, built here via :func:`get_bearer_token_provider`.
- Azure AI Search ``SearchClient`` takes the ``TokenCredential`` directly.

The credential is a process-wide singleton: it caches tokens and refreshes them
in the background, so it must be built once rather than per request.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Awaitable, Callable

from azure.core.credentials import TokenCredential
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

_credential_lock = threading.Lock()
_credential: DefaultAzureCredential | None = None


def get_azure_credential() -> TokenCredential:
    """Return the process-wide ``DefaultAzureCredential`` singleton."""

    global _credential
    if _credential is not None:
        return _credential
    with _credential_lock:
        if _credential is None:
            _credential = DefaultAzureCredential()
    return _credential


def get_token_provider(scope: str) -> Callable[[], str]:
    """Return a bearer-token provider callable for ``scope``.

    The returned callable is what Azure OpenAI clients expect for
    ``azure_ad_token_provider``; it fetches and caches AAD tokens on demand.
    """

    return get_bearer_token_provider(get_azure_credential(), scope)


def get_async_token_provider(scope: str) -> Callable[[], Awaitable[str]]:
    """Return an async bearer-token provider for ``scope``.

    ``DefaultAzureCredential`` acquires tokens with blocking I/O (``AzureCliCredential``
    shells out / calls ``os.access``, ``ManagedIdentityCredential`` hits IMDS). When the
    async Azure OpenAI client invokes a *sync* token provider, that blocking call runs on
    the event loop and stalls it (and trips blocking-call detectors). Wrapping the sync
    provider in ``asyncio.to_thread`` moves the blocking acquisition to a worker thread.
    """

    provider = get_token_provider(scope)

    async def _async_provider() -> str:
        return await asyncio.to_thread(provider)

    return _async_provider


__all__ = [
    "get_azure_credential",
    "get_token_provider",
    "get_async_token_provider",
]
