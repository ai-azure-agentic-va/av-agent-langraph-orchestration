"""Tests for the ServiceNow spec-alignment fixes (SERVICENOW_GAP_ANALYSIS).

Covers the new behaviors:
- real-path wire build is driven by the SUPPORTED_FILTERS allowlist: unknown /
  spec-forbidden keys are dropped, % wildcards are stripped, date bounds are
  expanded to the '<date> 00:00:00' wire form encoded with %20, and priority is
  reduced to a bare integer (NEW-ALLOWLIST, SN-04, NEW-DATE, SN-15);
- narrowed real-mode fallback + stage/prod config guards (NEW-FALLBACK, NEW-PATH);
- mock-side: close_notes_contains (SN-02/SN-06), category output (SN-11),
  comma-split assignment_group (SN-16), engineer fallback (SN-13), and tool
  pagination (NEW-PAGINATION).

Runs standalone (``python test_servicenow_fixes.py``) or under pytest.
"""

from __future__ import annotations

import asyncio
import os

import httpx

from v1.core.tools.servicenow import tools as tools_module
from v1.core.tools.servicenow.tools import (
    ServiceNowToolInputError,
    _ticket_base,
    servicenow_get_ticket_detail,
    servicenow_list_tickets,
)
from v1.utils.clients.servicenow import (
    SUPPORTED_FILTERS,
    ServiceNowClient,
    ServiceNowConfig,
    ServiceNowConfigurationError,
    _DEFAULT_MOCK_ORIGIN,
    _leading_int,
    _normalize_date_wire,
    _sanitize_contains,
    build_incident_url,
)


# -- real-path wire build (NEW-ALLOWLIST / SN-04 / NEW-DATE / SN-15) ------------


def _wire_query(filters: dict) -> str:
    """Drive the real path through a MockTransport and capture the GET query."""

    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"result": {"incidents": [], "result_count": 0}})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = ServiceNowConfig(
        mode="real",
        instance_url="https://finqa.service-now.com",
        fallback_to_mock=False,
    )
    client = ServiceNowClient(config, incidents=[], http_client=http_client)
    asyncio.run(client.list_incidents(filters=filters, limit=25, access_token="tok"))
    return captured["url"]


def test_wire_drops_unknown_and_forbidden_filters() -> None:
    url = _wire_query(
        {
            "short_description_contains": "feed",  # README-forbidden -> not in allowlist
            "configuration_item_contains": "PL-",  # non-spec -> not in allowlist
            "bogus": "x",  # unknown -> dropped
            "description_contains": "ACME",  # supported -> forwarded
        }
    )
    assert "short_description_contains" not in url
    assert "configuration_item_contains" not in url
    assert "bogus" not in url
    assert "description_contains=ACME" in url


def test_wire_strips_percent_wildcards() -> None:
    url = _wire_query({"description_contains": "%ACME%"})
    assert "description_contains=ACME" in url
    assert "%25" not in url  # no literal percent reached the wire


def test_wire_expands_date_bound_and_encodes_space_as_pct20() -> None:
    url = _wire_query({"created_after": "2026-05-10"})
    # Expanded to the ServiceNow datetime wire form with the space as %20, never '+'.
    assert "2026-05-10%2000" in url
    assert "created_after=2026-05-10+" not in url


def test_wire_before_bound_expands_to_end_of_day() -> None:
    # A date-only *_before must cover the whole boundary day on the live wire,
    # else 'before May 31' silently drops every May-31 ticket carrying a time.
    assert "created_before=2026-05-31%2023%3A59%3A59" in _wire_query(
        {"created_before": "2026-05-31"}
    )
    assert "updated_before=2026-05-31%2023%3A59%3A59" in _wire_query(
        {"updated_before": "2026-05-31"}
    )


def test_wire_reduces_priority_to_leading_int() -> None:
    url = _wire_query({"priority": "1 - Critical"})
    assert "priority=1" in url
    assert "Critical" not in url


def test_wire_forwards_supported_people_and_close_notes() -> None:
    url = _wire_query(
        {
            "close_notes_contains": "cluster",
            "assigned_to": "E7834",
            "resolved_by_name": "Jane Doe (E7834)",
        }
    )
    assert "close_notes_contains=cluster" in url
    assert "assigned_to=E7834" in url
    assert "resolved_by_name" in url


def test_real_get_never_replays_session_cookies() -> None:
    # Root cause of the live "200 but empty body" after idle: a long-lived httpx
    # client replays ServiceNow's session cookies (F5/glide), pinning the request to
    # a recycled session. Each request must go out cookie-free; auth is the Bearer
    # token. This test fails if the cookie jar is ever replayed.
    seen_cookie_headers: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_cookie_headers.append(request.headers.get("cookie"))
        resp = httpx.Response(200, json={"result": {"incidents": [], "result_count": 0}})
        resp.headers["set-cookie"] = "JSESSIONID=stale; Path=/"  # server tries to pin us
        return resp

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    # Pre-seed a stale F5 affinity cookie as if a prior call had set it.
    http_client.cookies.set(
        "BIGipServerpool_finqa", "deadnode", domain="finqa.service-now.com"
    )
    config = ServiceNowConfig(
        mode="real", instance_url="https://finqa.service-now.com", fallback_to_mock=False
    )
    client = ServiceNowClient(config, incidents=[], http_client=http_client)
    asyncio.run(client.list_incidents(filters={}, limit=25, access_token="tok"))
    asyncio.run(client.list_incidents(filters={}, limit=25, access_token="tok"))
    assert seen_cookie_headers == [None, None]


def test_wire_drops_category_and_opened_window() -> None:
    # README §3.2 has no category or opened_* filter: category is an output field
    # and date windows use created_*/updated_*. Neither may reach the live API.
    url = _wire_query(
        {
            "category": "Pipeline",
            "opened_after": "2026-05-01",
            "opened_before": "2026-05-31",
            "updated_after": "2026-05-01",  # supported -> forwarded
        }
    )
    assert "category" not in url
    assert "opened_after" not in url
    assert "opened_before" not in url
    assert "updated_after=2026-05-01%2000" in url


def test_supported_filters_excludes_forbidden_keys() -> None:
    # Filters the FIN instance does not expose (README §3.3) must never reach the
    # allowlist — and neither may record fields that are OUTPUTS, not filters
    # (category, opened_at). cause is exact-match only; date windows are created_*/
    # updated_*.
    for forbidden in (
        "short_description_contains",
        "resolution_notes_contains",
        "configuration_item_contains",
        "cause_contains",
        "probable_cause_contains",
        "solved_by_name",
        "category",  # output / display field + agent classification, NOT a filter
        "opened_after",  # no opened_* filter on /incidents; use created_*/updated_*
        "opened_before",
    ):
        assert forbidden not in SUPPORTED_FILTERS
    assert "close_notes_contains" in SUPPORTED_FILTERS
    # Exact cause keyword is the only cause filter FIN supports.
    assert "cause" in SUPPORTED_FILTERS
    # README §3.2 date filters: created_* / updated_*.
    assert "created_after" in SUPPORTED_FILTERS
    assert "created_before" in SUPPORTED_FILTERS
    assert "updated_after" in SUPPORTED_FILTERS
    assert "updated_before" in SUPPORTED_FILTERS


# -- wire-value helpers --------------------------------------------------------


def test_wire_value_helpers() -> None:
    assert _sanitize_contains("%ACME%") == "ACME"
    assert _sanitize_contains('"core banking"') == "core banking"  # inner space kept
    assert _leading_int("1 - Critical") == "1"
    assert _leading_int("3") == "3"
    assert _normalize_date_wire("2026-05-10") == "2026-05-10 00:00:00"
    assert _normalize_date_wire("2026-05-10", end_of_day=True) == "2026-05-10 23:59:59"
    assert _normalize_date_wire("2026-05-10 08:30:00") == "2026-05-10 08:30:00"


# -- narrowed fallback (NEW-FALLBACK) ------------------------------------------


def test_real_mode_fallback_off_raises_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = ServiceNowConfig(
        mode="real",
        instance_url="https://finqa.service-now.com",
        fallback_to_mock=False,
    )
    client = ServiceNowClient(config, incidents=[], http_client=http_client)
    try:
        asyncio.run(client.list_incidents(filters={"number": "INC0000001"}, access_token="t"))
    except Exception as exc:  # noqa: BLE001 - asserting the type below
        assert type(exc).__name__ == "ServiceNowError"
    else:
        raise AssertionError("expected ServiceNowError when fallback is disabled")


# -- config guards (NEW-PATH / NEW-FALLBACK default) ---------------------------


_GUARD_ENV_KEYS = (
    "APP_ENV",
    "SERVICENOW_MODE",
    "SERVICENOW_INCIDENT_LIST_API_PREFIX",
    "SERVICENOW_FALLBACK_TO_MOCK",
    "SERVICENOW_CLIENT_SECRET",
    "AZURE_KEY_VAULT_URI",
)


def _with_env(**overrides):
    saved = {k: os.environ.get(k) for k in _GUARD_ENV_KEYS}
    for k in _GUARD_ENV_KEYS:
        os.environ.pop(k, None)
    for k, v in overrides.items():
        if v is not None:
            os.environ[k] = v

    def restore() -> None:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    return restore


def test_prod_requires_explicit_incident_prefix() -> None:
    restore = _with_env(APP_ENV="prod", SERVICENOW_MODE="real")
    try:
        try:
            ServiceNowConfig.from_env()
        except ServiceNowConfigurationError as exc:
            assert "INCIDENT_LIST_API_PREFIX" in str(exc)
        else:
            raise AssertionError("expected prod-path guard to raise")
    finally:
        restore()


def test_fallback_default_off_in_prod_on_in_local() -> None:
    restore = _with_env(
        APP_ENV="prod",
        SERVICENOW_MODE="real",
        SERVICENOW_INCIDENT_LIST_API_PREFIX="/api/fini/va_support/incidents",
    )
    try:
        assert ServiceNowConfig.from_env().fallback_to_mock is False
    finally:
        restore()

    restore = _with_env(SERVICENOW_MODE="mock")  # local default
    try:
        assert ServiceNowConfig.from_env().fallback_to_mock is True
    finally:
        restore()


# -- mock-side behaviors -------------------------------------------------------

_PEOPLE_INCIDENT = {
    "number": "INC0000010",
    "short_description": "ledger mismatch",
    "state": {"value": "7", "display_value": "Closed"},
    "category": {"value": "Data Quality", "display_value": "Data Quality"},
    "close_notes": "Rebalanced the cluster and reran the load.",
    "assignment_group": {"value": "grp_sysid_123", "display_value": "OPS - PLATFORM L3"},
    "assigned_to": {"value": "E1594", "display_value": "Alex Doe (E1594)"},
    "resolved_by": {"value": "", "display_value": ""},
}


def _mock_match(filters: dict) -> set[str]:
    client = ServiceNowClient(ServiceNowConfig(mode="mock"), incidents=[_PEOPLE_INCIDENT])
    env = asyncio.run(client.list_incidents(filters=filters, limit=10))
    return {_ticket_base(inc)["ticket_number"] for inc in env["incidents"]}


def test_close_notes_contains_matches_mock() -> None:
    assert _mock_match({"close_notes_contains": "cluster reran"}) == {"INC0000010"}
    assert _mock_match({"close_notes_contains": "nonexistent"}) == set()


def test_category_surfaces_in_ticket_output() -> None:
    assert _ticket_base(_PEOPLE_INCIDENT)["category"] == "Data Quality"


def test_category_not_a_filter_is_ignored() -> None:
    # category is an OUTPUT field, not a filter (README §3.2). Passing it as a filter
    # must NOT constrain the result set — it is silently ignored, matching everything.
    # Pipeline / missing-data narrowing is intentionally agent-side, off this field.
    incidents = [
        {
            "number": "INC_PIPE",
            "short_description": "PL-CB-02 pipeline failed - auth token expired",
            "state": {"value": "2", "display_value": "In Progress"},
            "category": {"value": "Pipeline", "display_value": "Pipeline"},
        },
        {
            "number": "INC_DQ",
            "short_description": "row count mismatch on snapshot_date",
            "state": {"value": "2", "display_value": "In Progress"},
            "category": {"value": "Data Quality", "display_value": "Data Quality"},
        },
    ]
    client = ServiceNowClient(ServiceNowConfig(mode="mock"), incidents=incidents)

    def match(**f) -> set[str]:
        env = asyncio.run(client.list_incidents(filters=f, limit=10))
        return {_ticket_base(i)["ticket_number"] for i in env["incidents"]}

    # Filtering by category does not narrow anything — both incidents come back.
    assert match(category="Pipeline") == {"INC_PIPE", "INC_DQ"}
    # But category still surfaces as an output field for the agent to classify on.
    assert {_ticket_base(i)["category"] for i in incidents} == {"Pipeline", "Data Quality"}


def test_engineer_falls_back_resolved_to_assigned() -> None:
    # resolved_by is empty -> engineer falls back to assigned_to display value.
    assert _ticket_base(_PEOPLE_INCIDENT)["engineer"] == "Alex Doe (E1594)"


def test_assignment_group_comma_split_and_sys_id() -> None:
    assert _mock_match({"assignment_group": "nope,OPS - PLATFORM L3"}) == {"INC0000010"}
    assert _mock_match({"assignment_group": "grp_sysid_123"}) == {"INC0000010"}  # sys_id


# -- tool pagination (NEW-PAGINATION) ------------------------------------------


def test_assigned_to_and_resolved_by_cannot_combine() -> None:
    # The API ANDs filters, so assigned_to + resolved_by in one call means "assigned to
    # X AND resolved by X" (~always empty). The tool rejects it so the agent splits into
    # two searches and unions, instead of getting a silent zero.
    client = ServiceNowClient(ServiceNowConfig(mode="mock"), incidents=[_PEOPLE_INCIDENT])
    previous = tools_module._servicenow_client
    tools_module._servicenow_client = client
    try:
        result = asyncio.run(
            servicenow_list_tickets.ainvoke(
                {"assigned_to": "E1594", "resolved_by_name": "Alex Doe (E1594)"}
            )
        )
    finally:
        tools_module._servicenow_client = previous
    assert result["ok"] is False
    assert result["kind"] == "invalid_input"


def test_ticket_numbers_batch_ignores_status_and_returns_all() -> None:
    # Naming incidents by number is a DIRECT fetch: status is irrelevant, so a CLOSED
    # ticket comes back even though the default is open-only, and both land in ONE call
    # (no per-ticket fan-out, nothing dropped by the default limit).
    incidents = [
        {"number": "INC0000001", "state": {"value": "2", "display_value": "In Progress"}},
        {"number": "INC0000002", "state": {"value": "7", "display_value": "Closed"}},
    ]
    client = ServiceNowClient(ServiceNowConfig(mode="mock"), incidents=incidents)
    previous = tools_module._servicenow_client
    tools_module._servicenow_client = client
    try:
        result = asyncio.run(
            servicenow_list_tickets.ainvoke({"ticket_numbers": "INC0000001,INC0000002"})
        )
    finally:
        tools_module._servicenow_client = previous
    assert result["ok"] is True
    nums = {t["ticket_number"] for t in result["tickets"]}
    assert nums == {"INC0000001", "INC0000002"}  # closed one included despite open default


def test_offset_rejected_with_multiple_statuses() -> None:
    client = ServiceNowClient(ServiceNowConfig(mode="mock"), incidents=[_PEOPLE_INCIDENT])
    previous = tools_module._servicenow_client
    tools_module._servicenow_client = client
    try:
        result = asyncio.run(
            servicenow_list_tickets.ainvoke(
                {"statuses": "new,closed", "offset": 5, "limit": 10}
            )
        )
    finally:
        tools_module._servicenow_client = previous
    assert result["ok"] is False
    assert result["kind"] == "invalid_input"


def test_list_surfaces_offset_and_next_offset() -> None:
    client = ServiceNowClient(ServiceNowConfig(mode="mock"), incidents=[_PEOPLE_INCIDENT])
    previous = tools_module._servicenow_client
    tools_module._servicenow_client = client
    try:
        # Single status -> pageable: next_offset is a usable cursor.
        single = asyncio.run(
            servicenow_list_tickets.ainvoke({"limit": 10, "statuses": "in_progress"})
        )
        # Multi-status (default open = New+In Progress+On Hold) -> not pageable: the
        # per-state fan-out has no shared offset, so next_offset is null.
        multi = asyncio.run(servicenow_list_tickets.ainvoke({"limit": 10}))
    finally:
        tools_module._servicenow_client = previous
    assert single["ok"] is True
    assert single["offset"] == 0
    assert single["next_offset"] == single["count"]
    assert multi["next_offset"] is None


def test_offset_is_record_based_not_page_based() -> None:
    # FIN rule: offset counts RECORDS SKIPPED. Page 2 of a 5-row page is offset=5;
    # offset=2 must overlap page 1 (skips 2 records), never act like "page 2".
    incidents = [
        {**_PEOPLE_INCIDENT, "number": f"INC000010{i}", "state": {"value": "2", "display_value": "In Progress"}}
        for i in range(10)
    ]
    client = ServiceNowClient(ServiceNowConfig(mode="mock"), incidents=incidents)
    previous = tools_module._servicenow_client
    tools_module._servicenow_client = client
    try:
        def _page(offset: int) -> list[str]:
            result = asyncio.run(
                servicenow_list_tickets.ainvoke(
                    {"limit": 5, "offset": offset, "statuses": "in_progress"}
                )
            )
            assert result["ok"] is True
            return [t["ticket_number"] for t in result["tickets"]]

        page1 = _page(0)
        page2 = _page(5)  # = page1's next_offset
        assert len(page1) == 5
        assert set(page1).isdisjoint(page2)  # offset=5 is the NEXT 5 records
        assert _page(2) == page1[2:] + page2[:2]  # offset=2 skips 2 records, not 2 pages
    finally:
        tools_module._servicenow_client = previous


# -- exact cause (full keyword) + updated_at window ----------------------------
#
# cause is EXACT-match (not substring) and has MANY valid keyword values (e.g.
# 'Source timeout', 'Vendor outage', 'Data Availability', 'Network Cluster Issue',
# ...). Date windows use created_*/updated_* (no opened_*, per README §3.2).

_J_INCIDENTS = [
    {
        "number": "INC_TIMEOUT",
        "short_description": "Core Banking ledger ingest failed",
        "state": {"value": "7", "display_value": "Closed"},
        "cause": "Source timeout",
        "close_notes": "ADF copy activity timed out; re-triggered after source recovered.",
        "sys_updated_on": "2026-05-11 17:00:00",
    },
    {
        "number": "INC_AUTH",
        "short_description": "Payments file missing from landing zone",
        "state": {"value": "7", "display_value": "Closed"},
        "cause": "Authentication issue",
        "close_notes": "SFTP job failed due to credential expiry; credentials renewed.",
        "sys_updated_on": "2026-05-16 15:00:00",
    },
    {
        "number": "INC_VENDOR_JUN",
        "short_description": "Debit Card fraud alerts stale",
        "state": {"value": "3", "display_value": "On Hold"},
        "cause": "Vendor outage",
        "sys_updated_on": "2026-06-02 09:14:02",
    },
    {
        "number": "INC_VENDOR_MAR",
        "short_description": "Debit Card POS terminal feed delayed",
        "state": {"value": "7", "display_value": "Closed"},
        "cause": "Vendor outage",
        "sys_updated_on": "2026-03-13 15:00:00",
    },
]


def _j_match(filters: dict) -> set[str]:
    client = ServiceNowClient(ServiceNowConfig(mode="mock"), incidents=_J_INCIDENTS)
    env = asyncio.run(client.list_incidents(filters=filters, limit=50))
    return {_ticket_base(inc)["ticket_number"] for inc in env["incidents"]}


def test_cause_exact_match_works() -> None:
    # The full stored keyword still matches exactly.
    assert _j_match({"cause": "Source timeout"}) == {"INC_TIMEOUT"}
    assert _j_match({"cause": "Authentication issue"}) == {"INC_AUTH"}


def test_cause_partial_match_is_lenient_in_mock_mode() -> None:
    # The LIVE instance is exact-match only, but mock mode is deliberately lenient
    # so a user who supplies only PART of a cause label still surfaces the incident
    # whose stored cause is the full phrase (AND-of-tokens partial match).
    assert _j_match({"cause": "timeout"}) == {"INC_TIMEOUT"}
    assert _j_match({"cause": "authentication"}) == {"INC_AUTH"}
    # Leniency is partial-substring only — an unrelated term still finds nothing.
    assert _j_match({"cause": "banana"}) == set()


def test_loose_failure_term_found_via_close_notes() -> None:
    # When the user gives a loose term, the OR-arm is close_notes_contains:
    # 'credential' lives in INC_AUTH's close notes, not its cause keyword.
    assert _j_match({"close_notes_contains": "credential"}) == {"INC_AUTH"}


def test_updated_window_bounds_cause_search() -> None:
    may = {"updated_after": "2026-05-01", "updated_before": "2026-05-31"}
    # Timeout incident updated in May is returned (exact cause keyword).
    assert _j_match({**may, "cause": "Source timeout"}) == {"INC_TIMEOUT"}
    # Vendor-outage incidents were updated in June + March, both OUTSIDE May -> empty.
    assert _j_match({**may, "cause": "Vendor outage"}) == set()
    # Without the window, the same vendor search returns both.
    assert _j_match({"cause": "Vendor outage"}) == {"INC_VENDOR_JUN", "INC_VENDOR_MAR"}


def test_wire_forwards_cause_and_updated_window() -> None:
    url = _wire_query(
        {"cause": "Source timeout", "updated_after": "2026-05-01"}
    )
    # Exact cause forwarded verbatim (space encoded as %20, never '+').
    assert "cause=Source%20timeout" in url
    assert "cause_contains" not in url
    # Date bound expanded to the wire form with the space as %20, never '+'.
    assert "updated_after=2026-05-01%2000" in url


# -- deep-link ticket_url (§7: sys_id-based, built from the instance origin) ----
#
# The /incidents API returns no usable deep link, so the client constructs one from
# the configured instance origin + each record's sys_id. The sys_id lives ONLY inside
# the URL and is never surfaced as its own field.

_SYS_ID_INCIDENT = {
    "number": "INC3011201",
    "sys_id": "aa01bb02cc03dd04ee05ff06aa07bb08",
    "short_description": "ledger ingest failed",
    "state": {"value": "2", "display_value": "In Progress"},
    # A stale number-based link the fixture/API might carry — must be overridden.
    "ticket_url": (
        "https://finqa.service-now.com/nav_to.do?uri=incident.do"
        "?sysparm_query=number=INC3011201"
    ),
}

_EXPECTED_SYS_ID_URL = (
    "https://finqa.service-now.com/nav_to.do?uri=incident.do"
    "?sys_id=aa01bb02cc03dd04ee05ff06aa07bb08"
)


def test_build_incident_url_pattern() -> None:
    assert (
        build_incident_url("https://finqa.service-now.com", "abc123")
        == "https://finqa.service-now.com/nav_to.do?uri=incident.do?sys_id=abc123"
    )


def test_client_builds_sys_id_ticket_url_overriding_payload() -> None:
    client = ServiceNowClient(
        ServiceNowConfig(mode="mock", instance_url="https://finqa.service-now.com"),
        incidents=[_SYS_ID_INCIDENT],
    )
    env = asyncio.run(client.list_incidents(filters={"number": "INC3011201"}, limit=1))
    incident = env["incidents"][0]
    # The number-based fixture URL is replaced with the sys_id-based deep link.
    assert incident["ticket_url"] == _EXPECTED_SYS_ID_URL
    # The normalized ticket surfaces the deep link but never a bare sys_id field.
    ticket = _ticket_base(incident)
    assert ticket["ticket_url"] == _EXPECTED_SYS_ID_URL
    assert "sys_id" not in ticket


def test_ticket_url_always_built_from_sys_id_even_without_instance_url() -> None:
    # ticket_url is MANDATORY: even a bare mock client with no instance_url falls back
    # to the default mock origin and builds a correct SYS_ID-based deep link, replacing
    # any stale pre-baked URL — never empty, never the stale value. Only the sys_id form
    # resolves in ServiceNow, so the link is always keyed by sys_id (never the number).
    client = ServiceNowClient(ServiceNowConfig(mode="mock"), incidents=[_SYS_ID_INCIDENT])
    env = asyncio.run(client.list_incidents(filters={"number": "INC3011201"}, limit=1))
    url = env["incidents"][0]["ticket_url"]
    assert url == build_incident_url(_DEFAULT_MOCK_ORIGIN, _SYS_ID_INCIDENT["sys_id"])
    assert "sys_id=" in url and "sysparm_query=number" not in url
    assert url != _SYS_ID_INCIDENT["ticket_url"]  # stale link overridden


def test_every_fixture_incident_has_sys_id_ticket_url() -> None:
    # Mandatory-link guarantee across the whole bundled fixture set: every incident
    # carries a sys_id, so every one gets a sys_id-based ticket_url.
    client = ServiceNowClient(ServiceNowConfig(mode="mock"))
    env = asyncio.run(client.list_incidents(filters={}, limit=100))
    assert env["result_count"] >= 1
    for inc in env["incidents"]:
        url = inc.get("ticket_url")
        assert url, f"{inc.get('number')} has no ticket_url"
        assert "sys_id=" in url


def test_real_payload_trusts_api_pagination_flags() -> None:
    # The live API returns result_count/limit/offset/next_offset as FLOATS (25.0) and
    # its own has_more bool; result_count is the PAGE count, not the total. The client
    # must trust the API's has_more/next_offset, not re-derive them (a derived has_more
    # is always False, so the agent would never paginate).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": {
                    "result_count": 25.0,
                    "limit": 25.0,
                    "offset": 200.0,
                    "next_offset": 225.0,
                    "has_more": True,
                    "incidents": [
                        {"number": "INC3326003", "sys_id": "5c232d5bc33c431042df3aec05013115"}
                    ],
                }
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = ServiceNowConfig(
        mode="real", instance_url="https://finqa.service-now.com", fallback_to_mock=False
    )
    client = ServiceNowClient(config, incidents=[], http_client=http_client)
    env = asyncio.run(
        client.list_incidents(filters={"cause": "x"}, limit=25, offset=200, access_token="tok")
    )
    assert env["has_more"] is True  # the bug: was always False before
    assert env["next_offset"] == 225


def test_real_payload_gets_sys_id_ticket_url() -> None:
    # Real mode: the API returns no usable deep link, so the client constructs one.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": {
                    "incidents": [
                        {
                            "number": "INC3240140",
                            "sys_id": "deadbeefdeadbeefdeadbeefdeadbeef",
                        }
                    ],
                    "result_count": 1,
                }
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = ServiceNowConfig(
        mode="real",
        instance_url="https://finqa.service-now.com",
        fallback_to_mock=False,
    )
    client = ServiceNowClient(config, incidents=[], http_client=http_client)
    env = asyncio.run(
        client.list_incidents(
            filters={"number": "INC3240140"}, limit=1, access_token="tok"
        )
    )
    assert (
        env["incidents"][0]["ticket_url"]
        == "https://finqa.service-now.com/nav_to.do?uri=incident.do"
        "?sys_id=deadbeefdeadbeefdeadbeefdeadbeef"
    )


# -- list detail-mode rows eliminate the per-ticket fan-out (NO-FANOUT) ---------
#
# servicenow_get_ticket_detail is itself a number-filtered list_incidents call, so
# the list endpoint already returns every detail field on each record. The list tool
# now surfaces them per row by default (detail=True), so a single list call answers
# list/classify/card questions with NO per-ticket detail fetch. These tests pin that:
# the detail-mode list row carries the full card AND equals what the detail tool
# returns, so a per-result fan-out would be provably redundant.

# A CLOSED incident whose RESOLUTION text is stored under the bundled fixture's
# legacy ``resolution_notes`` key (the live wrapper uses ``close_notes``).
_FANOUT_INCIDENT = {
    "number": "INC0007777",
    "sys_id": "ffeeddccbbaa00112233445566778899",
    "short_description": "Debit Card settlement file missing rows",
    "description": "Debit Card daily settlement landed short by 4,210 records.",
    "state": {"value": "7", "display_value": "Closed"},
    "priority": {"value": "1", "display_value": "1 - Critical"},
    "category": {"value": "Data Quality", "display_value": "Data Quality"},
    "cause": "Data Availability",
    "assigned_to": {"value": "E1594", "display_value": "Alex Doe (E1594)"},
    "resolved_by": {"value": "E1594", "display_value": "Alex Doe (E1594)"},
    "opened_at": "2026-05-10 02:05:11",
    "closed_at": "2026-05-11 17:00:00",
    "resolved_at": "2026-05-11 16:40:00",
    # Stored under resolution_notes (NOT close_notes) on purpose — exercises the fallback.
    "resolution_notes": "Re-ran the settlement ingest after the upstream feed recovered.",
}

# Fields the detail-mode list row must carry that a compact row does NOT — the very
# fields whose absence used to force a per-ticket servicenow_get_ticket_detail call.
_DETAIL_ONLY_FIELDS = (
    "description",
    "close_notes",
    "closed_at",
    "resolved_at",
    "resolved_by",
    "assigned_to",
    "close_code",
)


def _list_with_incident(incident: dict, **kwargs) -> dict:
    """Invoke the list TOOL against a single in-memory incident (closed bucket)."""

    client = ServiceNowClient(ServiceNowConfig(mode="mock"), incidents=[incident])
    previous = tools_module._servicenow_client
    tools_module._servicenow_client = client
    try:
        # statuses='closed' so the closed-bucket fixture is not stripped by the
        # open-only safe default.
        return asyncio.run(
            servicenow_list_tickets.ainvoke({"statuses": "closed", "limit": 10, **kwargs})
        )
    finally:
        tools_module._servicenow_client = previous


def test_list_detail_true_is_the_default() -> None:
    result = _list_with_incident(_FANOUT_INCIDENT)
    assert result["ok"] is True
    assert result["detail"] is True  # default, no flag passed
    assert result["count"] == 1


def test_list_detail_row_carries_full_card_no_fetch_needed() -> None:
    row = _list_with_incident(_FANOUT_INCIDENT)["tickets"][0]
    # Every detail-only field is present on the list row...
    for field in _DETAIL_ONLY_FIELDS:
        assert field in row, f"detail-mode list row missing {field}"
    # ...and the resolution text + timestamps are actually populated, so the agent
    # never needs to fetch the ticket to render its card.
    assert row["description"].startswith("Debit Card daily settlement")
    assert row["close_notes"].startswith("Re-ran the settlement ingest")  # resolution_notes fallback
    assert row["opened_at"] == "2026-05-10 02:05:11 UTC"
    assert row["closed_at"] == "2026-05-11 17:00:00 UTC"
    assert row["resolved_by"] == "Alex Doe (E1594)"


def test_list_detail_row_equals_get_ticket_detail() -> None:
    # The per-ticket fan-out is redundant: the detail-mode list row equals exactly
    # what servicenow_get_ticket_detail returns for the same incident.
    client = ServiceNowClient(ServiceNowConfig(mode="mock"), incidents=[_FANOUT_INCIDENT])
    previous = tools_module._servicenow_client
    tools_module._servicenow_client = client
    try:
        listed = asyncio.run(
            servicenow_list_tickets.ainvoke({"statuses": "closed", "limit": 10})
        )["tickets"][0]
        detailed = asyncio.run(
            servicenow_get_ticket_detail.ainvoke({"ticket_number": "INC0007777"})
        )["ticket"]
    finally:
        tools_module._servicenow_client = previous
    assert listed == detailed


def test_list_detail_false_returns_compact_rows() -> None:
    # The opt-down path for lightweight scans: detail=False omits the heavy fields.
    result = _list_with_incident(_FANOUT_INCIDENT, detail=False)
    assert result["detail"] is False
    row = result["tickets"][0]
    for field in _DETAIL_ONLY_FIELDS:
        assert field not in row, f"compact row should not carry {field}"
    # The compact identifying fields are still present.
    assert row["ticket_number"] == "INC0007777"
    assert row["status"] == "closed"
    # opened_at rides EVERY row (compact included) so "when was it opened" never
    # renders 'Not available' off a detail=False result.
    assert row["opened_at"] == "2026-05-10 02:05:11 UTC"


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
