"""Tests for AdlsVerdictGuardMiddleware: rewrite bare timeliness verdicts, leave other answers alone."""

from __future__ import annotations

import asyncio
import logging

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from v1.core.middlewares.adls_verdict_guard import AdlsVerdictGuardMiddleware

_ANSWER_ID = "answer-1"

_INBOUND_CHECK = (
    "[adls-agent] File arrival check\n"
    "  landing path : lnd/sales/daily_orders (account datalake, container adls)\n"
    "  business date: 2026-10-12 (America/New_York)\n"
    "  SLA given    : 6:00 AM ET\n"
    "  status       : File received; still being processed\n"
    "  arrived      : 2026-10-12 07:40 EDT (11:40 UTC)\n"
    "  file         : lnd/sales/daily_orders/inbound/ORDERS_20261012.csv\n"
    "\n"
    "Folders checked:\n"
    "  archive: none\n"
    "  error: none\n"
    "  inbound: 1 file(s)\n"
    "    - lnd/sales/daily_orders/inbound/ORDERS_20261012.csv | 2048 bytes | created "
    "2026-10-12 07:39 EDT (11:39 UTC) | lastModified 2026-10-12 07:40 EDT (11:40 UTC)"
)
_ARRIVAL_CHECK = (
    "[adls-agent] File arrival check\n"
    "  landing path : lnd/loansys/daily_payments (account datalake, container adls)\n"
    "  SLA given    : 6:00 AM ET\n"
    "  status       : File received and processed successfully\n"
    "  arrived      : 2026-10-12 06:25 EDT (10:25 UTC)"
)
_ADF_LOOKUP = "[adf-agent] Run 54bb8906 status: Failed"

# name, question, answer, tool output, rewrite expected, substring the result must carry
_CASES = (
    (
        "bare verdict under yes/no pressure is rewritten",
        "Was the orders file late today? Answer strictly yes or no",
        "No.",
        _INBOUND_CHECK,
        True,
        "arrived      : 2026-10-12 07:40 EDT (11:40 UTC)",
    ),
    (
        "short verdict with filler words is still a verdict",
        "Did the file arrive on time?",
        "Yes it did",
        _INBOUND_CHECK,
        True,
        "lastModified 2026-10-12 07:40 EDT (11:40 UTC)",
    ),
    (
        "verdict followed by a prompt suffix is still a verdict",
        "Was it late?",
        "No. Want to explore further into the pipeline history?",
        _INBOUND_CHECK,
        True,
        "SLA given    : 6:00 AM ET",
    ),
    (
        "completeness wording also triggers the contract",
        "Is the dataset complete?",
        "Yes.",
        _INBOUND_CHECK,
        True,
        "complete/incomplete",
    ),
    (
        "a verdict with no ADLS lookup in the thread is left alone",
        "Was the file late?",
        "No.",
        None,
        False,
        None,
    ),
    (
        "an ADLS lookup with no facts falls back to the ask-me text",
        "Was the file late?",
        "No.",
        "[adls-agent] Path not allowed: 'raw/x' is outside the permitted landing folders",
        True,
        "Give me the landing path and the SLA",
    ),
    (
        "a one-word verdict without yes/no is still a verdict",
        "Was the orders file late?",
        "Late.",
        _INBOUND_CHECK,
        True,
        "arrived      : 2026-10-12 07:40 EDT (11:40 UTC)",
    ),
    (
        "'on time' and 'it was late' are verdicts too",
        "Did the file arrive on time?",
        "It was on time.",
        _INBOUND_CHECK,
        True,
        "SLA given    : 6:00 AM ET",
    ),
    (
        "landing-path arrival check quotes the SLA given",
        "Did lnd/loansys/daily_payments arrive on time? Just yes or no",
        "Yes.",
        _ARRIVAL_CHECK,
        True,
        "SLA given    : 6:00 AM ET",
    ),
    (
        "landing-path arrival check quotes the arrival time",
        "Was the daily_payments file late?",
        "No.",
        _ARRIVAL_CHECK,
        True,
        "arrived      : 2026-10-12 06:25 EDT (10:25 UTC)",
    ),
    (
        "a factual answer to the same question is left alone",
        "Was the file late?",
        "The SLA given was 06:00 EDT and the file's lastModified is 07:40 EDT.",
        _INBOUND_CHECK,
        False,
        None,
    ),
    (
        "a longer answer is not a bare verdict",
        "Was the file late?",
        "No the file was not late at all",
        _INBOUND_CHECK,
        False,
        None,
    ),
    (
        "'latest' is not 'late' and 'translate' is not 'sla'",
        "Is the latest run done? Translate the status too.",
        "Yes.",
        _ADF_LOOKUP,
        False,
        None,
    ),
    (
        "'incomplete' and 'arrived' still count",
        "Was the file incomplete when it arrived?",
        "No.",
        _ARRIVAL_CHECK,
        True,
        "arrived      : 2026-10-12 06:25 EDT (10:25 UTC)",
    ),
    (
        "a bare yes on an unrelated question is left alone",
        "Is the pipeline named pl_A1_NightlyCoreIngest?",
        "Yes.",
        _ADF_LOOKUP,
        False,
        None,
    ),
)


def _state(question: str, answer: str, tool_output: str | None) -> dict:
    messages = [HumanMessage(content=question)]
    if tool_output is not None:
        messages.append(ToolMessage(content=tool_output, tool_call_id="call-1"))
    messages.append(AIMessage(content=answer, id=_ANSWER_ID))
    return {"messages": messages}


def test_rewrites_only_bare_verdicts() -> None:
    middleware = AdlsVerdictGuardMiddleware()
    for name, question, answer, tool_output, expect_rewrite, expected_text in _CASES:
        result = asyncio.run(middleware.aafter_agent(_state(question, answer, tool_output), None))
        assert (result is not None) == expect_rewrite, (
            f"{name}: expected rewrite={expect_rewrite}, got {result is not None}"
        )
        if not expect_rewrite:
            continue

        replacement = result["messages"][0]
        # Same id, so the reducer replaces the verdict instead of appending.
        assert replacement.id == _ANSWER_ID, f"{name}: message id was not preserved"
        assert "bare yes or no" in replacement.content, f"{name}: deferral text missing"
        assert expected_text in replacement.content, (
            f"{name}: expected {expected_text!r} in the replacement"
        )


def test_quotes_the_adls_agent_task_result() -> None:
    # Live, the orchestrator only sees the adls-agent's prose as its `task` result.
    report = (
        "File ORDERS_20261012.csv arrived 2026-10-12 07:40 EDT in "
        "lnd/sales/daily_orders/inbound (still being processed).\n"
        "SLA given: 6:00 AM ET."
    )
    call = {"name": "task", "args": {"subagent_type": "adls-agent"}, "id": "call-9"}
    state = {
        "messages": [
            HumanMessage(content="Was the orders file late? yes or no"),
            AIMessage(content="", tool_calls=[call]),
            ToolMessage(content=report, tool_call_id="call-9"),
            AIMessage(content="No.", id=_ANSWER_ID),
        ]
    }
    result = AdlsVerdictGuardMiddleware().after_agent(state, None)
    assert result is not None, "the sync hook must guard too"
    replacement = result["messages"][0].content
    assert "arrived 2026-10-12 07:40 EDT" in replacement, replacement
    assert "SLA given: 6:00 AM ET" in replacement, replacement
    assert "Give me the landing path" not in replacement, replacement


def test_answers_from_another_source_are_left_alone() -> None:
    # An earlier ADLS turn, then a ServiceNow or ADF yes/no question.
    earlier = [
        HumanMessage(content="Did the payments file land in lnd/loansys/daily_payments today?"),
        ToolMessage(content=_ARRIVAL_CHECK, tool_call_id="call-1"),
        AIMessage(content="It landed at 06:25 EDT and was processed.", id="earlier"),
    ]
    for question, subagent in (
        ("Is CHG0012 due today? yes or no", "servicenow-ticket-agent"),
        ("Did the nightly pipeline run complete? Just yes or no", "adf-agent"),
    ):
        call = {"name": "task", "args": {"subagent_type": subagent}, "id": "call-2"}
        state = {
            "messages": [
                *earlier,
                HumanMessage(content=question),
                AIMessage(content="", tool_calls=[call]),
                ToolMessage(content="(subagent report)", tool_call_id="call-2"),
                AIMessage(content="Yes.", id=_ANSWER_ID),
            ]
        }
        assert AdlsVerdictGuardMiddleware().after_agent(state, None) is None, subagent

    # A follow-up with no new lookup still counts.
    follow_up = {
        "messages": [
            *earlier,
            HumanMessage(content="Was it late? Just yes or no."),
            AIMessage(content="No.", id=_ANSWER_ID),
        ]
    }
    result = AdlsVerdictGuardMiddleware().after_agent(follow_up, None)
    assert (
        result is not None
        and "arrived      : 2026-10-12 06:25 EDT" in result["messages"][0].content
    )


def test_only_a_rewrite_is_logged() -> None:
    records: list[logging.LogRecord] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    guard_logger = logging.getLogger(AdlsVerdictGuardMiddleware.__module__)
    sink, saved_level = _Sink(), guard_logger.level
    guard_logger.addHandler(sink)
    guard_logger.setLevel(logging.INFO)
    try:
        middleware = AdlsVerdictGuardMiddleware()
        for answer in ("No.", "No the file was not late at all"):
            state = _state("Was it late?", answer, _ARRIVAL_CHECK)
            asyncio.run(middleware.aafter_agent(state, None))
    finally:
        guard_logger.removeHandler(sink)
        guard_logger.setLevel(saved_level)

    logged = [record.getMessage() for record in records]
    expected = (
        "Verdict guard replaced a bare 'No.' answer to a timeliness question "
        "(quoting the latest lookup)"
    )
    assert logged == [expected], logged


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
