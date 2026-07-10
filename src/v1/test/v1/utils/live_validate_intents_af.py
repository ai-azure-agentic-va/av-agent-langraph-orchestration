"""LIVE agent validation for Types A-F (TC-001..TC-036).

Companion to ``live_validate_intents.py`` (which covers the hard Types G/H/I/J).
This file transcribes the *first* 36 cases from servicenow_intent_test_cases_v4
verbatim and drives the SAME live ServiceNow subagent against the SAME bundled
22-incident mock fixture, graded by the SAME strict LLM judge.

Types A-F are the "easier" intents:
  A - Direct datasource open-incident fetch
  B - Key-field / table-name fetch (STTM lookup to resolve the datasource)
  C - Conversational / NLU entity extraction
  D - Multi-intent (KB STTM lookup + ServiceNow, in parallel)
  E - Summarize a specific incident (+ KB links)
  F - Engineer lookup (by datasource, or reverse: datasource by engineer)

USAGE (identical flags to the G/H/I/J harness)::

    set -a && source .env; set +a && PYTHONPATH=src SERVICENOW_MODE=mock \
        ./.venv/Scripts/python.exe \
        src/v1/test/v1/utils/live_validate_intents_af.py --types A,B,C,D,E,F --runs 3

    ... live_validate_intents_af.py --case TC-005 --types A --verbose

The ``expected`` set is the case's "Expected Incident Numbers Returned"; the
``excluded`` set captures decoys / closed-but-must-not-be-returned incidents that
the case Notes call out. For E/F cases that are about *content* (summaries,
engineer names) rather than filtering, ``expected`` pins the incident(s) that must
be surfaced and the judge checks they are presented as the answer.
"""

from __future__ import annotations

# Reuse every piece of machinery from the G/H/I/J harness so the runner, judge,
# fixture injection, regex cross-check, and CLI all stay byte-identical.
from live_validate_intents import (  # type: ignore  # noqa: E402
    Case,
    _amain,
)
import live_validate_intents as base  # type: ignore  # noqa: E402

import argparse
import asyncio


# --- Type A: direct datasource -> open incidents -----------------------------------
A_CASES = [
    Case("TC-001", "A", "Show me all open incidents for Core Banking",
         {"INC3185010", "INC3190020", "INC3235130"}, {"INC3011201"},
         "All 3 OPEN Core Banking incidents (any sub-state); closed INC3011201 excluded."),
    Case("TC-002", "A", "Are there any active tickets for the Debit Card data source?",
         {"INC3195030", "INC3198040", "INC3240140"}, {"INC3025540", "INC3032200"},
         "3 active Debit Card tickets; the two closed Debit Card incidents excluded."),
    Case("TC-003", "A", "What incidents are currently open for Real Estate Pricing?",
         {"INC3205060", "INC3208070"}, {"INC2880300"},
         "Both open RE incidents; closed INC2880300 excluded."),
    Case("TC-004", "A", "Pull up all unresolved incidents for Loan Application",
         {"INC3200050", "INC3245150"}, {"INC2965100"},
         "2 open Loan Application incidents; closed INC2965100 excluded."),
    Case("TC-005", "A", "Any open issues for Payments Enterprise right now?",
         {"INC3220100"}, {"INC3045600"},
         "Only open Payments incident is INC3220100; closed INC3045600 must not appear."),
    Case("TC-006", "A", "Get me the current incidents for Relationship Manager",
         {"INC3210080"}, set(),
         "Only 1 open RM incident."),
    Case("TC-007", "A", "What is the status of open tickets for Customer Enterprise?",
         {"INC3215090"}, set(),
         "Only the Kafka-lag ticket is open."),
]

# --- Type B: key field / table name -> STTM lookup -> open incident ----------------
B_CASES = [
    Case("TC-008", "B", "We are seeing issues with cur_underwriting - are there any open incidents?",
         {"INC3200050"}, set(),
         "Resolve cur_underwriting -> Loan Application Underwriting Decision via STTM."),
    Case("TC-009", "B", "Any tickets open for the settlement_batch_id feed?",
         {"INC3220100"}, set(),
         "Resolve settlement_batch_id -> Payments Settlement Batches via STTM."),
    Case("TC-010", "B", "Is there an incident for int_fraud_alerts? It looks stale",
         {"INC3198040"}, set(),
         "Resolve int_fraud_alerts -> Debit Card Fraud Alerts via STTM."),
    Case("TC-011", "B", "Check if there is a ticket for raw.customer_cust_interaction - it is lagging",
         {"INC3215090"}, set(),
         "Resolve raw.customer_cust_interaction -> Customer Enterprise Customer Interaction."),
    Case("TC-012", "B", "Are there open incidents related to the account_balance_snapshot table?",
         {"INC3185010"}, set(),
         "Resolve account_balance_snapshot -> Core Banking Account Balance Snapshot."),
    Case("TC-013", "B", "Anything open for the neighborhood_trends load?",
         {"INC3208070"}, set(),
         "Resolve neighborhood_trends -> Real Estate Pricing Neighborhood Market Trends."),
    Case("TC-014", "B", "Show me tickets related to int_rm_customer_master - counts look low",
         {"INC3210080"}, set(),
         "Disambiguate customer_master: int_rm_ prefix -> RM, not Core Banking."),
]

# --- Type C: conversational / NLU entity extraction --------------------------------
C_CASES = [
    Case("TC-015", "C", "The KYC data looks wrong, any incidents I should know about?",
         {"INC3190020"}, set(),
         "Extract 'KYC' -> Core Banking KYC Profile incident."),
    Case("TC-016", "C", "Cardholder PII masking seems broken - has someone already raised a ticket?",
         {"INC3195030"}, set(),
         "Extract cardholder + PII masking -> Debit Card Cardholder Master."),
    Case("TC-017", "C", "Any open incident for valuation data?",
         {"INC3205060"}, set(),
         "Extract 'valuation' -> Real Estate Pricing Property Valuation Master."),
    Case("TC-018", "C", "Settlement numbers do not match the processor control totals - is this tracked?",
         {"INC3220100"}, set(),
         "Extract settlement + control totals -> Payments Settlement Batches."),
    Case("TC-019", "C", "Who is working on the fraud alerts delay?",
         {"INC3198040"}, set(),
         "Extract 'fraud alerts' -> Debit Card Fraud Alerts; highlight assignee Morgan Blake."),
    Case("TC-020", "C", "The underwriting decision field is coming back null - is that a known problem?",
         {"INC3200050"}, set(),
         "Extract underwriting decision + null -> Loan App Underwriting Decision."),
]

# --- Type D: multi-intent (KB STTM lookup + ServiceNow) ----------------------------
D_CASES = [
    Case("TC-021", "D", "What is the STTM mapping for cur_underwriting and are there any open incidents for it?",
         {"INC3200050"}, set(),
         "Dual-intent: STTM mapping AND the open incident must both be returned."),
    Case("TC-022", "D", "Show me the primary key for the Cardholder Master segment and any related open tickets",
         {"INC3195030"}, set(),
         "Dual-intent: PK cardholder_id from STTM AND the open incident."),
    Case("TC-023", "D", "What fields are PII-sensitive in the KYC Profile segment? Also, are there any incidents about PII exposure?",
         {"INC3190020"}, set(),
         "Dual-intent: 8 PII fields from STTM AND the PII-exposure incident."),
    Case("TC-024", "D", "How many raw attributes does the Applicant Master have, and has there been a schema drift incident recently?",
         {"INC2965100"}, set(),
         "'recently' -> search closed too; surface closed schema-drift INC2965100."),
    Case("TC-025", "D", "What is the source file for the settlement feed and are there open issues with it?",
         {"INC3220100"}, set(),
         "Dual-intent: settlement_src source file from STTM AND the open incident."),
]

# --- Type E: summarize a specific incident (+ KB links) ----------------------------
E_CASES = [
    Case("TC-026", "E", "Summarize INC3190020",
         {"INC3190020"}, set(),
         "Summarize the KYC PII incident; surface its details."),
    Case("TC-027", "E", "Give me details on incident INC2965100",
         {"INC2965100"}, set(),
         "Summarize the closed Applicant Master schema-drift incident."),
    Case("TC-028", "E", "What happened with INC3205060? Any related documentation?",
         {"INC3205060"}, set(),
         "Summarize the AVM vendor API 503 incident."),
    Case("TC-029", "E", "Tell me about INC3025540 and any supporting docs",
         {"INC3025540"}, set(),
         "Summarize the closed Transaction Clearing duplicate-rows incident."),
    Case("TC-030", "E", "Summarize incident INC3225110 for me",
         {"INC3225110"}, set(),
         "Summarize the Logistics shipment_line delimiter-mismatch incident."),
]

# --- Type F: engineer lookup (by datasource; TC-035 is reverse) --------------------
F_CASES = [
    Case("TC-031", "F", "Who has worked on Core Banking incidents?",
         {"INC3011201", "INC3185010", "INC3190020", "INC3235130"}, set(),
         "Aggregate engineers across all 4 Core Banking incidents (open + closed)."),
    Case("TC-032", "F", "Who has handled issues with cur_txn_clearing?",
         {"INC3025540"}, set(),
         "Resolve cur_txn_clearing -> Debit Card Transaction Clearing; Alex Vega."),
    Case("TC-033", "F", "Who's handling the settlement issues?",
         {"INC3220100"}, set(),
         "Extract 'settlement' -> Devin Marsh on INC3220100."),
    Case("TC-034", "F", "Which engineers have worked on Debit Card?",
         {"INC3025540", "INC3032200", "INC3195030", "INC3198040", "INC3240140"}, set(),
         "Aggregate across all 5 Debit Card incidents; flag unassigned tickets."),
    Case("TC-035", "F", "What datasources has Pat Rivers worked on?",
         {"INC2965100", "INC3230120"}, set(),
         "Reverse lookup by engineer: Pat Rivers -> Loan Application + HOP Enterprise."),
    Case("TC-036", "F", "Who is working on Customer Enterprise incidents?",
         {"INC3215090"}, set(),
         "Edge case: INC3215090 is unassigned; state it needs to be picked up."),
]

AF_CASES = {
    "A": A_CASES, "B": B_CASES, "C": C_CASES,
    "D": D_CASES, "E": E_CASES, "F": F_CASES,
}

# Register the A-F case sets into the base module's dispatch table so the shared
# _amain() (which reads base.ALL_CASES) can resolve --types A..F.
base.ALL_CASES = AF_CASES


def _main() -> int:
    p = argparse.ArgumentParser(description="Live LLM validation of ServiceNow intents A-F.")
    p.add_argument("--types", default="A,B,C,D,E,F", help="comma list of types (default A,B,C,D,E,F)")
    p.add_argument("--case", default="", help="comma list of specific case IDs, e.g. TC-005,TC-008")
    p.add_argument("--runs", type=int, default=1, help="repetitions per case (stability check)")
    p.add_argument("--verbose", action="store_true", help="print tool trace + answer snippet per case")
    args = p.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(_main())
