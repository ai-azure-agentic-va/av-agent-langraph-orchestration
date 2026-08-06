"""Offline tests for the no-verdict guard middleware.

Runs standalone (``python test_verdict_guard.py``) or under pytest.
"""

from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from v1.core.middlewares.verdict_guard import VerdictGuardMiddleware

_GUARD = VerdictGuardMiddleware()

_ADLS_EVIDENCE = ToolMessage(
    content=(
        "[adls-agent] Table 'speedpay_check_analytics' has 3 DQ rule row(s):\n"
        "       time target : 06:54 (America/New_York)\n"
        "  - pcur/.../f.csv | 149 bytes | created=x | lastModified=2026-07-30 11:04:46+00:00"
    ),
    tool_call_id="t1",
)


def _run(question: str, answer: str, with_evidence: bool = True):
    messages = [HumanMessage(content=question)]
    if with_evidence:
        messages.append(_ADLS_EVIDENCE)
    messages.append(AIMessage(content=answer, id="final-1"))
    state = {"messages": messages}
    return asyncio.run(_GUARD.aafter_agent(state, None))


def test_bare_no_on_timeliness_question_is_replaced() -> None:
    update = _run("Was the file late in the pre-curated zone? Answer strictly yes or no.", "No")
    assert update is not None
    replacement = update["messages"][0]
    assert replacement.id == "final-1"  # replaces, not appends
    assert "DQ timeliness process" in replacement.content
    assert "06:54 (America/New_York)" in replacement.content  # facts quoted
    assert "lastModified=2026-07-30" in replacement.content


def test_verdict_with_boilerplate_tail_is_replaced() -> None:
    update = _run(
        "Is the speedpay file on time today? yes or no only.",
        "Yes\n\n## Want to explore further?\n- More?",
    )
    assert update is not None


def test_full_factual_answer_is_untouched() -> None:
    update = _run(
        "Was the file late?",
        "The configured target is 06:54 (America/New_York) and the file's "
        "lastModified is 2026-07-30 11:04:46+00:00; the DQ process makes the determination.",
    )
    assert update is None


def test_yes_to_non_timeliness_question_is_untouched() -> None:
    update = _run("Can you query ServiceNow incidents?", "Yes")
    assert update is None


def test_no_evidence_still_replaces_with_retrieval_hint() -> None:
    update = _run("Was it late? yes or no.", "No", with_evidence=False)
    assert update is not None
    assert "I will retrieve both" in update["messages"][0].content


def _main() -> int:
    checks = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
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
