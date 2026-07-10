"""Async retry helper for transient upstream HTTP failures.

The orchestration service fans out to several upstream HTTP services
(Azure AI Search, ServiceNow, Azure OpenAI). A single dropped TCP
connection or a brief 503 from any of them currently fails the parent
graph node, surfaces to the caller as a hard error, and bypasses the
synthesis fallback. This module wraps those outbound calls with a small,
deterministic retry policy so transient flakes don't bubble up.

What we retry:
  * :class:`httpx.TransportError` — DNS, connect timeout, read timeout,
    pool timeout, write timeout, connection reset.
  * :class:`httpx.HTTPStatusError` with status in ``{429, 502, 503, 504}``
    — rate limit and upstream-transient codes.
  * Azure OpenAI SDK transient exceptions: :class:`openai.APIConnectionError`,
    :class:`openai.APITimeoutError`, and :class:`openai.APIStatusError`
    instances whose ``status_code`` is in the same transient set.
  * Azure SDK (``azure-core``) transient exceptions:
    :class:`azure.core.exceptions.ServiceRequestError`,
    :class:`azure.core.exceptions.ServiceResponseError`, and
    :class:`azure.core.exceptions.HttpResponseError` instances whose
    ``status_code`` is in the same transient set.

What we do NOT retry:
  * Any other 4xx response (client errors are by definition not
    transient).
  * Any other exception type (timeouts are wrapped as TransportError;
    arbitrary exceptions are surfaced so bugs aren't masked).

Tuning: the max attempt count is read from
``AGENT_HTTP_RETRY_MAX_ATTEMPTS`` (default ``3``) at the point each
decorator is applied. Tests monkeypatch the env var before importing the
client to exercise the retry boundary deterministically.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

_RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})


# Lazy / optional imports — these SDKs are runtime deps but we want the retry
# module to be importable in lean test environments. Missing imports degrade
# to "no extra exception types matched", which is the safe behavior.
try:  # pragma: no cover - exercised when openai is installed.
    from openai import APIConnectionError as _OpenAIConnectionError
    from openai import APIStatusError as _OpenAIStatusError
    from openai import APITimeoutError as _OpenAITimeoutError
except Exception:  # pragma: no cover
    _OpenAIConnectionError = None  # type: ignore[assignment,misc]
    _OpenAIStatusError = None  # type: ignore[assignment,misc]
    _OpenAITimeoutError = None  # type: ignore[assignment,misc]

try:  # pragma: no cover - exercised when azure-core is installed.
    from azure.core.exceptions import HttpResponseError as _AzureHttpResponseError
    from azure.core.exceptions import ServiceRequestError as _AzureServiceRequestError
    from azure.core.exceptions import ServiceResponseError as _AzureServiceResponseError
except Exception:  # pragma: no cover
    _AzureHttpResponseError = None  # type: ignore[assignment,misc]
    _AzureServiceRequestError = None  # type: ignore[assignment,misc]
    _AzureServiceResponseError = None  # type: ignore[assignment,misc]


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS_CODES

    # Azure OpenAI SDK — transport-level failures (connect/timeout) are always
    # transient; APIStatusError carries a status_code we can filter on.
    if _OpenAIConnectionError is not None and isinstance(exc, _OpenAIConnectionError):
        return True
    if _OpenAITimeoutError is not None and isinstance(exc, _OpenAITimeoutError):
        return True
    if _OpenAIStatusError is not None and isinstance(exc, _OpenAIStatusError):
        status_code = getattr(exc, "status_code", None)
        return status_code in _RETRYABLE_STATUS_CODES

    # azure-core — ServiceRequestError/ServiceResponseError are transport
    # failures; HttpResponseError exposes status_code.
    if _AzureServiceRequestError is not None and isinstance(exc, _AzureServiceRequestError):
        return True
    if _AzureServiceResponseError is not None and isinstance(exc, _AzureServiceResponseError):
        return True
    if _AzureHttpResponseError is not None and isinstance(exc, _AzureHttpResponseError):
        status_code = getattr(exc, "status_code", None)
        return status_code in _RETRYABLE_STATUS_CODES

    return False


def _max_attempts() -> int:
    raw = os.getenv("AGENT_HTTP_RETRY_MAX_ATTEMPTS", "3") or "3"
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 3
    return max(1, value)


def _log_retry(state: RetryCallState) -> None:
    outcome = state.outcome
    if outcome is None or not outcome.failed:
        return
    exc = outcome.exception()
    status_code: int | None = None
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
    else:
        candidate = getattr(exc, "status_code", None)
        if isinstance(candidate, int):
            status_code = candidate
    logger.warning(
        "upstream.retry",
        extra={
            "event": "upstream.retry",
            "attempt": state.attempt_number,
            "error": type(exc).__name__ if exc is not None else None,
            "status_code": status_code,
            "function": state.fn.__name__ if state.fn else None,
        },
    )


def http_retry_async() -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Return a tenacity async retry decorator for outbound HTTP calls.

    The wrapped coroutine should perform a single httpx request and call
    ``raise_for_status()`` (or otherwise raise
    :class:`httpx.HTTPStatusError`) so the retry layer can see the status
    code and decide whether to give up.
    """

    return retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(_max_attempts()),
        wait=wait_exponential(multiplier=0.25, min=0.25, max=4.0),
        before_sleep=_log_retry,
        reraise=True,
    )
