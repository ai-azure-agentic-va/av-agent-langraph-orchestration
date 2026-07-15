"""Mode-switchable async ServiceNow client.

Two modes share one return envelope so callers never branch on transport:

* ``mock`` — serves the ServiceNow incident contract from an in-process fixture list.
* ``real`` — OAuth client-credentials token + GET against a ServiceNow instance,
  with optional fallback to the mock dataset when the live call fails.

Filtering (``_apply_filters``) is shared between mock mode and the real-mode
fallback so both paths honour the same query semantics. Secrets and bearer
tokens are never logged.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import httpx

from v1.utils.azure_key_vault import aresolve_env_secret, resolve_env_secret
from v1.utils.helper import env_bool, env_float
from v1.utils.retry import http_retry_async

logger = logging.getLogger(__name__)

# Placeholder path — every real environment must set the client-specific prefix
# via SERVICENOW_INCIDENT_LIST_API_PREFIX (mock mode never hits it).
_DEFAULT_INCIDENT_PREFIX = "/api/v1/example_support/incidents"
_BUNDLED_FIXTURE = Path(__file__).parent / "fixtures" / "servicenow_incidents.json"

# Origin used to build incident deep links when running in mock mode with no
# SERVICENOW_INSTANCE_URL configured. Lets local/mock testing render links in the
# correct ``?sys_id=`` format without forcing the user to set an instance URL.
# Real mode never uses this — it requires the per-environment SERVICENOW_INSTANCE_URL.
_DEFAULT_MOCK_ORIGIN = "https://mock.service-now.com"

# Renew a cached OAuth token this many seconds before its stated expiry so an
# in-flight request never races the token going stale on the server side.
_TOKEN_EXPIRY_BUFFER_SECONDS = 60.0

# Assumed token lifetime (30 minutes) when the token endpoint omits ``expires_in``.
_DEFAULT_TOKEN_EXPIRES_IN_SECONDS = 1800.0

_REAL_MODE_ALIASES = frozenset({"real", "live", "servicenow"})
_MOCK_MODE_ALIASES = frozenset({"mock", "servicenow_mock", "servicenow-mock"})

# Reference fields whose accessor unwraps a {display_value, value} pair.
_REFERENCE_FILTER_FIELDS = {
    "assigned_to": "assigned_to",
    "resolved_by": "resolved_by",
    "assigned_to_name": "assigned_to",
    "resolved_by_name": "resolved_by",
}

# ``<filter key>`` -> ``<incident field>`` for case-insensitive substring filters.
# ``description_contains`` has its own match branch (long description);
# ``short_description_contains`` searches the ticket title (live-verified 2026-07-13:
# the live wrapper accepts it and it narrows results server-side);
# ``close_notes_contains`` searches the close notes (resolution / cluster evidence).
# Cause is matched ONLY via the exact ``cause`` filter — the ServiceNow instance does not
# expose a cause substring filter, so ``cause_contains`` is removed. The other
# substring filters the instance does not support (resolution_notes_contains,
# configuration_item_contains) remain removed; pipeline /
# missing-data narrowing is done agent-side from the ``category`` and
# ``configuration_item`` fields already present in each result.
_CONTAINS_FILTER_FIELDS = {
    "short_description_contains": "short_description",
    "close_notes_contains": "close_notes",
}


class ServiceNowError(RuntimeError):
    """Raised when a real ServiceNow call fails and fallback is disabled."""


class ServiceNowConfigurationError(RuntimeError):
    """Raised when ServiceNow configuration is invalid for the active environment."""


def _normalize_mode(raw: str | None) -> str:
    value = (raw or "mock").strip().lower()
    if value in _REAL_MODE_ALIASES:
        return "real"
    if value in _MOCK_MODE_ALIASES:
        return "mock"
    return "mock"


def _prod_like_env() -> bool:
    return os.getenv("APP_ENV", "local").strip().lower() in {"stage", "prod", "production"}


def _prod_env() -> bool:
    """Production only (excludes stage). Stage legitimately uses the ``/v1/`` path;
    Production omits it, so the prod-path guard must not fire for stage."""

    return os.getenv("APP_ENV", "local").strip().lower() in {"prod", "production"}


@dataclass(frozen=True)
class ServiceNowConfig:
    """Immutable ServiceNow connection configuration."""

    mode: str = "mock"
    instance_url: str | None = None
    incident_list_api_prefix: str = _DEFAULT_INCIDENT_PREFIX
    token_url: str | None = None
    oauth_scope: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    timeout_seconds: float = 20.0
    fallback_to_mock: bool = True
    mock_fixture_path: str | None = None
    verify_ssl: bool = True

    @classmethod
    def from_env(cls) -> ServiceNowConfig:
        """Build config, resolving Key Vault secrets synchronously.

        Use :meth:`afrom_env` from async code so the Key Vault round-trip never
        blocks the event loop.
        """

        return cls._from_env_with_secrets(
            client_id=resolve_env_secret("SERVICENOW_CLIENT_ID"),
            client_secret=resolve_env_secret("SERVICENOW_CLIENT_SECRET"),
        )

    @classmethod
    async def afrom_env(cls) -> ServiceNowConfig:
        """Async sibling of :meth:`from_env`; resolves secrets without blocking the loop."""

        client_id = await aresolve_env_secret("SERVICENOW_CLIENT_ID")
        client_secret = await aresolve_env_secret("SERVICENOW_CLIENT_SECRET")
        return cls._from_env_with_secrets(client_id=client_id, client_secret=client_secret)

    @classmethod
    def _from_env_with_secrets(
        cls, *, client_id: str | None, client_secret: str | None
    ) -> ServiceNowConfig:
        config = cls(
            mode=_normalize_mode(os.getenv("SERVICENOW_MODE")),
            instance_url=os.getenv("SERVICENOW_INSTANCE_URL"),
            incident_list_api_prefix=os.getenv(
                "SERVICENOW_INCIDENT_LIST_API_PREFIX", _DEFAULT_INCIDENT_PREFIX
            ),
            token_url=os.getenv("SERVICENOW_TOKEN_URL"),
            oauth_scope=os.getenv("SERVICENOW_OAUTH_SCOPE"),
            client_id=client_id,
            client_secret=client_secret,
            timeout_seconds=env_float("SERVICENOW_TIMEOUT_SECONDS", default=20.0),
            # Default fallback OFF in stage/prod so a misconfigured real client raises
            # instead of silently serving mock data behind degraded=true; local/dev
            # keeps the convenient default. Opt back in with SERVICENOW_FALLBACK_TO_MOCK=true.
            fallback_to_mock=env_bool(
                "SERVICENOW_FALLBACK_TO_MOCK", default=not _prod_like_env()
            ),
            mock_fixture_path=os.getenv("SERVICENOW_MOCK_FIXTURE_PATH"),
            verify_ssl=env_bool("SERVICENOW_VERIFY_SSL", default=True),
        )
        # Prod-path guard: the default incident prefix carries ``/v1/`` (correct for
        # Dev5/QA/stage), but Production omits it. In prod a real-mode client must set
        # the prefix explicitly, or every request silently 404s a non-existent path.
        # Keyed off ``is None`` so an explicit prefix equal to the default still passes.
        if (
            _prod_env()
            and config.is_real
            and os.getenv("SERVICENOW_INCIDENT_LIST_API_PREFIX") is None
        ):
            raise ServiceNowConfigurationError(
                "SERVICENOW_INCIDENT_LIST_API_PREFIX must be set explicitly in "
                "production — the default includes '/v1/', which Production omits"
            )
        # Prod guard (mirrors azure_search): in stage/prod a real-mode client must
        # source its secret from Key Vault, not a plaintext app-setting env var.
        # Mild on purpose — it only fires when a plaintext secret is actually set.
        if (
            _prod_like_env()
            and config.is_real
            and os.getenv("SERVICENOW_CLIENT_SECRET")
            and not os.getenv("AZURE_KEY_VAULT_URI")
        ):
            raise ServiceNowConfigurationError(
                "SERVICENOW_CLIENT_SECRET must be resolved via Key Vault "
                "(set AZURE_KEY_VAULT_URI) in stage/prod"
            )
        return config

    @property
    def is_real(self) -> bool:
        return self.mode == "real"

    @property
    def has_oauth_credentials(self) -> bool:
        """True when both client credentials are present for client-credentials OAuth."""

        return bool(self.client_id) and bool(self.client_secret)

    @property
    def origin(self) -> str | None:
        if not self.instance_url:
            return None
        parts = urlsplit(self.instance_url)
        if not parts.scheme or not parts.netloc:
            return None
        return f"{parts.scheme}://{parts.netloc}"


def _reference_value(raw: Any) -> str:
    """Read a possibly-reference field as a display string."""

    if isinstance(raw, Mapping):
        value = raw.get("display_value")
        if value in (None, ""):
            value = raw.get("value")
        return "" if value is None else str(value)
    return "" if raw is None else str(raw)


def _reference_display(raw: Any) -> str:
    """Read a reference field's DISPLAY value only — never its raw ``value``.

    Unlike :func:`_reference_value`, this does NOT fall back to ``value`` when
    ``display_value`` is empty. For PEOPLE / group reference fields (assigned_to,
    resolved_by, assignment_group) the raw ``value`` is a sys_id, which must never
    reach the user — when there is no display name we return empty (the tool layer
    renders 'Not available'). A scalar passes through as its own display string.
    """

    if isinstance(raw, Mapping):
        display = raw.get("display_value")
        return "" if display in (None, "") else str(display)
    return "" if raw is None else str(raw)


def build_incident_url(origin: str, sys_id: str) -> str:
    """Construct a ServiceNow incident deep link from the instance origin + sys_id.

    The ``/incidents`` API does not return a usable deep link, so the client builds
    one from the per-environment instance URL (``SERVICENOW_INSTANCE_URL`` -> origin)
    and the ``sys_id`` already present on each record. The agent is fixed to one
    instance per environment (Dev5 / QA / Prod), so the origin is stable per env.

    The ``sys_id`` appears ONLY inside this URL — it is never surfaced as a
    standalone field (the tool normalizers emit ``ticket_url``, never ``sys_id``).
    ``sys_id`` is instance-specific, so the same logical incident has different
    sys_ids across Dev5/QA/Prod; never use it to match records across environments.
    """

    return f"{origin}/nav_to.do?uri=incident.do?sys_id={sys_id}"


def _display_and_plain(raw: Any) -> tuple[str, str]:
    """Split a possibly-reference field into ``(display_value, value)`` strings.

    A scalar is treated as both its own display and plain value so the
    ``priority``/``state`` match branches share one consistent rule.
    """

    if isinstance(raw, Mapping):
        display = "" if raw.get("display_value") is None else str(raw.get("display_value"))
        plain = "" if raw.get("value") is None else str(raw.get("value"))
        return display, plain
    text = "" if raw is None else str(raw)
    return text, text


def _sanitize_contains(value: str) -> str:
    """Plain-substring value for a ``*_contains`` filter.

    The API does plain substring matching automatically, so SQL-LIKE ``%``
    wildcards and stray surrounding quotes must be dropped (a literal ``%TSYS%``
    would otherwise search for percent signs). Inner spaces are kept — the wire
    encoder turns them into ``%20``. Shared by the real wire path and the mock
    matcher so both strip identically.
    """

    return value.strip().strip("%\"' ").strip()


def _contains_all_tokens(needle: str, haystack: str) -> bool:
    """True when every whitespace-delimited token of ``needle`` is in ``haystack``.

    Conversational / multi-word queries rarely appear as one contiguous phrase,
    so a plain ``needle in haystack`` test (e.g. 'missing data') silently returns
    nothing even when each word is present. Splitting into tokens and requiring
    all of them (AND-of-substrings) keeps matching narrow while tolerating word
    order and interleaving. ``needle`` is expected pre-lowercased.
    """

    tokens = needle.split()
    if not tokens:
        return True
    return all(token in haystack for token in tokens)


def _coerce_boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


def _coerce_int(value: Any) -> int | None:
    """Coerce an int/float wire number (ServiceNow sends ``25.0``) to int; else None.

    Bools are rejected (``True`` is an int subclass but never a valid count/offset).
    """

    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


# ServiceNow serves timestamps as ``YYYY-MM-DD HH:MM:SS``; the tools also accept
# a date-only ``YYYY-MM-DD`` bound.
_SN_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_SN_DATE_FORMAT = "%Y-%m-%d"


def _parse_sn_datetime(value: str) -> datetime | None:
    """Parse a ServiceNow timestamp or date; return None if neither shape matches."""

    text = value.strip()
    if not text:
        return None
    for fmt in (_SN_DATETIME_FORMAT, _SN_DATE_FORMAT):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _parse_date_bound(value: str, *, is_before: bool) -> datetime | None:
    """Parse a filter bound, expanding a date-only upper bound to end-of-day.

    A bare ``YYYY-MM-DD`` ``*_before`` covers the whole day, so it is pushed to
    ``23:59:59.999999``; otherwise a lexical/naive compare like
    ``'2026-05-10 02:05:11' <= '2026-05-10'`` would drop every same-day ticket
    that carries a time component. Lower bounds already start at midnight, which
    is inclusive of the whole day, so they need no adjustment.
    """

    text = value.strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, _SN_DATETIME_FORMAT)
    except ValueError:
        pass
    try:
        day = datetime.strptime(text, _SN_DATE_FORMAT)
    except ValueError:
        return None
    if is_before:
        return day.replace(hour=23, minute=59, second=59, microsecond=999999)
    return day


def _matches_date_bound(raw_value: Any, bound: Any, *, is_before: bool) -> bool:
    """Compare an incident timestamp against a filter bound as datetimes.

    Falls back to a lexical comparison only when either side is unparsable, so
    malformed fixture data still yields a deterministic answer.
    """

    actual_text = _reference_value(raw_value)
    bound_text = str(bound)
    actual_dt = _parse_sn_datetime(actual_text)
    bound_dt = _parse_date_bound(bound_text, is_before=is_before)
    if actual_dt is None or bound_dt is None:
        return actual_text <= bound_text if is_before else actual_text >= bound_text
    return actual_dt <= bound_dt if is_before else actual_dt >= bound_dt


@dataclass(frozen=True)
class _FilterSpec:
    """How one supported filter maps onto the real-API wire param.

    ``kind`` selects the value encoder in :func:`_wire_value`:
    ``contains`` (strip ``%``/quotes), ``date`` (lower bound -> ``<date> 00:00:00``),
    ``date_upper`` (``*_before`` upper bound -> ``<date> 23:59:59`` so the boundary
    day is included), ``int_prefix`` (reduce ``'1 - Critical'`` to ``'1'``),
    ``exact``/``passthrough`` (sent verbatim).
    """

    api_name: str
    kind: str


# Single source of truth for which filter keys may reach the real ``/incidents``
# endpoint and how each value is encoded on the wire. A key absent from this table
# is dropped (and logged) rather than forwarded, so a filter the ServiceNow instance does
# not expose can never silently hit the live API. This mirrors the authoritative
# supported-filter list in the ServiceNow agent contract README §3.2 exactly, plus
# short_description_contains (README §3.3 marked it unsupported, but it was
# live-verified working against the live wrapper on 2026-07-13). The keys that stay
# NOT supported (cause_contains,
# probable_cause_contains, resolution_notes_contains, solved_by_name) — and the
# record fields that are OUTPUTS rather than filters (``category``, ``opened_at``) —
# are deliberately absent. ``cause`` is exact-match against the closed "Probable cause"
# value set on the ServiceNow instance: Action Request, Code Error, Data Availability, Data
# Quality, Deployment Issue, Documentation Issues, Education/Training, False Positive,
# Holiday, Maintenance, Network Cluster Issue, Network or Connectivity Issue,
# Requirements Issues, Software Upgrade, Subnet Issue, Timing/Scheduling Issue — a
# paraphrase, partial word, or off-list value returns zero, and there is no cause
# substring filter. Pipeline / missing-data
# classification and the cluster decoy checks are done agent-side from
# description / short_description / close_notes plus the ``category`` output field
# already present in each result. Date windows use ``created_*`` / ``updated_*``.
SUPPORTED_FILTERS: dict[str, _FilterSpec] = {
    "number": _FilterSpec("number", "passthrough"),
    "assignment_group": _FilterSpec("assignment_group", "passthrough"),
    "priority": _FilterSpec("priority", "int_prefix"),
    "state": _FilterSpec("state", "passthrough"),
    "active": _FilterSpec("active", "passthrough"),
    "description_contains": _FilterSpec("description_contains", "contains"),
    "short_description_contains": _FilterSpec("short_description_contains", "contains"),
    "close_notes_contains": _FilterSpec("close_notes_contains", "contains"),
    "cause": _FilterSpec("cause", "exact"),
    "assigned_to": _FilterSpec("assigned_to", "passthrough"),
    "resolved_by": _FilterSpec("resolved_by", "passthrough"),
    "assigned_to_name": _FilterSpec("assigned_to_name", "passthrough"),
    "resolved_by_name": _FilterSpec("resolved_by_name", "passthrough"),
    "created_after": _FilterSpec("created_after", "date"),
    "created_before": _FilterSpec("created_before", "date_upper"),
    "updated_after": _FilterSpec("updated_after", "date"),
    "updated_before": _FilterSpec("updated_before", "date_upper"),
}


def _leading_int(value: Any) -> str:
    """Reduce a priority like ``'1 - Critical'`` to its leading integer ``'1'``.

    The real API expects a bare integer; a value with no leading digit is passed
    through trimmed so it fails loudly server-side rather than silently vanishing.
    """

    match = re.match(r"\s*(\d+)", str(value))
    return match.group(1) if match else str(value).strip()


def _normalize_date_wire(value: Any, *, end_of_day: bool = False) -> str:
    """Expand a date-only bound to the ServiceNow ``<date> HH:MM:SS`` wire form.

    A date-only ``*_before`` covers the whole day, so ``end_of_day`` pushes it to
    ``23:59:59`` (ServiceNow stores to the second); otherwise the live API would
    treat ``<date> 00:00:00`` as the day's start and drop every same-day ticket
    that carries a time component — the exact loss the mock already guards against
    in :func:`_parse_date_bound`. A value already carrying a time is unchanged.
    """

    text = str(value).strip()
    try:
        datetime.strptime(text, _SN_DATE_FORMAT)
    except ValueError:
        return text
    return f"{text} 23:59:59" if end_of_day else f"{text} 00:00:00"


def _wire_value(spec: _FilterSpec, value: Any) -> str | None:
    """Encode a filter value for the real query string per its spec kind.

    Returns ``None`` when a contains value sanitizes to empty, so the caller drops
    the param instead of sending an empty (match-everything) filter.
    """

    if spec.kind == "contains":
        return _sanitize_contains(str(value)) or None
    if spec.kind == "date":
        return _normalize_date_wire(value)
    if spec.kind == "date_upper":
        return _normalize_date_wire(value, end_of_day=True)
    if spec.kind == "int_prefix":
        return _leading_int(value)
    return str(value)


class ServiceNowClient:
    """Async ServiceNow client supporting mock and real modes behind one envelope."""

    def __init__(
        self,
        config: ServiceNowConfig | None = None,
        *,
        incidents: list[dict[str, Any]] | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config or ServiceNowConfig.from_env()
        self._incidents = self._load_incidents(incidents)
        self._http_client = http_client
        self._http_client_injected = http_client is not None
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

    @classmethod
    def from_env(cls) -> ServiceNowClient:
        return cls(ServiceNowConfig.from_env())

    @classmethod
    async def afrom_env(cls) -> ServiceNowClient:
        """Async sibling of :meth:`from_env`; resolves Key Vault secrets off the loop."""

        return cls(await ServiceNowConfig.afrom_env())

    # -- fixture loading -----------------------------------------------------

    def _load_incidents(self, incidents: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        if incidents is not None:
            return [dict(incident) for incident in incidents]

        path = Path(self.config.mock_fixture_path) if self.config.mock_fixture_path else (
            _BUNDLED_FIXTURE
        )
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError) as exc:
            # The fixture file is owned by another specialist and may not exist
            # yet; degrade to an empty dataset rather than crashing.
            logger.warning(
                "servicenow.fixture_load_failed",
                extra={
                    "event": "servicenow.fixture_load_failed",
                    "path": str(path),
                    "error": str(exc),
                },
            )
            return []

        if isinstance(data, Mapping):
            records = data.get("incidents", [])
        elif isinstance(data, list):
            records = data
        else:
            records = []

        if not isinstance(records, list):
            logger.warning(
                "servicenow.fixture_invalid_shape",
                extra={"event": "servicenow.fixture_invalid_shape", "path": str(path)},
            )
            return []
        return [dict(record) for record in records if isinstance(record, Mapping)]

    # -- public API ----------------------------------------------------------

    async def list_incidents(
        self,
        *,
        filters: Mapping[str, Any] | None = None,
        limit: int = 25,
        offset: int = 0,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        if self.config.is_real:
            return await self._list_incidents_real(
                filters=filters, limit=limit, offset=offset, access_token=access_token
            )
        return self._list_incidents_mock(filters=filters, limit=limit, offset=offset)

    async def aclose(self) -> None:
        if self._http_client is not None and not self._http_client_injected:
            await self._http_client.aclose()
            self._http_client = None

    # -- mock mode -----------------------------------------------------------

    def _list_incidents_mock(
        self,
        *,
        filters: Mapping[str, Any] | None,
        limit: int,
        offset: int,
        mode: str = "mock",
        degraded: bool = False,
    ) -> dict[str, Any]:
        matched = self._apply_filters(self._incidents, filters)
        page = matched[offset : offset + limit] if limit >= 0 else matched[offset:]
        return self._build_envelope(
            incidents=[dict(incident) for incident in page],
            result_count=len(matched),
            limit=limit,
            offset=offset,
            mode=mode,
            source="servicenow_mock",
            degraded=degraded,
        )

    # -- real mode -----------------------------------------------------------

    async def _list_incidents_real(
        self,
        *,
        filters: Mapping[str, Any] | None,
        limit: int,
        offset: int,
        access_token: str | None,
    ) -> dict[str, Any]:
        try:
            # When client credentials are configured, always mint our own OAuth
            # token; only fall back to a caller-supplied token otherwise.
            if self.config.has_oauth_credentials:
                token = await self._resolve_token()
            else:
                token = access_token or await self._resolve_token()
            payload = await self._fetch_incidents(
                token=token, filters=filters, limit=limit, offset=offset
            )
            return self._envelope_from_real_payload(
                payload, limit=limit, offset=offset
            )
        except (httpx.HTTPError, ServiceNowError) as exc:
            # Narrow to expected transport / API failures so programming errors
            # (KeyError, TypeError, ...) crash loudly instead of masquerading as
            # degraded mock data.
            if not self.config.fallback_to_mock:
                raise ServiceNowError(
                    "ServiceNow real-mode request failed and fallback is disabled"
                ) from exc
            logger.error(
                "servicenow.real_failed_fallback_to_mock",
                extra={
                    "event": "servicenow.real_failed_fallback_to_mock",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            return self._list_incidents_mock(
                filters=filters, limit=limit, offset=offset, mode="real", degraded=True
            )

    async def _resolve_token(self) -> str:
        # Reuse the cached token only while it is still valid (with a renewal
        # buffer); otherwise mint a fresh one.
        if self._access_token and time.monotonic() < self._token_expires_at:
            return self._access_token

        origin = self.config.origin
        token_url = self.config.token_url or (
            f"{origin}/oauth_token.do" if origin else None
        )
        if not token_url:
            raise ServiceNowError("ServiceNow token URL is not configured")

        body: dict[str, Any] = {
            "grant_type": "client_credentials",
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
        }
        if self.config.oauth_scope:
            body["scope"] = self.config.oauth_scope

        client = self._get_http_client()

        @http_retry_async()
        async def _do_token() -> httpx.Response:
            # ponytail: never replay a ServiceNow session cookie — see _do_get below.
            # client_credentials is stateless; the request authenticates by body alone.
            client.cookies.clear()
            # ServiceNow's OAuth token endpoint expects an
            # application/x-www-form-urlencoded body, not JSON.
            resp = await client.post(token_url, data=body)
            resp.raise_for_status()
            return resp

        response = await _do_token()
        data = response.json()
        if not isinstance(data, Mapping):
            raise ServiceNowError("ServiceNow token endpoint returned an unexpected shape")
        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            raise ServiceNowError("ServiceNow token endpoint did not return an access token")
        self._access_token = token
        self._token_expires_at = time.monotonic() + self._token_lifetime(data.get("expires_in"))
        return token

    @staticmethod
    def _token_lifetime(expires_in: Any) -> float:
        """Seconds the freshly minted token may be cached, net of the renewal buffer.

        Falls back to a 30-minute default when ``expires_in`` is missing or
        unparsable, so the token is still cached and reused across requests.
        """

        try:
            lifetime = float(expires_in)
        except (TypeError, ValueError):
            lifetime = _DEFAULT_TOKEN_EXPIRES_IN_SECONDS
        return max(0.0, lifetime - _TOKEN_EXPIRY_BUFFER_SECONDS)

    async def _fetch_incidents(
        self,
        *,
        token: str,
        filters: Mapping[str, Any] | None,
        limit: int,
        offset: int,
    ) -> Any:
        origin = self.config.origin
        if not origin:
            raise ServiceNowError("ServiceNow instance URL is not configured")
        url = f"{origin}{self.config.incident_list_api_prefix}"

        params: dict[str, Any] = {}
        for key, value in (filters or {}).items():
            if value in (None, ""):
                continue
            spec = SUPPORTED_FILTERS.get(key)
            if spec is None:
                # Unknown / spec-forbidden key: never forward it to the live API.
                logger.warning(
                    "servicenow.dropped_unknown_filter",
                    extra={
                        "event": "servicenow.dropped_unknown_filter",
                        "filter_key": key,
                    },
                )
                continue
            wired = _wire_value(spec, value)
            if wired in (None, ""):
                continue
            params[spec.api_name] = wired
        params["limit"] = limit
        params["offset"] = offset

        # Encode with quote() so a space becomes %20 (not '+') and the datetime
        # separator matches ServiceNow's documented ``<date>%2000:00:00`` wire form.
        # Build the query onto the URL string so httpx preserves the encoding
        # verbatim instead of re-quoting a dict.
        query = urlencode(params, quote_via=quote)
        request_url = f"{url}?{query}"

        headers = {"Authorization": f"Bearer {token}"}
        client = self._get_http_client()

        @http_retry_async()
        async def _do_get() -> httpx.Response:
            # ponytail: clear the cookie jar before every send. On a long-lived client
            # httpx replays the session cookies ServiceNow sets (BIGipServerpool_*/
            # JSESSIONID/glide_user_route/glide_node_id_for_js); after idle that session
            # is recycled and the replayed cookie pins the request to it -> 200 with an
            # EMPTY body. Auth must ride the Bearer token alone. (Mirrors clearing
            # cookies in Postman, which is the manual workaround for the same symptom.)
            client.cookies.clear()
            resp = await client.get(request_url, headers=headers)
            resp.raise_for_status()
            return resp

        response = await _do_get()
        payload = response.json()
        # Diagnostic: a live 200 whose body doesn't match the expected shape
        # ({"incidents": [...]} / {"result": {...}} / a bare list) silently unwraps
        # to zero records downstream — the classic "Postman shows rows but the chain
        # shows nothing". Log the SHAPE only (top-level keys + record count), never the
        # record contents or any secret, so the next failure pinpoints whether the body
        # shape is the cause. status_code is always 200 here (raise_for_status above).
        if isinstance(payload, Mapping):
            top_level_keys = sorted(str(key) for key in payload.keys())
            records = payload.get("incidents")
            if not isinstance(records, list):
                result = payload.get("result")
                if isinstance(result, Mapping):
                    records = result.get("incidents")
                elif isinstance(result, list):
                    records = result
            record_count = len(records) if isinstance(records, list) else None
        elif isinstance(payload, list):
            top_level_keys = ["<top-level array>"]
            record_count = len(payload)
        else:
            top_level_keys = [f"<non-collection: {type(payload).__name__}>"]
            record_count = None
        logger.info(
            "servicenow.real_response_shape",
            extra={
                "event": "servicenow.real_response_shape",
                "top_level_keys": top_level_keys,
                "record_count": record_count,
                "request_path": self.config.incident_list_api_prefix,
            },
        )
        return payload

    def _envelope_from_real_payload(
        self, payload: Any, *, limit: int, offset: int
    ) -> dict[str, Any]:
        body = payload
        if isinstance(body, Mapping) and "incidents" not in body and "result" in body:
            unwrapped = body.get("result")
            if isinstance(unwrapped, Mapping):
                body = unwrapped

        if isinstance(body, Mapping):
            incidents = body.get("incidents", [])
            if not isinstance(incidents, list):
                raise ServiceNowError("ServiceNow response 'incidents' was not a list")
            result_count = _coerce_int(body.get("result_count"))
            if result_count is None:
                result_count = offset + len(incidents)
            # Trust the API's own pagination flags — it returns them as floats
            # (25.0) / bools, and its result_count is only the PAGE count, so a
            # locally-derived has_more would always be False.
            api_has_more = body.get("has_more")
            api_has_more = api_has_more if isinstance(api_has_more, bool) else None
            api_next_offset = _coerce_int(body.get("next_offset"))
        elif isinstance(body, list):
            incidents = body
            result_count = offset + len(incidents)
            api_has_more = None
            api_next_offset = None
        else:
            raise ServiceNowError("ServiceNow response had an unexpected shape")

        clean = [dict(incident) for incident in incidents if isinstance(incident, Mapping)]
        return self._build_envelope(
            incidents=clean,
            result_count=result_count,
            limit=limit,
            offset=offset,
            mode="real",
            source="servicenow_real",
            degraded=False,
            has_more=api_has_more,
            next_offset=api_next_offset,
        )

    # -- shared helpers ------------------------------------------------------

    def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.config.timeout_seconds),
                verify=self.config.verify_ssl,
            )
        return self._http_client

    def _build_envelope(
        self,
        *,
        incidents: list[dict[str, Any]],
        result_count: int,
        limit: int,
        offset: int,
        mode: str,
        source: str,
        degraded: bool,
        has_more: bool | None = None,
        next_offset: int | None = None,
    ) -> dict[str, Any]:
        # Construct a SYS_ID-based deep link for every record (ticket_url is mandatory —
        # the agent must always be able to hand back a clickable incident link, in mock
        # AND real mode). The /incidents API returns no usable link, so we build one from
        # the instance origin + the record's sys_id. ONLY the sys_id form resolves in
        # ServiceNow; a number-based ``sysparm_query`` link does NOT reliably open the
        # incident, so we never fall back to the number — sys_id is the only key used.
        # Each ``incidents`` list here is a fresh copy, so this never mutates
        # caller/fixture state. The sys_id-based link is ALWAYS rebuilt so the correct
        # ``?sys_id=`` format wins over any pre-baked URL carried on the record (e.g. a
        # mock fixture's hand-written ``sysparm_query`` string, which is NOT a valid
        # deep link). Origin falls back to a default mock origin in ANY mode as a last
        # resort, so a missing SERVICENOW_INSTANCE_URL can never strip the link.
        origin = self.config.origin or _DEFAULT_MOCK_ORIGIN
        for incident in incidents:
            sys_id = _reference_value(incident.get("sys_id")).strip()
            if sys_id:
                incident["ticket_url"] = build_incident_url(origin, sys_id)
            else:
                # Every incident is expected to carry a sys_id (the link is always built
                # from it). If one ever arrives without one, log loudly so the invariant
                # violation is visible rather than a silently missing UI link.
                logger.warning(
                    "servicenow.incident_missing_sys_id",
                    extra={
                        "event": "servicenow.incident_missing_sys_id",
                        "number": _reference_value(incident.get("number")),
                        "mode": mode,
                    },
                )

        # Mock derives next_offset/has_more from the full matched set (result_count is
        # the TOTAL). Real mode passes the API's authoritative values instead, because
        # its result_count is only the PAGE count and a derived has_more is always False.
        computed_next = offset + len(incidents)
        resolved_next = computed_next if next_offset is None else next_offset
        resolved_has_more = (
            resolved_next < result_count if has_more is None else has_more
        )
        return {
            "result_count": result_count,
            "limit": limit,
            "offset": offset,
            "next_offset": resolved_next,
            "has_more": resolved_has_more,
            "incidents": incidents,
            "mode": mode,
            "source": source,
            "degraded": degraded,
        }

    def _apply_filters(
        self, incidents: list[dict[str, Any]], filters: Mapping[str, Any] | None
    ) -> list[dict[str, Any]]:
        if not filters:
            return list(incidents)

        active_filters = {
            key: value for key, value in filters.items() if value not in (None, "")
        }
        if not active_filters:
            return list(incidents)

        return [
            incident
            for incident in incidents
            if self._matches(incident, active_filters)
        ]

    def _matches(self, incident: Mapping[str, Any], filters: Mapping[str, Any]) -> bool:
        for key, value in filters.items():
            if not self._matches_one(incident, key, value):
                return False
        return True

    def _matches_one(self, incident: Mapping[str, Any], key: str, value: Any) -> bool:
        if key == "number":
            wanted_numbers = {
                part.strip().upper() for part in str(value).split(",") if part.strip()
            }
            return _reference_value(incident.get("number")).strip().upper() in wanted_numbers

        if key == "description_contains":
            needle = _sanitize_contains(str(value)).lower()
            if not needle:
                return True
            # description only — short_description has its own live-verified filter
            # (short_description_contains, handled by _CONTAINS_FILTER_FIELDS below).
            haystack = _reference_value(incident.get("description")).lower()
            return _contains_all_tokens(needle, haystack)

        if key in _CONTAINS_FILTER_FIELDS:
            needle = _sanitize_contains(str(value)).lower()
            if not needle:
                return True
            field = _CONTAINS_FILTER_FIELDS[key]
            return _contains_all_tokens(needle, _reference_value(incident.get(field)).lower())

        if key == "cause":
            # The LIVE instance matches cause EXACTLY (off-list/partial -> 0). Two
            # layers cooperate so partial USER input still works end to end:
            #   * In REAL mode, tools.normalize_cause resolves a partial term
            #     ('subnet', 'network cluster') to a full VALID_CAUSES label UPSTREAM,
            #     so the value arriving here is already complete and the exact compare
            #     below trivially matches.
            #   * In MOCK mode the fixtures carry free-text causes that are NOT in the
            #     official set (e.g. 'Source timeout', 'schema drift'), which
            #     normalize_cause can't resolve. We keep an AND-of-tokens fallback so a
            #     partial term ('timeout', 'drift') still surfaces the fixture incident
            #     whose stored cause is the full phrase.
            actual = _reference_value(incident.get("cause")).strip().lower()
            wanted = str(value).strip().lower()
            if actual == wanted:
                return True
            return _contains_all_tokens(wanted, actual)

        if key in _REFERENCE_FILTER_FIELDS:
            field = _REFERENCE_FILTER_FIELDS[key]
            haystack = _reference_value(incident.get(field)).lower()
            return str(value).strip().lower() in haystack

        if key == "assignment_group":
            # README §3.2: name (substring) or sys_id, comma-separated -> match any
            # part against either the display name or the raw value/sys_id.
            display, plain = _display_and_plain(incident.get("assignment_group"))
            haystacks = (display.lower(), plain.lower())
            parts = [part.strip().lower() for part in str(value).split(",") if part.strip()]
            if not parts:
                return True
            return any(part in haystack for part in parts for haystack in haystacks)

        if key == "priority":
            display, plain = _display_and_plain(incident.get("priority"))
            wanted = str(value).strip()
            return display.startswith(wanted) or plain == wanted

        if key == "state":
            display, plain = _display_and_plain(incident.get("state"))
            wanted = str(value).strip()
            return plain == wanted or display.strip().lower() == wanted.lower()

        if key == "active":
            wanted_active = _coerce_boolish(value)
            raw = incident.get("active")
            if isinstance(raw, Mapping):
                source: Any = raw.get("value")
                if source is None:
                    source = raw.get("display_value")
            else:
                source = raw
            return _coerce_boolish(source) is wanted_active

        if key in {"created_after", "created_before"}:
            return _matches_date_bound(
                incident.get("sys_created_on"),
                value,
                is_before=key == "created_before",
            )

        if key in {"updated_after", "updated_before"}:
            return _matches_date_bound(
                incident.get("sys_updated_on"),
                value,
                is_before=key == "updated_before",
            )

        # Unknown keys (and record fields that are outputs, not filters — e.g.
        # ``category``, ``opened_at``) are ignored (do not constrain the result set).
        return True


__all__ = [
    "ServiceNowClient",
    "ServiceNowConfig",
    "ServiceNowConfigurationError",
    "ServiceNowError",
    "build_incident_url",
]
