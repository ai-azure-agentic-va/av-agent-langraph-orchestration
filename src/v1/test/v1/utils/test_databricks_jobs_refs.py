"""Tests for run/job reference parsing (ported from databricks-agent ``test_refs.py``).

Pure: no I/O. Runs standalone (``python test_databricks_jobs_refs.py``).
"""

from __future__ import annotations

from databricks_test_support import run_tests

from v1.core.tools.databricks.jobs.refs import link_host, parse_job_id, parse_run_ref

HOST = "https://adb-123.azuredatabricks.net"


def test_url_forms() -> None:
    cases = [
        ("987654", 987654),
        (" 987654 ", 987654),
        (f"{HOST}/jobs/12/runs/987654", 987654),
        (f"{HOST}/jobs/12/runs/987654/", 987654),
        (f"{HOST}/jobs/12/runs/987654?o=123456", 987654),
        ("https://ADB-123.azuredatabricks.net/jobs/12/runs/987654", 987654),
        (f"{HOST}/?o=123#job/12/run/987654", 987654),
    ]
    for text, run_id in cases:
        assert parse_run_ref(text, HOST) == run_id, text


def test_host_without_scheme_matches() -> None:
    assert parse_run_ref(f"{HOST}/jobs/1/runs/2", "adb-123.azuredatabricks.net") == 2


def test_foreign_or_malformed_urls_rejected() -> None:
    cases = [
        "https://adb-999.azuredatabricks.net/jobs/12/runs/987654",  # foreign workspace
        "http://adb-123.azuredatabricks.net/jobs/12/runs/987654",  # not https
        f"{HOST}/jobs/12/runs/abc",
        f"{HOST}/jobs/12",
        f"{HOST}/sql/editor",
        "12abc",
        "-5",
        "0",
        "9" * 20,
        "",
        "run 42",
    ]
    for text in cases:
        assert parse_run_ref(text, HOST) is None, text


def test_urls_need_a_known_host() -> None:
    assert parse_run_ref(f"{HOST}/jobs/12/runs/987654", None) is None
    assert parse_run_ref("987654", None) == 987654


def test_job_id_or_name() -> None:
    assert parse_job_id(" 42 ") == 42
    assert parse_job_id("nightly claims load") is None


def test_link_host() -> None:
    cases = [
        (f"{HOST}/jobs/12/runs/987654", "adb-123.azuredatabricks.net"),
        (f" {HOST}/jobs/12/runs/987654/?o=1 ", "adb-123.azuredatabricks.net"),
        ("https://ADB-999.azuredatabricks.net/jobs/1/runs/2", "adb-999.azuredatabricks.net"),
        ("https://adb-999.azuredatabricks.net/?o=1#job/12/run/9", "adb-999.azuredatabricks.net"),
        ("987654", None),  # a bare run ID has no host
        ("http://adb-999.azuredatabricks.net/jobs/1/runs/2", None),  # not https
        (f"{HOST}/jobs/12", None),  # not a run link
        (f"{HOST}/sql/editor", None),
        ("https:///jobs/1/runs/2", None),
        ("https://[::1/jobs/1/runs/2", None),  # malformed
        ("", None),
        ("run 42", None),
    ]
    for text, host in cases:
        assert link_host(text) == host, text


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
