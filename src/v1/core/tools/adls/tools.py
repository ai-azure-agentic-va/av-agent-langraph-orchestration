"""Azure Data Lake Storage tools for the ``adls-agent`` subagent.

Mirrors :mod:`v1.core.tools.adf.tools` deliberately — same auth story, same
alias-resolution shape, same surface-errors-to-the-model convention — so the
two subagents behave identically from the orchestrator's point of view.

Authentication uses the process-wide ``DefaultAzureCredential`` through
:class:`v1.utils.azure_credentials.ThreadOffloadAsyncCredential`, so the same
code runs locally off the developer's ``az login`` session and in Azure off the
resource's managed identity. No keys or secrets are stored — the identity needs
*Storage Blob Data Reader* on each target account, plus *Storage Table Data
Reader* for the DQ rules table. The tools are read-only by construction: they
only list blobs, download blobs, and query table entities.

The target account comes from ``ADLS_ACCOUNT_MAPPING`` (friendly alias →
account_url / filesystem / config_path). With a single entry it is used
automatically, so the optional ``account`` alias stays empty in normal use.

The blob SDK is used rather than ``azure-storage-file-datalake`` because a Gen2
filesystem IS a blob container, and everything here is flat listing plus
whole-blob reads; the datalake SDK only adds directory/ACL semantics these
read-only tools never touch.

Storage knows only what physically landed (name, size, last-modified). The
EXPECTATIONS come from configuration, in two shapes:

- a per-dataset JSON manifest under ``config_path`` in the same filesystem
  (expected path, arrival SLA, file metadata, data quality rules); and
- the enterprise ``dq_rules_config`` Azure Table, whose PartitionKey is the
  dataset/table name a ServiceNow DQ ticket carries — one row per DQ rule, with
  the expected file path inside its ``dq_parameters`` JSON.

All errors (auth, permission, unknown account/dataset, ...) are returned as
``[adls-agent]``-prefixed text so the model can read them and react.

Responses are *structured text*: stable ``label: value`` lines under a fixed set
of section headers, so the parent orchestrator can lift fields out of them
deterministically (the same contract the adf-agent's output follows).
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

# Manifests are small config documents; anything larger is a misconfiguration
# (someone pointed config_path at a data folder). Bounding the read keeps a
# rogue blob from being pulled into memory.
_MAX_MANIFEST_BYTES = 256 * 1024

# Listing caps — mirror the adf-agent's 40-row display cap so a wide folder
# cannot drown the answer.
_MAX_FILES = 40
_MAX_DATASETS = 200

_CONFIG_PATH_DEFAULT = "config/datasets"

_DATASET_HELP = "[adls-agent] Please provide a dataset name (get one from list_datasets)."

_NOT_CONFIGURED = "(not configured)"
_EXPECTED_PATH_LABEL = "  expected file path  : "

# Manifest fields rendered under "expected file metadata", in display order.
_METADATA_FIELDS = (
    ("source_system", "source system"),
    ("dataset", "dataset"),
    ("file_name_pattern", "file name"),
    ("ingestion_frequency", "ingestion frequency"),
)

# Top-level manifest keys rendered by a dedicated section, so the generic
# "other attributes" tail does not repeat them.
_RENDERED_KEYS = frozenset(
    {"expected_path", "arrival_sla", "data_quality_rules"}
    | {key for key, _ in _METADATA_FIELDS}
)


def _truncate(text) -> str:
    """Collapse whitespace and cap length with a visible truncation marker."""
    text = " ".join(str(text).split())
    return text if len(text) <= _MAX_MSG else text[:_MAX_MSG] + " …[truncated]"


class _InputError(ValueError):
    """Raised for caller input a tool cannot use; the message is model-facing.

    Every tool turns this into its return value, so the model reads what was
    wrong (unknown account, unknown or unreadable dataset, unconfigured DQ
    table) and can retry.
    """


def _account_aliases() -> list[str]:
    return sorted(settings.adls_account_mapping)


def _default_alias() -> str | None:
    mapping = settings.adls_account_mapping
    if settings.adls_default_account and settings.adls_default_account in mapping:
        return settings.adls_default_account
    if len(mapping) == 1:
        return next(iter(mapping))
    return None


def _blob_endpoint(account_url: str) -> str:
    """Accept either ADLS endpoint form and return the blob one.

    Portal and ARM hand out the ``.dfs.`` (Data Lake) hostname while the blob
    SDK needs ``.blob.``; both address the same account. Normalizing here means
    an operator can paste whichever URL they have without it silently failing
    at request time.
    """
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
    alias = (account or "").strip()
    if not alias:
        alias = _default_alias() or ""
        if not alias:
            raise _InputError(
                "[adls-agent] Several storage accounts are configured and no default is set — "
                "pass account=<alias>. Available: " + ", ".join(_account_aliases())
            )
    entry = mapping.get(alias)
    if entry is None:
        raise _InputError(
            f"[adls-agent] Unknown account '{alias}'. Available: " + ", ".join(_account_aliases())
        )
    missing = [key for key in ("account_url", "filesystem") if not entry.get(key)]
    if missing:
        raise _InputError(
            f"[adls-agent] Account '{alias}' is misconfigured — ADLS_ACCOUNT_MAPPING entry "
            f"is missing: {', '.join(missing)}."
        )
    config_path = (entry.get("config_path") or _CONFIG_PATH_DEFAULT).strip("/")
    return alias, _blob_endpoint(entry["account_url"]), entry["filesystem"], config_path


# One BlobServiceClient per account endpoint and one TableClient per table; all
# share the process-wide async credential adapter so token caches are reused.
_clients: dict[str, BlobServiceClient] = {}
_clients_lock = asyncio.Lock()
_table_clients: dict[str, TableClient] = {}
_table_lock = asyncio.Lock()


async def _client(account_url: str) -> BlobServiceClient:
    client = _clients.get(account_url)
    if client is None:
        async with _clients_lock:
            client = _clients.get(account_url)
            if client is None:
                logger.info("Creating BlobServiceClient for %s", account_url)
                client = BlobServiceClient(
                    account_url=account_url,
                    # The native async credential acquires tokens with blocking
                    # work on the event loop, which `langgraph dev`'s
                    # blocking-call detector rejects; this adapter offloads it.
                    credential=ThreadOffloadAsyncCredential(),
                )
                _clients[account_url] = client
    return client


async def close_adls_resources() -> None:
    """Close every cached blob/table client (idempotent)."""
    global _clients, _table_clients
    clients, _clients = _clients, {}
    for account_url, client in clients.items():
        try:
            await client.close()
        except Exception:  # noqa: BLE001 - best-effort shutdown
            logger.warning("Error closing BlobServiceClient for %s", account_url, exc_info=True)
    tables, _table_clients = _table_clients, {}
    for key, client in tables.items():
        try:
            await client.close()
        except Exception:  # noqa: BLE001 - best-effort shutdown
            logger.warning("Error closing TableClient for %s", key, exc_info=True)


async def _container(account_url: str, filesystem: str):
    client = await _client(account_url)
    return client.get_container_client(filesystem)


def _is_directory(blob) -> bool:
    """True for an ADLS Gen2 directory placeholder.

    With a hierarchical namespace every folder ALSO exists as a zero-byte blob
    carrying ``hdi_isfolder=true``, and a flat listing returns those alongside
    real files. Without this filter a folder is reported as a 0-byte file that
    "arrived", which is the wrong answer during an arrival investigation. A
    non-HNS account simply has no such blobs.
    """
    return (blob.metadata or {}).get("hdi_isfolder") == "true"


async def _iter_files(container, prefix: str):
    """Yield the real files under ``prefix``, skipping directory placeholders.

    ``include=["metadata"]`` is required — without it ``blob.metadata`` is None
    and every Gen2 folder looks like a file.
    """
    async for blob in container.list_blobs(name_starts_with=prefix, include=["metadata"]):
        if not _is_directory(blob):
            yield blob


async def _list_blob_names(account_url: str, filesystem: str, prefix: str, cap: int) -> list[str]:
    """Return up to ``cap`` file names under ``prefix`` (flat listing)."""
    container = await _container(account_url, filesystem)
    names: list[str] = []
    async for blob in _iter_files(container, prefix):
        names.append(blob.name)
        if len(names) >= cap:
            break
    return names


def _dataset_name(blob_name: str, prefix: str) -> str:
    """``config/datasets/cur_alpha.json`` -> ``cur_alpha``."""
    stem = blob_name[len(prefix) :].lstrip("/") if prefix else blob_name
    return stem[: -len(".json")] if stem.endswith(".json") else stem


async def _known_datasets(
    account_url: str, filesystem: str, config_path: str, cap: int = _MAX_DATASETS
) -> list[str]:
    names = await _list_blob_names(account_url, filesystem, config_path, cap)
    return sorted(
        _dataset_name(name, config_path) for name in names if name.endswith(".json")
    )


async def _load_manifest(
    alias: str, account_url: str, filesystem: str, config_path: str, dataset: str
) -> tuple[str, dict]:
    """Load one dataset's manifest, resolving the name case-insensitively.

    Returns ``(resolved_dataset_name, manifest)``. Raises :class:`_InputError`
    with model-facing text when the dataset is unknown or the manifest is not
    readable/parseable.
    """
    dataset = (dataset or "").strip().strip("/")
    if not dataset:
        raise _InputError(_DATASET_HELP)

    container = await _container(account_url, filesystem)
    blob_path = f"{config_path}/{dataset}.json" if config_path else f"{dataset}.json"
    try:
        downloader = await container.download_blob(blob_path, offset=0, length=_MAX_MANIFEST_BYTES)
        raw = await downloader.readall()
    except Exception as download_error:
        # Exact miss: fall back to a case-insensitive match before giving up,
        # since the model echoes user casing ("CUR_ALPHA") far more often than
        # the manifest's.
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
            # The manifest is listed under this exact name, so the read itself
            # failed; retrying the same path would recurse without end.
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
    """Render the "expected file metadata" block from a config mapping."""
    lines = []
    for key, label in fields:
        value = config.get(key)
        lines.append(f"    {label:<20}: {_render_value(value) if value else _NOT_CONFIGURED}")
    return lines


def _expected_path_line(expected_path) -> str:
    """Render the expected file path line, configured or not."""
    value = _render_value(expected_path) if expected_path else _NOT_CONFIGURED
    return _EXPECTED_PATH_LABEL + value


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
        # One past the cap, so a full page can be told apart from a truncated one
        # and the count is never reported as exact when it is not.
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
        # Reported as malformed rather than as "0 configured", which would read
        # as "this dataset has no DQ rules" when it actually has a broken block.
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
        for key, value in rule.items():
            if key in ("rule_id", "id"):
                continue
            out.append(f"       {key:<12}: {_render_value(value)}")
    return "\n".join(out)


def _literal_prefix(expected_path: str) -> str:
    """The fixed part of an expected path, up to its first date/param token.

    Expected paths are templates (``raw/alpha/{yyyy}/{MM}/{dd}/``). Listing is
    prefix-based, so we list from the literal head and let the caller read the
    dates off the returned names. A template that puts a token early
    (``raw/{yyyy}/alpha/``) therefore lists that token's whole subtree.
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
    # Stripped the same way _literal_prefix strips a dataset's expected path, so
    # both branches render one trailing slash, not two.
    prefix = (path or "").strip().strip("/")
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

    # None sorts before any datetime, so missing timestamps go last rather than
    # raising on the comparison.
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
#
# One row per data quality rule per dataset/table: PartitionKey = the
# dataset/table name (all a ServiceNow DQ ticket carries), RowKey =
# <etl_stage>-<dq_rule_id> (e.g. LND-TLE), and the `dq_parameters` /
# `oprl_configs` properties hold JSON strings. `dq_parameters` is what builds the
# expected file path: source_base_path + file_name.

# Azure Table system/service properties never rendered as rule attributes.
_TABLE_SYSTEM_KEYS = frozenset({"PartitionKey", "RowKey", "Timestamp", "etag"})

# JSON-string properties the agent parses; rendered via dedicated sections.
_TABLE_JSON_KEYS = frozenset({"dq_parameters", "oprl_configs"})

_RULE_MEANINGS = {"TLE": "timeliness", "CLE": "completeness"}

# Rule-row properties rendered first, in this order; the remaining non-system
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

# Properties _render_dq_rule handles explicitly, excluded from the sorted tail.
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

_MAX_RULE_ROWS = 50  # one dataset has a handful of rules; more is a misquery


def _resolve_dq_table() -> tuple[str, str]:
    """Return ``(endpoint, table_name)`` or raise :class:`_InputError`."""
    endpoint = (settings.adls_table_endpoint or "").strip().rstrip("/")
    if not endpoint:
        raise _InputError(
            "[adls-agent] No DQ rules table is configured (ADLS_TABLE_ENDPOINT is unset)."
        )
    return endpoint, settings.adls_dq_table


async def _dq_table(endpoint: str, table_name: str) -> TableClient:
    key = f"{endpoint}/{table_name}"
    client = _table_clients.get(key)
    if client is None:
        async with _table_lock:
            client = _table_clients.get(key)
            if client is None:
                logger.info("Creating TableClient for %s", key)
                client = TableClient(
                    endpoint=endpoint,
                    table_name=table_name,
                    # Same thread-offloaded adapter, for the same reason as the
                    # blob client above.
                    credential=ThreadOffloadAsyncCredential(),
                )
                _table_clients[key] = client
    return client


def _parse_json_property(row: dict, key: str):
    """Parse a JSON-string property; return None when absent or malformed."""
    raw = row.get(key)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def _known_dq_tables(client: TableClient) -> list[str]:
    """Distinct PartitionKeys (dataset/table names), capped."""
    names: set[str] = set()
    async for row in client.list_entities(select=["PartitionKey"]):
        names.add(row["PartitionKey"])
        if len(names) >= _MAX_DATASETS:
            break
    return sorted(names)


def _render_dq_rule(index: int, row: dict) -> list[str]:
    """Render one rule row: identity line, then its attributes."""
    meaning = _RULE_MEANINGS.get(str(row.get("dq_rule_id", "")).upper())
    identity = f"  {index}. {row.get('RowKey', f'rule-{index}')}"
    if meaning:
        identity += f" ({meaning})"
    out = [identity]
    params = _parse_json_property(row, "dq_parameters") or {}
    if params.get("time_target"):
        out.append(f"       time target : {_render_value(params['time_target'])}")
    if params.get("file_name"):
        out.append(f"       file name   : {_render_value(params['file_name'])}")
    detail = [(key, row.get(key)) for key in _RULE_DETAIL_KEYS]
    tail = sorted(
        (key, value) for key, value in row.items() if key not in _RULE_RENDERED_KEYS
    )
    for key, value in detail + tail:
        if value not in (None, ""):
            out.append(f"       {key:<12}: {_render_value(value)}")
    return out


def _partition_filter(value: str) -> str:
    """OData equality filter on PartitionKey (a quote is escaped by doubling)."""
    return "PartitionKey eq '" + value.replace("'", "''") + "'"


async def _dq_rule_rows(client: TableClient, name: str) -> list[dict]:
    """Every rule row for one dataset/table name, capped at _MAX_RULE_ROWS."""
    rows: list[dict] = []
    async for row in client.query_entities(_partition_filter(name)):
        rows.append(dict(row))
        if len(rows) >= _MAX_RULE_ROWS:
            break
    return rows


async def _resolve_dq_rows(client: TableClient, name: str, dq_table: str) -> tuple[str, list[dict]]:
    """The rule rows for ``name``, resolving its casing against the table's keys.

    Raises :class:`_InputError` when no rule is configured under that name.
    """
    rows = await _dq_rule_rows(client, name)
    if rows:
        return name, rows
    # Tickets and users echo arbitrary casing, so retry a case-insensitive match.
    known = await _known_dq_tables(client)
    match = next((key for key in known if key.lower() == name.lower()), None)
    if match is not None and match != name:
        rows = await _dq_rule_rows(client, match)
        name = match
    if not rows:
        available = ", ".join(known) if known else "(none configured)"
        raise _InputError(
            f"[adls-agent] No DQ rules for table '{name}' in '{dq_table}'. "
            f"Available tables: {available}"
        )
    return name, rows


def _merged_dq_params(rows: list[dict]) -> dict:
    """First non-empty value per key across every row's ``dq_parameters``.

    ``dq_parameters`` is per-rule, so the file/path metadata a DQ ticket needs
    can sit on any one row (the timeliness row typically carries the time
    target, the completeness row its thresholds). Merging keeps the summary from
    reporting "not configured" for a value a sibling rule does configure.
    """
    merged: dict = {}
    for row in rows:
        for key, value in (_parse_json_property(row, "dq_parameters") or {}).items():
            if value not in (None, "") and key not in merged:
                merged[key] = value
    return merged


def _dq_sort_key(row: dict) -> tuple:
    """Order rows by their configured sort_order, unordered rows last."""
    order = row.get("sort_order")
    return (order is None, order, row.get("RowKey", ""))


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

    try:
        endpoint, dq_table = _resolve_dq_table()
    except _InputError as exc:
        return str(exc)

    try:
        client = await _dq_table(endpoint, dq_table)
        name, rows = await _resolve_dq_rows(client, name, dq_table)
    except _InputError as exc:  # before Exception: this carries model-facing text
        return str(exc)
    except Exception as exc:  # surface auth/permission errors to the model
        return f"[adls-agent] ERROR reading DQ rules table '{dq_table}': {_truncate(exc)}"

    rows.sort(key=_dq_sort_key)
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
    configs = (_parse_json_property(row, "oprl_configs") for row in rows)
    oprl = next((parsed for parsed in configs if parsed), None)
    if oprl:
        out.append(f"  on failure (oprl)   : {_render_value(oprl)}")
    return "\n".join(out)


# The adls-agent's tool set (see v1.core.subagents.adls).
ADLS_TOOLS = [
    list_datasets,
    get_dataset_config,
    get_data_quality_rules,
    list_dataset_files,
    get_dq_config,
]


__all__ = ["ADLS_TOOLS", "close_adls_resources"]
