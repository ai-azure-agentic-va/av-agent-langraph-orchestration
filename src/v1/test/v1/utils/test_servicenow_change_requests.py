"""Regression tests for the ServiceNow change-request search.

Covers the client (MuleSoft-only path, its own filter allow-table, the live
``{"result": {"change_requests": [...]}}`` envelope, the sys_id deep link) and the
tool (closed-only search, sort by Actual End Date across pages, the rendered
fields). Runs under pytest; async calls go through ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlsplit

import httpx

import v1.core.tools.servicenow.tools as sn_tools
from v1.core.tools.servicenow.change_requests import servicenow_list_change_requests
from v1.utils.clients.servicenow import ServiceNowClient, ServiceNowConfig, ServiceNowError


def _pair(value: str, display: str | None = None) -> dict[str, str]:
    return {"value": value, "display_value": value if display is None else display}


def _change(number: str, work_end: str | None, **extra: dict[str, str]) -> dict:
    record = {
        "sys_id": _pair(f"{number.lower()}0000"),
        "number": _pair(number),
        "short_description": _pair(f"Ledger Hub change {number}"),
        "state": _pair("3", "Closed"),
        "assigned_to": _pair("u1", "Alex Rivera (A1042)"),
        "assignment_group": _pair("g1", "LEDGER OPS SUPPORT"),
        "close_code": _pair("successful", "Successful"),
        "close_notes": _pair("Deployed.\nVerified by the team.", "Deployed.\r\nVerified by the team."),
        "change_url": f"https://instance.example/{number}",
        **extra,
    }
    if work_end:
        # value is UTC, display_value US Eastern, as the gateway sends them.
        record["work_end"] = _pair(work_end, "earlier Eastern time")
    return record


class _Stub:
    """Serves records in the given (newest-updated) order, 50 per page.

    Honours ``keyword`` the way the mock server does: every word, in the title or
    description, case-insensitive. Other filters are not modelled.
    """

    def __init__(self, records: list[dict]) -> None:
        self.records = records
        self.calls: list[tuple[dict, int]] = []

    async def list_change_requests(self, *, filters, limit, offset):
        self.calls.append((dict(filters), offset))
        words = str(filters.get("keyword", "")).lower().split()
        group = str(filters.get("assignment_group", "")).lower()
        states = str(filters.get("state", "")).split(",")
        matched = [
            r for r in self.records
            if all(
                w in f"{r['short_description']['value']} {r.get('description', {}).get('value', '')}".lower()
                for w in words
            )
            and (not group or r["assignment_group"]["display_value"].lower() == group)
            # Live drops a zero state, so a Review change has none: it is state 0.
            and (states == [""] or r.get("state", {"value": "0"})["value"] in states)
        ]
        page = matched[offset : offset + limit]
        return {
            "change_requests": page,
            "total_count": len(matched),
            "has_more": offset + len(page) < len(matched),
            "next_offset": offset + len(page),
        }


def _invoke(stub: _Stub, **payload) -> dict:
    previous = sn_tools._servicenow_client
    sn_tools._servicenow_client = stub
    try:
        return asyncio.run(servicenow_list_change_requests.ainvoke(payload))
    finally:
        sn_tools._servicenow_client = previous


def test_closed_changes_come_back_newest_implemented_first_across_pages() -> None:
    # 60 rows in updated order: two pages. The newest work_end sits on page 2, and
    # one change without an Actual End Date must never pose as the newest.
    records = [_change(f"CHG{1000 + i:07d}", f"2026-08-{1 + i % 28:02d} 10:00:00") for i in range(59)]
    records.insert(3, _change("CHG0009999", None))
    records.append(_change("CHG0008888", "2026-10-02 09:00:00"))
    stub = _Stub(records)

    result = _invoke(stub, query="recent changes related to Ledger Hub", limit=3)

    assert result["ok"] is True
    assert [c["number"] for c in result["change_requests"]] == [
        "CHG0008888", "CHG0001027", "CHG0001055"
    ]
    # Closed only, one search per word of the subject, each read to its last page.
    # The ask's own words ('recent changes related to') never reach the search.
    assert all(f["state"] == "3" for f, _ in stub.calls)
    assert sorted((f["keyword"], offset) for f, offset in stub.calls) == [
        ("Hub", 0), ("Hub", 50), ("Ledger", 0), ("Ledger", 50)
    ]
    rendered = result["rendered_answer"]
    assert rendered.startswith(
        "### Recent Ledger Hub Changes\n\n"
        "Found 61 closed change requests related to 'Ledger Hub'; showing the 3 most recent"
    )
    assert "1. **Change Number:** [CHG0008888](" in rendered
    for line in (
        "- **Assigned To:** Alex Rivera (A1042)",
        "- **Assignment Group:** LEDGER OPS SUPPORT",
        # The UTC value, not the Eastern display, labelled UTC like incident times
        # so the UI shows it in the viewer's own zone.
        "- **Actual End Date:** 2026-10-02 09:00:00 UTC",
        "- **Closure Code:** Successful",
        "- **Closure Notes:** Deployed. Verified by the team.",
    ):
        assert line in rendered
    assert "State" not in rendered  # every row is Closed; the line would be noise


def test_change_number_fetches_that_change_in_any_state() -> None:
    stub = _Stub([_change(
        "CHG0001234", None,
        state=_pair("-2", "Scheduled"),
        implementation_plan=_pair("Stop the job.\nDeploy.", "Stop the job.\r\nDeploy."),
    )])

    result = _invoke(stub, change_number="chg0001234", query="Ledger Hub")

    assert stub.calls == [({"number": "CHG0001234"}, 0)]
    assert result["rendered_answer"].startswith("**Change Number:** [CHG0001234](")
    assert "- **State:** Scheduled" in result["rendered_answer"]
    # The lookup is the FULL view: plans included, each kept on its bullet's line.
    assert "- **Implementation Plan:** Stop the job. Deploy." in result["rendered_answer"]
    assert _invoke(_Stub([]), change_number="CHG1,CHG2")["kind"] == "invalid_input"


def test_a_plural_subject_finds_changes_naming_it_singular_or_only_in_their_ci() -> None:
    singular = _change(
        "CHG0000001", "2026-08-01 10:00:00",
        short_description=_pair("Ledger Invoice Details hot fix"),
    )
    ci_only = _change(
        "CHG0000002", "2026-08-02 10:00:00",
        short_description=_pair("Deploy Ledger notice letter"),
        cmdb_ci=_pair("ci1", "Ledger Invoice Details"),
        assigned_to=_pair("u9", ""),  # live sends people with no display name
    )
    decoy = _change(
        "CHG0000003", "2026-08-03 10:00:00",
        short_description=_pair("Ledger dashboard refresh"),
    )
    stub = _Stub([singular, ci_only, decoy])

    result = _invoke(stub, query="Ledger Invoices")

    assert [c["number"] for c in result["change_requests"]] == ["CHG0000002", "CHG0000001"]
    assert sorted(f["keyword"] for f, _ in stub.calls) == ["Invoice", "Ledger"]
    rendered = result["rendered_answer"]
    assert rendered.startswith(
        "### Recent Ledger Invoices Changes\n\n"
        "Found 2 closed change requests related to 'Ledger Invoices', most recent first"
    )
    # A blank assignee keeps its template row rather than vanishing.
    assert "- **Assigned To:** Not available" in rendered


def test_a_team_name_passed_as_the_subject_is_tried_as_the_group() -> None:
    stub = _Stub([_change("CHG0000001", "2026-08-01 10:00:00")])

    result = _invoke(stub, query="LEDGER OPS SUPPORT")

    assert [c["number"] for c in result["change_requests"]] == ["CHG0000001"]
    assert stub.calls[-1] == ({"state": "3", "assignment_group": "LEDGER OPS SUPPORT"}, 0)
    assert "Found 1 closed change request for LEDGER OPS SUPPORT" in result["rendered_answer"]


def test_closed_by_default_and_open_or_cancelled_only_when_asked() -> None:
    closed = _change("CHG0000001", "2026-08-01 10:00:00")
    scheduled = _change(
        "CHG0000002", None,
        state=_pair("-2", "Scheduled"), start_date=_pair("2026-10-14 01:00:00"),
    )
    del scheduled["close_code"], scheduled["close_notes"]  # no closure yet
    review = _change("CHG0000003", "2026-10-03 23:31:02")
    del review["state"]  # live drops a zero state
    cancelled = _change("CHG0000004", None, state=_pair("4", "Cancelled"))
    stub = _Stub([closed, scheduled, review, cancelled])

    default = _invoke(stub, query="Ledger Hub")
    assert [c["number"] for c in default["change_requests"]] == ["CHG0000001"]

    stub.calls.clear()
    asked_open = _invoke(stub, query="Ledger Hub", statuses="open")
    assert {f["state"] for f, _ in stub.calls} == {"-5,-4,-3,-2,-1,0"}
    assert [c["number"] for c in asked_open["change_requests"]] == ["CHG0000002", "CHG0000003"]
    rendered = asked_open["rendered_answer"]
    assert rendered.startswith(
        "### Open Ledger Hub Changes\n\n"
        "Found 2 open change requests related to 'Ledger Hub', most recent first by Planned Start."
    )
    assert "- **State:** Scheduled" in rendered
    assert "- **Planned Start:** 2026-10-14 01:00:00 UTC" in rendered
    # An open change has no closure yet: no 'Not available' closure lines for it.
    assert "- **Closure Code:** Not available" not in rendered

    cancelled_only = _invoke(stub, query="Ledger Hub", statuses="canceled")
    assert [c["number"] for c in cancelled_only["change_requests"]] == ["CHG0000004"]
    assert _invoke(stub, statuses="someday")["kind"] == "invalid_input"


def test_no_match_says_so() -> None:
    result = _invoke(_Stub([]), query="Ledger Hub")
    assert result["rendered_answer"] == "No closed change requests found related to 'Ledger Hub'."


def _real_client(handler) -> ServiceNowClient:
    config = ServiceNowConfig(
        mode="real",
        transport="mulesoft",
        mulesoft_change_request_api_url="https://gateway.example/change_requests",
        instance_url="https://instance.example",
    )
    return ServiceNowClient(config, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_client_sends_only_allowed_filters_and_unwraps_the_envelope() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(parse_qs(urlsplit(str(request.url)).query))
        body = {
            "result": {
                "result_count": 1.0, "total_count": 7.0, "limit": 50, "offset": 0,
                "next_offset": 1.0, "has_more": True,
                "change_requests": [_change("CHG0001234", "2026-10-02 09:00:00")],
            }
        }
        return httpx.Response(200, json=body)

    envelope = asyncio.run(
        _real_client(handler).list_change_requests(
            filters={"state": "3", "keyword": "Ledger Hub", "assigned_to": "Rivera", "priority": "1"}, limit=50
        )
    )

    assert seen == [{
        "state": ["3"], "keyword": ["Ledger Hub"], "assigned_to": ["Rivera"],
        "limit": ["50"], "offset": ["0"],
    }]
    assert envelope["total_count"] == 7 and envelope["has_more"] is True
    assert envelope["change_requests"][0]["change_url"] == (
        "https://instance.example/nav_to.do?uri=change_requests.do?sys_id=chg00012340000"
    )


def test_client_refuses_without_the_change_request_url() -> None:
    client = ServiceNowClient(ServiceNowConfig(mode="mock"), incidents=[])
    try:
        asyncio.run(client.list_change_requests(filters={}))
    except ServiceNowError as exc:
        assert "MULESOFT_CHANGE_REQUEST_API_URL" in str(exc)
    else:
        raise AssertionError("expected ServiceNowError")
