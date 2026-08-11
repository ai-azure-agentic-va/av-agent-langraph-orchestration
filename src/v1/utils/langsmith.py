"""Safe observability helpers for LangSmith and application logging."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from v1.utils.helper import hash_identifier, sanitize_for_logging

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


def _sorted_strings(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        return [values]
    try:
        return sorted({str(value) for value in values})
    except TypeError:
        return [str(values)]
