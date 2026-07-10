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
