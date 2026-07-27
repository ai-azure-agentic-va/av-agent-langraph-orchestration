"""Regression tests for the ServiceNow tools and client (PROD_DEPLOYMENT_TODO §2).

Covers the date-boundary fix (SNCLIENT-2), date validation (SNTOOLS-7), the
unified status normalization (SNTOOLS-STATUS), the shared display/plain split
(SNCLIENT-MATCHES), concurrent multi-status fan-out (SNTOOLS-MULTI), and the
promoted env helpers (SNCLIENT-ENVDUP).

Runs standalone (``python test_servicenow.py``) or under pytest — the async
checks are driven through ``asyncio.run`` so no pytest-asyncio plugin is needed.
"""

from __future__ import annotations

import asyncio

from v1.core.tools.servicenow.tools import (
    _STATUS_TO_STATE,
    _canonical_status,
    _validate_date_bound,
    normalize_status,
    servicenow_list_tickets,
)
from v1.core.tools.servicenow.tools import ServiceNowToolInputError
from v1.utils.clients.servicenow import (
    ServiceNowClient,
    ServiceNowConfig,
    _display_and_plain,
    _matches_date_bound,
)
from v1.utils.helper import env_bool, env_float, truthy

# An incident whose timestamps carry a time component, so a date-only bound that
# only compared the date prefix would wrongly drop it.
_INCIDENT = {
    "number": "INC0000001",
    "short_description": "boundary day ticket",
    "state": {"value": "1", "display_value": "New"},
    "priority": {"value": "1", "display_value": "1 - Critical"},
    "sys_created_on": "2026-05-10 02:05:11",
    "sys_updated_on": "2026-05-10 17:00:00",
}


def _mock_client(incidents: list[dict]) -> ServiceNowClient:
    return ServiceNowClient(ServiceNowConfig(mode="mock"), incidents=incidents)


def _matches(filters: dict) -> bool:
    client = _mock_client([_INCIDENT])
    envelope = asyncio.run(client.list_incidents(filters=filters, limit=10))
    return envelope["result_count"] == 1


# -- SNCLIENT-2: date-only *_before must include the whole boundary day --------


def test_date_only_before_includes_boundary_day() -> None:
    # The bug: '2026-05-10 02:05:11' <= '2026-05-10' is False lexically.
    assert _matches({"created_before": "2026-05-10"})
    assert _matches({"updated_before": "2026-05-10"})


def test_date_only_after_includes_boundary_day() -> None:
    assert _matches({"created_after": "2026-05-10"})


def test_before_excludes_earlier_day() -> None:
    assert not _matches({"created_before": "2026-05-09"})


def test_after_excludes_later_day() -> None:
    assert not _matches({"created_after": "2026-05-11"})


def test_exact_datetime_bounds_are_inclusive() -> None:
    assert _matches({"created_before": "2026-05-10 02:05:11"})
    assert not _matches({"created_before": "2026-05-10 02:05:10"})
    assert _matches({"created_after": "2026-05-10 02:05:11"})


def test_matches_date_bound_helper_direct() -> None:
    assert _matches_date_bound("2026-05-10 02:05:11", "2026-05-10", is_before=True)
    assert not _matches_date_bound("2026-05-10 02:05:11", "2026-05-09", is_before=True)
    # Unparsable actual value falls back to lexical compare rather than raising.
    assert _matches_date_bound("not-a-date", "zzzz", is_before=True)


# -- SNTOOLS-7: real strptime validation, not just a digit-shape regex ---------


def test_validate_date_bound_accepts_valid() -> None:
    assert _validate_date_bound("created_before", " 2026-05-10 ") == "2026-05-10"
    assert (
        _validate_date_bound("created_before", "2026-05-10 02:05:11")
        == "2026-05-10 02:05:11"
    )


def test_validate_date_bound_rejects_impossible_calendar_values() -> None:
    for bad in ("2026-13-45", "2026-05-10 99:99:99", "2026-02-30", "garbage"):
        try:
            _validate_date_bound("created_before", bad)
        except ServiceNowToolInputError:
            continue
        raise AssertionError(f"expected rejection for {bad!r}")


# -- SNTOOLS-STATUS: input and output normalizers round-trip -------------------


def test_status_normalizers_round_trip() -> None:
    for canonical, display in _STATUS_TO_STATE.items():
        # closed_state is an input-only alias for state 7; on OUTPUT state 7
        # deliberately canonicalizes to the plain 'closed' users see.
        expected = "closed" if canonical == "closed_state" else canonical
        assert _canonical_status(display) == expected
        assert normalize_status(canonical) == canonical


def test_canonical_status_is_alias_aware() -> None:
    # British spelling served as a display value still canonicalizes correctly.
    assert _canonical_status("Cancelled") == "canceled"
    # Unknown server states degrade to a slug instead of raising.
    assert _canonical_status("Awaiting Info") == "awaiting_info"


# -- SNCLIENT-MATCHES: one display/plain split, consistent for scalars ---------


def test_display_and_plain_split() -> None:
    assert _display_and_plain({"display_value": "1 - Critical", "value": "1"}) == (
        "1 - Critical",
        "1",
    )
    assert _display_and_plain("In Progress") == ("In Progress", "In Progress")
    assert _display_and_plain(None) == ("", "")


def test_priority_filter_matches_either_side() -> None:
    assert _matches({"priority": "1"})
    assert _matches({"priority": "1 - Critical"})
    assert not _matches({"priority": "2"})


# -- SNTOOLS-MULTI: concurrent multi-status fan-out, order preserved -----------


def test_multi_status_list_merges_and_preserves_order() -> None:
    incidents = [
        {**_INCIDENT, "number": "INC0000001", "state": {"value": "1", "display_value": "New"}},
        {
            **_INCIDENT,
            "number": "INC0000002",
            "state": {"value": "7", "display_value": "Closed"},
        },
    ]
    client = _mock_client(incidents)

    import v1.core.tools.servicenow.tools as tools_module

    previous = tools_module._servicenow_client
    tools_module._servicenow_client = client
    try:
        result = asyncio.run(
            servicenow_list_tickets.ainvoke({"statuses": "new,closed", "limit": 10})
        )
    finally:
        tools_module._servicenow_client = previous

    assert result["ok"] is True
    numbers = [ticket["ticket_number"] for ticket in result["tickets"]]
    assert numbers == ["INC0000001", "INC0000002"]


def test_all_status_fetches_every_state_not_just_open() -> None:
    # Team rule: statuses='all' returns EVERY state (open + closed), never silently
    # narrowed to open. Contrast: omitting statuses stays open-only.
    incidents = [
        {**_INCIDENT, "number": "OPEN-1", "state": {"value": "1", "display_value": "New"}},
        {**_INCIDENT, "number": "RES-1", "state": {"value": "6", "display_value": "Resolved"}},
        {**_INCIDENT, "number": "CLO-1", "state": {"value": "7", "display_value": "Closed"}},
    ]
    import v1.core.tools.servicenow.tools as tools_module

    def _list(**kwargs) -> set[str]:
        previous = tools_module._servicenow_client
        tools_module._servicenow_client = _mock_client(incidents)
        try:
            result = asyncio.run(servicenow_list_tickets.ainvoke({"limit": 10, **kwargs}))
        finally:
            tools_module._servicenow_client = previous
        assert result["ok"] is True
        return {t["ticket_number"] for t in result["tickets"]}

    assert _list(statuses="all") == {"OPEN-1", "RES-1", "CLO-1"}  # every state
    assert _list() == {"OPEN-1"}  # safe default unchanged: open-only


def test_misaligned_offset_rejected() -> None:
    # A legitimate next_offset is always a multiple of the page size; offset=25
    # with 10-row pages is model-invented and would silently skip records.
    client = _mock_client([_INCIDENT])

    import v1.core.tools.servicenow.tools as tools_module

    previous = tools_module._servicenow_client
    tools_module._servicenow_client = client
    try:
        bad = asyncio.run(servicenow_list_tickets.ainvoke({"statuses": "closed_state", "offset": 25}))
        good = asyncio.run(servicenow_list_tickets.ainvoke({"statuses": "closed_state", "offset": 20}))
    finally:
        tools_module._servicenow_client = previous

    assert bad["ok"] is False and bad["kind"] == "invalid_input" and "next_offset" in bad["error"]
    assert good["ok"] is True


def test_multi_status_pagination_cursor_round_trips() -> None:
    # Multi-state results page via a per-state cursor next_offset ('new:5,...'):
    # passed back verbatim it yields the NEXT rows — no repeats, nothing skipped —
    # while a model-invented integer offset on a multi-state query stays rejected.
    incidents = [
        {**_INCIDENT, "number": f"NEW-{i}", "state": {"value": "1", "display_value": "New"}}
        for i in range(6)
    ] + [
        {
            **_INCIDENT,
            "number": f"WIP-{i}",
            "state": {"value": "2", "display_value": "In Progress"},
        }
        for i in range(6)
    ]
    import v1.core.tools.servicenow.tools as tools_module

    def _list(payload: dict) -> dict:
        previous = tools_module._servicenow_client
        tools_module._servicenow_client = _mock_client(incidents)
        try:
            return asyncio.run(servicenow_list_tickets.ainvoke(payload))
        finally:
            tools_module._servicenow_client = previous

    page1 = _list({"statuses": "new,in_progress", "limit": 10})
    assert page1["ok"] is True and page1["count"] == 10 and page1["has_more"] is True
    cursor = page1["next_offset"]
    assert isinstance(cursor, str) and "new:" in cursor and "in_progress:" in cursor

    page2 = _list({"statuses": "new,in_progress", "limit": 10, "offset": cursor})
    assert page2["ok"] is True and page2["has_more"] is False

    first = {t["ticket_number"] for t in page1["tickets"]}
    second = {t["ticket_number"] for t in page2["tickets"]}
    assert not first & second, "cursor page repeated rows"
    assert first | second == {f"NEW-{i}" for i in range(6)} | {
        f"WIP-{i}" for i in range(6)
    }, "cursor paging skipped rows"

    bad = _list({"statuses": "new,in_progress", "offset": 10})
    assert bad["ok"] is False and bad["kind"] == "invalid_input"

    drifted = _list({"statuses": "new,on_hold", "offset": "new:5,in_progress:5"})
    assert drifted["ok"] is False and drifted["kind"] == "invalid_input"


def test_limit_above_default_is_clamped() -> None:
    # HARD ENFORCEMENT: page size is deployment-controlled (SERVICENOW_DEFAULT_LIMIT),
    # never model-controlled — the model kept passing limit=25 despite the prompt.
    from v1.core.tools.servicenow.tools import DEFAULT_TICKET_LIMIT, validate_ticket_limit

    assert validate_ticket_limit(None) == DEFAULT_TICKET_LIMIT
    assert validate_ticket_limit(25) == DEFAULT_TICKET_LIMIT
    assert validate_ticket_limit(1) == 1


# -- SNCLIENT-ENVDUP: promoted env helpers ------------------------------------


def test_env_helpers(monkeypatch=None) -> None:
    import os

    assert truthy("Yes") is True
    assert truthy("nope") is False
    assert truthy(None) is False

    os.environ.pop("SN_TEST_FLAG", None)
    assert env_bool("SN_TEST_FLAG", default=True) is True
    os.environ["SN_TEST_FLAG"] = "on"
    assert env_bool("SN_TEST_FLAG", default=False) is True
    os.environ["SN_TEST_FLAG"] = ""
    assert env_bool("SN_TEST_FLAG", default=True) is False
    os.environ.pop("SN_TEST_FLAG", None)

    os.environ.pop("SN_TEST_FLOAT", None)
    assert env_float("SN_TEST_FLOAT", default=20.0) == 20.0
    os.environ["SN_TEST_FLOAT"] = "not-a-float"
    assert env_float("SN_TEST_FLOAT", default=20.0) == 20.0
    os.environ["SN_TEST_FLOAT"] = "1.5"
    assert env_float("SN_TEST_FLOAT", default=20.0) == 1.5
    os.environ.pop("SN_TEST_FLOAT", None)


# -- SNCLIENT-TOTALCI: total_count, *_contains people filters, cmdb_ci ---------


def test_total_count_contains_filters_and_ci() -> None:
    """The three 2026-07-27 live-instance additions, end to end.

    total_count is the ONLY honest source for "how many match" (result_count is
    just the page), the ``*_contains`` people filters must match a first OR last
    name alone, and CI must be read from the live ``cmdb_ci`` field as well as the
    mock fixture's ``configuration_item``.
    """

    from v1.core.tools.servicenow.tools import _ticket_base, normalize_ticket_list

    real_payload = {
        "result": {
            "total_count": 356.0,  # float on the wire
            "result_count": 2.0,  # PAGE count only
            "next_offset": 2.0,
            "has_more": True,
            "incidents": [
                {
                    "sys_id": "a1",
                    "number": "INC0000001",
                    "cmdb_ci": {"value": "x", "display_value": "PL-500-COPY_SESSION_REQUEST"},
                },
                {"sys_id": "a2", "number": "INC0000002"},
            ],
        }
    }
    real = ServiceNowClient(ServiceNowConfig(mode="real"))._envelope_from_real_payload(
        real_payload, limit=2, offset=0
    )
    assert real["total_count"] == 356
    assert real["result_count"] == 2  # page count, unchanged

    # Mock: the matched set IS the total, so a short page still reports the total.
    fixture = [
        {
            "sys_id": str(i),
            "number": f"INC000000{i}",
            "state": {"value": "1", "display_value": "New"},
            "assigned_to": {"value": "sid", "display_value": "Dion Okafor (F4678)"},
            "configuration_item": {"value": "ci", "display_value": "ASL"},
        }
        for i in range(7)
    ]
    client = ServiceNowClient(ServiceNowConfig(mode="mock"), incidents=fixture)
    page = asyncio.run(
        client.list_incidents(filters={"assigned_to_contains": "okafor"}, limit=3)
    )
    assert page["total_count"] == 7 and len(page["incidents"]) == 3

    # First name alone matches; the filter stays scoped to its own people field.
    assert (
        asyncio.run(client.list_incidents(filters={"assigned_to_contains": "Dion"}))[
            "total_count"
        ]
        == 7
    )
    assert (
        asyncio.run(client.list_incidents(filters={"resolved_by_contains": "Dion"}))[
            "total_count"
        ]
        == 0
    )

    # CI: live cmdb_ci and mock configuration_item both surface.
    assert (
        _ticket_base(real_payload["result"]["incidents"][0])["configuration_item"]
        == "PL-500-COPY_SESSION_REQUEST"
    )
    assert _ticket_base(fixture[0])["configuration_item"] == "ASL"

    # total_count survives to the tool payload and is distinct from the page count.
    out = normalize_ticket_list([], statuses=("new",), limit=10, total_count=356)
    assert out["total_count"] == 356 and out["count"] == 0

    # ci is NOT a filter: it must be dropped rather than forwarded to the live API.
    from v1.utils.clients.servicenow import SUPPORTED_FILTERS

    assert "ci" not in SUPPORTED_FILTERS

    # *_contains is an ADDITION, not a replacement: all four people forms stay wired,
    # with different match semantics (substring vs exact 'Name (CODE)').
    for key in ("assigned_to_contains", "resolved_by_contains", "assigned_to_name", "resolved_by_name"):
        assert key in SUPPORTED_FILTERS, key
    assert SUPPORTED_FILTERS["assigned_to_contains"].kind == "contains"
    assert SUPPORTED_FILTERS["assigned_to_name"].kind == "passthrough"


def test_pagination_never_repeats_a_row_across_pages() -> None:
    """Two ways page 2 used to re-serve rows from page 1.

    1. next_offset advanced by rows SHOWN, but the merge passes over duplicate and
       wrong-state rows, so it under-counted what was actually read and rewound into
       the previous page.
    2. The wrapper pages a LIVE result set by raw record offset: an incident created
       between two calls shifts every later record down a slot, so the same offset
       hands back a row the previous page already showed.
    """

    import asyncio

    from v1.core.tools.servicenow.tools import servicenow_list_tickets

    def record(number: int, state: str) -> dict[str, str]:
        return {
            "number": f"INC{number}",
            "state": state,
            "short_description": f"row {number}",
            "priority": "3 - Moderate",
        }

    def install(pages) -> object:
        class _Stub:
            async def list_incidents(self, *, filters=None, limit=10, offset=0):
                rows = pages(str((filters or {}).get("state")), offset, limit)
                return {
                    "ok": True,
                    "mode": "real",
                    "incidents": rows,
                    "result_count": len(rows),
                    "total_count": 100,
                    "has_more": True,
                    "next_offset": offset + limit,
                    "degraded": False,
                }

        stub = _Stub()
        tools_module.get_servicenow_client = lambda: asyncio.sleep(0, result=stub)
        return stub

    import v1.core.tools.servicenow.tools as tools_module

    original_client = tools_module.get_servicenow_client
    page = servicenow_list_tickets.coroutine
    try:
        # (1) The state backstop drops one row of a full page. The next offset must
        # step past every row READ (10), not just the 9 rendered.
        install(lambda state, offset, limit: [
            record(3000 + i, "closed" if i == 4 else "new")
            for i in range(offset, offset + limit)
        ])
        first = asyncio.run(page(statuses=["new"], limit=10))
        assert first["count"] == 9, first["count"]
        assert str(first["next_offset"]).split("|")[0] == "10", first["next_offset"]
        second = asyncio.run(page(statuses=["new"], limit=10, offset=first["next_offset"]))
        assert second.get("ok"), second
        assert not _numbers(first) & _numbers(second)

        # (2) A new incident lands at the front of 'new' between the two pages, so
        # every later record shifts down one. The cursor carries page 1's numbers, so
        # the straddling row is skipped instead of shown twice.
        shift = {"rows": 0}
        base = {"1": 1000, "2": 2000, "3": 3000}
        canonical = {"1": "new", "2": "in_progress", "3": "on_hold"}

        def shifting(state, offset, limit):
            rows = []
            for i in range(offset, offset + limit):
                index = i - (shift["rows"] if state == "1" else 0)
                rows.append(
                    record(base[state] + index, canonical[state])
                    if index >= 0
                    else record(9999, "new")
                )
            return rows

        install(shifting)
        first = asyncio.run(page(limit=6))
        shift["rows"] = 1
        second = asyncio.run(page(limit=6, offset=first["next_offset"]))
        assert _numbers(first) and _numbers(second)
        assert not _numbers(first) & _numbers(second), sorted(
            _numbers(first) & _numbers(second)
        )
    finally:
        tools_module.get_servicenow_client = original_client


def _numbers(payload: dict) -> set[str]:
    return {ticket["ticket_number"] for ticket in payload["tickets"]}


def test_timestamps_carry_no_timezone_label() -> None:
    """Incident timestamps render BARE — the UI converts them to the viewer's zone.

    User report: "sometimes the UI still shows UTC next to the timestamps". The
    label was appended in code and then preserved by three prompt layers, so this
    pins both halves: the tool emits none, and no layer asks for one back.
    """

    from pathlib import Path

    from v1.core.tools.servicenow.tools import (
        _incident_timestamp,
        normalize_ticket_detail,
    )

    # Stripped whether or not the record already carries a marker.
    assert _incident_timestamp("2026-05-10 17:00:00") == "2026-05-10 17:00:00"
    assert _incident_timestamp("2026-05-10 17:00:00 UTC") == "2026-05-10 17:00:00"
    assert _incident_timestamp("2026-05-10 17:00:00 utc") == "2026-05-10 17:00:00"
    assert _incident_timestamp("  ") is None
    assert _incident_timestamp("UTC") is None

    client = ServiceNowClient(
        ServiceNowConfig(mode="mock", instance_url="https://example.service-now.com")
    )
    envelope = asyncio.run(client.list_incidents(limit=10))
    stamps = ("opened_at", "updated_at", "resolved_at", "closed_at")
    seen = 0
    for incident in envelope["incidents"]:
        ticket = normalize_ticket_detail(incident)["ticket"]
        for key in stamps:
            value = ticket.get(key)
            if value is None:
                continue
            seen += 1
            assert not value.upper().endswith("UTC"), f"{key}: {value}"
    assert seen, "fixture produced no timestamps to check"

    # The prompt layers must not ask the model to put the label back.
    root = Path(__file__).resolve().parents[4]
    for relative in (
        "v1/core/prompts/servicenow.py",
        "v1/core/prompts/orchestrator.py",
        "v1/core/skills/message-formatting/SKILL.md",
    ):
        text = (root / relative).read_text(encoding="utf-8")
        for banned in ("UTC marker intact", "never drop the time or", "and UTC timestamps"):
            assert banned not in text, f"{relative}: still mandates a UTC label ({banned})"


def test_ticket_url_present_on_every_view() -> None:
    """ticket_url rides EVERY normalized shape, including the single-ticket detail.

    User report: "the incident URL is missing on summarize queries". A summary is
    rendered from the DETAIL payload, so this pins the data half of the invariant —
    the link must never be the thing that distinguishes one view from another.
    """

    from pathlib import Path

    from v1.core.tools.servicenow.tools import (
        normalize_ticket_detail,
        normalize_ticket_list,
    )

    client = ServiceNowClient(
        ServiceNowConfig(mode="mock", instance_url="https://example.service-now.com")
    )
    envelope = asyncio.run(client.list_incidents(limit=5))
    incidents = envelope["incidents"]
    assert incidents, "mock fixture returned no incidents"

    def expect(url: object, where: str) -> None:
        assert isinstance(url, str) and url, f"{where}: missing ticket_url ({url!r})"
        # sys_id-based deep link — the only form that resolves in ServiceNow.
        assert "sys_id=" in url, f"{where}: not a sys_id deep link ({url})"

    detail = normalize_ticket_detail(incidents[0])
    expect(detail["ticket"].get("ticket_url"), "ticket_detail")

    for is_detail in (True, False):
        listed = normalize_ticket_list(
            incidents, statuses=("new",), limit=5, detail=is_detail
        )
        for ticket in listed["tickets"]:
            expect(ticket.get("ticket_url"), f"ticket_list(detail={is_detail})")

    # Render half: the SUMMARY view is defined by SUBTRACTION from the full card and
    # sits under an aggressive "omit empty fields" rule, so unless each layer names
    # the link on the summary path the model drops it there and only there.
    root = Path(__file__).resolve().parents[4]
    for relative, marker in (
        ("v1/core/prompts/servicenow.py", "SUMMARY or the FULL CARD"),
        ("v1/core/prompts/orchestrator.py", "any single-incident summary"),
        ("v1/core/skills/message-formatting/SKILL.md", "a summary OR a full-details"),
    ):
        text = (root / relative).read_text(encoding="utf-8")
        assert marker in text, f"{relative}: lost the summary ticket_url mandate"


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
