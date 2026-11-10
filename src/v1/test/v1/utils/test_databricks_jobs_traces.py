"""Tests for trace trimming and child-call parsing (ported from databricks-agent
``test_traces.py``). Pure: no I/O. Runs standalone (``python test_databricks_jobs_traces.py``).
"""

from __future__ import annotations

import time

from databricks_jobs_fakes import CHILD
from databricks_jobs_fakes import PARENT as CAPTURED
from databricks_test_support import run_tests

from v1.core.tools.databricks.jobs.traces import (
    child_call,
    locate,
    resolve_child_path,
    strip_ansi,
    trace_tail,
    trim_error,
    where_failed,
)


def failing_line(trace: str | None) -> tuple[int, str] | None:
    found = locate(trace)
    return (found["line"], found["code"]) if found else None


def test_captured_trace_names_the_child_call() -> None:
    assert child_call(CAPTURED["error_trace"]) == "./example_child"
    assert "ValueError" not in CAPTURED["error_trace"]


def test_last_arrowed_call_wins_and_unarrowed_calls_are_ignored() -> None:
    trace = (
        '      1 dbutils.notebook.run("./not_this", 60)\n'
        '----> 2 dbutils.notebook.run("./first", 60)\n'
        "----> 9 dbutils.notebook.run('/Shared/second', 60)\n"
    )
    assert child_call(trace) == "/Shared/second"


def test_resolve_child_path() -> None:
    folder = "/Users/dev@example.com/run-failures-fixture"
    parent = f"{folder}/example_parent"
    assert resolve_child_path(parent, "./example_child") == f"{folder}/example_child"
    assert resolve_child_path(parent, "../lib/x") == "/Users/dev@example.com/lib/x"
    assert resolve_child_path(parent, "sibling") == f"{folder}/sibling"
    assert resolve_child_path(None, "/Shared//a/./b") == "/Shared/a/b"
    # normpath keeps a leading "//"; workspace paths have one slash.
    assert resolve_child_path(None, "//Shared/x") == "/Shared/x"
    assert resolve_child_path(None, "./x") is None


def test_ansi_codes_are_stripped() -> None:
    assert strip_ansi("\x1b[31mValueError\x1b[39m: bad") == "ValueError: bad"
    assert strip_ansi("\x1b[2K\x1b[?25lok\x1b[1A") == "ok"  # other CSI codes too
    assert trace_tail("\x1b[32m" + "a" * 10) == "a" * 10


def test_plain_trace_has_no_child() -> None:
    assert child_call("Traceback ...\nValueError: bad") is None
    assert child_call(None) is None and child_call("") is None


def test_trim_error_and_trace_tail() -> None:
    assert trim_error("x" * 5000) == "x" * 1000 + "…"
    tail = trace_tail("a" * 3000 + "END")
    assert tail.endswith("END") and len(tail) == 2001 and tail.startswith("…")
    assert trim_error("short") == "short" and trace_tail(None) is None
    # Boundaries: exactly at the limit is kept whole.
    assert trim_error("x" * 1000) == "x" * 1000
    assert trim_error("x" * 1001) == "x" * 1000 + "…"
    assert trace_tail("a" * 2000) == "a" * 2000
    assert trace_tail("a" * 2001) == "…" + "a" * 2000


def test_child_call_stays_fast_on_hostile_traces() -> None:
    hostile = [
        "-" * 1_000_000 + " dbutils.notebook.run(",
        "-" * 1_000_000 + "> 1 dbutils.notebook.run(",
        "----> 1 x " * 200_000 + 'dbutils.notebook.run("',
        "\n".join(['----> 1 dbutils.notebook.run("' + "a" * 2000] * 500),
        # Each run below sits between two pieces of the pattern that could both match it.
        " " * 1_000_000 + "-- dbutils.notebook.run(",
        "--> " + " " * 1_000_000 + "x dbutils.notebook.run(",
        "--> " + "1" * 1_000_000 + "x dbutils.notebook.run(",
        "----> 1" + " " * 1_000_000 + "dbutils.notebook.run(",
        "----> 1" + "\t " * 500_000 + "dbutils.notebook.run(",
        "----> 1 dbutils.notebook.run(" + " " * 1_000_000 + "x",
        "----> 1 " + "dbutils.notebook.run('\"" * 50_000,
        "----> 1 " + 'dbutils.notebook.run("' + "a" * 1_000_000,
    ]
    for trace in hostile:
        start = time.perf_counter()
        assert child_call(trace) is None
        assert time.perf_counter() - start < 2


def test_failing_line_in_the_fixtures() -> None:
    child_call_line = 'dbutils.notebook.run("./example_child", 300)'
    assert failing_line(CAPTURED["error_trace"]) == (2, child_call_line)
    child_line = 'total = sum(float(row["amount"]) for row in rows)  # fails on "n/a"'
    assert failing_line(CHILD["error_trace"]) == (1, child_line)


def test_library_frames_are_not_the_users_line() -> None:
    trace = (
        "File /databricks/python_shell/lib/dbruntime/dbutils.py:384, in run(self)\n"
        "--> 384 return self._driver_connection.notebookRun(\n"
    )
    assert failing_line(trace) is None
    assert failing_line(None) is None and failing_line("") is None


def test_ipython_cell_frames_count_too() -> None:
    trace = "Cell In[3], line 4\n      3 x = 1\n----> 4 y = x / 0\n"
    assert failing_line(trace) == (4, "y = x / 0")


def test_long_code_line_is_cut() -> None:
    trace = "File <command-1>, line 1\n----> 1 " + "x" * 5000 + "\n"
    line_no, code = failing_line(trace)
    assert line_no == 1 and len(code) <= 301 and code.endswith("…")


FAILING_LINE_HOSTILE = [
    "File <command-1>, line 1\n" + "-" * 1_000_000 + ">",  # dash-run
    "File <command-1>, line 1\n----> 1" + " " * 1_000_000 + "x",  # space-run
    "File <command-1>, line 1\n" + "----> 1 x " * 200_000,  # arrow-run
]


def test_failing_line_stays_linear() -> None:
    for trace in FAILING_LINE_HOSTILE:
        started = time.perf_counter()
        failing_line(trace)
        assert time.perf_counter() - started < 2


OTHER_CELL = (
    "ZeroDivisionError    Traceback (most recent call last)\n"
    "File <command-11>, line 3\n"
    "      2 y = 0\n"
    "----> 3 x = f(1, y)\n"
    "File <command-22>, line 2, in f(a, b)\n"
    "      1 def f(a, b):\n"
    "----> 2     return a / b\n"
    "ZeroDivisionError: division by zero\n"
)


def test_error_raised_in_another_cell_reports_the_cell_that_ran() -> None:
    # A function from another cell (or a %run notebook): its lines aren't the failing cell's.
    assert failing_line(OTHER_CELL) == (3, "x = f(1, y)")
    assert locate(OTHER_CELL)["raised"] == {"function": "f", "line": 2, "code": "return a / b"}


def test_function_in_the_same_cell_needs_no_second_line() -> None:
    trace = (
        "File <command-11>, line 5\n----> 5 g()\nFile <command-11>, line 2, in g()\n----> 2 1 / 0\n"
    )
    assert locate(trace) == {"line": 2, "code": "1 / 0", "raised": None}


CHAINED = (
    "ValueError    Traceback (most recent call last)\n"
    "File <command-11>, line 4\n----> 4 n = int(raw)\n"
    "ValueError: invalid literal\n\n"
    "During handling of the above exception, another exception occurred:\n\n"
    "RuntimeError    Traceback (most recent call last)\n"
    'File <command-11>, line 6\n----> 6 raise RuntimeError("config missing")\n'
    "RuntimeError: config missing\n"
)


def test_chained_exception_uses_the_reported_one() -> None:
    assert failing_line(CHAINED) == (6, 'raise RuntimeError("config missing")')


def test_chained_exception_falls_back_when_the_last_has_no_user_frame() -> None:
    trace = CHAINED.split("RuntimeError    Traceback")[0] + (
        "RuntimeError    Traceback (most recent call last)\n"
        "File /databricks/python/lib/x.py:9, in y()\n--> 9 raise RuntimeError()\n"
    )
    assert failing_line(trace) == (4, "n = int(raw)")


def test_where_prefers_the_child_notebook() -> None:
    task = {
        "task_key": "parent",
        "iteration": None,
        "notebook_path": "/Users/dev/fixture/example_parent",
        "trace": CAPTURED["error_trace"],
        "child_notebook": "/Users/dev/fixture/example_child",
        "child_trace": CHILD["error_trace"],
    }
    assert where_failed(task) == {
        "task_key": "parent",
        "iteration": None,
        "notebook": "/Users/dev/fixture/example_child",
        "line": 1,
        "code": 'total = sum(float(row["amount"]) for row in rows)  # fails on "n/a"',
        "raised": None,
        "calls": None,
    }


def test_where_falls_back_to_the_parents_call_when_the_child_wasnt_read() -> None:
    task = {
        "task_key": "parent",
        "notebook_path": "/Users/dev/fixture/example_parent",
        "trace": CAPTURED["error_trace"],
        "child_notebook": "/Users/dev/fixture/example_child",
        "child_trace": None,
    }
    assert where_failed(task) == {
        "task_key": "parent",
        "iteration": None,
        "notebook": "/Users/dev/fixture/example_parent",
        "line": 2,
        "code": 'dbutils.notebook.run("./example_child", 300)',
        "raised": None,
        "calls": "/Users/dev/fixture/example_child",
    }


def test_where_uses_the_task_notebook_without_a_child() -> None:
    task = {
        "task_key": "p",
        "iteration": 2,
        "notebook_path": "/n",
        "trace": CAPTURED["error_trace"],
    }
    assert where_failed(task)["notebook"] == "/n" and where_failed(task)["line"] == 2


def test_where_without_a_code_line_names_task_and_notebook() -> None:
    task = {"task_key": "p", "notebook_path": "/n", "trace": None, "child_notebook": "/c"}
    assert where_failed(task) == {
        "task_key": "p",
        "iteration": None,
        "notebook": "/n",
        "line": None,
        "code": None,
        "raised": None,
        "calls": "/c",
    }


def test_where_reports_a_line_raised_in_another_cell() -> None:
    task = {"task_key": "p", "notebook_path": "/n", "trace": OTHER_CELL}
    where = where_failed(task)
    assert (where["line"], where["raised"]["function"]) == (3, "f")


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
