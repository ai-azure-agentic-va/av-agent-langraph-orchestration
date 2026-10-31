"""Regression tests for the view-only markdown layout of a subagent's tool results.

The ADF and ADLS tools print plain text whose lines are told apart by line
breaks and indentation, which markdown drops: copied into an answer, a recovery
validation rendered as one paragraph ('Pipeline Name : pl_x Failed Run ID : …').
Each of the subagent's model calls now sees those results laid out as markdown,
line for line: a paragraph per header, a bullet per indented line, nested by its
indentation, '**Label:** value' fields, and a column header in bold above its
rows. Never a table, and never a word changed, dropped or moved. Only the view
changes: persisted messages, other tools' results and failed or non-text
results are left exactly as they were.

Runs standalone (``python test_tool_markdown.py``) or under pytest.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, replace
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from v1.core.middlewares import AdfResultKeepMiddleware, ToolMarkdownMiddleware
from v1.core.middlewares.adf_result_keep import KEEP_REQUIREMENT
from v1.core.middlewares.tool_markdown import as_markdown

RECOVERY = """\
[adf-agent] Recovery validation in factory 'adf-fin'
  Pipeline Name : pl_load
  Failed Run ID : f [open run](<https://adf.azure.com/en/monitoring/pipelineruns/f>)
  Run Started   : 2026-06-02 09:00:00 UTC
  Trigger Name  : tr_a
  Recovery Run ID  : (none)
  Recovery Decision: Not Recovered
  Reason: no subsequent execution matched the pipeline, trigger and exact parameter set.
  REJECTED LATER SUCCESS(ES): 1 run(s) Succeeded after the failure:
    - runId=s | 2026-06-02 10:00:00 UTC | triggeredBy=tr_b (ScheduleTrigger) | [open run](<https://adf.azure.com/en/monitoring/pipelineruns/s>)
      differs on: trigger (tr_a -> tr_b)"""

RECOVERY_MARKDOWN = """\
[adf-agent] Recovery validation in factory 'adf-fin'

- **Pipeline Name:** pl_load
- **Failed Run ID:** f [open run](<https://adf.azure.com/en/monitoring/pipelineruns/f>)
- **Run Started:** 2026-06-02 09:00:00 UTC
- **Trigger Name:** tr_a
- **Recovery Run ID:** (none)
- **Recovery Decision:** Not Recovered
- **Reason:** no subsequent execution matched the pipeline, trigger and exact parameter set.
- **REJECTED LATER SUCCESS(ES):** 1 run(s) Succeeded after the failure:
  - runId=s | 2026-06-02 10:00:00 UTC | triggeredBy=tr_b (ScheduleTrigger) | [open run](<https://adf.azure.com/en/monitoring/pipelineruns/s>)
    - **differs on:** trigger (tr_a -> tr_b)"""

TRIGGERS = """\
[adf-agent] Current trigger states (factory 'adf-fin') - 2/2 found
  Trigger | Factory | ADF State | Business State | Starts pipeline(s)
  tr_daily | adf-fin | Stopped | Disabled | pl_load
  TRDAILY | Fin | Started | Enabled | Main
  NOTE: '*' is a name pattern (letter case and separators ignored); 2 trigger(s) match it."""

DISCOVERY = """\
[adf-agent] Source system 'SRC' — 2 active candidate pipeline(s) in factory 'adf-fin':
  Factory | Pipeline | Status | Last Run | Run Id | Evidence | Open
  GROUP A — candidates that also reference view 'SRC_VW' (1):
  adf-fin | pl_a | Succeeded | 2026-10-29 17:28:11 UTC | runId=ra | matched: name | [open run](<https://x/ra>)
  GROUP B — candidates that reference 'SRC' without naming view 'SRC_VW' (1):
  adf-fin | pl_b | Succeeded | 2026-10-28 17:28:11 UTC | runId=rb | matched: name | [open run](<https://x/rb>)"""

RUN_TREE = """\
[adf-agent] Pipeline family (factory 'adf-fin')
  run leaf is a CHILD; its family root is pl_root (runId=root):
pl_root (runId=root) → Failed [open run](<https://x/root>)
  start: (unknown), end: (unknown), duration: 1s
  • Exec [ExecutePipeline] → Failed (failed because its child run below failed)
    pl_leaf (runId=leaf) → Failed [open run](<https://x/leaf>)
      • Copy [Copy] → Failed
          error 2200: sink timeout
      (1 activities: 1 failed)
  (1 activities: 1 failed)"""

FILE_ARRIVAL = """\
[adls-agent] File arrival check
  landing path : lnd/src/feed (account lake, container adls)
  SLA given    : none in the question
  status       : File received and processed successfully

Folders checked:
  archive: 1 file(s)
    - lnd/src/feed/archive/FEED_20261012.csv | 152 bytes | created 2026-10-12 06:25 EDT (10:25 UTC)
  error: none"""

SAMPLES = (RECOVERY, TRIGGERS, DISCOVERY, RUN_TREE, FILE_ARRIVAL)


def lines_stay_apart(markdown: str) -> None:
    """Fail if any two lines of ``markdown`` would render as one.

    Every non-blank line is a bullet, nested at most one level below the bullet
    before it, or a paragraph with a blank line (or the edge) on both sides.
    """

    lines = markdown.split("\n")
    depth = -1
    for index, line in enumerate(lines):
        if not line.strip():
            depth = -1
            continue
        bullet = re.match(r"( *)- \S", line)
        if bullet:
            indent = len(bullet.group(1))
            assert indent % 2 == 0 and indent // 2 <= depth + 1, f"mis-nested: {line!r}"
            depth = indent // 2
            continue
        assert not line.startswith(" "), f"indented text, not a bullet: {line!r}"
        before = index == 0 or not lines[index - 1].strip()
        after = index == len(lines) - 1 or not lines[index + 1].strip()
        assert before and after, f"paragraph joins a neighbour: {line!r}"
        depth = -1


def _words(text: str) -> str:
    """``text`` without its whitespace, list markers, bold and escapes."""

    lines = (re.sub(r"^(?:[-•] +|\\)", "", line.strip()) for line in text.split("\n"))
    lines = (re.sub(r"^(\d+)\\", r"\1", line) for line in lines)
    return "".join(line.replace("**", "") for line in lines).replace(" ", "")


# --- as_markdown ----------------------------------------------------------------


def test_a_recovery_validation_reads_one_field_per_bullet() -> None:
    assert as_markdown(RECOVERY) == RECOVERY_MARKDOWN


def test_rows_stay_one_bullet_each_under_a_bold_column_header() -> None:
    assert as_markdown(TRIGGERS).split("\n") == [
        "[adf-agent] Current trigger states (factory 'adf-fin') - 2/2 found",
        "",
        "**Trigger | Factory | ADF State | Business State | Starts pipeline(s)**",
        "",
        "- tr_daily | adf-fin | Stopped | Disabled | pl_load",
        # A row of capitalised words right below the header is still a row.
        "- TRDAILY | Fin | Started | Enabled | Main",
        (
            "- **NOTE:** '*' is a name pattern (letter case and separators ignored); "
            "2 trigger(s) match it."
        ),
    ]


def test_group_headings_stand_above_their_rows() -> None:
    assert as_markdown(DISCOVERY).split("\n")[2:] == [
        "**Factory | Pipeline | Status | Last Run | Run Id | Evidence | Open**",
        "",
        "GROUP A — candidates that also reference view 'SRC_VW' (1):",
        "",
        (
            "- adf-fin | pl_a | Succeeded | 2026-10-29 17:28:11 UTC | runId=ra | matched: name "
            "| [open run](<https://x/ra>)"
        ),
        "",
        "GROUP B — candidates that reference 'SRC' without naming view 'SRC_VW' (1):",
        "",
        (
            "- adf-fin | pl_b | Succeeded | 2026-10-28 17:28:11 UTC | runId=rb | matched: name "
            "| [open run](<https://x/rb>)"
        ),
    ]


def test_nesting_follows_the_indentation() -> None:
    assert as_markdown(RUN_TREE).split("\n")[2:] == [
        "- run leaf is a CHILD; its family root is pl_root (runId=root):",
        "",
        "pl_root (runId=root) → Failed [open run](<https://x/root>)",
        "",
        # Several fields on one line: none of them is bolded.
        "- start: (unknown), end: (unknown), duration: 1s",
        "- Exec [ExecutePipeline] → Failed (failed because its child run below failed)",
        "  - pl_leaf (runId=leaf) → Failed [open run](<https://x/leaf>)",
        "    - Copy [Copy] → Failed",
        # Four columns deeper in the text, one level deeper in the list.
        "      - **error 2200:** sink timeout",
        "    - (1 activities: 1 failed)",
        "- (1 activities: 1 failed)",
    ]


def test_a_file_arrival_check_keeps_its_folders_apart() -> None:
    assert as_markdown(FILE_ARRIVAL).split("\n") == [
        "[adls-agent] File arrival check",
        "",
        "- **landing path:** lnd/src/feed (account lake, container adls)",
        "- **SLA given:** none in the question",
        "- **status:** File received and processed successfully",
        "",
        "Folders checked:",
        "",
        "- **archive:** 1 file(s)",
        (
            "  - lnd/src/feed/archive/FEED_20261012.csv | 152 bytes | created 2026-10-12 06:25 EDT "
            "(10:25 UTC)"
        ),
        "- **error:** none",
    ]


def test_text_that_would_open_a_block_stays_text() -> None:
    assert as_markdown("[adf-agent] x\n  # 3 runs\n  > 5 min\n  1. first\n  * star") == (
        "[adf-agent] x\n\n- \\# 3 runs\n- \\> 5 min\n- 1\\. first\n- \\* star"
    )


def test_no_line_joins_another_and_no_word_changes() -> None:
    for sample in SAMPLES:
        markdown = as_markdown(sample)
        lines_stay_apart(markdown)
        assert _words(markdown) == _words(sample)
        assert as_markdown(markdown) == markdown, "laying it out twice changes it"
        assert "|---" not in markdown.replace(" ", ""), "never a markdown table"


def test_a_one_line_result_is_left_as_it_is() -> None:
    line = "[adf-agent] Run 'r1' was not found in factory 'adf-fin'."
    assert as_markdown(line) == line


# --- the middleware ---------------------------------------------------------------

TOOLS = ("validate_pipeline_recovery", "get_trigger_states")


@dataclass
class _FakeModelRequest:
    messages: list

    def override(self, **overrides: Any) -> _FakeModelRequest:
        return replace(self, **overrides)


def _call(call_id: str, name: str) -> dict:
    return {"name": name, "args": {}, "id": call_id, "type": "tool_call"}


def _turn(*results: tuple[str, str, str]) -> list:
    """A task with one tool call and result per (call id, tool name, content)."""

    messages: list = [HumanMessage(content="Did the failed run recover?")]
    messages.append(
        AIMessage(content="", tool_calls=[_call(cid, name) for cid, name, _ in results])
    )
    messages += [ToolMessage(content=content, tool_call_id=cid) for cid, _, content in results]
    return messages


def _seen(request: _FakeModelRequest, *middleware: Any) -> _FakeModelRequest:
    """The request the model is handed after ``middleware``, outermost first."""

    seen: list[_FakeModelRequest] = []

    def model(req: _FakeModelRequest) -> AIMessage:
        seen.append(req)
        return AIMessage(content="done")

    def wrapped(layer, inner):
        return lambda req: layer.wrap_model_call(req, inner)

    handler = model
    for layer in reversed(middleware):
        handler = wrapped(layer, handler)
    handler(request)
    return seen[0]


def test_the_model_sees_its_tools_results_as_markdown() -> None:
    request = _FakeModelRequest(messages=_turn(("c1", TOOLS[0], RECOVERY)))
    seen = _seen(request, ToolMarkdownMiddleware(TOOLS))
    assert seen.messages[-1].content == RECOVERY_MARKDOWN
    # View-only: the persisted message still holds the tool's own text.
    assert request.messages[-1].content == RECOVERY


def test_other_tools_and_failed_or_non_text_results_are_left_alone() -> None:
    blocks = [{"type": "text", "text": RECOVERY}]
    messages = _turn(
        ("c1", "ai_search_tool", RECOVERY),
        ("c2", TOOLS[0], RECOVERY),
        ("c3", TOOLS[1], blocks),
    )
    messages[-2] = messages[-2].model_copy(update={"status": "error"})
    request = _FakeModelRequest(messages=messages)
    assert _seen(request, ToolMarkdownMiddleware(TOOLS)) is request


def test_nothing_to_lay_out_passes_the_request_through() -> None:
    request = _FakeModelRequest(messages=[HumanMessage(content="hi")])
    assert _seen(request, ToolMarkdownMiddleware(TOOLS)) is request


def test_the_async_path_lays_them_out_too() -> None:
    seen: list[_FakeModelRequest] = []

    async def model(req: _FakeModelRequest) -> AIMessage:
        seen.append(req)
        return AIMessage(content="done")

    request = _FakeModelRequest(messages=_turn(("c1", TOOLS[1], TRIGGERS)))
    asyncio.run(ToolMarkdownMiddleware(TOOLS).awrap_model_call(request, model))
    assert seen[0].messages[-1].content == as_markdown(TRIGGERS)


def test_the_keep_requirement_follows_the_markdown_on_the_adf_agent() -> None:
    """Wired as on adf-agent: the layout first, then the untagged keep reminder."""

    request = _FakeModelRequest(messages=_turn(("c1", TOOLS[0], RECOVERY)))
    seen = _seen(request, ToolMarkdownMiddleware(TOOLS), AdfResultKeepMiddleware(TOOLS))
    expected = RECOVERY_MARKDOWN.replace("[adf-agent] ", "")
    assert seen.messages[-1].content == f"{expected}\n\n{KEEP_REQUIREMENT}"


def test_both_data_subagents_wire_it_over_their_own_tools() -> None:
    from v1.core.subagents import ADF_SUBAGENT, ADLS_SUBAGENT
    from v1.core.tools import ADF_TOOLS, ADLS_TOOLS

    adf = ADF_SUBAGENT["middleware"]
    layout = [m for m in adf if isinstance(m, ToolMarkdownMiddleware)]
    assert len(layout) == 1
    assert layout[0].tool_names == {tool.name for tool in ADF_TOOLS}
    keep = next(m for m in adf if isinstance(m, AdfResultKeepMiddleware))
    assert adf.index(layout[0]) < adf.index(keep), "the keep reminder goes below the layout"

    (adls,) = [m for m in ADLS_SUBAGENT["middleware"] if isinstance(m, ToolMarkdownMiddleware)]
    assert adls.tool_names == {tool.name for tool in ADLS_TOOLS}


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
