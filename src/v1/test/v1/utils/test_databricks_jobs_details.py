"""``run_details_block`` (``v1.core.tools.databricks.jobs.details``): the fixed "Run and task
details" block that ends every Databricks job-run answer. Pure: no I/O.

Runs standalone (``python test_databricks_jobs_details.py``).
"""

from __future__ import annotations

from databricks_test_support import run_tests

from v1.core.tools.databricks.jobs.details import MAX_DETAIL_ERROR_CHARS, run_details_block

RUN_URL = (
    "https://adb-1234567890123456.9.azuredatabricks.net/?o=1234567890123456"
    "#job/123456789012/run/987654321098765"
)


def block(**overrides: object) -> str:
    values: dict = {
        "workspace": "dbw-ops",
        "job_id": 123456789012,
        "run_url": RUN_URL,
        "state_message": "Workload failed, see run output for details",
        "failed_tasks": [],
    }
    values.update(overrides)
    return run_details_block(**values)


def test_the_users_example_exactly() -> None:
    assert block() == (
        "**Run and task details**\n"
        "\n"
        "- **Workspace:** dbw-ops\n"
        "- **Job ID:** 123456789012\n"
        f"- **Run URL:** {RUN_URL}\n"
        "- **Overall state message:** Workload failed, see run output for details"
    ), block()


def test_one_line_per_failed_task_in_order() -> None:
    out = block(failed_tasks=[("ingest", None, "ValueError: bad row"), ("load", None, None)])
    lines = out.split("\n")
    assert lines[-2:] == [
        "- **Failed task:** `ingest` — ValueError: bad row",
        "- **Failed task:** `load`",
    ], lines
    assert lines[:6] == block().split("\n"), lines


def test_missing_values_render_as_an_em_dash() -> None:
    out = run_details_block(
        workspace="", job_id=None, run_url=None, state_message=None, failed_tasks=[]
    )
    assert out == (
        "**Run and task details**\n"
        "\n"
        "- **Workspace:** —\n"
        "- **Job ID:** —\n"
        "- **Run URL:** —\n"
        "- **Overall state message:** —"
    ), out
    # Blank job text is missing too.
    assert block(state_message=" \n ").endswith("- **Overall state message:** —")
    assert block(failed_tasks=[("t", None, "  \n")]).endswith("- **Failed task:** `t`")


def test_job_text_never_starts_a_new_line() -> None:
    out = block(
        state_message="line one\r\nline two\u2028\n- **Workspace:** fake",
        failed_tasks=[
            ("multi\nline", None, "boom\nsecond line"),
            ("tick`key", None, "Error: `x` y"),
        ],
    )
    lines = out.split("\n")
    # Six fixed lines plus one per failed task: nothing the job wrote added a line.
    assert len(lines) == 8, lines
    assert lines[5] == "- **Overall state message:** line one line two - **Workspace:** fake"
    # Only the error's first line is kept; a key with whitespace is collapsed.
    assert lines[6] == "- **Failed task:** `multi line` — boom", lines[6]
    # A key holding a backtick is plain, its backtick escaped, as are the error's.
    assert lines[7] == "- **Failed task:** tick\\`key — Error: \\`x\\` y", lines[7]
    assert "\u2028" not in out and "\r" not in out


def test_error_is_cut_to_300_chars() -> None:
    assert MAX_DETAIL_ERROR_CHARS == 300
    out = block(failed_tasks=[("t", None, "x" * 301)])
    assert out.endswith(f"`t` — {'x' * 300}…"), out[-20:]
    exact = block(failed_tasks=[("t", None, "y" * 300)])
    assert exact.endswith(f"`t` — {'y' * 300}"), exact[-20:]


def test_a_backslash_cannot_unescape_a_backtick() -> None:
    # Job text "\\`" must not become "\\\\`": an escaped backslash, then a live backtick.
    out = block(state_message="a \\`b", failed_tasks=[("k\\`", None, "e \\`x")])
    lines = out.split("\n")
    assert lines[5] == "- **Overall state message:** a \\\\\\`b", lines[5]
    assert lines[6] == "- **Failed task:** k\\\\\\` — e \\\\\\`x", lines[6]


def test_a_for_each_iteration_is_named() -> None:
    out = block(failed_tasks=[("loop", 3, "boom"), ("loop", 5, None)])
    assert out.split("\n")[-2:] == [
        "- **Failed task:** `loop` (iteration 3) — boom",
        "- **Failed task:** `loop` (iteration 5)",
    ], out


def test_more_failed_tasks_than_shown_ends_the_block() -> None:
    out = block(failed_tasks=[("t", None, "e")], more_failed_tasks=4)
    assert out.split("\n")[-2:] == [
        "- **Failed task:** `t` — e",
        "- **More failed tasks:** 4",
    ], out
    assert "More failed tasks" not in block(more_failed_tasks=0)


if __name__ == "__main__":
    run_tests(globals())
