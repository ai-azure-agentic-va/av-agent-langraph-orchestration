"""Resolve a user's Entra group memberships via Microsoft Graph.

Fallback for when an access token does not carry a ``groups`` claim — e.g. the
app registration doesn't emit groups, or Entra returned a group "overage" for a
user in too many groups. Two acquisition paths, chosen per call:

- **On-behalf-of (OBO), preferred** — when a confidential-client credential is
  configured (``ENTRA_CLIENT_ID`` + ``ENTRA_CLIENT_SECRET`` + tenant) and the
  caller's raw access token is supplied, we exchange that token for a *delegated*
  Graph token and call ``/me/transitiveMemberOf``. The BE then acts **as the
  user**, so the identity is the same locally and in a container — sidestepping
  the managed-identity trap entirely. Requires the **delegated** permission
  ``GroupMember.Read.All`` (admin-consented) on the app registration. Because the
  FE and BE share one app registration, the incoming token's audience already
  matches, so only the delegated grant + a client secret/cert are needed.

- **App-only fallback** — when no user token / OBO config is available, we call
  ``/users/{oid}/transitiveMemberOf`` with the *application's own* credential
  (``DefaultAzureCredential``). Requires the **application** permission
  ``GroupMember.Read.All`` (admin-consented) on the app/managed identity.

``transitiveMemberOf`` (vs ``getMemberGroups``) returns full group objects, so we
can ``$select`` both the object-id and the ``displayName`` in a single call —
giving callers GUIDs *and* human-readable names to match against.

Results are cached per user object-id (``oid``) for ``GRAPH_GROUPS_CACHE_SECONDS``
(default 900s / 15 min) to keep the Graph round-trip off the request hot path.
Every failure path is non-fatal: callers get ``()`` and must not fail auth.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from typing import Any, NamedTuple

from v1.utils.azure_credentials import get_token_provider
from v1.utils.azure_key_vault import resolve_env_secret

logger = logging.getLogger(__name__)

_GRAPH_SCOPE = "https://graph.microsoft.com/.default"
# transitiveMemberOf with a /microsoft.graph.group type cast filters directory
# objects down to groups; $select keeps the payload to id + displayName.
_GRAPH_USERS_ENDPOINT = (
    "https://graph.microsoft.com/v1.0/users/{oid}/transitiveMemberOf/"
    "microsoft.graph.group?$select=id,displayName&$top=999"
)
# /me variant for the OBO (delegated) path — there is no oid in the URL because
# the Graph token itself identifies the user.
_GRAPH_ME_ENDPOINT = (
    "https://graph.microsoft.com/v1.0/me/transitiveMemberOf/"
    "microsoft.graph.group?$select=id,displayName&$top=999"
)
_OBO_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"


class GraphGroup(NamedTuple):
    """A resolved Entra group: stable object-id plus optional display name."""

    id: str
    display_name: str | None


# (expires_at, groups) per oid, ordered for LRU eviction. A successful empty
# result is cached too, so a user with genuinely no groups doesn't re-hit Graph
# every request. Failures are NOT cached (transient errors shouldn't be sticky
# for 15 minutes). The lock guards only O(1)/sweep cache mutations — never the
# blocking Graph fetch (see resolve_groups_via_graph).
_cache: "OrderedDict[str, tuple[float, tuple[GraphGroup, ...]]]" = OrderedDict()
_cache_lock = threading.Lock()

# Hard cap on distinct cached oids so a churn of unique users can't grow the
# cache without bound (the "memory leak" the cache used to have).
_DEFAULT_CACHE_MAX_ENTRIES = 10_000


def _cache_ttl_seconds() -> int:
    try:
        return int(os.getenv("GRAPH_GROUPS_CACHE_SECONDS", "900"))
    except ValueError:
        return 900


def _cache_max_entries() -> int:
    try:
        value = int(os.getenv("GRAPH_GROUPS_CACHE_MAX_ENTRIES", str(_DEFAULT_CACHE_MAX_ENTRIES)))
    except ValueError:
        return _DEFAULT_CACHE_MAX_ENTRIES
    return value if value > 0 else _DEFAULT_CACHE_MAX_ENTRIES


def graph_groups_enabled() -> bool:
    """Whether the Graph fallback is on (default: enabled)."""

    value = os.getenv("AGENT_AUTH_GRAPH_GROUPS_FALLBACK", "true").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _truthy_env(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _obo_tenant() -> str | None:
    value = os.getenv("ENTRA_TENANT_ID") or os.getenv("AGENT_AUTH_TENANT")
    return value.strip() if value and value.strip() else None


def _obo_client_id() -> str | None:
    value = os.getenv("ENTRA_CLIENT_ID") or os.getenv("AGENT_AUTH_CLIENT_ID")
    return value.strip() if value and value.strip() else None


def _obo_client_secret() -> str | None:
    # Key-Vault-aware (falls back to the local env value) so the secret material
    # never has to live in container app settings in production.
    value = resolve_env_secret("ENTRA_CLIENT_SECRET")
    return value.strip() if value and value.strip() else None


def _obo_scope() -> str:
    return os.getenv("ENTRA_GRAPH_OBO_SCOPE", "https://graph.microsoft.com/.default").strip()


def obo_configured() -> bool:
    """True when OBO is enabled *and* a confidential-client credential is present.

    OBO needs a client secret/cert on the app registration — the BE is otherwise
    passwordless (managed identity), so without this it cannot act as the user.
    """

    return not _obo_missing_config()


def _obo_missing_config() -> list[str]:
    """Names of the OBO prerequisites that aren't satisfied (empty => configured).

    Used both to gate the OBO path and to explain *why* a request fell back to the
    app-only path (e.g. "container has no ENTRA_CLIENT_SECRET").
    """

    missing: list[str] = []
    if not _truthy_env("AGENT_AUTH_GROUPS_OBO", "true"):
        missing.append("AGENT_AUTH_GROUPS_OBO disabled")
    if not _obo_tenant():
        missing.append("ENTRA_TENANT_ID")
    if not _obo_client_id():
        missing.append("ENTRA_CLIENT_ID")
    if not _obo_client_secret():
        missing.append("ENTRA_CLIENT_SECRET")
    return missing


def resolve_groups_via_graph(
    oid: str,
    timeout: float = 5.0,
    *,
    user_assertion: str | None = None,
) -> tuple[GraphGroup, ...]:
    """Return the user's transitive groups (id + display name), cached per ``oid``.

    When ``user_assertion`` (the caller's raw access token) is supplied and OBO is
    configured, the groups are read **as the user** via on-behalf-of + ``/me``;
    otherwise we fall back to the app-only ``/users/{oid}`` path. Best-effort:
    returns ``()`` on any error (missing permission, network, bad response) so the
    caller can proceed with an empty group set.
    """

    if not oid:
        return ()

    now = time.time()
    with _cache_lock:
        cached = _cache.get(oid)
        if cached and now < cached[0]:
            _cache.move_to_end(oid)  # mark most-recently-used for LRU eviction
            return cached[1]

    use_obo = bool(user_assertion) and obo_configured()
    # Surface which acquisition path resolved the groups: OBO (delegated, /me) vs
    # app-only (/users/{oid}). Helps explain "local works, container fails" cases
    # where the two paths use different identities.
    if use_obo:
        logger.info("Resolving groups via OBO (delegated /me) path")
    elif user_assertion:
        # A user token was present but OBO couldn't run — name the missing pieces
        # so the container case ("no ENTRA_CLIENT_SECRET") is obvious in the logs.
        logger.info(
            "Resolving groups via app-only (/users/{oid}) path — OBO not configured (missing: %s)",
            ", ".join(_obo_missing_config()),
        )
    else:
        logger.info("Resolving groups via app-only (/users/{oid}) path — no user token")

    # Fetch OUTSIDE the lock so distinct users never serialize on the blocking,
    # paged Graph round-trip. Two concurrent requests for the *same* cold oid may
    # both fetch, but the result is identical and idempotent to cache, so the
    # rare duplicate call is an acceptable trade for not serializing every user.
    try:
        if use_obo:
            groups = _fetch_groups_obo(user_assertion, timeout)  # type: ignore[arg-type]
        else:
            groups = _fetch_groups(oid, timeout)
    except urllib.error.HTTPError:
        # A 401/403 (the "managed identity has no Graph app role" / wrong-identity
        # case) is already logged with full caller-identity diagnostics inside
        # _fetch_groups. Stay best-effort: return empty so auth never fails.
        return ()
    except Exception as exc:  # noqa: BLE001 - fallback must never raise
        # Token-acquisition failures (no credential / IMDS unreachable), network
        # timeouts, and bad responses land here. The exception text usually names
        # the cause (e.g. a DefaultAzureCredential chain dump) without leaking the
        # user's identifiers.
        logger.warning("Graph group resolution failed (oid hash omitted): %s", exc)
        return ()

    expires_at = time.time() + _cache_ttl_seconds()
    with _cache_lock:
        _store_locked(oid, expires_at, groups)
    return groups


def _store_locked(oid: str, expires_at: float, groups: tuple[GraphGroup, ...]) -> None:
    """Insert/refresh one entry, sweep expired ones, and enforce the LRU cap.

    Caller must hold ``_cache_lock``. Cheap because writes happen only on a cache
    miss (bounded by request rate), not on every read.
    """

    now = time.time()
    # TTL sweep: drop entries that have aged out so oids that stop appearing
    # don't linger forever.
    expired = [key for key, (exp, _) in _cache.items() if exp <= now]
    for key in expired:
        del _cache[key]

    _cache[oid] = (expires_at, groups)
    _cache.move_to_end(oid)

    # LRU cap: evict least-recently-used until within bound.
    max_entries = _cache_max_entries()
    while len(_cache) > max_entries:
        _cache.popitem(last=False)


def _fetch_groups(oid: str, timeout: float) -> tuple[GraphGroup, ...]:
    """App-only path: call ``/users/{oid}/transitiveMemberOf`` with the app's token."""

    token = get_token_provider(_GRAPH_SCOPE)()
    url = _GRAPH_USERS_ENDPOINT.format(oid=urllib.parse.quote(oid, safe=""))
    return _collect_groups(url, token, timeout, path="app-only")


def _fetch_groups_obo(user_assertion: str, timeout: float) -> tuple[GraphGroup, ...]:
    """OBO path: exchange the user's token for a Graph token, then call ``/me``."""

    token = _acquire_obo_graph_token(user_assertion, timeout)
    return _collect_groups(_GRAPH_ME_ENDPOINT, token, timeout, path="obo")


def _collect_groups(
    url: str | None, token: str, timeout: float, *, path: str
) -> tuple[GraphGroup, ...]:
    """Page a ``transitiveMemberOf`` endpoint and return id + displayName pairs."""

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    by_id: dict[str, GraphGroup] = {}

    try:
        while url:
            request = urllib.request.Request(url, method="GET", headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                payload = json.loads(response.read().decode("utf-8"))

            value = payload.get("value")
            if isinstance(value, list):
                for item in value:
                    if not isinstance(item, dict):
                        continue
                    group_id = item.get("id")
                    if not isinstance(group_id, str) or not group_id.strip():
                        continue
                    name = item.get("displayName")
                    by_id[group_id] = GraphGroup(
                        id=group_id,
                        display_name=name if isinstance(name, str) and name.strip() else None,
                    )

            next_link = payload.get("@odata.nextLink")
            url = next_link if isinstance(next_link, str) and next_link else None
    except urllib.error.HTTPError as exc:
        # The "local works, container fails" smoking gun. We log the *Graph token's
        # own* identity claims (never the raw token) so the container says exactly
        # who it acted as. For the app-only path a 401/403 usually means the managed
        # identity lacks the GroupMember.Read.All *Application* role; for OBO it
        # means the *Delegated* GroupMember.Read.All scope is missing from the
        # exchanged token (see `scp` / `idtyp` in the logged identity).
        permission = "Application" if path == "app-only" else "Delegated"
        logger.warning(
            "Graph group resolution HTTP %s (%s path) — verify GroupMember.Read.All "
            "(%s) is admin-consented for this identity: %s",
            exc.code,
            path,
            permission,
            json.dumps(
                {"graph_status": exc.code, "caller_identity": _decode_token_identity(token)},
                default=str,
            ),
        )
        raise

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "Graph group resolution ok (%s path): %s",
            path,
            json.dumps(
                {"caller_identity": _decode_token_identity(token), "group_count": len(by_id)},
                default=str,
            ),
        )

    return tuple(sorted(by_id.values(), key=lambda g: g.id))


def _acquire_obo_graph_token(user_assertion: str, timeout: float) -> str:
    """Exchange the caller's access token for a delegated Graph token (OBO).

    Raises on misconfiguration or an Entra error (e.g. ``AADSTS65001`` consent
    required, ``AADSTS500131`` audience mismatch) so the caller's best-effort
    handler returns ``()`` and logs the cause. Never logs the secret or assertion.
    """

    tenant = _obo_tenant()
    client_id = _obo_client_id()
    client_secret = _obo_client_secret()
    if not (tenant and client_id and client_secret):
        # Should not happen (obo_configured() gates the OBO path) but guard anyway.
        raise RuntimeError("OBO is not fully configured (tenant/client_id/secret)")

    token_url = f"https://login.microsoftonline.com/{urllib.parse.quote(tenant)}/oauth2/v2.0/token"
    body = urllib.parse.urlencode(
        {
            "grant_type": _OBO_GRANT_TYPE,
            "client_id": client_id,
            "client_secret": client_secret,
            "assertion": user_assertion,
            "scope": _obo_scope(),
            "requested_token_use": "on_behalf_of",
        }
    ).encode("ascii")
    request = urllib.request.Request(
        token_url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = _read_aad_error(exc)
        logger.warning(
            "OBO token exchange failed HTTP %s: %s",
            exc.code,
            json.dumps(detail, default=str),
        )
        raise

    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("OBO token response missing access_token")
    return access_token


def _read_aad_error(exc: urllib.error.HTTPError) -> dict[str, Any]:
    """Pull the AADSTS error code/description from an Entra error body (no secrets)."""

    try:
        payload = json.loads(exc.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - diagnostics must never raise
        return {"status": exc.code}
    if not isinstance(payload, dict):
        return {"status": exc.code}
    return {
        "status": exc.code,
        "error": payload.get("error"),
        # error_description carries the AADSTS code; it does not contain the token.
        "error_description": payload.get("error_description"),
    }


def _decode_token_identity(token: str) -> dict[str, Any]:
    """Unverified decode of the Graph token's non-sensitive identity claims.

    For diagnostics only — never an auth decision, and never logs the raw token.
    The subject of this token is the *calling application* (the app/managed
    identity ``DefaultAzureCredential`` resolved), not the end user, so these
    fields carry no user PII:

    - ``idtyp``: ``"app"`` => app-only (managed identity / client creds). A
      ``"user"`` or absent value would mean a delegated token reached an app-only
      endpoint — itself a misconfiguration.
    - ``appid`` / ``oid``: the client id and object id of the calling principal —
      match these against the managed identity you intended to grant Graph rights.
    - ``roles``: app-role values on an *app-only* token (the Application
      permission). If ``GroupMember.Read.All`` / ``Group.Read.All`` is absent here,
      the application permission was never granted/consented.
    - ``scp``: delegated scopes on an *OBO* token. ``GroupMember.Read.All`` must
      appear here for the ``/me`` call to succeed.
    """

    parts = token.split(".")
    if len(parts) < 2:
        return {}
    try:
        segment = parts[1]
        decoded = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        claims = json.loads(decoded.decode("utf-8"))
    except Exception:  # noqa: BLE001 - diagnostics must never raise
        return {}
    if not isinstance(claims, dict):
        return {}
    return {
        "idtyp": claims.get("idtyp"),
        "appid": claims.get("appid") or claims.get("azp"),
        "app_displayname": claims.get("app_displayname"),
        "aud": claims.get("aud"),
        "oid": claims.get("oid"),
        "roles": claims.get("roles"),
        "scp": claims.get("scp"),
    }


__all__ = [
    "GraphGroup",
    "graph_groups_enabled",
    "obo_configured",
    "resolve_groups_via_graph",
]
