"""Azure Data Lake Storage tools for the ``adls-agent`` subagent.

Deliberately parallel to the adf-agent's tools: the same process-wide
``DefaultAzureCredential`` through ``ThreadOffloadAsyncCredential`` (az login
locally, managed identity deployed — no stored keys; the identity needs
*Storage Blob Data Reader*, plus *Storage Table Data Reader* for the DQ rules
table), the same alias resolution, and the same contract that every error is
returned as ``[adls-agent]``-prefixed text the model can read and react to.
Read-only by construction: list blobs, download blobs, query table entities.

Storage knows only what physically landed; the EXPECTATIONS come from
configuration in two shapes: a per-dataset JSON manifest under ``config_path``,
and the enterprise ``dq_rules_config`` Azure Table keyed by the dataset/table
name a ServiceNow DQ ticket carries. The blob SDK (not
``azure-storage-file-datalake``) suffices because a Gen2 filesystem IS a blob
container and these tools only flat-list and read whole blobs.

Responses are structured ``label: value`` text under fixed section headers, so
the parent orchestrator can lift fields out of them deterministically.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from azure.data.tables.aio import TableClient
from azure.storage.blob.aio import BlobServiceClient
from langchain_core.tools import tool

from v1.core.config import get_settings
from v1.utils.azure_credentials import ThreadOffloadAsyncCredential

logger = logging.getLogger(__name__)
settings = get_settings()

_MAX_MSG = 600  # truncate long Azure error blobs
_MAX_MANIFEST_BYTES = 256 * 1024  # bounded read: a bigger "manifest" is a misconfiguration
_MAX_FILES = 40  # display cap, mirrors the adf-agent's run listing
_MAX_DATASETS = 200
_MAX_RULE_ROWS = 50  # one dataset has a handful of rules; more is a misquery

_CONFIG_PATH_DEFAULT = "config/datasets"
_NOT_CONFIGURED = "(not configured)"
_EXPECTED_PATH_LABEL = "  expected file path  : "

# Manifest fields rendered under "expected file metadata", in display order.
_METADATA_FIELDS = (
    ("source_system", "source system"),
    ("dataset", "dataset"),
    ("file_name_pattern", "file name"),
    ("ingestion_frequency", "ingestion frequency"),
)

# Manifest keys with a dedicated section, excluded from the "other attributes" tail.
_RENDERED_KEYS = frozenset(
    {"expected_path", "arrival_sla", "data_quality_rules"}
    | {key for key, _ in _METADATA_FIELDS}
)


def _truncate(text) -> str:
    """Collapse whitespace and cap length with a visible truncation marker."""
    text = " ".join(str(text).split())
    return text if len(text) <= _MAX_MSG else text[:_MAX_MSG] + " …[truncated]"


class _InputError(ValueError):
    """Unusable caller input; the message is model-facing text a tool returns."""


def _default_alias() -> str | None:
    mapping = settings.adls_account_mapping
    if settings.adls_default_account and settings.adls_default_account in mapping:
        return settings.adls_default_account
    if len(mapping) == 1:
        return next(iter(mapping))
    return None


def _blob_endpoint(account_url: str) -> str:
    """Normalize either ADLS endpoint form (.dfs or .blob) to the blob one."""
    return account_url.strip().rstrip("/").replace(
        ".dfs.core.windows.net", ".blob.core.windows.net"
    )


def _resolve_account(account: str) -> tuple[str, str, str, str]:
    """Resolve an alias to ``(alias, blob_endpoint, filesystem, config_path)``."""
    mapping = settings.adls_account_mapping
    if not mapping:
        raise _InputError(
            "[adls-agent] No Data Lake account is configured (ADLS_ACCOUNT_MAPPING is empty)."
        )
    alias = (account or "").strip() or _default_alias() or ""
    if not alias:
        raise _InputError(
            "[adls-agent] Several storage accounts are configured and no default is set — "
            "pass account=<alias>. Available: " + ", ".join(sorted(mapping))
        )
    entry = mapping.get(alias)
    if entry is None:
        raise _InputError(
            f"[adls-agent] Unknown account '{alias}'. Available: " + ", ".join(sorted(mapping))
        )
    missing = [key for key in ("account_url", "filesystem") if not entry.get(key)]
    if missing:
        raise _InputError(
            f"[adls-agent] Account '{alias}' is misconfigured — ADLS_ACCOUNT_MAPPING entry "
            f"is missing: {', '.join(missing)}."
        )
    config_path = (entry.get("config_path") or _CONFIG_PATH_DEFAULT).strip("/")
    return alias, _blob_endpoint(entry["account_url"]), entry["filesystem"], config_path


# One client per endpoint/table, all sharing the process-wide credential adapter
# so token caches are reused.
_clients: dict[str, BlobServiceClient] = {}
_table_clients: dict[str, TableClient] = {}
_cache_lock = asyncio.Lock()


async def _cached_client(cache: dict, key: str, factory):
    client = cache.get(key)
    if client is None:
        async with _cache_lock:
            client = cache.get(key)
            if client is None:
                logger.info("Creating client for %s", key)
                client = cache[key] = factory()
    return client


async def _client(account_url: str) -> BlobServiceClient:
    # ThreadOffloadAsyncCredential: the native async credential acquires tokens
    # with blocking work on the event loop, which `langgraph dev` rejects.
    return await _cached_client(
        _clients,
        account_url,
        lambda: BlobServiceClient(
            account_url=account_url, credential=ThreadOffloadAsyncCredential()
        ),
    )


async def _dq_table(endpoint: str, table_name: str) -> TableClient:
    return await _cached_client(
        _table_clients,
        f"{endpoint}/{table_name}",
        lambda: TableClient(
            endpoint=endpoint, table_name=table_name, credential=ThreadOffloadAsyncCredential()
        ),
    )


async def close_adls_resources() -> None:
    """Close every cached blob/table client (idempotent)."""
    global _clients, _table_clients
    stale = list(_clients.items()) + list(_table_clients.items())
    _clients, _table_clients = {}, {}
    for key, client in stale:
        try:
            await client.close()
        except Exception:  # noqa: BLE001 - best-effort shutdown
            logger.warning("Error closing ADLS client for %s", key, exc_info=True)


async def _container(account_url: str, filesystem: str):
    client = await _client(account_url)
    return client.get_container_client(filesystem)


async def _iter_files(container, prefix: str):
    """Yield the real files under ``prefix``, skipping directory placeholders.

    On a hierarchical-namespace account every folder is ALSO returned by a flat
    listing, as a zero-byte blob whose metadata carries ``hdi_isfolder=true`` —
    without this filter a folder reads as a 0-byte file that "arrived".
    ``include=["metadata"]`` is required or ``blob.metadata`` is None.
    """
    async for blob in container.list_blobs(name_starts_with=prefix, include=["metadata"]):
        if (blob.metadata or {}).get("hdi_isfolder") != "true":
            yield blob


async def _known_datasets(
    account_url: str, filesystem: str, config_path: str, cap: int = _MAX_DATASETS
) -> list[str]:
    """Up to ``cap`` dataset names — the ``*.json`` stems under ``config_path``."""
    container = await _container(account_url, filesystem)
    names: list[str] = []
    async for blob in _iter_files(container, config_path):
        if not blob.name.endswith(".json"):
            continue
        stem = blob.name[len(config_path):].lstrip("/") if config_path else blob.name
        names.append(stem[: -len(".json")])
        if len(names) >= cap:
            break
    return sorted(names)


async def _load_manifest(
    alias: str, account_url: str, filesystem: str, config_path: str, dataset: str
) -> tuple[str, dict]:
    """Load one dataset's manifest as ``(resolved_name, manifest)``.

    Resolves the name case-insensitively (the model echoes user casing far more
    often than the manifest's). Raises :class:`_InputError` when the dataset is
    unknown or its manifest is unreadable.
    """
    dataset = (dataset or "").strip().strip("/")
    if not dataset:
        raise _InputError(
            "[adls-agent] Please provide a dataset name (get one from list_datasets)."
        )

    container = await _container(account_url, filesystem)
    blob_path = f"{config_path}/{dataset}.json" if config_path else f"{dataset}.json"
    try:
        downloader = await container.download_blob(blob_path, offset=0, length=_MAX_MANIFEST_BYTES)
        raw = await downloader.readall()
    except Exception as download_error:
        try:
            known = await _known_datasets(account_url, filesystem, config_path)
        except Exception as exc:  # noqa: BLE001 - auth/permission surfaces here
            raise _InputError(
                f"[adls-agent] ERROR reading dataset config in account '{alias}': {_truncate(exc)}"
            ) from exc
        match = next((name for name in known if name.lower() == dataset.lower()), None)
        if match is None:
            available = ", ".join(known) if known else "(none configured)"
            raise _InputError(
                f"[adls-agent] Unknown dataset '{dataset}' in account '{alias}'. "
                f"Available: {available}"
            )
        if match == dataset:
            # Listed under this exact name, so the read itself failed; retrying
            # the same path would recurse without end.
            raise _InputError(
                f"[adls-agent] ERROR reading dataset config for '{dataset}' in account "
                f"'{alias}': {_truncate(download_error)}"
            ) from download_error
        return await _load_manifest(alias, account_url, filesystem, config_path, match)

    try:
        manifest = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise _InputError(
            f"[adls-agent] Dataset config for '{dataset}' in account '{alias}' is not valid "
            f"JSON: {_truncate(exc)}"
        ) from exc
    if not isinstance(manifest, dict):
        raise _InputError(
            f"[adls-agent] Dataset config for '{dataset}' in account '{alias}' must be a JSON "
            "object."
        )
    return dataset, manifest


def _render_value(value) -> str:
    """One-line rendering for a scalar, list, or nested mapping."""
    if isinstance(value, dict):
        return ", ".join(f"{k}={_render_value(v)}" for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return ", ".join(_render_value(v) for v in value)
    return _truncate(value)


def _metadata_lines(config: dict, fields: tuple[tuple[str, str], ...]) -> list[str]:
    return [
        f"    {label:<20}: {_render_value(config[key]) if config.get(key) else _NOT_CONFIGURED}"
        for key, label in fields
    ]


def _expected_path_line(expected_path) -> str:
    return _EXPECTED_PATH_LABEL + (
        _render_value(expected_path) if expected_path else _NOT_CONFIGURED
    )


@tool
async def list_datasets(account: str = "") -> str:
    """List every dataset that has a configuration in Azure Data Lake Storage.

    Args:
        account: Optional storage account alias. Leave empty to use the default
                 account.

    Use this when the user asks which datasets exist, or as a first step before
    looking up a dataset's expected file configuration or data quality rules.
    """
    try:
        alias, url, filesystem, config_path = _resolve_account(account)
    except _InputError as exc:
        return str(exc)
    try:
        # One past the cap, so a truncated page is never reported as an exact count.
        datasets = await _known_datasets(url, filesystem, config_path, _MAX_DATASETS + 1)
    except Exception as exc:  # surface auth/permission errors to the model as text
        return f"[adls-agent] ERROR listing datasets in account '{alias}': {_truncate(exc)}"
    if not datasets:
        return (
            f"[adls-agent] Account '{alias}' has no dataset configuration under "
            f"'{config_path}/' in filesystem '{filesystem}'."
        )
    truncated = len(datasets) > _MAX_DATASETS
    datasets = datasets[:_MAX_DATASETS]
    count = f"more than {_MAX_DATASETS}" if truncated else str(len(datasets))
    shown = f" (showing the first {_MAX_DATASETS})" if truncated else ""
    listing = "\n".join(f"  - {name}" for name in datasets)
    return f"[adls-agent] Account '{alias}' has {count} configured dataset(s){shown}:\n{listing}"


@tool
async def get_dataset_config(dataset: str, account: str = "") -> str:
    """Fetch a dataset's expected file path, arrival SLA, and file metadata.

    Args:
        dataset: Exact dataset name, e.g. "cur_alpha_daily" (from list_datasets).
        account: Optional storage account alias. Leave empty to use the default
                 account.

    Returns the expected storage location, the expected file arrival SLA, and
    the expected file metadata — source system, dataset, file name pattern and
    ingestion frequency. This is what a Production Support Engineer needs to
    know what SHOULD have arrived, where, and by when. For the dataset's data
    quality rules use get_data_quality_rules; for what ACTUALLY landed use
    list_dataset_files.
    """
    try:
        alias, url, filesystem, config_path = _resolve_account(account)
        name, manifest = await _load_manifest(alias, url, filesystem, config_path, dataset)
    except _InputError as exc:
        return str(exc)

    out = [
        f"[adls-agent] Dataset '{name}' configuration (account '{alias}', filesystem "
        f"'{filesystem}')",
        "  expected file metadata:",
        *_metadata_lines(manifest, _METADATA_FIELDS),
        _expected_path_line(manifest.get("expected_path")),
    ]

    sla = manifest.get("arrival_sla")
    if isinstance(sla, dict) and sla:
        out.append("  expected arrival SLA:")
        out.extend(f"    {key:<20}: {_render_value(value)}" for key, value in sla.items())
    elif sla:
        out.append(f"  expected arrival SLA: {_render_value(sla)}")
    else:
        out.append(f"  expected arrival SLA: {_NOT_CONFIGURED}")

    rules = manifest.get("data_quality_rules")
    if isinstance(rules, list):
        rule_summary = f"{len(rules)} configured"
    elif rules:
        # Not "0 configured", which would hide that the block exists but is broken.
        rule_summary = "malformed (expected a list)"
    else:
        rule_summary = "0 configured"
    out.append(
        f"  data quality rules  : {rule_summary} (call get_data_quality_rules for the detail)"
    )

    extra = {k: v for k, v in manifest.items() if k not in _RENDERED_KEYS}
    if extra:
        out.append("  other attributes:")
        out.extend(f"    {key:<20}: {_render_value(value)}" for key, value in extra.items())
    return "\n".join(out)


@tool
async def get_data_quality_rules(dataset: str, account: str = "") -> str:
    """Fetch the data quality rules configured for a dataset.

    Args:
        dataset: Exact dataset name, e.g. "cur_alpha_daily" (from list_datasets).
        account: Optional storage account alias. Leave empty to use the default
                 account.

    Returns every configured rule with its id, the column it applies to, its
    type, severity, and description. Use this during incident investigation to
    explain which quality checks the dataset is held to.
    """
    try:
        alias, url, filesystem, config_path = _resolve_account(account)
        name, manifest = await _load_manifest(alias, url, filesystem, config_path, dataset)
    except _InputError as exc:
        return str(exc)

    rules = manifest.get("data_quality_rules")
    if not rules:
        return (
            f"[adls-agent] Dataset '{name}' (account '{alias}') has no data quality rules "
            "configured."
        )
    if not isinstance(rules, list):
        return (
            f"[adls-agent] Dataset '{name}' (account '{alias}') has a malformed "
            "data_quality_rules entry — expected a list of rules."
        )

    out = [
        f"[adls-agent] Dataset '{name}' has {len(rules)} data quality rule(s) "
        f"(account '{alias}'):"
    ]
    for index, rule in enumerate(rules, start=1):
        if not isinstance(rule, dict):
            out.append(f"  {index}. {_render_value(rule)}")
            continue
        rule_id = rule.get("rule_id") or rule.get("id") or f"rule-{index}"
        out.append(f"  {index}. {rule_id}")
        out.extend(
            f"       {key:<12}: {_render_value(value)}"
            for key, value in rule.items()
            if key not in ("rule_id", "id")
        )
    return "\n".join(out)


def _literal_prefix(expected_path: str) -> str:
    """The fixed head of a path template (``raw/alpha/{yyyy}/…`` -> ``raw/alpha``).

    Listing is prefix-based, so date folders are discovered rather than computed;
    a template whose token comes early lists that token's whole subtree.
    """
    return str(expected_path).split("{", 1)[0].strip("/")


@tool
async def list_dataset_files(
    dataset: str = "",
    path: str = "",
    last_n_days: int = 7,
    account: str = "",
) -> str:
    """List the files that ACTUALLY landed in Azure Data Lake Storage, newest first.

    Args:
        dataset:     Dataset name — its configured expected_path is used as the
                     folder to list. Leave empty and pass `path` instead to list
                     an arbitrary folder.
        path:        Optional explicit folder prefix, e.g. "raw/alpha/". Ignored
                     when `dataset` is given.
        last_n_days: Only files modified in this many days (default 7). Pass 0
                     for no time filter.
        account:     Optional storage account alias. Leave empty to use the
                     default account.

    Returns each file's full path, size in bytes, and last-modified timestamp
    (UTC). Use this to check whether an expected file arrived, and to compare
    what landed against the expected path/SLA from get_dataset_config.
    """
    scope = ""
    prefix = (path or "").strip().strip("/")  # match _literal_prefix's stripping
    try:
        alias, url, filesystem, config_path = _resolve_account(account)
        if dataset and dataset.strip():
            name, manifest = await _load_manifest(alias, url, filesystem, config_path, dataset)
            expected_path = manifest.get("expected_path")
            if not expected_path:
                return (
                    f"[adls-agent] Dataset '{name}' has no expected_path configured — pass an "
                    "explicit path= to list a folder."
                )
            prefix = _literal_prefix(expected_path)
            scope = f" for dataset '{name}' (expected path '{expected_path}')"
        elif not prefix:
            return (
                "[adls-agent] Please provide a dataset name or an explicit path= folder prefix "
                "(get a dataset from list_datasets)."
            )
    except _InputError as exc:
        return str(exc)

    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=last_n_days) if last_n_days > 0 else None
    )
    try:
        container = await _container(url, filesystem)
        files = []
        async for blob in _iter_files(container, prefix):
            modified = blob.last_modified
            if cutoff is not None and modified is not None and modified < cutoff:
                continue
            files.append((modified, blob.name, blob.size))
    except Exception as exc:
        return f"[adls-agent] ERROR listing files in account '{alias}': {_truncate(exc)}"

    if not files:
        window = f" modified in the last {last_n_days} day(s)" if cutoff else ""
        return (
            f"[adls-agent] No files{scope} under '{prefix}/' in account '{alias}' "
            f"(filesystem '{filesystem}'){window}."
        )

    # (is not None, value) sorts missing timestamps last instead of raising.
    files.sort(key=lambda item: (item[0] is not None, item[0]), reverse=True)
    lines = [
        f"  - {name} | {size if size is not None else '?'} bytes | lastModified={modified}"
        for modified, name, size in files[:_MAX_FILES]
    ]
    header = (
        f"[adls-agent] {len(files)} file(s){scope} under '{prefix}/' in account '{alias}' "
        f"(filesystem '{filesystem}'), newest first"
    )
    if len(files) > _MAX_FILES:
        header += f" — showing the newest {_MAX_FILES} of {len(files)}"
    return header + ":\n" + "\n".join(lines)


# --- DQ rules configuration (Azure Table) -------------------------------------
# One row per rule per dataset/table: PartitionKey = the dataset/table name a
# ServiceNow DQ ticket carries, RowKey = <etl_stage>-<dq_rule_id> (e.g. LND-TLE),
# and `dq_parameters` / `oprl_configs` are JSON-string properties.

_TABLE_SYSTEM_KEYS = frozenset({"PartitionKey", "RowKey", "Timestamp", "etag"})
_TABLE_JSON_KEYS = frozenset({"dq_parameters", "oprl_configs"})
_RULE_MEANINGS = {"TLE": "timeliness", "CLE": "completeness"}

# Rule-row properties rendered first, in this order; remaining non-system
# properties follow, sorted, so a new column still surfaces.
_RULE_DETAIL_KEYS = (
    "etl_stage",
    "active_flag",
    "quarantine_flag",
    "column_name",
    "threshold_cnt",
    "threshold_pct",
    "notes",
)
_RULE_RENDERED_KEYS = (
    _TABLE_SYSTEM_KEYS | _TABLE_JSON_KEYS | frozenset(_RULE_DETAIL_KEYS) | {"dq_rule_id"}
)

# dq_parameters fields shown under "expected file metadata", in display order —
# the same contract get_dataset_config honors for the manifest path.
_DQ_METADATA_FIELDS = (
    ("source_system_name", "source system"),
    ("int_table_name", "dataset/table"),
    ("file_name", "file name"),
    ("ingestion_cadence", "ingestion frequency"),
    ("ingestion_days", "ingestion days"),
)


def _parse_json_property(row: dict, key: str):
    """Parse a JSON-string property; None when absent, malformed, or not an object."""
    try:
        parsed = json.loads(row.get(key) or "")
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _partition_filter(value: str) -> str:
    """OData equality filter on PartitionKey (a quote is escaped by doubling)."""
    return "PartitionKey eq '" + value.replace("'", "''") + "'"


async def _dq_rule_rows(client: TableClient, name: str) -> list[dict]:
    rows: list[dict] = []
    async for row in client.query_entities(_partition_filter(name)):
        rows.append(dict(row))
        if len(rows) >= _MAX_RULE_ROWS:
            break
    return rows


def _dq_endpoint() -> str:
    """The configured DQ table endpoint; raises when no table is set up."""
    endpoint = (settings.adls_table_endpoint or "").strip().rstrip("/")
    if not endpoint:
        raise _InputError(
            "[adls-agent] No DQ rules table is configured (ADLS_TABLE_ENDPOINT is unset)."
        )
    return endpoint


async def _dq_table_names(client: TableClient, cap: int = _MAX_DATASETS) -> list[str]:
    """Up to ``cap`` dataset/table names — the distinct PartitionKeys, sorted.

    Table storage has no DISTINCT, so the rows are scanned with only the key
    projected and collapsed here.
    """
    names: set[str] = set()
    async for row in client.list_entities(select=["PartitionKey"]):
        names.add(row["PartitionKey"])
        if len(names) >= cap:
            break
    return sorted(names)


async def _resolve_dq_rows(client: TableClient, name: str, dq_table: str) -> tuple[str, list[dict]]:
    """The rule rows for ``name``, resolving its casing against the table's keys."""
    rows = await _dq_rule_rows(client, name)
    if rows:
        return name, rows
    # Tickets and users echo arbitrary casing, so retry a case-insensitive match.
    known = await _dq_table_names(client)
    match = next((key for key in known if key.lower() == name.lower()), None)
    if match is not None and match != name:
        name = match
        rows = await _dq_rule_rows(client, name)
    if not rows:
        available = ", ".join(known) if known else "(none configured)"
        raise _InputError(
            f"[adls-agent] No DQ rules for table '{name}' in '{dq_table}'. "
            f"Available tables: {available}"
        )
    return name, rows


def _merged_dq_params(rows: list[dict]) -> dict:
    """First non-empty value per key across every row's ``dq_parameters``.

    The file/path metadata a DQ ticket needs can sit on any one rule row, so the
    summary must not report "not configured" for a value a sibling row carries.
    """
    merged: dict = {}
    for row in rows:
        for key, value in (_parse_json_property(row, "dq_parameters") or {}).items():
            if value not in (None, "") and key not in merged:
                merged[key] = value
    return merged


def _rule_sort_key(row: dict) -> tuple[int, float, str]:
    """Display order for a rule row: ``sort_order``, then RowKey.

    The column reaches the table as a string from some writers and as a number
    from others, so it is coerced instead of compared across types — an
    uncomparable mix would otherwise raise out of the tool. Rows without a usable
    value sort last.
    """
    try:
        return 0, float(row.get("sort_order")), str(row.get("RowKey", ""))
    except (TypeError, ValueError):
        return 1, 0.0, str(row.get("RowKey", ""))


def _render_dq_rule(index: int, row: dict) -> list[str]:
    """Render one rule row: identity line, then its attributes."""
    meaning = _RULE_MEANINGS.get(str(row.get("dq_rule_id", "")).upper())
    identity = f"  {index}. {row.get('RowKey', f'rule-{index}')}"
    if meaning:
        identity += f" ({meaning})"
    out = [identity]
    params = _parse_json_property(row, "dq_parameters") or {}
    if params.get("time_target"):
        target = _render_value(params["time_target"])
        if params.get("time_target_timezone"):
            target += f" ({_render_value(params['time_target_timezone'])})"
        out.append(f"       time target : {target}")
    if params.get("file_name"):
        out.append(f"       file name   : {_render_value(params['file_name'])}")
    # Zone paths differ per rule row (LND/PCUR/INT), so each rule shows its own.
    if params.get("source_base_path"):
        out.append(f"       zone path   : {_render_value(params['source_base_path'])}")
    detail = [(key, row.get(key)) for key in _RULE_DETAIL_KEYS]
    tail = sorted(
        (key, value) for key, value in row.items() if key not in _RULE_RENDERED_KEYS
    )
    out.extend(
        f"       {key:<12}: {_render_value(value)}"
        for key, value in detail + tail
        if value not in (None, "")
    )
    return out


@tool
async def get_dq_config(table_name: str) -> str:
    """Fetch a dataset's DQ rules and expected file config from the dq_rules_config table.

    Args:
        table_name: The dataset/table name exactly as it appears on the ticket,
                    e.g. "speedpay_check_analytics". This is the PartitionKey of
                    the DQ rules table — a ServiceNow DQ ticket carries only this.

    Returns every DQ rule row configured for that table (timeliness TLE,
    completeness CLE, ...) with its stage, thresholds and time target, plus the
    expected file metadata and the EXPECTED file path (the source_base_path from
    dq_parameters). To check what ACTUALLY landed, follow up with
    list_dataset_files passing that expected path as path=.
    """
    name = (table_name or "").strip()
    if not name:
        return "[adls-agent] Please provide the dataset/table name from the ticket."

    dq_table = settings.adls_dq_table
    try:
        client = await _dq_table(_dq_endpoint(), dq_table)
        name, rows = await _resolve_dq_rows(client, name, dq_table)
    except _InputError as exc:  # before Exception: this carries model-facing text
        return str(exc)
    except Exception as exc:  # surface auth/permission errors to the model
        return f"[adls-agent] ERROR reading DQ rules table '{dq_table}': {_truncate(exc)}"

    rows.sort(key=_rule_sort_key)
    params = _merged_dq_params(rows)
    out = [
        f"[adls-agent] Table '{name}' has {len(rows)} DQ rule row(s) (from '{dq_table}'):",
        "  expected file metadata:",
        *_metadata_lines(params, _DQ_METADATA_FIELDS),
        _expected_path_line(params.get("source_base_path")),
        "  rules:",
    ]
    for index, row in enumerate(rows, start=1):
        out.extend(_render_dq_rule(index, row))
    oprl = next(
        (parsed for row in rows if (parsed := _parse_json_property(row, "oprl_configs"))), None
    )
    if oprl:
        out.append(f"  on failure (oprl)   : {_render_value(oprl)}")
    return "\n".join(out)


@tool
async def list_dq_tables() -> str:
    """List the dataset/table names that have DQ rules configured.

    Use this when the user asks which tables the data quality configuration
    covers and names none — "what tables do we have", "which tables have DQ
    rules". Pass any name it returns to get_dq_config for that table's rules,
    time target and expected file path.
    """
    dq_table = settings.adls_dq_table
    try:
        client = await _dq_table(_dq_endpoint(), dq_table)
        # One past the cap, so a truncated listing is never reported as an exact count.
        names = await _dq_table_names(client, _MAX_DATASETS + 1)
    except _InputError as exc:
        return str(exc)
    except Exception as exc:  # surface auth/permission errors to the model
        return f"[adls-agent] ERROR reading DQ rules table '{dq_table}': {_truncate(exc)}"

    if not names:
        return f"[adls-agent] The DQ rules table '{dq_table}' has no rules configured."
    truncated = len(names) > _MAX_DATASETS
    names = names[:_MAX_DATASETS]
    count = f"more than {_MAX_DATASETS}" if truncated else str(len(names))
    shown = f" (showing the first {_MAX_DATASETS})" if truncated else ""
    listing = "\n".join(f"  - {name}" for name in names)
    return (
        f"[adls-agent] The DQ rules table '{dq_table}' has rules for {count} "
        f"table(s){shown}:\n{listing}"
    )


ADLS_TOOLS = [
    list_datasets,
    get_dataset_config,
    get_data_quality_rules,
    list_dataset_files,
    get_dq_config,
    list_dq_tables,
]

__all__ = ["ADLS_TOOLS", "close_adls_resources"]
