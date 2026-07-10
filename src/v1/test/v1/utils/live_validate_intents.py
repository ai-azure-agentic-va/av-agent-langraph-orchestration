"""LIVE agent validation for the prompt-driven ServiceNow intents (Types G/H/I/J).

WHY THIS EXISTS
---------------
The deterministic suites (``test_servicenow_intents.py`` / ``test_servicenow_fixes.py``)
pin the *filter mechanics* of the client. They cannot validate Types G/H/I, because
the hybrid fix solves those **agent-side**: the tool returns the broad result set and
the subagent narrows it *in its own answer* (drop PII decoys, keep ``category='Data
Quality'``, surface a cross-datasource resolution, …). That narrowing is invisible in
the tool-call arguments, so the only faithful test is to run the **live LLM subagent**
against the mock fixture and grade its final answer.

DESIGN — runner + adversarial LLM judge
---------------------------------------
1. RUNNER: the real ServiceNow subagent (its production prompt + tools, on
   ``AzureChatOpenAI``) answers each query against the bundled 22-incident mock.
2. JUDGE: a second model call grades the answer against the case's expected /
   excluded incident sets with a strict rubric (an incident named only as an
   explicitly-excluded "for reference / closed" aside does NOT count as a wrong
   inclusion; an expected incident must be presented as a returned match).
A regex pre-check (INC numbers in the answer) is printed alongside the judge verdict
as a cheap cross-check. Non-determinism is handled with ``--runs N`` (stability rate).

REQUIREMENTS
------------
- Azure OpenAI must be configured (same env as the app: endpoint/deployment/auth).
- AI Search reachable improves fidelity on the two STTM cases (TC-038, TC-048), but
  is not required here: the fixture descriptions contain the table names, so
  ``description_contains`` resolves them even when the KB lookup is unavailable.
- ServiceNow runs in MOCK mode (injected below) — no live ServiceNow needed.

USAGE
-----
    PYTHONPATH=src ./.venv/Scripts/python.exe \
        src/v1/test/v1/utils/live_validate_intents.py            # G,H,I (default)
    ... live_validate_intents.py --types G,H,I,J --runs 3        # all four, 3x each
    ... live_validate_intents.py --case TC-037 --verbose         # one case + trace

This is a MANUAL harness, not a unit test — it is intentionally NOT named ``test_*``
so the standalone ``_main`` test convention never sweeps it into a CI run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import dataclass, field

from langgraph.prebuilt import create_react_agent

from v1.core.agent import build_azure_chat_model
from v1.core.subagents.servicenow.subagent import SERVICENOW_SUBAGENT
from v1.core.tools.servicenow import tools as sn_tools
from v1.utils.clients.servicenow import ServiceNowClient, ServiceNowConfig

INC_RE = re.compile(r"INC\d{6,}")


@dataclass
class Case:
    """One intent test case, transcribed verbatim from servicenow_intent_test_cases_v4."""

    id: str
    type: str  # G | H | I | J
    query: str
    expected: set[str]  # incidents that MUST be presented as returned matches
    excluded: set[str] = field(default_factory=set)  # decoys/closed/out-of-window
    note: str = ""  # the discriminator the agent has to get right


# --- Type G: open pipeline failures only (filter out PII decoys + DQ + closed) -----
G_CASES = [
    Case("TC-037", "G", "List all pipeline incidents for Core Banking",
         {"INC3235130"}, {"INC3190020", "INC3185010", "INC3011201"},
         "INC3190020 is a PII config error mislabeled category=Pipeline w/ a PL- CI — must be dropped."),
    Case("TC-038", "G", "Show me pipeline failures for int_merchant_master",
         {"INC3240140"}, set(),
         "STTM/desc maps int_merchant_master -> Debit Card Merchant Master pipeline failure."),
    Case("TC-039", "G", "Any pipeline issues for Debit Card?",
         {"INC3240140"}, {"INC3195030", "INC3198040", "INC3025540"},
         "INC3195030 PII masking decoy (category=Pipeline) and INC3198040 vendor DQ must be excluded."),
    Case("TC-040", "G", "Are there pipeline failures for Real Estate Pricing?",
         set(), {"INC3205060", "INC3208070"},
         "Both open RE incidents are vendor/transfer DQ — answer must be NONE open pipeline failures."),
    Case("TC-041", "G", "What pipeline incidents are open for the repayment schedule feed?",
         {"INC3245150"}, {"INC3200050"},
         "INC3200050 (underwriting DQ) is not a pipeline failure."),
]

# --- Type H: surface the similar historical incident's resolution notes ------------
H_CASES = [
    Case("TC-042", "H", "What were the resolution notes for incidents similar to INC3235130?",
         {"INC3011201"}, set(),
         "Same-segment (Core Banking) pipeline failure; surface INC3011201 close notes. Source=INC3235130."),
    Case("TC-043", "H", "Find historical resolutions for incidents like INC3245150",
         {"INC3011201"}, set(),
         "CROSS-datasource: Loan App source-timeout -> Core Banking INC3011201 via failure type. Source=INC3245150."),
    Case("TC-044", "H", "INC3220100 - any similar past incidents with resolution notes?",
         {"INC3045600"}, set(),
         "Same datasource (Payments Enterprise) file-delivery failure. Source=INC3220100."),
    Case("TC-045", "H", "What was done to resolve incidents like INC3198040?",
         {"INC3032200"}, set(),
         "Same datasource (Debit Card) + same cause (vendor outage). Source=INC3198040."),
    Case("TC-046", "H", "Can you find past resolution notes for something similar to INC3215090?",
         {"INC2920400"}, set(),
         "Same datasource + same cause + same feed (Kafka consumer lag). Source=INC3215090."),
]

# --- Type I: open missing-data (Data Quality) only; drop PII/pipeline/Kafka/closed --
I_CASES = [
    Case("TC-047", "I", "Show me all missing data incidents for Real Estate Pricing",
         {"INC3205060", "INC3208070"}, {"INC2880300"},
         "Both open RE DQ incidents (no-data + partial load); exclude closed INC2880300."),
    Case("TC-048", "I", "Any missing data issues for cur_underwriting?",
         {"INC3200050"}, set(),
         "Records dropped on null derived field IS missing data."),
    Case("TC-049", "I", "List all open missing data incidents",
         {"INC3185010", "INC3198040", "INC3200050", "INC3205060",
          "INC3208070", "INC3210080", "INC3220100"},
         {"INC3190020", "INC3195030", "INC3215090", "INC3235130",
          "INC3240140", "INC3245150", "INC3225110"},
         "Broad scan: 7 DQ-missing in, 7 PII/pipeline/Kafka/parser out."),
    Case("TC-050", "I", "Show missing data incidents for Debit Card",
         {"INC3198040"}, {"INC3195030", "INC3240140"},
         "Only the vendor-outage stale feed; exclude PII masking + pipeline connection."),
    Case("TC-051", "I", "Any missing data incidents for HOP Enterprise?",
         set(), {"INC3230120"},
         "No OPEN incidents; INC3230120 is closed and must not be a returned match."),
]

# --- Type J: cause keyword within an opened_at window (last month = May 2026) -------
J_CASES = [
    Case("TC-052", "J", "Show all incidents from last month related to pipeline issues",
         {"INC3011201", "INC3190020"}, {"INC3235130", "INC3240140", "INC3245150"},
         "May-2026 window; June pipeline incidents are out of window."),
    Case("TC-053", "J", "Any incidents from last month related to credential or authentication problems?",
         {"INC3045600"}, set(),
         "'credential' lives in close notes, cause='Authentication issue' — OR the two."),
    Case("TC-054", "J", "What incidents from last month were caused by source data defects?",
         {"INC3230120"}, set(),
         "Direct cause match 'Source data defect' within May 2026 (xlsx query cell was blank)."),
    Case("TC-055", "J", "What incidents from last month had timeout as the root cause?",
         {"INC3011201"}, {"INC3245150"},
         "Map 'timeout' to cause='Source timeout' (exact). INC3245150 is a June timeout (out of window)."),
    Case("TC-056", "J", "Were there any vendor outage incidents last month?",
         set(), {"INC3198040", "INC3032200"},
         "Matches exist (June + March) but NONE in May — answer must respect the window."),
]

ALL_CASES = {"G": G_CASES, "H": H_CASES, "I": I_CASES, "J": J_CASES}


_JUDGE_SYSTEM = (
    "You are a strict grader for a ServiceNow ticket agent. You are given a user "
    "query, the agent's final answer, and the ground-truth sets for the case. "
    "Decide PASS or FAIL.\n\n"
    "RUBRIC:\n"
    "- The answer PASSES only if EVERY incident in EXPECTED_RETURNED is presented as a "
    "returned/matching result, AND NO incident in MUST_NOT_RETURN is presented as a "
    "returned/matching result.\n"
    "- An incident named only as an explicitly-excluded aside (e.g. 'for reference', "
    "'this is closed', 'not a pipeline failure', 'outside the time window') does NOT "
    "count as returned — that is correct behavior, not a violation.\n"
    "- If EXPECTED_RETURNED is empty, the answer must clearly state there are no "
    "matching incidents (referencing closed/out-of-window ones as context is fine).\n"
    "- For historical-resolution cases, an EXPECTED incident must have its resolution / "
    "close notes surfaced as the similar match, not merely be mentioned.\n\n"
    "Respond with ONLY a JSON object: "
    '{"verdict":"PASS"|"FAIL","missing":[...],"wrongly_returned":[...],"reason":"<one sentence>"}'
)


def _final_text(messages) -> str:
    for msg in reversed(messages):
        if getattr(msg, "type", None) == "ai" and getattr(msg, "content", None):
            return msg.content if isinstance(msg.content, str) else str(msg.content)
    return ""


def _tool_trace(messages) -> list[str]:
    trace = []
    for msg in messages:
        for tc in getattr(msg, "tool_calls", None) or []:
            args = {k: v for k, v in (tc.get("args") or {}).items() if v not in (None, "", [])}
            trace.append(f"{tc.get('name')}({json.dumps(args, ensure_ascii=False)})")
    return trace


async def _judge(model, case: Case, answer: str) -> dict:
    payload = (
        f"USER_QUERY: {case.query}\n"
        f"EXPECTED_RETURNED: {sorted(case.expected) or '[]'}\n"
        f"MUST_NOT_RETURN: {sorted(case.excluded) or '[]'}\n"
        f"DISCRIMINATOR: {case.note}\n\n"
        f"AGENT_ANSWER:\n{answer}"
    )
    resp = await model.ainvoke(
        [{"role": "system", "content": _JUDGE_SYSTEM}, {"role": "user", "content": payload}]
    )
    raw = resp.content if isinstance(resp.content, str) else str(resp.content)
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"verdict": "ERROR", "reason": f"unparseable judge output: {raw[:200]}"}


async def _run_case(agent, model, case: Case, verbose: bool) -> dict:
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": case.query}]},
        config={"recursion_limit": 25},
    )
    answer = _final_text(result["messages"])
    found = set(INC_RE.findall(answer))
    # Regex cross-check (naive: counts any mention, including excluded asides).
    naive_missing = sorted(case.expected - found)
    naive_extra = sorted((found & case.excluded))
    judged = await _judge(model, case, answer)

    if verbose:
        print(f"\n--- {case.id} ({case.type}) :: {case.query}")
        for line in _tool_trace(result["messages"]):
            print(f"      tool> {line}")
        print(f"      answer> {answer[:600].replace(chr(10), ' ')}{'…' if len(answer) > 600 else ''}")
        print(f"      regex INC in answer: {sorted(found)}")
    return {
        "verdict": judged.get("verdict", "ERROR"),
        "reason": judged.get("reason", ""),
        "judge_missing": judged.get("missing", []),
        "judge_wrong": judged.get("wrongly_returned", []),
        "naive_missing": naive_missing,
        "naive_extra": naive_extra,
    }


async def _amain(args) -> int:
    # Force MOCK ServiceNow (bundled 22-incident fixture) regardless of env config.
    sn_tools._servicenow_client = ServiceNowClient(ServiceNowConfig(mode="mock"))

    try:
        model = build_azure_chat_model()
    except Exception as exc:  # noqa: BLE001 - surface a clear setup error
        print(f"FATAL: could not build AzureChatOpenAI — is the env configured? {type(exc).__name__}: {exc}")
        return 2

    agent = create_react_agent(
        model=model,
        tools=SERVICENOW_SUBAGENT["tools"],
        prompt=SERVICENOW_SUBAGENT["system_prompt"],
    )

    types = [t.strip().upper() for t in args.types.split(",") if t.strip()]
    cases = [c for t in types for c in ALL_CASES.get(t, [])]
    if args.case:
        wanted = {c.strip().upper() for c in args.case.split(",")}
        cases = [c for c in cases if c.id.upper() in wanted]
    if not cases:
        print("No matching cases. Use --types G,H,I,J and/or --case TC-037.")
        return 2

    print(f"Running {len(cases)} case(s) x {args.runs} run(s) against the mock fixture "
          f"(deployment={model.deployment_name}).\n")

    totals = {"PASS": 0, "FAIL": 0, "ERROR": 0}
    per_case_pass: dict[str, int] = {}
    for case in cases:
        for r in range(args.runs):
            try:
                res = await _run_case(agent, model, case, args.verbose)
            except Exception as exc:  # noqa: BLE001 - keep the harness going
                res = {"verdict": "ERROR", "reason": f"{type(exc).__name__}: {exc}",
                       "judge_missing": [], "judge_wrong": [], "naive_missing": [], "naive_extra": []}
            verdict = res["verdict"] if res["verdict"] in totals else "ERROR"
            totals[verdict] += 1
            per_case_pass[case.id] = per_case_pass.get(case.id, 0) + (verdict == "PASS")
            tag = {"PASS": "ok  ", "FAIL": "FAIL", "ERROR": "ERR "}[verdict]
            run_suffix = f" [run {r + 1}/{args.runs}]" if args.runs > 1 else ""
            detail = ""
            if verdict != "PASS":
                bits = []
                if res["judge_missing"]:
                    bits.append(f"missing={res['judge_missing']}")
                if res["judge_wrong"]:
                    bits.append(f"wrong={res['judge_wrong']}")
                detail = f"  ({'; '.join(bits)}) " if bits else "  "
                detail += res["reason"]
            print(f"{tag} {case.id} ({case.type}){run_suffix}{('  ' + detail) if detail else ''}")

    n = len(cases) * args.runs
    print(f"\n{'='*64}\nTOT:  {totals['PASS']}/{n} PASS   {totals['FAIL']} FAIL   {totals['ERROR']} ERROR")
    if args.runs > 1:
        flaky = [cid for cid, p in per_case_pass.items() if 0 < p < args.runs]
        if flaky:
            print(f"FLAKY (non-deterministic across runs): {sorted(flaky)}")
    return 1 if (totals["FAIL"] or totals["ERROR"]) else 0


def _main() -> int:
    p = argparse.ArgumentParser(description="Live LLM validation of ServiceNow intents G/H/I/J.")
    p.add_argument("--types", default="G,H,I", help="comma list of types to run (default G,H,I)")
    p.add_argument("--case", default="", help="comma list of specific case IDs, e.g. TC-037,TC-040")
    p.add_argument("--runs", type=int, default=1, help="repetitions per case (stability check)")
    p.add_argument("--verbose", action="store_true", help="print tool trace + answer snippet per case")
    args = p.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(_main())
