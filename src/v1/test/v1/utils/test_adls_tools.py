"""Offline tests for check_file_arrival (in-memory blob fake; no network, no credentials)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import v1.core.tools.adls.tools as adls

ET = ZoneInfo("America/New_York")
FEED = "lnd/loansys/daily_payments"
DAY = "2026-10-12"
ON_DAY = datetime(2026, 10, 12, 10, 25, tzinfo=UTC)  # 06:25 EDT
DAY_BEFORE = datetime(2026, 10, 11, 23, 0, tzinfo=UTC)  # 19:00 EDT on the 11th
LATE_EVENING = datetime(2026, 10, 13, 3, 30, tzinfo=UTC)  # 23:30 EDT on the 12th

_ACCOUNT = {"account_url": "https://stlake.dfs.core.windows.net", "filesystem": "adls"}


class _Settings:
    def __init__(
        self,
        mapping: dict | None = None,
        allowed: list[str] | None = None,
        default: str | None = None,
        timezone: str = "America/New_York",
    ) -> None:
        self.adls_account_mapping = {"datalake": _ACCOUNT} if mapping is None else mapping
        self.adls_allowed_path_prefixes = ["lnd/"] if allowed is None else allowed
        self.adls_default_account = default
        self.adls_business_timezone = timezone


class _FakeBlob:
    def __init__(
        self, name: str, modified: datetime, size: int = 152, folder: bool = False
    ) -> None:
        self.name = name
        self.size = 0 if folder else size
        self.last_modified = modified
        self.creation_time = modified
        self.metadata = {"hdi_isfolder": "true"} if folder else {}


class _StorageError(Exception):
    """Shaped like azure-storage's HttpResponseError: a code, a status and a noisy message."""

    error_code = "AuthorizationPermissionMismatch"
    status_code = 403

    def __init__(self) -> None:
        super().__init__("This request is not authorized. RequestId:0f1e-internal Time:2026-10-12")


class _FakeContainer:
    """Mimics the aio ContainerClient listing, folder placeholders included."""

    def __init__(self, files: dict[str, datetime], failing_prefixes: tuple[str, ...] = ()) -> None:
        self._files = files
        self._failing = failing_prefixes
        self.list_calls = 0

    def _entries(self) -> list[_FakeBlob]:
        folders = {
            "/".join(name.split("/")[:depth])
            for name in self._files
            for depth in range(1, name.count("/") + 1)
        }
        entries = [_FakeBlob(name, modified) for name, modified in self._files.items()]
        entries += [_FakeBlob(name, ON_DAY, folder=True) for name in folders]
        return sorted(entries, key=lambda blob: blob.name)

    def list_blobs(
        self,
        name_starts_with: str = "",
        include: list[str] | None = None,
        results_per_page: int | None = None,
    ):
        self.list_calls += 1

        async def _gen():
            if any(name_starts_with.startswith(prefix) for prefix in self._failing):
                raise _StorageError()
            for blob in self._entries():
                if blob.name.startswith(name_starts_with):
                    yield blob

        return _gen()


def _patch(settings: _Settings, container: _FakeContainer):
    saved_settings, saved_container = adls.settings, adls._container
    adls.settings = settings

    def _fake_container(account_url: str, filesystem: str) -> _FakeContainer:
        return container

    adls._container = _fake_container

    def restore() -> None:
        adls.settings, adls._container = saved_settings, saved_container

    return restore


def _check(container: _FakeContainer, settings: _Settings | None = None, **args) -> str:
    restore = _patch(settings or _Settings(), container)
    try:
        return asyncio.run(adls.check_file_arrival.ainvoke({"path": FEED, "date": DAY, **args}))
    finally:
        restore()


def _logged(container: _FakeContainer, **args) -> list[tuple[int, str]]:
    """Run one check and return the tool's log records as (level, message)."""
    records: list[logging.LogRecord] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    tool_logger = logging.getLogger(adls.__name__)
    sink, saved_level = _Sink(), tool_logger.level
    tool_logger.addHandler(sink)
    tool_logger.setLevel(logging.INFO)
    try:
        _check(container, **args)
    finally:
        tool_logger.removeHandler(sink)
        tool_logger.setLevel(saved_level)
    return [(record.levelno, record.getMessage()) for record in records]


def test_status_follows_the_landing_folder() -> None:
    test_cases = [
        {
            "name": "archive_means_processed",
            "files": {f"{FEED}/archive/20261012062500/DAILY_PAYMENTS_20261012.csv": ON_DAY},
            "expected": "File received and processed successfully",
        },
        {
            "name": "error_means_failed",
            "files": {f"{FEED}/error/DAILY_PAYMENTS_20261012.csv": ON_DAY},
            "expected": "File received; processing failed",
        },
        {
            "name": "inbound_means_processing",
            "files": {f"{FEED}/inbound/DAILY_PAYMENTS_20261012.csv": ON_DAY},
            "expected": "File received; still being processed",
        },
        {
            "name": "newest_file_wins",
            "files": {
                f"{FEED}/error/DAILY_PAYMENTS_20261012.csv": ON_DAY,
                f"{FEED}/archive/x/DAILY_PAYMENTS_20261012.csv": ON_DAY + timedelta(hours=1),
            },
            "expected": "File received and processed successfully",
        },
        {
            "name": "late_evening_still_counts_for_the_business_day",
            "files": {f"{FEED}/inbound/DAILY_PAYMENTS_20261012.csv": LATE_EVENING},
            "expected": "File received; still being processed",
        },
        {
            "name": "previous_day_file_is_not_counted",
            "files": {f"{FEED}/archive/x/DAILY_PAYMENTS_20261011.csv": DAY_BEFORE},
            "expected": f"File not found for this date — no file under inbound, error or archive was last modified on {DAY}",
        },
        {
            "name": "existing_empty_feed_is_not_found",
            "files": {f"{FEED}/archive/.keep": DAY_BEFORE},
            "expected": "File not found for this date",
        },
    ]
    for test_data in test_cases:
        out = _check(_FakeContainer(test_data["files"]))
        assert f"status       : {test_data['expected']}" in out, test_data["name"]


def test_arrival_and_sla_are_reported_as_facts() -> None:
    test_cases = [
        {
            "name": "sla_shown_as_given",
            "args": {"sla_time": "6:00 AM ET"},
            "expected": [
                "  SLA given    : 6:00 AM ET",
                "  business date: 2026-10-12 (America/New_York)",
                "  arrived      : 2026-10-12 06:25 EDT (10:25 UTC)",
            ],
        },
        {
            "name": "no_sla_given",
            "args": {},
            "expected": ["  SLA given    : none in the question"],
        },
    ]
    files = {f"{FEED}/archive/x/DAILY_PAYMENTS_20261012.csv": ON_DAY}
    for test_data in test_cases:
        out = _check(_FakeContainer(files), **test_data["args"])
        for line in test_data["expected"]:
            assert line in out, (test_data["name"], line, out)
        assert "late" not in out.lower() and "on time" not in out.lower(), test_data["name"]


def test_daylight_saving_days_use_the_local_day() -> None:
    test_cases = [
        {
            "name": "spring_forward",
            "date": "2026-03-08",
            "inside": datetime(2026, 3, 8, 5, 0, tzinfo=UTC),
            "outside": datetime(2026, 3, 9, 4, 0, tzinfo=UTC),
        },
        {
            "name": "fall_back",
            "date": "2025-11-02",
            "inside": datetime(2025, 11, 3, 4, 30, tzinfo=UTC),
            "outside": datetime(2025, 11, 3, 5, 0, tzinfo=UTC),
        },
    ]
    for test_data in test_cases:
        files = {
            f"{FEED}/archive/a/inside.csv": test_data["inside"],
            f"{FEED}/archive/b/outside.csv": test_data["outside"],
        }
        out = _check(_FakeContainer(files), date=test_data["date"])
        assert "inside.csv" in out and "outside.csv" not in out, test_data["name"]


def test_rejected_inputs_never_touch_storage() -> None:
    test_cases = [
        {"name": "missing_path", "args": {"path": ""}, "expected": "No landing path was given"},
        {
            "name": "parent_segment",
            "args": {"path": "lnd/../secret"},
            "expected": "'.' and '..' are not permitted",
        },
        {"name": "backslash", "args": {"path": "lnd\\loansys"}, "expected": "use forward slashes"},
        {
            "name": "outside_allow_list",
            "args": {"path": "raw/loansys/daily_payments"},
            "expected": "outside the permitted landing folders (lnd)",
        },
        {
            "name": "prefix_lookalike",
            "args": {"path": "lndx/loansys"},
            "expected": "outside the permitted landing folders",
        },
        {"name": "folder_only", "args": {"path": "archive"}, "expected": "names no feed folder"},
        {
            "name": "unknown_url_account",
            "args": {"path": "abfss://adls@other.dfs.core.windows.net/lnd/a"},
            "expected": "is not a configured Data Lake account",
        },
        {
            "name": "wrong_url_container",
            "args": {"path": "abfss://raw@stlake.dfs.core.windows.net/lnd/a"},
            "expected": "is not a configured Data Lake account",
        },
        {
            "name": "bad_date_format",
            "args": {"date": "10/12/2026"},
            "expected": "Pass it as YYYY-MM-DD",
        },
        {
            "name": "impossible_date",
            "args": {"date": "2026-02-30"},
            "expected": "Could not read the date",
        },
        {"name": "future_date", "args": {"date": "2999-01-01"}, "expected": "is in the future"},
    ]
    for test_data in test_cases:
        container = _FakeContainer({})
        out = _check(container, **test_data["args"])
        assert out.startswith("[adls-agent]"), test_data["name"]
        assert test_data["expected"] in out, (test_data["name"], out)
        assert container.list_calls == 0, test_data["name"]


def test_configuration_gaps_are_reported() -> None:
    test_cases = [
        {
            "name": "empty_allow_list",
            "settings": _Settings(allowed=[]),
            "expected": "ADLS_ALLOWED_PATH_PREFIXES is empty",
        },
        {
            "name": "no_account",
            "settings": _Settings(mapping={}),
            "expected": "No usable Data Lake account is configured",
        },
        {
            "name": "several_accounts_no_default",
            "settings": _Settings(mapping={"a": _ACCOUNT, "b": _ACCOUNT}),
            "expected": "No usable Data Lake account is configured",
        },
        {
            "name": "misconfigured_account",
            "settings": _Settings(
                mapping={"a": {"account_url": "https://x.blob.core.windows.net"}}
            ),
            "expected": "No usable Data Lake account is configured",
        },
        {
            "name": "unknown_timezone",
            "settings": _Settings(timezone="Mars/Olympus_Mons"),
            "expected": "ADLS_BUSINESS_TIMEZONE 'Mars/Olympus_Mons' is not a valid timezone",
        },
    ]
    for test_data in test_cases:
        container = _FakeContainer({})
        out = _check(container, settings=test_data["settings"])
        assert test_data["expected"] in out, (test_data["name"], out)
        assert container.list_calls == 0, test_data["name"]


def test_path_forms_resolve_to_the_same_feed() -> None:
    test_cases = [
        {"name": "relative", "path": FEED},
        {"name": "trailing_slash", "path": f"/{FEED}/"},
        {"name": "landing_folder_suffix", "path": f"{FEED}/archive"},
        {"name": "abfss_url", "path": f"abfss://adls@stlake.dfs.core.windows.net/{FEED}"},
        {
            "name": "https_blob_url",
            "path": f"https://stlake.blob.core.windows.net/adls/{FEED}/inbound/",
        },
        {"name": "wasbs_url", "path": f"wasbs://adls@stlake.blob.core.windows.net/{FEED}"},
        {
            "name": "percent_encoded_url",
            "path": "abfss://adls@stlake.dfs.core.windows.net/lnd/loansys/daily%5Fpayments",
        },
        {"name": "timestamp_subfolder", "path": f"{FEED}/archive/20261012102500"},
        {
            "name": "path_to_the_file",
            "path": f"{FEED}/archive/x/DAILY_PAYMENTS_20261012.csv",
            "filter": "DAILY_PAYMENTS_20261012.csv",
        },
        {
            "name": "url_to_the_file",
            "path": f"abfss://adls@stlake.dfs.core.windows.net/{FEED}/inbound/PMT.csv",
            "filter": "PMT.csv",
        },
    ]
    files = {f"{FEED}/archive/x/DAILY_PAYMENTS_20261012.csv": ON_DAY}
    for test_data in test_cases:
        out = _check(_FakeContainer(files), path=test_data["path"])
        assert f"  landing path : {FEED} (account datalake, container adls)" in out, test_data[
            "name"
        ]
        if "filter" in test_data:
            assert f"name contains '{test_data['filter']}'" in out, (test_data["name"], out)
        else:
            assert "File received and processed successfully" in out, test_data["name"]
            assert "file filter" not in out, test_data["name"]


def test_listing_problems_are_reported_not_hidden() -> None:
    test_cases = [
        {
            "name": "missing_path_is_incorrect_path",
            "files": {"lnd/other/feed/archive/x.csv": ON_DAY},
            "failing": (),
            "expected": [f"status       : Incorrect path — nothing exists under '{FEED}/'"],
            "absent": ["Folders checked:", "File not found"],
        },
        {
            "name": "one_folder_unreadable_partial",
            "files": {f"{FEED}/archive/.keep": DAY_BEFORE},
            "failing": (f"{FEED}/error/",),
            "expected": [
                "status       : Partial result",
                "  error: could not be read (AuthorizationPermissionMismatch, HTTP 403)",
                "  archive: none",
            ],
            "absent": ["RequestId", "not authorized"],
        },
        {
            "name": "unreadable_folder_but_file_found",
            "files": {f"{FEED}/archive/x/DAILY_PAYMENTS_20261012.csv": ON_DAY},
            "failing": (f"{FEED}/inbound/",),
            "expected": [
                "File received and processed successfully",
                "  note         : Partial result — inbound could not be fully read",
                "  inbound: could not be read (AuthorizationPermissionMismatch, HTTP 403)",
            ],
            "absent": ["RequestId"],
        },
    ]
    for test_data in test_cases:
        out = _check(_FakeContainer(test_data["files"], test_data["failing"]))
        for line in test_data["expected"]:
            assert line in out, (test_data["name"], line, out)
        for line in test_data["absent"]:
            assert line not in out, (test_data["name"], line)


def test_capped_listing_is_a_partial_result_not_a_miss() -> None:
    old_files = {f"{FEED}/archive/old_{i}.csv": DAY_BEFORE for i in range(5)}
    saved, adls._MAX_LISTED = adls._MAX_LISTED, 3
    try:
        out = _check(_FakeContainer(old_files))
    finally:
        adls._MAX_LISTED = saved
    assert "status       : Partial result — no file found in the first 3 entries" in out, out
    assert "File not found for this date" not in out, out

    found = {**old_files, f"{FEED}/inbound/DAILY_PAYMENTS_20261012.csv": ON_DAY}
    saved, adls._MAX_LISTED = adls._MAX_LISTED, 3
    try:
        out = _check(_FakeContainer(found))
    finally:
        adls._MAX_LISTED = saved
    assert "File received; still being processed" in out, out
    assert "note         : Partial result — archive could not be fully read" in out, out
    assert "  archive: none in the first 3 entries (the rest was not read)" in out, out


def test_refusals_failures_and_outcomes_are_logged() -> None:
    found = {f"{FEED}/archive/x/DAILY_PAYMENTS_20261012.csv": ON_DAY}
    test_cases = [
        {
            "name": "refused_path_warns",
            "container": _FakeContainer({}),
            "args": {"path": "raw/loansys/daily_payments"},
            "expected": [(logging.WARNING, "outside the permitted landing folders (lnd)")],
        },
        {
            "name": "input_mistake_is_info_only",
            "container": _FakeContainer({}),
            "args": {"date": "10/12/2026"},
            "expected": [(logging.INFO, "Could not read the date '10/12/2026'")],
        },
        {
            "name": "unreadable_folder_warns_and_outcome_names_it",
            "container": _FakeContainer(found, (f"{FEED}/inbound/",)),
            "args": {"sla_time": "6:00 AM ET"},
            "expected": [
                (
                    logging.INFO,
                    (
                        f"ADLS file arrival check: account=datalake path='{FEED}' "
                        "date=2026-10-12 sla='6:00 AM ET' filter=''"
                    ),
                ),
                (
                    logging.WARNING,
                    (
                        f"ADLS listing of '{FEED}/inbound/' failed (_StorageError): "
                        "This request is not authorized. RequestId:0f1e-internal"
                    ),
                ),
                (logging.INFO, "archive=1, error=0, inbound=unreadable"),
            ],
        },
        {
            "name": "incorrect_path_outcome",
            "container": _FakeContainer({"lnd/other/feed/archive/x.csv": ON_DAY}),
            "args": {},
            "expected": [(logging.INFO, "returned in"), (logging.INFO, "nothing exists under")],
        },
    ]
    for test_data in test_cases:
        logged = _logged(test_data["container"], **test_data["args"])
        for level, text in test_data["expected"]:
            assert any(lvl == level and text in msg for lvl, msg in logged), (
                test_data["name"],
                text,
                logged,
            )
        if all(level < logging.WARNING for level, _ in test_data["expected"]):
            assert all(lvl < logging.WARNING for lvl, _ in logged), (test_data["name"], logged)


def test_file_listing_details() -> None:
    many = {
        f"{FEED}/archive/run{index:02d}/file_{index:02d}.csv": ON_DAY + timedelta(minutes=index)
        for index in range(45)
    }
    test_cases = [
        {
            "name": "pattern_filters_files",
            "files": {
                f"{FEED}/archive/x/DAILY_PAYMENTS_20261012.csv": ON_DAY,
                f"{FEED}/archive/x/OTHER_20261012.csv": ON_DAY + timedelta(hours=1),
            },
            "args": {"file_pattern": "daily_payments_*.csv"},
            "expected": [
                "  archive: 1 file(s)",
                f"  file         : {FEED}/archive/x/DAILY_PAYMENTS_20261012.csv",
            ],
            "absent": ["OTHER_20261012.csv"],
        },
        {
            "name": "folder_placeholders_are_not_files",
            "files": {f"{FEED}/inbound/DEBIT.csv": ON_DAY},
            "args": {},
            "expected": ["  inbound: 1 file(s)", "  archive: none", "  error: none"],
            "absent": [],
        },
        {
            "name": "both_timestamps_and_size_shown",
            "files": {f"{FEED}/error/PMT.csv": ON_DAY},
            "args": {},
            "expected": [
                (
                    f"    - {FEED}/error/PMT.csv | 152 bytes | created 2026-10-12 06:25 EDT (10:25 UTC) "
                    "| lastModified 2026-10-12 06:25 EDT (10:25 UTC)"
                )
            ],
            "absent": [],
        },
        {
            "name": "long_listing_is_capped",
            "files": many,
            "args": {},
            "expected": ["  archive: 45 file(s)", "    … 5 more not shown", "file_44.csv"],
            "absent": ["file_00.csv"],
        },
    ]
    for test_data in test_cases:
        out = _check(_FakeContainer(test_data["files"]), **test_data["args"])
        for line in test_data["expected"]:
            assert line in out, (test_data["name"], line, out)
        for line in test_data["absent"]:
            assert line not in out, (test_data["name"], line)


def test_the_result_reaches_the_model_one_fact_per_line() -> None:
    """adls-agent returns these facts verbatim; as plain lines they rendered as
    one paragraph, so its model reads them laid out as markdown."""
    from v1.core.middlewares.tool_markdown import as_markdown
    from v1.test.v1.utils.test_tool_markdown import lines_stay_apart

    markdown = as_markdown(_check(_FakeContainer({f"{FEED}/archive/x/PMT.csv": ON_DAY})))
    lines_stay_apart(markdown)
    lines = markdown.split("\n")
    assert "- **SLA given:** none in the question" in lines
    assert "- **archive:** 1 file(s)" in lines
    assert any(line.startswith(f"  - {FEED}/archive/x/PMT.csv | ") for line in lines)


def test_file_filter_matches_object_ids_by_contains() -> None:
    files = {
        f"{FEED}/archive/x/DAILY_PAYMENTS_A1B2C3_20261012.csv": ON_DAY,
        f"{FEED}/inbound/OBJ_5f3c-9e21_payload.json": ON_DAY + timedelta(hours=1),
        f"{FEED}/error/OTHER_20261012.csv": ON_DAY + timedelta(hours=2),
    }
    test_cases = [
        {
            "name": "object_id_inside_the_name_ignores_case",
            "pattern": "a1b2c3",
            "expected": [
                "  file filter  : name contains 'a1b2c3' (case-insensitive)",
                f"  file         : {FEED}/archive/x/DAILY_PAYMENTS_A1B2C3_20261012.csv",
            ],
            "absent": ["OTHER_20261012.csv", "OBJ_5f3c-9e21_payload.json"],
        },
        {
            "name": "dashed_object_id",
            "pattern": "5F3C-9E21",
            "expected": [f"  file         : {FEED}/inbound/OBJ_5f3c-9e21_payload.json"],
            "absent": ["DAILY_PAYMENTS_A1B2C3_20261012.csv"],
        },
        {
            "name": "full_file_name_still_matches",
            "pattern": "OTHER_20261012.csv",
            "expected": [f"  file         : {FEED}/error/OTHER_20261012.csv"],
            "absent": ["OBJ_5f3c-9e21_payload.json"],
        },
        {
            "name": "wildcard_is_a_pattern_not_contains",
            "pattern": "daily_payments_*.csv",
            "expected": [
                "  file filter  : name matches 'daily_payments_*.csv' (case-insensitive)",
                f"  file         : {FEED}/archive/x/DAILY_PAYMENTS_A1B2C3_20261012.csv",
            ],
            "absent": ["OTHER_20261012.csv"],
        },
        {
            "name": "unknown_object_id_is_not_found_for_the_date",
            "pattern": "zz9999",
            "expected": [
                (
                    "status       : File not found for this date — no file matching 'zz9999' under "
                    f"inbound, error or archive was last modified on {DAY}"
                )
            ],
            "absent": ["Incorrect path", "  file         :"],
        },
    ]
    for test_data in test_cases:
        out = _check(_FakeContainer(files), file_pattern=test_data["pattern"])
        for line in test_data["expected"]:
            assert line in out, (test_data["name"], line, out)
        for line in test_data["absent"]:
            assert line not in out, (test_data["name"], line)


def test_relative_dates() -> None:
    today = datetime.now(ET).date()
    test_cases = [
        {"name": "empty_is_today", "date": "", "expected": today},
        {"name": "today", "date": "today", "expected": today},
        {"name": "yesterday", "date": "Yesterday", "expected": today - timedelta(days=1)},
    ]
    for test_data in test_cases:
        out = _check(_FakeContainer({}), date=test_data["date"])
        assert f"  business date: {test_data['expected']} (America/New_York)" in out, test_data[
            "name"
        ]


def test_tool_is_the_only_adls_tool_and_wired_on_the_subagent() -> None:
    from v1.core.subagents.adls import ADLS_SUBAGENT

    assert adls.ADLS_TOOLS == [adls.check_file_arrival]
    assert [tool.name for tool in ADLS_SUBAGENT["tools"]] == ["check_file_arrival"]
    assert ADLS_SUBAGENT["system_prompt"].startswith("You are the adls-agent.")


def test_reporting_format_lives_in_the_formatting_skill() -> None:
    from pathlib import Path

    from v1.core.prompts.orchestrator import ADLS_ROUTING_BLOCK

    skill = Path(adls.__file__).parents[2] / "skills/message-formatting/SKILL.md"
    assert "## Data Lake answers — file arrival checks" in skill.read_text()
    assert "message-formatting" in ADLS_ROUTING_BLOCK


def test_subagent_needs_the_flag_and_an_account() -> None:
    from v1.core.config import Settings

    assert Settings.model_fields["adls_agent_enabled"].default is False
    test_cases = [
        {
            "flag": False,
            "mapping": {"lake": _ACCOUNT},
            "enabled": False,
            "missing": "ADLS_AGENT_ENABLED",
        },
        {"flag": True, "mapping": {}, "enabled": False, "missing": "ADLS_ACCOUNT_MAPPING"},
        {"flag": True, "mapping": {"lake": _ACCOUNT}, "enabled": True, "missing": None},
    ]
    for test_data in test_cases:
        # model_construct skips .env.
        s = Settings.model_construct(
            adls_agent_enabled=test_data["flag"], adls_account_mapping=test_data["mapping"]
        )
        assert s.adls_enabled is test_data["enabled"], test_data
        assert dict(s.unconfigured_capabilities).get("adls-agent") == test_data["missing"], (
            test_data
        )


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
