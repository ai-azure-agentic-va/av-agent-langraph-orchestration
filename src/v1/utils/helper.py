"""Security helpers for redacting credentials before state or logs see them."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from typing import Any

REDACTED = "[REDACTED]"
MAX_METADATA_DEPTH = 8
MAX_STRING_LENGTH = 2048
MAX_SEQUENCE_LENGTH = 50

_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "client_secret",
    "cookie",
    "credential",
    "id_token",
    "jwt",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "session",
    "token",
)

_JWT_RE = re.compile(r"(?P<jwt>eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+(?P<token>[A-Za-z0-9._~+/=-]{12,})")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?P<name>api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password)"
    r"\s*[:=]\s*(?P<secret>[^\s,;]+)"
)


def _split_csv(value: Any) -> Any:
    """Parse comma-separated env values into a list of strings for pydantic fields."""

    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


def truthy(value: str | None) -> bool:
    """Return True for the common truthy strings ('1', 'true', 'yes', 'on')."""

    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def env_bool(name: str, *, default: bool) -> bool:
    """Read a boolean env var, returning ``default`` when the var is unset."""

    value = os.getenv(name)
    if value is None:
        return default
    return truthy(value)


def env_float(name: str, *, default: float) -> float:
    """Read a float env var, returning ``default`` when unset or unparsable."""

    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def token_fingerprint(token: str | bytes | None, *, length: int = 16) -> str | None:
    """Return a stable non-secret fingerprint for correlating credential failures."""

    if token is None:
        return None
    if isinstance(token, str):
        token_bytes = token.encode("utf-8", "surrogatepass")
    else:
        token_bytes = token
    return hashlib.sha256(token_bytes).hexdigest()[:length]


def hash_identifier(value: str | bytes | None, *, length: int = 24) -> str | None:
    """Hash a subject-like identifier before it is placed in trace metadata."""

    if value is None:
        return None
    if isinstance(value, str):
        value_bytes = value.encode("utf-8", "surrogatepass")
    else:
        value_bytes = value
    return hashlib.sha256(value_bytes).hexdigest()[:length]


def is_sensitive_key(key: object) -> bool:
    """Return True when a mapping key usually carries a credential or secret."""

    normalized = str(key).lower().replace("-", "_")
    if "fingerprint" in normalized or normalized.endswith("_hash"):
        return False
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _scrub_credentials(value: str) -> str:
    """Replace bearer / JWT / `secret=…` patterns; no length cap applied."""

    def _replace_bearer(match: re.Match[str]) -> str:
        fingerprint = token_fingerprint(match.group("token"))
        return f"Bearer {REDACTED}:{fingerprint}"

    def _replace_jwt(match: re.Match[str]) -> str:
        fingerprint = token_fingerprint(match.group("jwt"))
        return f"{REDACTED}:jwt:{fingerprint}"

    scrubbed = _BEARER_RE.sub(_replace_bearer, value)
    scrubbed = _JWT_RE.sub(_replace_jwt, scrubbed)
    scrubbed = _SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group('name')}={REDACTED}", scrubbed
    )
    return scrubbed


def redact_text(value: str) -> str:
    """Redact bearer credentials, capped at MAX_STRING_LENGTH for logs/state."""

    redacted = _scrub_credentials(value)
    if len(redacted) > MAX_STRING_LENGTH:
        return redacted[:MAX_STRING_LENGTH] + "...[TRUNCATED]"
    return redacted


def sanitize_for_logging(value: Any, *, _depth: int = 0) -> Any:
    """Recursively make a value safe for logs, traces, and persisted state."""

    if _depth >= MAX_METADATA_DEPTH:
        return "[MAX_DEPTH]"

    if value is None or isinstance(value, bool | int | float):
        return value

    if isinstance(value, bytes):
        return f"{REDACTED}:bytes:{token_fingerprint(value)}"

    if isinstance(value, str):
        return redact_text(value)

    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            safe_key = redact_text(str(key))
            if is_sensitive_key(key):
                fingerprint = token_fingerprint(str(item)) if item is not None else None
                sanitized[safe_key] = f"{REDACTED}:{fingerprint}" if fingerprint else REDACTED
            else:
                sanitized[safe_key] = sanitize_for_logging(item, _depth=_depth + 1)
        return sanitized

    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        items = list(value[:MAX_SEQUENCE_LENGTH] if hasattr(value, "__getitem__") else value)
        sanitized_items = [sanitize_for_logging(item, _depth=_depth + 1) for item in items]
        if len(value) > MAX_SEQUENCE_LENGTH:
            sanitized_items.append("[TRUNCATED]")
        return sanitized_items

    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return redact_text(repr(value))
    return value
