"""Offline tests for the ADLS subagent tools.

The Azure blob and table clients are replaced with in-memory fakes, so the tests
cover the tool-facing behavior: account alias resolution (default / named /
unknown / unset / misconfigured), endpoint normalization, dataset manifest
loading (including the case-insensitive fallback and malformed JSON), every
tool's rendered output, and the expected-path prefix logic behind file listing.
No network, no credentials.

Runs standalone (``python test_adls_tools.py``) or under pytest.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import v1.core.tools.adls.tools as adls


# --- fakes -------------------------------------------------------------------


class _Settings:
    def __init__(
        self,
        mapping: dict,
        default: str | None = None,
        table_endpoint: str | None = None,
        dq_table: str = "dqrulesconfig",
    ) -> None:
        self.adls_account_mapping = mapping
        self.adls_default_account = default
        self.adls_table_endpoint = table_endpoint
        self.adls_dq_table = dq_table


_FIN = {
    "account_url": "https://finlake.dfs.core.windows.net",
    "filesystem": "curated",
    "config_path": "config/datasets",
}
_RISK = {
    "account_url": "https://risklake.blob.core.windows.net",
    "filesystem": "raw",
}

# Anchored to the run's clock, not a fixed date: the tools filter by
# `last_n_days` against the real current time, so a hard-coded NOW makes these
# fixtures fall out of the window as the calendar moves on.
NOW = datetime.now(timezone.utc)
YESTERDAY = NOW - timedelta(days=1)
LONG_AGO = NOW - timedelta(days=200)

_ALPHA_BASE_PATH = "raw/alpha/cur_alpha"


def _dated_name(moment: datetime) -> str:
    """The file name a daily cur_alpha drop carries for that date."""
    return f"cur_alpha_{moment:%Y%m%d}.csv"


def _dated_path(moment: datetime) -> str:
    """The dated folder plus file name a daily cur_alpha drop lands under."""
    return f"{_ALPHA_BASE_PATH}/{moment:%Y/%m/%d}/{_dated_name(moment)}"

_ALPHA_MANIFEST = {
    "dataset": "cur_alpha_daily",
    "source_system": "Alpha Core Banking",
    "file_name_pattern": "cur_alpha_{yyyyMMdd}.csv",
    "ingestion_frequency": "Daily",
    "expected_path": "raw/alpha/cur_alpha/{yyyy}/{MM}/{dd}/",
    "arrival_sla": {
        "expected_by": "06:00",
        "timezone": "America/New_York",
        "grace_minutes": 60,
    },
    "data_quality_rules": [
        {
            "rule_id": "DQ-001",
            "column": "account_id",
            "rule_type": "not_null",
            "severity": "critical",
            "description": "Account id must always be present",
        },
        {
            "rule_id": "DQ-002",
            "column": "balance",
            "rule_type": "range",
            "severity": "warning",
            "description": "Balance between -1e9 and 1e9",
        },
    ],
    "owner": "Data Platform Ops",
}


class _FakeBlobProps:
    def __init__(
        self,
        name: str,
        size: int,
        last_modified: datetime | None,
        metadata: dict | None = None,
    ) -> None:
        self.name = name
        self.size = size
        self.last_modified = last_modified
        self.metadata = metadata


class _FakeDownloader:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def readall(self) -> bytes:
        return self._payload


class _FakeContainer:
    """Mimics the aio ContainerClient surface the tools touch.

    Reproduces real ADLS Gen2 behaviour: with a hierarchical namespace every
    parent folder is ALSO returned by a flat listing, as a zero-byte blob whose
    metadata carries ``hdi_isfolder=true``. Verified against a live Gen2 account.
    """

    def __init__(
        self,
        blobs: dict[str, bytes],
        meta: dict[str, tuple[int, datetime]],
        list_error: str = "",
    ) -> None:
        self._blobs = blobs
        self._meta = meta
        self._list_error = list_error

    def _directories(self) -> set[str]:
        dirs: set[str] = set()
        for name in self._blobs:
            parts = name.split("/")[:-1]
            for depth in range(1, len(parts) + 1):
                dirs.add("/".join(parts[:depth]))
        return dirs

    def list_blobs(self, name_starts_with: str = "", include: list[str] | None = None):
        want_metadata = bool(include and "metadata" in include)

        async def _gen():
            if self._list_error:
                raise RuntimeError(self._list_error)
            entries = [(name, False) for name in self._blobs]
            entries += [(name, True) for name in self._directories()]
            for name, is_dir in sorted(entries):
                if not name.startswith(name_starts_with):
                    continue
                if is_dir:
                    # A caller that does not ask for metadata cannot tell this
                    # is a folder — which is precisely the bug being guarded.
                    yield _FakeBlobProps(
                        name, 0, NOW, {"hdi_isfolder": "true"} if want_metadata else None
                    )
                    continue
                size, modified = self._meta.get(name, (len(self._blobs[name]), NOW))
                yield _FakeBlobProps(name, size, modified, {} if want_metadata else None)

        return _gen()

    async def download_blob(self, path: str, offset: int = 0, length: int | None = None):
        try:
            payload = self._blobs[path]
        except KeyError as missing:
            raise RuntimeError(f"BlobNotFound: {path}") from missing
        end = None if length is None else offset + length
        return _FakeDownloader(payload[offset:end])


class _FakeServiceClient:
    def __init__(self, container: _FakeContainer) -> None:
        self._container = container

    def get_container_client(self, filesystem: str) -> _FakeContainer:
        return self._container


def _blobs(
    manifests: dict[str, dict] | None = None,
    files: dict[str, tuple[int, datetime]] | None = None,
) -> tuple[dict[str, bytes], dict[str, tuple[int, datetime]]]:
    """Build the fake blob store: manifests under config/, data files elsewhere."""
    blobs: dict[str, bytes] = {}
    meta: dict[str, tuple[int, datetime]] = {}
    for name, manifest in (manifests or {}).items():
        path = f"config/datasets/{name}.json"
        blobs[path] = json.dumps(manifest).encode()
        meta[path] = (len(blobs[path]), NOW)
    for path, (size, modified) in (files or {}).items():
        blobs[path] = b"data"
        meta[path] = (size, modified)
    return blobs, meta


def _patch(settings: _Settings, container: _FakeContainer | None = None):
    """Patch settings + client factory on the tools module; restore."""

    saved_settings = adls.settings
    saved_client = adls._client
    adls.settings = settings
    fake = _FakeServiceClient(container or _FakeContainer({}, {}))

    async def _fake_client(account_url: str) -> _FakeServiceClient:
        return fake

    adls._client = _fake_client

    def restore() -> None:
        adls.settings = saved_settings
        adls._client = saved_client

    return restore


def _alpha_container(**overrides) -> _FakeContainer:
    manifest = {**_ALPHA_MANIFEST, **overrides}
    blobs, meta = _blobs(
        manifests={
            "cur_alpha_daily": manifest,
            "cur_bravo_hourly": {"dataset": "cur_bravo_hourly"},
        },
        files={
            _dated_path(NOW): (2048, NOW),
            _dated_path(YESTERDAY): (2040, YESTERDAY),
            _dated_path(LONG_AGO): (1900, LONG_AGO),
        },
    )
    return _FakeContainer(blobs, meta)


class _FakeTableClient:
    """Mimics the aio TableClient surface get_dq_config touches."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def query_entities(self, query_filter: str):
        # "PartitionKey eq 'name'" — undo the OData ''-escaping.
        name = query_filter.split(" eq ", 1)[1].strip("'").replace("''", "'")

        async def _gen():
            for row in self._rows:
                if row["PartitionKey"] == name:
                    yield row

        return _gen()

    def list_entities(self, select: list[str] | None = None):
        async def _gen():
            for row in self._rows:
                yield {"PartitionKey": row["PartitionKey"]} if select else row

        return _gen()


_SPEEDPAY_DQ_PARAMS = {
    "file_name": "speedpay_check_analytics",
    "source_base_path": "lnd/speedpay-check/archive/",
    "source_system_name": "Speedpaycheck",
    "int_table_name": "speedpay_check_analytics",
    "ingestion_cadence": "Daily",
    "ingestion_days": ["Monday", "Friday"],
    "time_target": ["09:33"],
    "time_target_timezone": "America/New_York",
}

_SPEEDPAY_OPRL = {"PublishSnow": "True", "SnowQueue": "EDL DQ MONITORING"}


def _speedpay_rows() -> list[dict]:
    def row(row_key: str, stage: str, rule: str, order: int, **extra) -> dict:
        params = dict(_SPEEDPAY_DQ_PARAMS)
        if rule != "TLE":
            params.pop("time_target")
        return {
            "PartitionKey": "speedpay_check_analytics",
            "RowKey": row_key,
            "etl_stage": stage,
            "dq_rule_id": rule,
            "active_flag": "Y",
            "sort_order": order,
            "notes": f"{rule} check for {stage}",
            "dq_parameters": json.dumps(params),
            "oprl_configs": json.dumps(_SPEEDPAY_OPRL),
            **extra,
        }

    return [
        row("INT-CLE", "INT", "CLE", 3, threshold_cnt=1, threshold_pct=95),
        row("LND-TLE", "LND", "TLE", 1),
        row("PCUR-TLE", "PCUR", "TLE", 2),
    ]


def _patch_dq(settings: _Settings, rows: list[dict] | None = None):
    """Patch settings + table client factory on the tools module; restore."""
    saved_settings = adls.settings
    saved_factory = adls._dq_table
    adls.settings = settings
    fake = _FakeTableClient(rows or [])

    async def _fake_table(endpoint: str, table_name: str) -> _FakeTableClient:
        return fake

    adls._dq_table = _fake_table

    def restore() -> None:
        adls.settings = saved_settings
        adls._dq_table = saved_factory

    return restore


def _run(coro):
    return asyncio.run(coro)


# --- account resolution -------------------------------------------------------


def test_default_account_used_when_unset_and_single_mapping() -> None:
    restore = _patch(_Settings({"fin": _FIN}))
    try:
        alias, url, filesystem, config_path = adls._resolve_account("")
        assert (alias, filesystem, config_path) == ("fin", "curated", "config/datasets")
        # the .dfs endpoint is normalized to the blob endpoint the SDK needs
        assert url == "https://finlake.blob.core.windows.net"
    finally:
        restore()


def test_config_path_defaults_when_absent() -> None:
    restore = _patch(_Settings({"risk": _RISK}))
    try:
        assert adls._resolve_account("")[3] == adls._CONFIG_PATH_DEFAULT
    finally:
        restore()


def test_configured_default_wins_with_multiple_accounts() -> None:
    restore = _patch(_Settings({"fin": _FIN, "risk": _RISK}, default="risk"))
    try:
        assert adls._resolve_account("")[0] == "risk"
        assert adls._resolve_account("fin")[0] == "fin"  # explicit alias still works
    finally:
        restore()


def test_multiple_accounts_without_default_asks_for_alias() -> None:
    restore = _patch(_Settings({"fin": _FIN, "risk": _RISK}))
    try:
        result = _run(adls.list_datasets.ainvoke({"account": ""}))
        assert "no default is set" in result
        assert "fin" in result and "risk" in result
    finally:
        restore()


def test_unknown_alias_lists_available_accounts() -> None:
    restore = _patch(_Settings({"fin": _FIN, "risk": _RISK}, default="fin"))
    try:
        result = _run(adls.list_datasets.ainvoke({"account": "nope"}))
        assert "Unknown account 'nope'" in result
        assert "fin" in result and "risk" in result
    finally:
        restore()


def test_no_mapping_configured_is_reported() -> None:
    restore = _patch(_Settings({}))
    try:
        result = _run(adls.list_datasets.ainvoke({"account": ""}))
        assert "No Data Lake account is configured" in result
    finally:
        restore()


def test_misconfigured_entry_names_missing_keys() -> None:
    restore = _patch(_Settings({"fin": {"account_url": "https://x.blob.core.windows.net"}}))
    try:
        result = _run(adls.list_datasets.ainvoke({"account": "fin"}))
        assert "misconfigured" in result
        assert "filesystem" in result
    finally:
        restore()


# --- list_datasets -----------------------------------------------------------


def test_list_datasets_names_account_and_datasets() -> None:
    restore = _patch(_Settings({"fin": _FIN}), _alpha_container())
    try:
        result = _run(adls.list_datasets.ainvoke({}))
        assert "Account 'fin' has 2 configured dataset(s)" in result
        assert "cur_alpha_daily" in result and "cur_bravo_hourly" in result
        assert ".json" not in result  # the extension is stripped from the names
    finally:
        restore()


def test_list_datasets_reports_empty_config_folder() -> None:
    restore = _patch(_Settings({"fin": _FIN}), _FakeContainer({}, {}))
    try:
        result = _run(adls.list_datasets.ainvoke({}))
        assert "no dataset configuration" in result
        assert "config/datasets/" in result
    finally:
        restore()


def test_list_datasets_discloses_a_truncated_listing() -> None:
    """A capped listing must not report its cap as the exact dataset count."""
    blobs, meta = _blobs(manifests={f"cur_{i}": {"dataset": f"cur_{i}"} for i in range(3)})
    restore = _patch(_Settings({"fin": _FIN}), _FakeContainer(blobs, meta))
    saved = adls._MAX_DATASETS
    adls._MAX_DATASETS = 2
    try:
        result = _run(adls.list_datasets.ainvoke({}))
        assert "has more than 2 configured dataset(s) (showing the first 2)" in result
        assert result.count("  - cur_") == 2
    finally:
        adls._MAX_DATASETS = saved
        restore()


def test_list_datasets_error_is_returned_as_text() -> None:
    container = _FakeContainer({}, {}, list_error="(403) AuthorizationPermissionMismatch")
    restore = _patch(_Settings({"fin": _FIN}), container)
    try:
        result = _run(adls.list_datasets.ainvoke({}))
        assert "ERROR listing datasets in account 'fin'" in result
        assert "AuthorizationPermissionMismatch" in result
    finally:
        restore()


# --- get_dataset_config (expected path + SLA + file metadata) ------------------


def test_dataset_config_returns_path_sla_and_metadata() -> None:
    restore = _patch(_Settings({"fin": _FIN}), _alpha_container())
    try:
        result = _run(adls.get_dataset_config.ainvoke({"dataset": "cur_alpha_daily"}))
        # requirement 1: expected file path
        assert "expected file path" in result
        assert "raw/alpha/cur_alpha/{yyyy}/{MM}/{dd}/" in result
        # requirement 2: expected file arrival SLA
        assert "expected arrival SLA" in result
        assert "expected_by" in result and "06:00" in result
        assert "America/New_York" in result and "grace_minutes" in result
        # requirement 3: source system, dataset, file name, ingestion frequency
        assert "Alpha Core Banking" in result
        assert "cur_alpha_daily" in result
        assert "cur_alpha_{yyyyMMdd}.csv" in result
        assert "Daily" in result
        # requirement 4 is summarized here and detailed by its own tool
        assert "data quality rules  : 2 configured" in result
        # unknown manifest keys still surface rather than being silently dropped
        assert "owner" in result and "Data Platform Ops" in result
    finally:
        restore()


def test_dataset_config_marks_missing_fields_rather_than_inventing() -> None:
    restore = _patch(_Settings({"fin": _FIN}), _alpha_container())
    try:
        result = _run(adls.get_dataset_config.ainvoke({"dataset": "cur_bravo_hourly"}))
        assert result.count("(not configured)") >= 4  # path, SLA, and the empty metadata
        assert "data quality rules  : 0 configured" in result
    finally:
        restore()


def test_dataset_name_resolves_case_insensitively() -> None:
    restore = _patch(_Settings({"fin": _FIN}), _alpha_container())
    try:
        result = _run(adls.get_dataset_config.ainvoke({"dataset": "CUR_Alpha_Daily"}))
        assert "Dataset 'cur_alpha_daily' configuration" in result
        assert "Alpha Core Banking" in result
    finally:
        restore()


def test_unknown_dataset_lists_available_ones() -> None:
    restore = _patch(_Settings({"fin": _FIN}), _alpha_container())
    try:
        result = _run(adls.get_dataset_config.ainvoke({"dataset": "cur_missing"}))
        assert "Unknown dataset 'cur_missing'" in result
        assert "cur_alpha_daily" in result and "cur_bravo_hourly" in result
    finally:
        restore()


def test_empty_dataset_name_asks_for_one() -> None:
    restore = _patch(_Settings({"fin": _FIN}), _alpha_container())
    try:
        result = _run(adls.get_dataset_config.ainvoke({"dataset": "  "}))
        assert "provide a dataset name" in result
    finally:
        restore()


def test_malformed_manifest_is_reported_not_raised() -> None:
    container = _FakeContainer({"config/datasets/broken.json": b"{not json"}, {})
    restore = _patch(_Settings({"fin": _FIN}), container)
    try:
        result = _run(adls.get_dataset_config.ainvoke({"dataset": "broken"}))
        assert "not valid JSON" in result
    finally:
        restore()


def test_non_object_manifest_is_reported() -> None:
    container = _FakeContainer({"config/datasets/listy.json": b"[1, 2, 3]"}, {})
    restore = _patch(_Settings({"fin": _FIN}), container)
    try:
        result = _run(adls.get_dataset_config.ainvoke({"dataset": "listy"}))
        assert "must be a JSON object" in result
    finally:
        restore()


def test_listed_but_unreadable_manifest_is_reported_without_recursing() -> None:
    """A manifest that lists but will not download must not retry itself forever.

    The case-insensitive fallback resolves to the SAME name here, so re-loading
    it would recurse until the stack blows; the read error is reported instead.
    """

    class _UnreadableContainer(_FakeContainer):
        async def download_blob(self, path: str, offset: int = 0, length: int | None = None):
            raise RuntimeError("AuthorizationPermissionMismatch")

    blobs, meta = _blobs(manifests={"cur_alpha_daily": _ALPHA_MANIFEST})
    restore = _patch(_Settings({"fin": _FIN}), _UnreadableContainer(blobs, meta))
    try:
        result = _run(adls.get_dataset_config.ainvoke({"dataset": "cur_alpha_daily"}))
    finally:
        restore()
    assert "ERROR reading dataset config for 'cur_alpha_daily'" in result
    assert "AuthorizationPermissionMismatch" in result


# --- get_data_quality_rules ---------------------------------------------------


def test_data_quality_rules_are_listed_with_their_attributes() -> None:
    restore = _patch(_Settings({"fin": _FIN}), _alpha_container())
    try:
        result = _run(adls.get_data_quality_rules.ainvoke({"dataset": "cur_alpha_daily"}))
        assert "has 2 data quality rule(s)" in result
        assert "DQ-001" in result and "DQ-002" in result
        assert "account_id" in result and "not_null" in result and "critical" in result
        assert "Account id must always be present" in result
    finally:
        restore()


def test_data_quality_rules_absent_is_stated_plainly() -> None:
    restore = _patch(_Settings({"fin": _FIN}), _alpha_container())
    try:
        result = _run(adls.get_data_quality_rules.ainvoke({"dataset": "cur_bravo_hourly"}))
        assert "no data quality rules configured" in result
    finally:
        restore()


def test_malformed_rules_entry_is_reported() -> None:
    restore = _patch(_Settings({"fin": _FIN}), _alpha_container(data_quality_rules={"a": 1}))
    try:
        result = _run(adls.get_data_quality_rules.ainvoke({"dataset": "cur_alpha_daily"}))
        assert "malformed" in result
    finally:
        restore()


def test_config_summary_flags_malformed_rules_instead_of_counting_zero() -> None:
    """"0 configured" would read as "no DQ rules exist" for a broken rules block."""
    restore = _patch(_Settings({"fin": _FIN}), _alpha_container(data_quality_rules={"a": 1}))
    try:
        result = _run(adls.get_dataset_config.ainvoke({"dataset": "cur_alpha_daily"}))
        assert "data quality rules  : malformed (expected a list)" in result
    finally:
        restore()


# --- list_dataset_files (what actually landed) --------------------------------


def test_list_files_uses_the_expected_path_prefix_and_sorts_newest_first() -> None:
    restore = _patch(_Settings({"fin": _FIN}), _alpha_container())
    try:
        result = _run(
            adls.list_dataset_files.ainvoke({"dataset": "cur_alpha_daily", "last_n_days": 7})
        )
        assert "2 file(s)" in result  # the 200-day-old file is outside the window
        assert _dated_name(NOW) in result and "2048 bytes" in result
        assert _dated_name(LONG_AGO) not in result
        # newest first
        assert result.index(_dated_name(NOW)) < result.index(_dated_name(YESTERDAY))
        assert "expected path 'raw/alpha/cur_alpha/{yyyy}/{MM}/{dd}/'" in result
    finally:
        restore()


def test_list_files_without_time_filter_includes_old_files() -> None:
    restore = _patch(_Settings({"fin": _FIN}), _alpha_container())
    try:
        result = _run(
            adls.list_dataset_files.ainvoke({"dataset": "cur_alpha_daily", "last_n_days": 0})
        )
        assert "3 file(s)" in result
        assert _dated_name(LONG_AGO) in result
    finally:
        restore()


def test_list_files_accepts_an_explicit_path() -> None:
    restore = _patch(_Settings({"fin": _FIN}), _alpha_container())
    try:
        result = _run(
            adls.list_dataset_files.ainvoke({"path": "raw/alpha/", "last_n_days": 0})
        )
        assert "3 file(s)" in result
        assert "under 'raw/alpha/'" in result  # one trailing slash, not two
        assert "//" not in result
    finally:
        restore()


def test_list_files_reports_an_empty_folder() -> None:
    restore = _patch(_Settings({"fin": _FIN}), _alpha_container())
    try:
        result = _run(adls.list_dataset_files.ainvoke({"path": "raw/nothing/", "last_n_days": 0}))
        assert "No files" in result
    finally:
        restore()


def test_list_files_without_dataset_or_path_asks_for_one() -> None:
    restore = _patch(_Settings({"fin": _FIN}), _alpha_container())
    try:
        result = _run(adls.list_dataset_files.ainvoke({}))
        assert "provide a dataset name or an explicit path" in result
    finally:
        restore()


def test_list_files_needs_an_expected_path_on_the_manifest() -> None:
    restore = _patch(_Settings({"fin": _FIN}), _alpha_container())
    try:
        result = _run(adls.list_dataset_files.ainvoke({"dataset": "cur_bravo_hourly"}))
        assert "no expected_path configured" in result
    finally:
        restore()


def test_list_files_error_is_returned_as_text() -> None:
    container = _FakeContainer({}, {}, list_error="(403) AuthorizationPermissionMismatch")
    restore = _patch(_Settings({"fin": _FIN}), container)
    try:
        result = _run(adls.list_dataset_files.ainvoke({"path": "raw/alpha/"}))
        assert "ERROR listing files in account 'fin'" in result
        assert "AuthorizationPermissionMismatch" in result
    finally:
        restore()


def test_list_files_caps_the_displayed_rows() -> None:
    many = {f"raw/wide/file_{i:03d}.csv": (10, NOW) for i in range(adls._MAX_FILES + 5)}
    blobs, meta = _blobs(files=many)
    restore = _patch(_Settings({"fin": _FIN}), _FakeContainer(blobs, meta))
    try:
        result = _run(adls.list_dataset_files.ainvoke({"path": "raw/wide/", "last_n_days": 0}))
        assert f"showing the newest {adls._MAX_FILES}" in result
        assert result.count("| 10 bytes |") == adls._MAX_FILES
    finally:
        restore()


# --- helpers -------------------------------------------------------------------


def test_gen2_directory_placeholders_are_never_reported_as_files() -> None:
    """Regression: on a hierarchical-namespace account a flat listing returns
    every parent folder as a zero-byte blob. Reporting those as arrived files is
    the wrong answer during an arrival investigation."""
    restore = _patch(_Settings({"fin": _FIN}), _alpha_container())
    try:
        result = _run(
            adls.list_dataset_files.ainvoke({"dataset": "cur_alpha_daily", "last_n_days": 0})
        )
        assert "3 file(s)" in result  # not 3 files + 6 folder placeholders
        assert "| 0 bytes |" not in result  # a placeholder would render zero-length
        # every listed row must be a real file, never a folder path
        rows = [line for line in result.splitlines() if line.startswith("  - ")]
        assert len(rows) == 3
        assert all(".csv |" in row for row in rows)
        # datasets come from the same listing path, so they are covered too
        listed = _run(adls.list_datasets.ainvoke({}))
        assert "config/datasets" not in listed
    finally:
        restore()


def test_literal_prefix_stops_at_the_first_token() -> None:
    assert adls._literal_prefix("raw/alpha/cur_alpha/{yyyy}/{MM}/{dd}/") == "raw/alpha/cur_alpha"
    assert adls._literal_prefix("/raw/flat/") == "raw/flat"


def test_blob_endpoint_normalizes_dfs_and_trailing_slash() -> None:
    assert (
        adls._blob_endpoint("https://x.dfs.core.windows.net/")
        == "https://x.blob.core.windows.net"
    )
    assert (
        adls._blob_endpoint("https://x.blob.core.windows.net")
        == "https://x.blob.core.windows.net"
    )


def test_truncate_caps_and_marks() -> None:
    result = adls._truncate("word " * 400)
    assert len(result) <= adls._MAX_MSG + len(" …[truncated]")
    assert result.endswith("…[truncated]")


def test_render_value_flattens_nested_structures() -> None:
    assert adls._render_value({"a": 1, "b": [2, 3]}) == "a=1, b=2, 3"


# --- get_dq_config (dq_rules_config Azure Table) --------------------------------


def test_dq_config_reports_unset_endpoint() -> None:
    restore = _patch_dq(_Settings({"fin": _FIN}, table_endpoint=None))
    try:
        out = _run(adls.get_dq_config.ainvoke({"table_name": "speedpay_check_analytics"}))
    finally:
        restore()
    assert "ADLS_TABLE_ENDPOINT is unset" in out


def test_dq_config_requires_a_table_name() -> None:
    restore = _patch_dq(
        _Settings({"fin": _FIN}, table_endpoint="https://acct.table.core.windows.net")
    )
    try:
        out = _run(adls.get_dq_config.ainvoke({"table_name": "  "}))
    finally:
        restore()
    assert "provide the dataset/table name" in out


def test_dq_config_renders_rules_path_and_metadata() -> None:
    restore = _patch_dq(
        _Settings({"fin": _FIN}, table_endpoint="https://acct.table.core.windows.net"),
        rows=_speedpay_rows(),
    )
    try:
        out = _run(adls.get_dq_config.ainvoke({"table_name": "speedpay_check_analytics"}))
    finally:
        restore()
    assert "3 DQ rule row(s)" in out
    assert "expected file path  : lnd/speedpay-check/archive/" in out
    assert "zone path   : lnd/speedpay-check/archive/" in out
    assert "source system       : Speedpaycheck" in out
    assert "ingestion frequency : Daily" in out
    # sort_order drives display order: LND-TLE, PCUR-TLE, INT-CLE
    assert out.index("LND-TLE") < out.index("PCUR-TLE") < out.index("INT-CLE")
    assert "LND-TLE (timeliness)" in out
    assert "INT-CLE (completeness)" in out
    assert "time target : 09:33 (America/New_York)" in out
    assert "threshold_pct: 95" in out
    assert "SnowQueue=EDL DQ MONITORING" in out


def test_dq_config_unknown_table_lists_available_ones() -> None:
    restore = _patch_dq(
        _Settings({"fin": _FIN}, table_endpoint="https://acct.table.core.windows.net"),
        rows=_speedpay_rows(),
    )
    try:
        out = _run(adls.get_dq_config.ainvoke({"table_name": "no_such_table"}))
    finally:
        restore()
    assert "No DQ rules for table 'no_such_table'" in out
    assert "speedpay_check_analytics" in out


def test_dq_config_resolves_case_insensitively() -> None:
    restore = _patch_dq(
        _Settings({"fin": _FIN}, table_endpoint="https://acct.table.core.windows.net"),
        rows=_speedpay_rows(),
    )
    try:
        out = _run(adls.get_dq_config.ainvoke({"table_name": "SPEEDPAY_CHECK_ANALYTICS"}))
    finally:
        restore()
    assert "Table 'speedpay_check_analytics' has 3 DQ rule row(s)" in out


def test_dq_metadata_is_merged_across_the_rule_rows() -> None:
    """dq_parameters is per-rule, so the file metadata can sit on any row.

    Reading it off the first row alone reported "(not configured)" for a path or
    time target that a sibling rule does configure.
    """
    rows = [
        {
            "PartitionKey": "tbl_x",
            "RowKey": "LND-TLE",
            "dq_rule_id": "TLE",
            "sort_order": 1,
            "dq_parameters": json.dumps({"time_target": ["09:33"]}),
        },
        {
            "PartitionKey": "tbl_x",
            "RowKey": "INT-CLE",
            "dq_rule_id": "CLE",
            "sort_order": 2,
            "dq_parameters": json.dumps(
                {"source_base_path": "lnd/tbl-x/archive/", "source_system_name": "Xsys"}
            ),
            "oprl_configs": json.dumps({"SnowQueue": "EDL DQ MONITORING"}),
        },
    ]
    restore = _patch_dq(
        _Settings({"fin": _FIN}, table_endpoint="https://acct.table.core.windows.net"), rows=rows
    )
    try:
        out = _run(adls.get_dq_config.ainvoke({"table_name": "tbl_x"}))
    finally:
        restore()
    assert "expected file path  : lnd/tbl-x/archive/" in out
    assert "source system       : Xsys" in out
    assert "time target : 09:33" in out  # still rendered on its own rule row
    assert "SnowQueue=EDL DQ MONITORING" in out  # found on a row other than the first


def test_dq_config_reports_rules_despite_malformed_parameters() -> None:
    rows = [
        {
            "PartitionKey": "tbl_x",
            "RowKey": "LND-TLE",
            "dq_rule_id": "TLE",
            "etl_stage": "LND",
            "dq_parameters": "{not-json",
        }
    ]
    restore = _patch_dq(
        _Settings({"fin": _FIN}, table_endpoint="https://acct.table.core.windows.net"), rows=rows
    )
    try:
        out = _run(adls.get_dq_config.ainvoke({"table_name": "tbl_x"}))
    finally:
        restore()
    assert "1 DQ rule row(s)" in out
    assert "LND-TLE (timeliness)" in out  # the rule itself still renders
    assert f"expected file path  : {adls._NOT_CONFIGURED}" in out  # not invented


def test_dq_rows_order_whatever_type_sort_order_arrives_as() -> None:
    """Table writers disagree on sort_order's type; ordering must survive the mix.

    Comparing a string against a number raises, and the sort runs after the
    tool's try block, so an uncomparable mix escaped as an exception instead of
    the [adls-agent] text every tool promises to return.
    """
    test_cases = [
        {"name": "all numbers", "orders": [3, 1, 2]},
        {"name": "all strings", "orders": ["3", "1", "2"]},
        {"name": "mixed string and number", "orders": ["3", 1, 2.0]},
        {"name": "missing and unparseable values", "orders": [None, 1, "not-a-number"]},
    ]
    for test_data in test_cases:
        rows = [
            {
                "PartitionKey": "tbl_x",
                "RowKey": key,
                "dq_rule_id": "TLE",
                "sort_order": order,
            }
            for key, order in zip(("INT-CLE", "LND-TLE", "PCUR-TLE"), test_data["orders"])
        ]
        restore = _patch_dq(
            _Settings({"fin": _FIN}, table_endpoint="https://acct.table.core.windows.net"),
            rows=rows,
        )
        try:
            out = _run(adls.get_dq_config.ainvoke({"table_name": "tbl_x"}))
        finally:
            restore()
        assert "3 DQ rule row(s)" in out, test_data["name"]
        # sort_order 1 wins wherever it is parseable; unusable values sort last.
        assert out.index("LND-TLE") < out.index("PCUR-TLE"), test_data["name"]


def test_dq_config_path_can_be_handed_straight_to_list_dataset_files() -> None:
    """The two-step DQ recipe in the subagent prompt must actually connect.

    Step 1 reports the expected file path; step 2 lists what landed there. If the
    rendered path were not a usable prefix, the agent would report "nothing
    landed" for a file that is present.
    """
    settings = _Settings({"fin": _FIN}, table_endpoint="https://acct.table.core.windows.net")
    landed = f"lnd/speedpay-check/archive/{_dated_name(NOW)}"
    blobs, meta = _blobs(files={landed: (4096, NOW)})
    restore_blobs = _patch(settings, _FakeContainer(blobs, meta))
    restore_table = _patch_dq(settings, rows=_speedpay_rows())
    try:
        config = _run(adls.get_dq_config.ainvoke({"table_name": "speedpay_check_analytics"}))
        expected_path = config.split(adls._EXPECTED_PATH_LABEL, 1)[1].splitlines()[0]
        files = _run(adls.list_dataset_files.ainvoke({"path": expected_path, "last_n_days": 0}))
    finally:
        restore_table()
        restore_blobs()
    assert expected_path == "lnd/speedpay-check/archive/"
    assert landed in files and "4096 bytes" in files


def test_dq_config_surfaces_read_errors_as_text() -> None:
    settings = _Settings({"fin": _FIN}, table_endpoint="https://acct.table.core.windows.net")
    restore = _patch_dq(settings)
    saved = adls._dq_table

    async def _boom(endpoint: str, table_name: str):
        raise RuntimeError("AuthorizationPermissionMismatch")

    adls._dq_table = _boom
    try:
        out = _run(adls.get_dq_config.ainvoke({"table_name": "speedpay_check_analytics"}))
    finally:
        adls._dq_table = saved
        restore()
    assert "ERROR reading DQ rules table" in out
    assert "AuthorizationPermissionMismatch" in out


# --- wiring -------------------------------------------------------------------


def test_list_dq_tables_reports_each_configured_table_once() -> None:
    """'What tables do we have' has to be answerable without naming one first.

    Every rule row repeats its PartitionKey, so the listing must collapse them
    rather than repeating a table once per rule.
    """
    rows = _speedpay_rows() + [
        {"PartitionKey": "tbl_other", "RowKey": "LND-TLE", "dq_rule_id": "TLE"}
    ]
    restore = _patch_dq(
        _Settings({"fin": _FIN}, table_endpoint="https://acct.table.core.windows.net"), rows=rows
    )
    try:
        out = _run(adls.list_dq_tables.ainvoke({}))
    finally:
        restore()
    assert "rules for 2 table(s)" in out  # 4 rows, 2 distinct tables
    assert "  - speedpay_check_analytics" in out
    assert "  - tbl_other" in out


def test_list_dq_tables_edge_cases() -> None:
    test_cases = [
        {
            "name": "table not configured at all",
            "endpoint": None,
            "rows": [],
            "expected": "No DQ rules table is configured",
        },
        {
            "name": "configured but empty",
            "endpoint": "https://acct.table.core.windows.net",
            "rows": [],
            "expected": "has no rules configured",
        },
    ]
    for test_data in test_cases:
        restore = _patch_dq(
            _Settings({"fin": _FIN}, table_endpoint=test_data["endpoint"]),
            rows=test_data["rows"],
        )
        try:
            out = _run(adls.list_dq_tables.ainvoke({}))
        finally:
            restore()
        assert test_data["expected"] in out, test_data["name"]


def test_list_dq_tables_surfaces_read_errors_as_text() -> None:
    """A 403 on the table must read back as text, not raise out of the tool."""
    settings = _Settings({"fin": _FIN}, table_endpoint="https://acct.table.core.windows.net")
    restore = _patch_dq(settings)

    async def _boom(endpoint: str, table_name: str):
        raise RuntimeError("AuthorizationPermissionMismatch")

    adls._dq_table = _boom
    try:
        out = _run(adls.list_dq_tables.ainvoke({}))
    finally:
        restore()
    assert out.startswith("[adls-agent] ERROR reading DQ rules table")
    assert "AuthorizationPermissionMismatch" in out


def test_subagent_exposes_every_tool_under_the_gated_name() -> None:
    """The subagent's name must match the access gate's, or the gate silently
    stops protecting it; its tool list must cover every capability."""
    from v1.core.middlewares.subagent_access import ADLS_SUBAGENT_NAME
    from v1.core.subagents import ADLS_SUBAGENT

    assert ADLS_SUBAGENT["name"] == ADLS_SUBAGENT_NAME
    assert {tool.name for tool in ADLS_SUBAGENT["tools"]} == {
        "list_datasets",
        "get_dataset_config",
        "get_data_quality_rules",
        "list_dataset_files",
        "get_dq_config",
        "list_dq_tables",
    }
    assert ADLS_SUBAGENT["system_prompt"].startswith("You are the adls-agent.")


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
