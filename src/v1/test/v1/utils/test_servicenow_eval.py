"""Deterministic eval harness for the ServiceNow intent test cases (v4 workbook).

This is the regression harness PROD_DEPLOYMENT_TODO §73 said was missing: the real
``(query -> expected incident IDs)`` cases now live in
``docs/intents/servicenow_intent_test_cases_v4.xlsx`` (converted to
``fixtures/servicenow_intent_cases.json`` by ``docs/intents/convert_intents.py``).

What it tests — and what it deliberately does NOT:
* It scores the **ServiceNow retrieval layer**: for each case it executes the
  retrieval STRATEGY a correctly-prompted subagent should run — one or more
  ``servicenow_list_tickets`` calls plus the agent-side post-filtering the prompt
  teaches (e.g. "keep only ``configuration_item`` ``PL-*`` pipeline failures") —
  against the bundled 22-incident fixture, then compares the resulting incident
  set to the workbook's expected set (EXACT match, so returning extras fails).
* Post-filter predicates read ONLY fields the tool actually returns in its list
  output, so the harness faithfully reflects what the agent can see. A predicate
  that needs ``cause`` returns nothing while ``cause`` is absent from the list
  output — that is the intended signal that the one ``tools.py`` change (surface
  ``cause`` in list rows) is still pending, NOT a bug in the case.
* It does NOT run the LLM. Cases whose crux is upstream NLU entity
  resolution (Type B/C/D/E and the ``... -> ServiceNow`` variants) are tagged
  ``requires_resolution`` and reported separately — their resolution step needs
  the live agent and is out of scope for a deterministic fixture test.

Run as a scoreboard::

    PYTHONPATH=src .venv/bin/python src/v1/test/v1/utils/test_servicenow_eval.py

or under pytest (asserts the deterministic cases all pass; resolution-dependent
cases are reported but never fail the build).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from v1.core.tools.servicenow.tools import servicenow_list_tickets

_CASES_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "servicenow_intent_cases.json"
)

Ticket = Mapping[str, object]
Predicate = Callable[[Ticket], bool]


# -- post-filter predicate helpers --------------------------------------------
# Predicates read only fields present in a `servicenow_list_tickets` list row
# (ticket_number, short_description, status, priority, category,
# assignment_group, configuration_item, engineer, updated_at, and — once the
# tools.py change lands — cause). `.get(..., "")` keeps a missing field from
# crashing; it instead makes the predicate fall through to "no match", which is
# the correct baseline signal when `cause` is not yet surfaced.


def _ci(ticket: Ticket) -> str:
    return str(ticket.get("configuration_item", "")).upper()


def _cause(ticket: Ticket) -> str:
    return str(ticket.get("cause", "")).lower()


def is_pipeline_ci(ticket: Ticket) -> bool:
    """True for an ingest/ADF pipeline configuration item (``PL-*``)."""

    return _ci(ticket).startswith("PL-")


def cause_contains(*keywords: str) -> Predicate:
    return lambda t: any(kw.lower() in _cause(t) for kw in keywords)


def cause_excludes(*keywords: str) -> Predicate:
    return lambda t: not any(kw.lower() in _cause(t) for kw in keywords)


def all_of(*preds: Predicate) -> Predicate:
    return lambda t: all(p(t) for p in preds)


def any_of(*preds: Predicate) -> Predicate:
    return lambda t: any(p(t) for p in preds)


# A true pipeline-INFRA failure: PL-* config item AND a cause that is an
# infra/connectivity failure — NOT a PII / config error that merely happens to
# carry a PL-* CI (INC3190020, INC3195030). This is the distinction TC-037/039/040
# require. Falls through to PII-config exclusion via cause once cause is surfaced.
pipeline_infra_failure: Predicate = all_of(
    is_pipeline_ci, cause_excludes("configuration error")
)


@dataclass(frozen=True)
class Step:
    """One ``servicenow_list_tickets`` call plus an optional post-filter."""

    filters: dict
    keep: Predicate | None = None


@dataclass(frozen=True)
class Plan:
    """A retrieval strategy scored against the fixture.

    ``expected`` overrides the workbook's parsed incident set — needed for Type H
    (the source incident is an INPUT fetched via detail, not a search result, so
    only the historical MATCH should appear in the searched set).
    """

    steps: list[Step]
    expected: set[str] | None = None
    requires_resolution: bool = False
    note: str = ""


async def _run_step(step: Step) -> set[str]:
    payload = await servicenow_list_tickets.ainvoke({"limit": 25, **step.filters})
    if not payload.get("ok", False):
        raise AssertionError(f"tool returned error: {payload.get('error')}")
    tickets = payload.get("tickets", [])
    keep = step.keep or (lambda _t: True)
    return {str(t["ticket_number"]).upper() for t in tickets if keep(t)}


async def run_plan(plan: Plan) -> set[str]:
    """Union the kept incident numbers across every step of the plan."""

    found: set[str] = set()
    for step in plan.steps:
        found |= await _run_step(step)
    return found


# -- retrieval plans -----------------------------------------------------------
# Authored from each case's "Expected ServiceNow Filter" column, expressed with
# CURRENT tool capabilities + post-filtering (no clients/servicenow.py change).
# `requires_resolution=True` marks cases whose crux is NLU resolution; they
# are reported separately and never fail the build.

# 'open' is the bucket macro New+In Progress+On Hold (still being worked). Per the
# FIN business rule Resolved is now in the CLOSED bucket, so 'open' no longer
# returns Resolved tickets (it used to, via active=true). Use 'open,closed' when a
# query should span every status regardless of whether work is finished.
_OPEN = "open"
_ALL = "all"  # every status, both buckets (opt into closed/resolved explicitly).


PLANS: dict[str, Plan] = {
    # -- Type A: direct open-incidents-by-datasource --------------------------
    # The workbook lists INC3190020 (RESOLVED) under "open incidents", but the FIN
    # business rule puts Resolved in the CLOSED bucket, so a strict 'open' query
    # excludes it. Override the workbook's expected set to the two genuinely-open
    # (In Progress) Core Banking tickets. (Confirmed: Resolved = closed, not open.)
    "TC-001": Plan(
        [Step({"statuses": _OPEN, "description_contains": "Core Banking"})],
        expected={"INC3185010", "INC3235130"},
    ),
    "TC-002": Plan([Step({"statuses": _OPEN, "description_contains": "Debit Card"})]),
    "TC-003": Plan([Step({"statuses": _OPEN, "description_contains": "Real Estate Pricing"})]),
    "TC-004": Plan([Step({"statuses": _OPEN, "description_contains": "Loan Application"})]),
    # TC-005: search the DISTINCTIVE keyword 'Payments', NOT the full resolved
    # name 'Payments Enterprise' (the qualifier 'Enterprise' is not in the text).
    "TC-005": Plan([Step({"statuses": _OPEN, "description_contains": "Payments"})]),
    "TC-006": Plan([Step({"statuses": _OPEN, "description_contains": "Relationship Manager"})]),
    "TC-007": Plan([Step({"statuses": _OPEN, "description_contains": "Customer Enterprise"})]),

    # -- Type C: conversational / NLU (retrieval given the extracted entity) ---
    # TC-015: 'KYC' IS in INC3190020's description, but that ticket is RESOLVED, which
    # the FIN rule now places in the CLOSED bucket — so 'open' alone no longer
    # returns it. A KYC overview must span both buckets, hence 'open,closed'.
    "TC-015": Plan([Step({"statuses": _ALL, "description_contains": "KYC"})]),

    # -- Type F: engineer lookup ----------------------------------------------
    # "who has worked on X" => historical too, so it must span EVERY status. With the
    # safe open-only default, omitting statuses would now hide closed/resolved work,
    # so these explicitly pass statuses='all' to opt into the closed bucket.
    "TC-031": Plan([Step({"statuses": _ALL, "description_contains": "Core Banking"})]),
    "TC-034": Plan([Step({"statuses": _ALL, "description_contains": "Debit Card"})]),
    # Reverse lookup by engineer name (substring match on assigned/resolved) — also
    # historical, so all statuses.
    "TC-035": Plan(
        [
            Step({"statuses": _ALL, "assigned_to_name": "Pat Rivers"}),
            Step({"statuses": _ALL, "resolved_by_name": "Pat Rivers"}),
        ]
    ),
    # "who is WORKING on" => open only (excludes the closed Customer Ent. ticket).
    "TC-036": Plan([Step({"statuses": _OPEN, "description_contains": "Customer Enterprise"})]),

    # -- Type G: pipeline-infra incidents (the core filtering test) -----------
    "TC-037": Plan(
        [Step({"statuses": _OPEN, "description_contains": "Core Banking"}, keep=pipeline_infra_failure)]
    ),
    "TC-039": Plan(
        [Step({"statuses": _OPEN, "description_contains": "Debit Card"}, keep=pipeline_infra_failure)]
    ),
    "TC-040": Plan(
        [Step({"statuses": _OPEN, "description_contains": "Real Estate Pricing"}, keep=pipeline_infra_failure)],
        expected=set(),
    ),

    # -- Type H: historical resolution for a similar incident -----------------
    # Step-2 search only (source incident is fetched via detail, not searched).
    # Same-segment match, narrowed to the source incident's failure TYPE by cause
    # so the same-segment-but-different-cause incident (INC3190020) is excluded.
    "TC-042": Plan(
        [
            Step(
                {"statuses": "resolved,closed", "description_contains": "Core Banking"},
                keep=cause_contains("timeout", "authentication", "connection"),
            )
        ],
        expected={"INC3011201"},
    ),
    # Cross-datasource: same cause TYPE (timeout) but a different segment, so the
    # segment search finds nothing and the agent must fall back to a cause search.
    "TC-043": Plan(
        [Step({"statuses": "resolved,closed"}, keep=cause_contains("timeout"))],
        expected={"INC3011201"},
    ),
    "TC-044": Plan(
        [
            Step(
                {"statuses": "resolved,closed", "description_contains": "Payments"},
                keep=cause_contains("authentication", "incomplete", "file"),
            )
        ],
        expected={"INC3045600"},
    ),
    "TC-045": Plan(
        [Step({"statuses": "resolved,closed"}, keep=cause_contains("vendor"))],
        expected={"INC3032200"},
    ),
    "TC-046": Plan(
        [
            Step(
                {"statuses": "resolved,closed", "description_contains": "Customer Enterprise"},
                keep=cause_contains("kafka", "consumer lag", "lag"),
            )
        ],
        expected={"INC2920400"},
    ),

    # -- Type I: missing-data incidents ---------------------------------------
    # Union of "missing data" wording variants, then exclude pipeline-infra
    # failures and PII/config errors (those are not missing-data).
    "TC-047": Plan(
        [
            Step({"statuses": _OPEN, "description_contains": "Real Estate Pricing not landed"}),
            Step({"statuses": _OPEN, "description_contains": "Real Estate Pricing incomplete"}),
            Step({"statuses": _OPEN, "description_contains": "Real Estate Pricing partial"}),
            Step({"statuses": _OPEN, "description_contains": "Real Estate Pricing stale"}),
        ]
    ),
    "TC-050": Plan(
        [
            Step(
                {"statuses": _OPEN, "description_contains": "Debit Card stale"},
                keep=all_of(lambda t: not is_pipeline_ci(t)),
            ),
            Step(
                {"statuses": _OPEN, "description_contains": "Debit Card not refreshed"},
                keep=lambda t: not is_pipeline_ci(t),
            ),
        ],
        expected={"INC3198040"},
    ),
    "TC-051": Plan(
        [Step({"statuses": _OPEN, "description_contains": "HOP Enterprise stale"})],
        expected=set(),
    ),

    # -- Type J: cause-based search in a time window (last month = May 2026) ---
    # These are HISTORICAL cause-analysis queries ("what happened last month due to
    # X") that legitimately span resolved/closed tickets, so every step passes
    # statuses='all' to opt into the closed bucket past the open-only safe default.
    # INC3011201 (PL-CB-04) + INC3190020 (PL-CB-11) both match on the PL-* CI,
    # which alone is exact. (The spec's extra `resolution_notes LIKE '%pipeline%'`
    # OR-clause is redundant here and over-matches — INC3045600 / INC3025540 close
    # notes mention "pipeline" but are not pipeline incidents — so it is dropped.)
    "TC-052": Plan(
        [
            Step(
                {"statuses": _ALL, "created_after": "2026-05-01", "created_before": "2026-05-31"},
                keep=is_pipeline_ci,
            ),
        ],
        expected={"INC3011201", "INC3190020"},
    ),
    # 'credential' lives in close notes; 'authentication' in cause.
    "TC-053": Plan(
        [
            Step(
                {
                    "statuses": _ALL,
                    "created_after": "2026-05-01",
                    "created_before": "2026-05-31",
                    "close_notes_contains": "credential",
                }
            ),
            Step(
                {"statuses": _ALL, "created_after": "2026-05-01", "created_before": "2026-05-31"},
                keep=cause_contains("authentication"),
            ),
        ],
        expected={"INC3045600"},
    ),
    "TC-054": Plan(
        [
            Step(
                {"statuses": _ALL, "created_after": "2026-05-01", "created_before": "2026-05-31"},
                keep=cause_contains("source data", "source defect", "data defect"),
            )
        ],
        expected={"INC3230120"},
    ),
    "TC-055": Plan(
        [
            Step(
                {"statuses": _ALL, "created_after": "2026-05-01", "created_before": "2026-05-31"},
                keep=cause_contains("timeout"),
            ),
            Step(
                {
                    "statuses": _ALL,
                    "created_after": "2026-05-01",
                    "created_before": "2026-05-31",
                    "close_notes_contains": "timeout",
                }
            ),
        ],
        expected={"INC3011201"},
    ),
    # Vendor-outage matches exist only OUTSIDE May (June INC3198040, March
    # INC3032200), so the May window must return nothing.
    "TC-056": Plan(
        [
            Step(
                {"statuses": _ALL, "created_after": "2026-05-01", "created_before": "2026-05-31"},
                keep=cause_contains("vendor"),
            )
        ],
        expected=set(),
    ),
}

# Cases whose crux is NLU resolution (needs the live agent). Listed so the
# scoreboard accounts for every workbook case; not scored deterministically.
_RESOLUTION_DEPENDENT = {
    "TC-008", "TC-009", "TC-010", "TC-011", "TC-012", "TC-013", "TC-014",
    "TC-016", "TC-017", "TC-018", "TC-019", "TC-020",
    "TC-021", "TC-022", "TC-023", "TC-024", "TC-025",
    "TC-026", "TC-027", "TC-028", "TC-029", "TC-030",
    "TC-032", "TC-033", "TC-038", "TC-041", "TC-048", "TC-049",
}


def _load_cases() -> list[dict]:
    return json.loads(_CASES_PATH.read_text(encoding="utf-8"))


def _expected_for(plan: Plan, case: dict) -> set[str]:
    if plan.expected is not None:
        return plan.expected
    return set(case["expected_incidents"])


# -- scoreboard runner ---------------------------------------------------------


async def _evaluate() -> tuple[list[dict], list[dict]]:
    """Return (scored_results, skipped_results)."""

    cases = {c["test_id"]: c for c in _load_cases()}
    scored: list[dict] = []
    skipped: list[dict] = []
    for test_id, case in cases.items():
        if test_id in PLANS:
            plan = PLANS[test_id]
            expected = _expected_for(plan, case)
            got = await run_plan(plan)
            scored.append(
                {
                    "test_id": test_id,
                    "type": case["query_type"],
                    "expected": expected,
                    "got": got,
                    "passed": got == expected,
                    "workbook_status": case["status_in_workbook"],
                }
            )
        else:
            skipped.append(
                {
                    "test_id": test_id,
                    "type": case["query_type"],
                    "reason": "requires_resolution" if test_id in _RESOLUTION_DEPENDENT else "no_plan",
                }
            )
    return scored, skipped


def _print_scoreboard(scored: list[dict], skipped: list[dict]) -> int:
    failures = 0
    print("\nServiceNow intent eval — deterministic retrieval scoreboard\n" + "=" * 64)
    for r in sorted(scored, key=lambda r: r["test_id"]):
        ok = r["passed"]
        failures += 0 if ok else 1
        mark = "PASS" if ok else "FAIL"
        line = f"{mark}  {r['test_id']}  [{r['type'].split('-')[0].strip()}]"
        print(line)
        if not ok:
            missing = sorted(r["expected"] - r["got"])
            extra = sorted(r["got"] - r["expected"])
            if missing:
                print(f"        missing: {missing}")
            if extra:
                print(f"        extra:   {extra}")
    total = len(scored)
    passed = total - failures
    print("=" * 64)
    print(f"Deterministic cases: {passed}/{total} passed ({100 * passed // max(total, 1)}%)")
    print(f"Resolution-dependent (need live LLM, not scored): {len(skipped)}")
    return failures


def main() -> int:
    scored, skipped = asyncio.run(_evaluate())
    return _print_scoreboard(scored, skipped)


# -- pytest entry points -------------------------------------------------------


def test_servicenow_intent_retrieval() -> None:
    """Every deterministic retrieval plan returns exactly its expected incidents."""

    scored, _ = asyncio.run(_evaluate())
    failures = {
        r["test_id"]: {"expected": sorted(r["expected"]), "got": sorted(r["got"])}
        for r in scored
        if not r["passed"]
    }
    assert not failures, f"retrieval mismatches: {json.dumps(failures, indent=2)}"


if __name__ == "__main__":
    raise SystemExit(main())
