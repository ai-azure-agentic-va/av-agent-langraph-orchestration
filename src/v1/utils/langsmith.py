"""Safe observability helpers for LangSmith and application logging."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, cast
from urllib.parse import urlparse

from v1.utils.helper import hash_identifier, sanitize_for_logging, truthy

_DEFAULT_LANGSMITH_ENDPOINT = "https://api.smith.langchain.com"

_LANGSMITH_SAFE_KEYS = {
    "auth_mode",
    "authenticated",
    "audience_hash",
    "issuer_hash",
    "permissions",
    "scopes",
    "subject_hash",
    "tenant_hash",
    "token_fingerprint",
}




def log_extra(event: str, **fields: Any) -> dict[str, Any]:
    """Build a sanitized ``extra=`` logging payload, auto-attaching the in-scope
    ``request_id`` and dropping ``None`` values."""

    payload: dict[str, Any] = {"event": event}
    request_id = fields.pop("request_id", None)
    if request_id:
        payload["request_id"] = request_id
    for key, value in fields.items():
        if value is None:
            continue
        payload[key] = value
    return cast(dict[str, Any], sanitize_for_logging(payload))


def safe_langsmith_metadata(
    *,
    principal: Any | None = None,
    request_metadata: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build run metadata without raw tokens, user ids, or request secrets."""

    metadata: dict[str, Any] = {}
    if request_metadata:
        metadata.update(sanitize_for_logging(request_metadata))

    if principal is not None:
        subject = getattr(principal, "subject", None)
        tenant = getattr(principal, "tenant", None)
        issuer = getattr(principal, "issuer", None)
        audience = getattr(principal, "audience", None)

        metadata.update(
            {
                "authenticated": bool(getattr(principal, "authenticated", True)),
                "auth_mode": getattr(principal, "auth_mode", None),
                "issuer_hash": hash_identifier(issuer),
                "audience_hash": hash_identifier(audience),
                "tenant_hash": hash_identifier(tenant),
                "subject_hash": hash_identifier(subject),
                "scopes": _sorted_strings(getattr(principal, "scopes", ())),
                "permissions": _sorted_strings(getattr(principal, "permissions", ())),
                "token_fingerprint": getattr(principal, "token_fingerprint", None),
            }
        )

    if extra:
        metadata.update(sanitize_for_logging(extra))

    return {
        key: value for key, value in sanitize_for_logging(metadata).items() if value is not None
    }


def public_auth_metadata(principal: Any | None) -> dict[str, Any]:
    """Return only auth fields that are intentionally safe for state exposure."""

    if principal is None:
        return {"authenticated": False}
    metadata = safe_langsmith_metadata(principal=principal)
    return {key: metadata[key] for key in _LANGSMITH_SAFE_KEYS if key in metadata}


def langsmith_status() -> dict[str, Any]:
    """Return safe LangSmith tracing configuration status for readiness/debug endpoints."""

    tracing_enabled = truthy(os.getenv("LANGSMITH_TRACING"))
    endpoint = (os.getenv("LANGSMITH_ENDPOINT") or _DEFAULT_LANGSMITH_ENDPOINT).strip()
    project = (os.getenv("LANGSMITH_PROJECT") or "agent-orchestration").strip()
    api_key_configured = bool((os.getenv("LANGSMITH_API_KEY") or "").strip())
    status = "disabled"
    detail = None
    if tracing_enabled:
        status = "configured" if api_key_configured else "error"
        if not api_key_configured:
            detail = "LANGSMITH_API_KEY is required when LANGSMITH_TRACING=true"
    return cast(
        dict[str, Any],
        sanitize_for_logging(
            {
                "status": status,
                "tracing_enabled": tracing_enabled,
                "endpoint_host": _endpoint_host(endpoint),
                "endpoint_mode": (
                    "default_saas"
                    if endpoint.rstrip("/") == _DEFAULT_LANGSMITH_ENDPOINT
                    else "custom"
                ),
                "project": project,
                "key_configured": api_key_configured,
                "detail": detail,
            }
        ),
    )


def _sorted_strings(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        return [values]
    try:
        return sorted({str(value) for value in values})
    except TypeError:
        return [str(values)]


def _endpoint_host(value: str) -> str:
    parsed = urlparse(value)
    if parsed.netloc:
        return parsed.netloc
    return value.split("/", 1)[0]
