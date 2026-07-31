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
            "assigned_to": {"value": "sid", "display_value": "Dion Nakamura (F4678)"},
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


def test_lost_cursor_resumes_instead_of_restarting() -> None:
    """The intermittent duplicate: paging resumed with NO offset re-serves page 1.

    next_offset exists only inside a tool-result body, and ContextEditingMiddleware
    clears older tool-result bodies to a placeholder — so on a later turn the model
    no longer sees the cursor. Restarting from the beginning then shows the SAME
    incidents again. The incident NUMBERS survive (they are in the model's own
    answers), and offset='0|INC…,INC…' resumes from them with no new machinery.
    """

    import asyncio

    import v1.core.tools.servicenow.tools as tools_module
    from v1.core.tools.servicenow.tools import _STATUS_TO_STATE, servicenow_list_tickets

    states = ("new", "in_progress", "on_hold")
    by_code = {str(_STATUS_TO_STATE[s]): s for s in states}
    rows = {s: [f"INC{s[:2].upper()}{i:03d}" for i in range(30)] for s in states}

    class _Stub:
        async def list_incidents(self, *, filters=None, limit=10, offset=0):
            state = by_code[str((filters or {}).get("state"))]
            window = rows[state][offset : offset + limit]
            return {
                "ok": True,
                "mode": "real",
                "incidents": [
                    {
                        "number": n,
                        "state": state,
                        "short_description": f"row {n}",
                        "priority": "3 - Moderate",
                    }
                    for n in window
                ],
                "result_count": len(window),
                "total_count": len(rows[state]),
                "has_more": offset + limit < len(rows[state]),
                "next_offset": offset + limit,
                "degraded": False,
            }

    original_client = tools_module.get_servicenow_client
    page = servicenow_list_tickets.coroutine
    try:
        stub = _Stub()
        tools_module.get_servicenow_client = lambda: asyncio.sleep(0, result=stub)

        first = asyncio.run(page(statuses=list(states), limit=10))
        shown = _numbers(first)
        assert len(shown) == 10, shown
        shown_csv = ",".join(sorted(shown))

        # The failure this guards against: no offset = every row served again.
        restarted = asyncio.run(page(statuses=list(states), limit=10))
        assert _numbers(restarted) == shown, "stub must be deterministic for this check"

        # The recovery: skip what was already shown, resume after it.
        resumed = asyncio.run(
            page(statuses=list(states), limit=10, offset="0|" + shown_csv)
        )
        assert resumed.get("ok"), resumed
        assert _numbers(resumed), "recovery returned an empty page"
        assert not (set(shown) & set(_numbers(resumed))), (
            f"lost-cursor recovery repeated rows: {sorted(set(shown) & set(_numbers(resumed)))}"
        )

        # Two lost pages still resume without repeating.
        second = asyncio.run(page(statuses=list(states), limit=10, offset=first["next_offset"]))
        both = shown | _numbers(second)
        deep = asyncio.run(
            page(statuses=list(states), limit=10, offset="0|" + ",".join(sorted(both)))
        )
        assert not (both & _numbers(deep)), _numbers(deep)
    finally:
        tools_module.get_servicenow_client = original_client

    # Both layers must keep pointing the model at the resume form; if either drops
    # it, the model falls back to a bare re-run and the duplicates come straight back.
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    prompt = (root / "v1/core/prompts/servicenow.py").read_text(encoding="utf-8")
    assert "LOST CURSOR" in prompt and "0|INC" in prompt, "prompt lost the resume rule"
    tools_src = (root / "v1/core/tools/servicenow/tools.py").read_text(encoding="utf-8")
    assert "LOST IT?" in tools_src, "offset schema lost the resume instruction"
    # The subagent is stateless per delegation (deepagents starts it with ONLY the
    # task text), so the already-shown numbers can reach it ONLY if the orchestrator
    # puts them there. Without this half the subagent rule above is unreachable.
    orchestrator = (root / "v1/core/prompts/orchestrator.py").read_text(encoding="utf-8")
    assert "Already shown:" in orchestrator, "orchestrator lost the paging handoff"


def test_meta_words_stripped_from_content_filters_only() -> None:
    """Query-type words never reach the wire; content words and names survive.

    The wrapper ANDs every word of a contains value, so a stray 'data source' or
    'incidents' demands the description contain it too and returns a false zero.
    """

    from v1.core.tools.servicenow.tools import _build_field_filters, strip_meta_words

    for raw, expected in (
        ("crm data source", "crm"),
        ("incidents related to core banking", "core banking"),
        ("open incidents for tsys", "open tsys"),  # status words are NOT ours to strip
        # Real content words a regex must leave alone, or the documented cluster and
        # missing-data recipes silently change meaning.
        ("cluster issue", "cluster issue"),
        ("missing records", "missing records"),
        # Stripping to empty would turn a real subject into a match-everything filter.
        ("incidents", "incidents"),
    ):
        assert strip_meta_words(raw) == expected, f"{raw!r} -> {strip_meta_words(raw)!r}"

    kwargs = dict.fromkeys(
        (
            "description_contains",
            "close_notes_contains",
            "cause",
            "assigned_to",
            "resolved_by",
            "assigned_to_contains",
            "resolved_by_contains",
            "assigned_to_name",
            "resolved_by_name",
            "assignment_group",
            "priority",
            "created_after",
            "created_before",
            "updated_after",
            "updated_before",
            "ticket_numbers",
        )
    )
    built = _build_field_filters(
        **{
            **kwargs,
            "description_contains": "incidents for tsys",
            # A group name is taken verbatim — 'for' inside a name is not a meta-word.
            "assignment_group": "Data for Payments",
        }
    )
    assert built["description_contains"] == "tsys", built
    assert built["assignment_group"] == "Data for Payments", built


def test_every_built_filter_is_wired_by_the_client() -> None:
    """A filter this module knows but the deployed client does not must RAISE.

    The client drops an unknown key with a log line and runs the query anyway, so
    the caller would get UNFILTERED rows presented as filtered — a silent wrong
    answer. Guards the partial-merge case where tools.py is newer than the client.
    """

    from v1.core.tools.servicenow.tools import _build_field_filters
    from v1.utils.clients.servicenow import SUPPORTED_FILTERS

    kwargs = dict.fromkeys(
        (
            "description_contains",
            "close_notes_contains",
            "cause",
            "assigned_to",
            "resolved_by",
            "assigned_to_contains",
            "resolved_by_contains",
            "assigned_to_name",
            "resolved_by_name",
            "assignment_group",
            "priority",
            "created_after",
            "created_before",
            "updated_after",
            "updated_before",
            "ticket_numbers",
        )
    )

    # Every filter the tool can build today is wired — no false alarm on the happy path.
    built = _build_field_filters(
        **{
            **kwargs,
            "description_contains": "tsys",
            "assigned_to_contains": "Nakamura",
            "priority": "1",
            "created_after": "2026-01-01",
            "ticket_numbers": "INC0000001",
        }
    )
    assert built and not set(built) - set(SUPPORTED_FILTERS), built

    # Simulate the stale client: drop a key the tool still sends.
    removed = SUPPORTED_FILTERS.pop("assigned_to_contains")
    try:
        raised = False
        try:
            _build_field_filters(**{**kwargs, "assigned_to_contains": "Nakamura"})
        except ServiceNowToolInputError as exc:
            raised = "assigned_to_contains" in str(exc)
        assert raised, "a filter the client cannot wire must raise, not be dropped"
    finally:
        SUPPORTED_FILTERS["assigned_to_contains"] = removed


def test_list_row_is_rendered_by_the_backend_not_the_prompt() -> None:
    """The LIST ROW shape is code, and the prompts only say 'print it'.

    It used to be an English recipe written out twice (subagent prompt +
    message-formatting skill) and the copies drifted — one grew a **CI:** segment
    the other forbade, so CI rendered intermittently. Pin the exact string here:
    if anyone changes the shape, this fails instead of the users noticing.
    """

    from pathlib import Path

    from v1.core.tools.servicenow.tools import _ticket_base

    incident = {
        "number": "inc0001201",
        "short_description": "(AutoTkt) PL-CB-04 failure in ADF",
        "state": {"value": "2", "display_value": "In Progress"},
        "priority": {"value": "1", "display_value": "1 - Critical"},
        "assigned_to": {"value": "B2002", "display_value": ""},
        "resolved_by": {"value": "B2002", "display_value": "Jordan Blake (B2002)"},
        "configuration_item": {"value": "pl", "display_value": "PL-CB-04-LEDGER"},
        "ticket_url": "https://example.service-now.com/x?sys_id=abc",
    }
    assert _ticket_base(incident)["row"] == (
        "[INC0001201](https://example.service-now.com/x?sys_id=abc) — "
        "(AutoTkt) PL-CB-04 failure in ADF — **State:** In Progress — "
        "**Priority:** P1 - Critical — **Assigned to:** Jordan Blake (B2002) — "
        "**CI:** PL-CB-04-LEDGER"
    )

    # Empty segments drop out entirely — never 'Not available' on a list row.
    bare = _ticket_base({**incident, "configuration_item": None, "resolved_by": None})
    assert bare["row"].endswith("**Priority:** P1 - Critical"), bare["row"]

    # ...and both prompt copies must stay recipe-free, or the drift comes back.
    prompts = Path(__file__).resolve().parents[3] / "core"
    subagent = (prompts / "prompts" / "servicenow.py").read_text(encoding="utf-8")
    skill = (
        prompts / "skills" / "message-formatting" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "`row` field VERBATIM" in subagent
    for stale in ("**State:** <state>", "P<n> - <label>"):
        assert stale not in subagent and stale not in skill, stale


def test_raw_uris_are_fenced_so_nothing_half_linkifies() -> None:
    """A storage URI in incident text must never come back partly hyperlinked.

    Live bug: 'abfss://mft-fa-outbound@host.dfs.core.windows.net/Cases/' rendered
    with only the email-shaped middle in an anchor — the scheme and the trailing
    path sat outside the link, so the user saw one URI in three pieces and the
    link went nowhere. Fencing it stops the renderer's auto-linker cold.
    """

    from v1.core.tools.servicenow.tools import _shield_uris, normalize_ticket_detail

    uri = "abfss://mft-fa-outbound@dtprodeusdistadls.dfs.core.windows.net/Cases/"
    assert _shield_uris(f"files into the {uri} .") == f"files into the `{uri}` ."
    # Sentence punctuation stays OUTSIDE the fence, so the prose still reads right.
    assert _shield_uris(f"see {uri}.") == f"see `{uri}`."
    # Already-fenced text is left alone rather than double-wrapped.
    assert _shield_uris(f"see `{uri}`") == f"see `{uri}`"
    assert _shield_uris("no uri here") == "no uri here"

    # ...and it is applied on every free-text field the user actually sees.
    ticket = normalize_ticket_detail(
        {
            "number": "INC0001501",
            "short_description": f"eCRM outbound files into the {uri}",
            "description": f"alerts missing from {uri}alert_no_case",
            "close_notes": f"reran the copy into {uri}",
            "state": {"value": "2", "display_value": "In Progress"},
            "priority": {"value": "4", "display_value": "4 - Low"},
        }
    )["ticket"]
    for field in ("short_description", "description", "close_notes", "row"):
        assert f"`{uri}" in ticket[field], f"{field} left a URI unfenced"


def test_missing_data_recipe_fetches_both_causes_and_splits_them() -> None:
    """"Missing data" fetches by SUBJECT, then splits the umbrella agent-side.

    Live bug: the SAME question answered two ways on two runs — once listing ten
    'Failed DQ process for N rules' tickets AS missing data, once listing only the
    one true missing-data ticket and dropping the rest in silence.

    Fetching by cause would be the tidy split, but cause is BLANK on many records
    and the API ANDs filters, so cause='Data Quality' + description_contains=<x>
    returns nothing for exactly the tickets being sought. Subject is the only
    reliable fetch; the umbrella is separated afterwards, by reading the rows.
    """

    from pathlib import Path

    from v1.core.tools.servicenow.tools import VALID_CAUSES

    # Named in the recipe as a confirming hint, so it must still be a real value.
    assert "Data Availability" in VALID_CAUSES

    recipe = (
        Path(__file__).resolve().parents[3] / "core" / "prompts" / "servicenow.py"
    ).read_text(encoding="utf-8")
    recipe = recipe[recipe.index("- Missing-data records for a dataset:") :]
    recipe = recipe[: recipe.index("- Cluster issues")]
    for required in (
        "do\n  NOT filter on cause",  # blank cause must never gate the fetch
        "description_contains=<data\n  source>",  # subject IS the fetch
        "TWO groups",  # umbrella split after the fetch, not during it
        "NEVER merge",
        "NEVER let\n  group 2 stand IN PLACE OF group 1",
        "NEVER drop group 2",
    ):
        assert required in recipe, f"missing-data recipe lost: {required!r}"


def test_summary_card_is_rendered_by_the_backend_not_the_prompt() -> None:
    """Four runs of one "summarize INC…" produced four layouts. Pin the shape.

    The SUMMARY spec was an English recipe in THREE places (subagent prompt,
    message-formatting skill, orchestrator) and the skill copy sits behind an
    OPTIONAL read_file — so whether the model saw it varied per run. Observed
    drift on the SAME incident minutes apart: different field sets, a raw
    'in_progress' slug, and one run with the number as bold text and no link.
    """

    from pathlib import Path

    from v1.core.tools.servicenow.tools import normalize_ticket_detail

    incident = {
        "number": "inc0001401",
        "short_description": "asl.Electronic_Journal_Detail_VW shows NA values",
        "state": {"value": "2", "display_value": "In Progress"},
        "priority": {"value": "1", "display_value": "1 - Critical"},
        "category": "Data Quality",
        "assigned_to": {
            "value": "A1001",
            "display_value": "Alex Carter (A1001)",
        },
        "cmdb_ci": {"value": "db", "display_value": "Databricks"},
        "opened_at": "2025-06-17 13:42:30",
        "ticket_url": "https://example.service-now.com/x?sys_id=abc",
    }
    summary = normalize_ticket_detail(incident)["ticket"]["summary"]
    assert summary == (
        "[INC0001401](https://example.service-now.com/x?sys_id=abc) — "
        "asl.Electronic_Journal_Detail_VW shows NA values\n"
        "- **Priority:** 1 - Critical\n"
        "- **State:** In Progress\n"
        "- **Category:** Data Quality\n"
        "- **Assigned to:** Alex Carter (A1001)\n"
        "- **Opened at:** 2025-06-17 13:42:30\n"
        "- **Configuration item:** Databricks"
    ), summary
    # The link is the field users lost most often — a bold number is the bug.
    assert summary.startswith("[INC0001401](http"), summary
    # Absent fields vanish; an open incident carries no resolved/closed rows and
    # never a 'Not available' placeholder (that belongs to the FULL CARD only).
    for absent in ("Assignment group", "Cause", "Resolved at", "Closed at", "Not available"):
        assert absent not in summary, f"summary padded an empty field: {absent}"

    # Once resolved, the owner line flips to the resolver and the times appear.
    closed = normalize_ticket_detail(
        {
            **incident,
            "state": {"value": "6", "display_value": "Resolved"},
            "resolved_by": {"value": "B2002", "display_value": "Jordan Blake (B2002)"},
            "resolved_at": "2026-07-30 09:00:00",
            "closed_at": "2026-07-31 09:00:00",
        }
    )["ticket"]["summary"]
    assert "- **State:** Resolved" in closed, closed
    assert "- **Resolved by:** Jordan Blake (B2002)" in closed, closed
    assert "- **Assigned to:**" not in closed, closed
    assert "- **Resolved at:** 2026-07-30 09:00:00" in closed, closed

    # All THREE layers must stay recipe-free, or the per-run drift comes back.
    core = Path(__file__).resolve().parents[3] / "core"
    subagent = (core / "prompts" / "servicenow.py").read_text(encoding="utf-8")
    skill = (core / "skills" / "message-formatting" / "SKILL.md").read_text(encoding="utf-8")
    orchestrator = (core / "prompts" / "orchestrator.py").read_text(encoding="utf-8")
    assert "`summary` field VERBATIM" in subagent, "subagent stopped naming the field"
    assert "CHARACTER-FOR-CHARACTER" in skill, "skill lost the verbatim mandate"
    # The orchestrator must stand alone: the skill is an OPTIONAL read_file, so a
    # rule that lives only there is a rule the model may never load.
    assert "handed you WINS" in orchestrator, "orchestrator still defers to the skill"
    # The old English recipe must not survive anywhere and re-diverge from the code.
    assert "minus the two verbatim text blocks" not in subagent, (
        "subagent regrew the summary field-list recipe"
    )


def test_missing_total_is_reported_as_unknown_not_faked() -> None:
    """The disappearing count: a state with no reported total became its PAGE size.

    Real mode's result_count is only the rows on THIS page, so the old
    ``result_count if total_count is None`` fallback laundered "unknown" into a
    number that reads as authoritative and undercounts. The summed total then came
    back equal to the rows shown, the prompt's "total equals rows shown" branch
    fired, and one run printed "first 10 of 28" while the next printed a bare 10.
    """

    import asyncio

    import v1.core.tools.servicenow.tools as tools_module
    from v1.core.tools.servicenow.tools import _STATUS_TO_STATE, servicenow_list_tickets

    states = ("new", "in_progress", "on_hold")
    by_code = {str(_STATUS_TO_STATE[s]): s for s in states}
    totals = {"new": 12, "in_progress": 9, "on_hold": 7}

    def _stub(silent: set[str]):
        class _Stub:
            async def list_incidents(self, *, filters=None, limit=10, offset=0):
                state = by_code[str((filters or {}).get("state"))]
                window = range(offset, min(offset + limit, totals[state]))
                envelope = {
                    "ok": True,
                    "mode": "real",
                    "incidents": [
                        {
                            "number": f"INC{state[:2].upper()}{i:03d}",
                            "state": state,
                            "short_description": f"row {i}",
                            "priority": "3 - Moderate",
                        }
                        for i in window
                    ],
                    # Only the PAGE count in real mode — never a stand-in for the total.
                    "result_count": len(window),
                    "has_more": offset + limit < totals[state],
                    "next_offset": offset + limit,
                    "degraded": False,
                }
                if state not in silent:
                    envelope["total_count"] = totals[state]
                return envelope

        return _Stub()

    original_client = tools_module.get_servicenow_client
    page = servicenow_list_tickets.coroutine
    try:
        tools_module.get_servicenow_client = lambda: asyncio.sleep(0, result=_stub(set()))
        full = asyncio.run(page(statuses=list(states), limit=10))
        assert full["count"] == 10, full["count"]
        assert full["total_count"] == 28 == sum(totals.values()), full["total_count"]

        # One state answers without a total. A partial sum (21) or the page size (10)
        # would both print as a confident, wrong "N found".
        tools_module.get_servicenow_client = lambda: asyncio.sleep(
            0, result=_stub({"on_hold"})
        )
        partial = asyncio.run(page(statuses=list(states), limit=10))
        assert partial["count"] == 10, partial["count"]
        assert partial["total_count"] is None, (
            f"one silent state must make the total unknown, got {partial['total_count']!r}"
        )
    finally:
        tools_module.get_servicenow_client = original_client

    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    # Neither layer may re-introduce a page-derived stand-in for the total. The client
    # is merged hunk-by-hunk into the deployed repo, so guard its text explicitly.
    client_src = (root / "v1/utils/clients/servicenow.py").read_text(encoding="utf-8")
    assert "result_count if total_count is None" not in client_src, (
        "client re-introduced the page-count fallback for total_count"
    )
    tools_src = (root / "v1/core/tools/servicenow/tools.py").read_text(encoding="utf-8")
    assert "len(tickets) if total_count is None" not in tools_src, (
        "tool re-introduced the row-count fallback for total_count"
    )

    prompt = (root / "v1/core/prompts/servicenow.py").read_text(encoding="utf-8")
    for required in (
        "total_count = null",  # the model must know what an absent total means
        "exact count unavailable",
        "NO query type is exempt",  # person/engineer searches lead with it too
    ):
        assert required in prompt, f"total-count rule lost: {required!r}"
    # The contradiction that produced "I can fetch more if needed" with no number:
    # a second rule that blessed a number-free line while the total was in hand.
    assert '"showing the first N; more are available"' not in prompt, (
        "pagination rule still sanctions a number-free 'more are available'"
    )
    assert "state EACH search's own total" in prompt, (
        "assigned-to/resolved-by union lost its per-search totals"
    )


def test_servicenow_answers_require_the_formatting_skill_read() -> None:
    """The skill read must be a PRECONDITION for incident answers, not a suggestion.

    User report: the incident URL shows in the tool result (visible in the tool-call
    bar) but is missing from the final answer, and the skill "is not being called".
    deepagents exposes a skill as a FILE the model may choose to open, so the old
    soft wording ("read its SKILL.md ... and follow it", buried at the end of a long
    paragraph) let the model skip it and improvise the shape.

    Note the asymmetry this pins: the ORCHESTRATOR is the only side that can read the
    skill at all — SERVICENOW_SUBAGENT has no ``skills`` key, so the file is not even
    mounted for it. A rule that assumed the subagent could read it would be dead text.
    """

    from pathlib import Path

    from v1.core.subagents.servicenow.subagent import SERVICENOW_SUBAGENT

    core = Path(__file__).resolve().parents[3] / "core"
    orchestrator = (core / "prompts" / "orchestrator.py").read_text(encoding="utf-8")

    assert "MANDATORY SKILL READ" in orchestrator, "the read decayed back to optional"
    for marker in (
        "/skills/message-formatting/SKILL.md",  # the exact path, never guessed
        "in THIS turn",  # scoped per turn, so it cannot be satisfied by an old read
        "precondition, not a suggestion",
        "ONCE per turn",  # ...but not re-read on every step
    ):
        assert marker in orchestrator, f"mandatory-read gate lost: {marker}"

    # If the subagent ever gains ``skills``, the "orchestrator only" framing above
    # becomes wrong and the rule needs re-siting — fail loudly rather than drift.
    assert "skills" not in SERVICENOW_SUBAGENT, (
        "subagent gained skills: revisit which layer the mandatory read belongs to"
    )


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
