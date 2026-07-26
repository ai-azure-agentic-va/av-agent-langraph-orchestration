"""Azure Data Lake Storage tools for the ``adls-agent`` subagent.

Mirrors :mod:`v1.core.tools.adf.tools` deliberately — same auth story, same
alias-resolution shape, same surface-errors-to-the-model convention — so the
two subagents behave identically from the orchestrator's point of view.

Authentication uses the process-wide ``DefaultAzureCredential`` via a
thread-offloaded async adapter
(:class:`v1.utils.azure_credentials.ThreadOffloadAsyncCredential`), so the SAME
code works everywhere:

- **Locally** it picks up the developer's ``az login`` session.
- **Deployed in Azure** it uses the resource's **managed identity**.

No keys or secrets are stored — the identity just needs *Storage Blob Data
Reader* on each target account. The tools are read-only by construction: they
call only ``list_blobs`` and ``download_blob``.

Account config: the target account is configured through ``ADLS_ACCOUNT_MAPPING``
(friendly alias → account_url / filesystem / config_path); with a single entry
it is used automatically, so callers never pass ``account``.

Why the *blob* SDK and not ``azure-storage-file-datalake``: an ADLS Gen2
filesystem IS a blob container, and everything here is flat listing plus whole-
blob reads. The blob SDK is already a dependency and covers that; the datalake
SDK only adds directory/ACL semantics these read-only tools never touch.

**Where the expected-file configuration comes from.** ADLS itself knows only
what physically landed (name, size, last-modified). The *expectations* — path,
arrival SLA, source system, ingestion frequency, and data quality rules — live
in a per-dataset JSON manifest stored under ``config_path`` in the same
filesystem. That keeps one source of truth next to the data and needs no extra
service. If the enterprise source of truth turns out to be a catalog/DB instead,
only :func:`_load_manifest` changes; every tool above it is unaffected.

The tools:

1. ``list_datasets``           — which datasets have a manifest
2. ``get_dataset_config``      — expected path, arrival SLA, and file metadata
3. ``get_data_quality_rules``  — the configured DQ rules for a dataset
4. ``list_dataset_files``      — what actually landed in ADLS, newest first

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

from azure.storage.blob.aio import BlobServiceClient
from langchain_core.tools import tool

from v1.core.config import get_settings
from v1.utils.azure_credentials import ThreadOffloadAsyncCredential

logger = logging.getLogger(__name__)
settings = get_settings()

SOURCE = "adls"

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

_DATASET_HELP = (
    "[adls-agent] Please provide a dataset name (get one from list_datasets)."
)

# Manifest fields rendered under "expected file metadata", in display order.
# Requirement mapping: source system, dataset, file name, ingestion frequency.
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


class _AccountError(ValueError):
    """Raised when an account alias cannot be resolved; message is model-facing."""


class _DatasetError(ValueError):
    """Raised when a dataset manifest is missing or unreadable; model-facing."""


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
        raise _AccountError(
            "[adls-agent] No Data Lake account is configured (ADLS_ACCOUNT_MAPPING is empty)."
        )
    alias = (account or "").strip()
    if not alias:
        alias = _default_alias() or ""
        if not alias:
            raise _AccountError(
                "[adls-agent] Several storage accounts are configured and no default is set — "
                "pass account=<alias>. Available: " + ", ".join(_account_aliases())
            )
    entry = mapping.get(alias)
    if entry is None:
        raise _AccountError(
            f"[adls-agent] Unknown account '{alias}'. Available: " + ", ".join(_account_aliases())
        )
    missing = [key for key in ("account_url", "filesystem") if not entry.get(key)]
    if missing:
        raise _AccountError(
            f"[adls-agent] Account '{alias}' is misconfigured — ADLS_ACCOUNT_MAPPING entry "
            f"is missing: {', '.join(missing)}."
        )
    config_path = (entry.get("config_path") or _CONFIG_PATH_DEFAULT).strip("/")
    return alias, _blob_endpoint(entry["account_url"]), entry["filesystem"], config_path


# One BlobServiceClient per account endpoint; all share the process-wide async
# credential adapter so token caches are reused.
_clients: dict[str, BlobServiceClient] = {}
_clients_lock = asyncio.Lock()


async def _client(account_url: str) -> BlobServiceClient:
    client = _clients.get(account_url)
    if client is None:
        async with _clients_lock:
            client = _clients.get(account_url)
            if client is None:
                logger.info("Creating BlobServiceClient for %s", account_url)
                client = BlobServiceClient(
                    account_url=account_url,
                    # Thread-offloaded adapter over the sync credential, for the
                    # same reason the ADF client uses it: the native async
                    # credential blocks the event loop during acquisition, which
                    # `langgraph dev`'s blocking-call detector rejects.
                    credential=ThreadOffloadAsyncCredential(),
                )
                _clients[account_url] = client
    return client


async def close_adls_resources() -> None:
    """Close every cached blob service client (idempotent)."""
    global _clients
    clients, _clients = _clients, {}
    for account_url, client in clients.items():
        try:
            await client.close()
        except Exception:  # noqa: BLE001 - best-effort shutdown
            logger.warning("Error closing BlobServiceClient for %s", account_url, exc_info=True)


async def _container(account_url: str, filesystem: str):
    client = await _client(account_url)
    return client.get_container_client(filesystem)


def _is_directory(blob) -> bool:
    """True for an ADLS Gen2 directory placeholder.

    With a hierarchical namespace every folder ALSO exists as a zero-byte blob
    carrying ``hdi_isfolder=true``, and a flat listing returns those alongside
    real files. Without this filter the agent reports
    ``raw/alpha/cur_alpha/2026/07/21`` as a 0-byte file that "arrived" — which
    is exactly the wrong answer during an arrival investigation. Verified
    against a real Gen2 account; a non-HNS account simply has no such blobs.
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


async def _known_datasets(account_url: str, filesystem: str, config_path: str) -> list[str]:
    names = await _list_blob_names(account_url, filesystem, config_path, _MAX_DATASETS)
    return sorted(
        _dataset_name(name, config_path) for name in names if name.endswith(".json")
    )


async def _load_manifest(
    alias: str, account_url: str, filesystem: str, config_path: str, dataset: str
) -> tuple[str, dict]:
    """Load one dataset's manifest, resolving the name case-insensitively.

    Returns ``(resolved_dataset_name, manifest)``. Raises :class:`_DatasetError`
    with model-facing text when the dataset is unknown or the manifest is not
    readable/parseable.
    """
    dataset = (dataset or "").strip().strip("/")
    if not dataset:
        raise _DatasetError(_DATASET_HELP)

    container = await _container(account_url, filesystem)
    blob_path = f"{config_path}/{dataset}.json" if config_path else f"{dataset}.json"
    try:
        downloader = await container.download_blob(blob_path, offset=0, length=_MAX_MANIFEST_BYTES)
        raw = await downloader.readall()
    except Exception:
        # Exact miss: fall back to a case-insensitive match before giving up,
        # since the model echoes user casing ("CUR_ALPHA") far more often than
        # the manifest's.
        try:
            known = await _known_datasets(account_url, filesystem, config_path)
        except Exception as exc:  # noqa: BLE001 - auth/permission surfaces here
            raise _DatasetError(
                f"[adls-agent] ERROR reading dataset config in account '{alias}': {_truncate(exc)}"
            ) from exc
        match = next((name for name in known if name.lower() == dataset.lower()), None)
        if match is None:
            available = ", ".join(known) if known else "(none configured)"
            raise _DatasetError(
                f"[adls-agent] Unknown dataset '{dataset}' in account '{alias}'. "
                f"Available: {available}"
            )
        return await _load_manifest(alias, account_url, filesystem, config_path, match)

    try:
        manifest = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise _DatasetError(
            f"[adls-agent] Dataset config for '{dataset}' in account '{alias}' is not valid "
            f"JSON: {_truncate(exc)}"
        ) from exc
    if not isinstance(manifest, dict):
        raise _DatasetError(
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
    except _AccountError as exc:
        return str(exc)
    try:
        datasets = await _known_datasets(url, filesystem, config_path)
    except Exception as exc:  # surface auth/permission errors to the model as text
        return f"[adls-agent] ERROR listing datasets in account '{alias}': {_truncate(exc)}"
    if not datasets:
        return (
            f"[adls-agent] Account '{alias}' has no dataset configuration under "
            f"'{config_path}/' in filesystem '{filesystem}'."
        )
    listing = "\n".join(f"  - {name}" for name in datasets)
    return (
        f"[adls-agent] Account '{alias}' has {len(datasets)} configured dataset(s):\n{listing}"
    )


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
    except _AccountError as exc:
        return str(exc)
    try:
        name, manifest = await _load_manifest(alias, url, filesystem, config_path, dataset)
    except _DatasetError as exc:
        return str(exc)

    out = [
        f"[adls-agent] Dataset '{name}' configuration (account '{alias}', filesystem "
        f"'{filesystem}')",
        "  expected file metadata:",
    ]
    for key, label in _METADATA_FIELDS:
        value = manifest.get(key)
        out.append(f"    {label:<20}: {_render_value(value) if value else '(not configured)'}")

    expected_path = manifest.get("expected_path")
    out.append(
        f"  expected file path  : {_render_value(expected_path)}"
        if expected_path
        else "  expected file path  : (not configured)"
    )

    sla = manifest.get("arrival_sla")
    if isinstance(sla, dict) and sla:
        out.append("  expected arrival SLA:")
        for key, value in sla.items():
            out.append(f"    {key:<20}: {_render_value(value)}")
    elif sla:
        out.append(f"  expected arrival SLA: {_render_value(sla)}")
    else:
        out.append("  expected arrival SLA: (not configured)")

    rules = manifest.get("data_quality_rules")
    rule_count = len(rules) if isinstance(rules, list) else 0
    out.append(
        f"  data quality rules  : {rule_count} configured "
        "(call get_data_quality_rules for the detail)"
    )

    extra = {k: v for k, v in manifest.items() if k not in _RENDERED_KEYS}
    if extra:
        out.append("  other attributes:")
        for key, value in extra.items():
            out.append(f"    {key:<20}: {_render_value(value)}")
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
    except _AccountError as exc:
        return str(exc)
    try:
        name, manifest = await _load_manifest(alias, url, filesystem, config_path, dataset)
    except _DatasetError as exc:
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
    dates off the returned names.

    ponytail: prefix-only listing — a dataset whose template puts a token EARLY
    (``raw/{yyyy}/alpha/``) lists that token's whole subtree. Render the tokens
    for a concrete business date if that folder layout shows up.
    """
    head = str(expected_path).split("{", 1)[0]
    return head.strip("/")


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
    try:
        alias, url, filesystem, config_path = _resolve_account(account)
    except _AccountError as exc:
        return str(exc)

    scope = ""
    # Normalized the same way _literal_prefix normalizes a dataset's expected
    # path, so both branches render one trailing slash, not two.
    prefix = (path or "").strip().strip("/")
    if dataset and dataset.strip():
        try:
            name, manifest = await _load_manifest(alias, url, filesystem, config_path, dataset)
        except _DatasetError as exc:
            return str(exc)
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


ADLS_TOOLS = [
    list_datasets,
    get_dataset_config,
    get_data_quality_rules,
    list_dataset_files,
]


__all__ = [
    "ADLS_TOOLS",
    "close_adls_resources",
    "get_data_quality_rules",
    "get_dataset_config",
    "list_dataset_files",
    "list_datasets",
]
