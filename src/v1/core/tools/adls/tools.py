"""ADLS tool for the adls-agent: reports when a file landed in a landing folder (read-only)."""

from __future__ import annotations

import asyncio
import fnmatch
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from time import perf_counter
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from azure.storage.blob import BlobProperties
from azure.storage.blob.aio import BlobServiceClient, ContainerClient
from langchain_core.tools import tool

from v1.core.config import get_settings
from v1.utils.azure_credentials import ThreadOffloadAsyncCredential

logger = logging.getLogger(__name__)
settings = get_settings()

_MAX_MSG = 600
_MAX_FILES_SHOWN = 40
_MAX_LISTED = 5000
# Landing folder -> what a file there means.
_FOLDER_STATUS = {
    "archive": "File received and processed successfully",
    "error": "File received; processing failed",
    "inbound": "File received; still being processed",
}
_WILDCARDS = frozenset("*?[")


class _InputError(ValueError):
    """Unusable input; the message is returned to the model as is."""


class _Refused(_InputError):
    """A path or setting the deployment refuses; logged as a warning."""


@dataclass(frozen=True)
class _Request:
    alias: str
    endpoint: str
    filesystem: str
    prefix: str
    day: date
    zone: ZoneInfo
    sla: str
    pattern: str

    @property
    def window(self) -> tuple[datetime, datetime]:
        """The business day as a UTC range."""
        start = datetime.combine(self.day, time.min, tzinfo=self.zone)
        end = datetime.combine(self.day + timedelta(days=1), time.min, tzinfo=self.zone)
        return start.astimezone(UTC), end.astimezone(UTC)


@dataclass
class _FolderResult:
    folder: str
    files: list[BlobProperties] = field(default_factory=list)
    truncated: bool = False
    error: str = ""


@tool
async def check_file_arrival(
    path: str,
    sla_time: str = "",
    date: str = "",
    file_pattern: str = "",
) -> str:
    """Check whether a file landed in an ADLS landing (LND) folder on a business date.

    Lists the inbound, error and archive folders under ``path`` and reports the
    files last modified on that date, with where and when each landed, next to
    the SLA the user gave. Never returns an on-time/late verdict.

    Args:
        path: Landing folder from the user's question, e.g. ``lnd/loansys/daily_payments``
              or an ``abfss://`` URL. Required — never guess one.
        sla_time: Expected arrival time exactly as the user gave it, e.g. ``06:00``
                  or ``6:00 AM ET``. Leave empty when the user gave none.
        date: Business date ``YYYY-MM-DD``, ``today`` or ``yesterday``. Empty = today.
        file_pattern: Optional file name filter, case-insensitive. Plain text such as
                      an object ID matches any file name that contains it; use
                      ``*`` or ``?`` only for a wildcard pattern, e.g. ``PMT_*.csv``.
    """
    started = perf_counter()
    try:
        request = _build_request(path, sla_time, date, file_pattern)
    except _InputError as exc:
        level = logging.WARNING if isinstance(exc, _Refused) else logging.INFO
        logger.log(level, "ADLS file arrival check refused for path %r: %s", path, exc)
        return str(exc)
    logger.info(
        "ADLS file arrival check: account=%s path=%r date=%s sla=%r filter=%r",
        request.alias,
        request.prefix,
        request.day,
        request.sla,
        request.pattern,
    )
    container = _container(request.endpoint, request.filesystem)
    results = await asyncio.gather(
        *(_list_folder(container, request, folder) for folder in _FOLDER_STATUS)
    )
    exists = any(result.files for result in results) or await _path_exists(
        container, request.prefix
    )
    logger.info(
        "ADLS file arrival check for %r returned in %.0f ms: %s",
        request.prefix,
        (perf_counter() - started) * 1000,
        _outcome(results, exists),
    )
    return _render(request, results, exists)


ADLS_TOOLS = [check_file_arrival]


async def close_adls_resources() -> None:
    """Close every cached blob client (idempotent)."""
    global _clients
    stale = list(_clients.items())
    _clients = {}
    for key, client in stale:
        try:
            await client.close()
        except Exception:
            logger.warning("Error closing ADLS client for %s", key, exc_info=True)


def _build_request(path: str, sla_time: str, day_text: str, pattern: str) -> _Request:
    alias, endpoint, filesystem = _account()
    prefix, file_name = _clean_prefix(_relative_path(path, endpoint, filesystem))
    _check_allowed(prefix)
    zone = _zone()
    return _Request(
        alias=alias,
        endpoint=endpoint,
        filesystem=filesystem,
        prefix=prefix,
        day=_resolve_date(day_text, zone),
        zone=zone,
        sla=(sla_time or "").strip(),
        pattern=(pattern or "").strip() or file_name,
    )


def _zone() -> ZoneInfo:
    try:
        return ZoneInfo(settings.adls_business_timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise _Refused(
            f"[adls-agent] ADLS_BUSINESS_TIMEZONE '{settings.adls_business_timezone}' is not "
            "a valid timezone. Nothing was read."
        ) from exc


def _account() -> tuple[str, str, str]:
    """The configured storage account as ``(alias, blob_endpoint, filesystem)``."""
    mapping = settings.adls_account_mapping
    alias = settings.adls_default_account or (next(iter(mapping)) if len(mapping) == 1 else "")
    entry = mapping.get(alias) or {}
    if not (entry.get("account_url") and entry.get("filesystem")):
        raise _Refused(
            "[adls-agent] No usable Data Lake account is configured (check "
            "ADLS_ACCOUNT_MAPPING and ADLS_DEFAULT_ACCOUNT). Nothing was read."
        )
    return alias, _blob_endpoint(entry["account_url"]), entry["filesystem"]


def _blob_endpoint(account_url: str) -> str:
    """Normalize either ADLS endpoint form (.dfs or .blob) to the blob one."""
    return (
        account_url.strip().rstrip("/").replace(".dfs.core.windows.net", ".blob.core.windows.net")
    )


def _relative_path(path: str, endpoint: str, filesystem: str) -> str:
    """The folder path inside the container; a full URL must name the configured account."""
    raw = (path or "").strip()
    if not raw:
        raise _InputError(
            "[adls-agent] No landing path was given. Ask the user for the landing "
            "folder, e.g. lnd/<source>/<feed>."
        )
    if "://" not in raw:
        return raw
    url = urlparse(raw)
    if url.scheme.lower() in ("abfss", "abfs", "wasbs", "wasb"):  # <container>@<host>/<path>
        container, _, host = url.netloc.partition("@")
        relative = url.path
    else:  # https://<host>/<container>/<path>
        host = url.netloc
        container, _, relative = url.path.lstrip("/").partition("/")
    if _blob_endpoint(f"https://{host}").lower() != endpoint.lower() or container != filesystem:
        raise _Refused(
            f"[adls-agent] Path not allowed: container '{container}' on '{host}' is not a "
            "configured Data Lake account. Nothing was read."
        )
    return unquote(relative)


def _clean_prefix(relative: str) -> tuple[str, str]:
    """The feed folder, and the file name if the path goes down to a file."""
    if "\\" in relative:
        raise _Refused("[adls-agent] Path not allowed: use forward slashes. Nothing was read.")
    segments = [segment for segment in relative.split("/") if segment]
    if any(segment in (".", "..") for segment in segments):
        raise _Refused(
            "[adls-agent] Path not allowed: '.' and '..' are not permitted. Nothing was read."
        )
    marks = [i for i, segment in enumerate(segments) if segment.lower() in _FOLDER_STATUS]
    tail: list[str] = []
    if marks:
        segments, tail = segments[: marks[-1]], segments[marks[-1] + 1 :]
    if not segments:
        raise _InputError(
            "[adls-agent] The landing path names no feed folder. Ask the user for the "
            "landing folder, e.g. lnd/<source>/<feed>."
        )
    file_name = tail[-1] if tail and "." in tail[-1] else ""
    return "/".join(segments), file_name


def _check_allowed(prefix: str) -> None:
    allowed = [entry.strip().strip("/") for entry in settings.adls_allowed_path_prefixes]
    allowed = [entry for entry in allowed if entry]
    if not allowed:
        raise _Refused(
            "[adls-agent] No landing paths are allowed in this deployment "
            "(ADLS_ALLOWED_PATH_PREFIXES is empty). Nothing was read."
        )
    if not any(prefix == entry or prefix.startswith(entry + "/") for entry in allowed):
        raise _Refused(
            f"[adls-agent] Path not allowed: '{prefix}' is outside the permitted landing "
            f"folders ({', '.join(sorted(allowed))}). Nothing was read."
        )


def _resolve_date(text: str, zone: ZoneInfo) -> date:
    today = datetime.now(zone).date()
    raw = (text or "").strip().lower()
    if raw in ("", "today"):
        return today
    if raw == "yesterday":
        return today - timedelta(days=1)
    try:
        day = date.fromisoformat(raw)
    except ValueError as exc:
        raise _InputError(
            f"[adls-agent] Could not read the date '{text}'. Pass it as YYYY-MM-DD."
        ) from exc
    if day > today:
        raise _InputError(
            f"[adls-agent] The date {day} is in the future; no file can have landed yet."
        )
    return day


# One cached client per storage endpoint.
_clients: dict[str, BlobServiceClient] = {}


def _container(account_url: str, filesystem: str) -> ContainerClient:
    client = _clients.get(account_url)
    if client is None:
        logger.info("Creating ADLS blob client for %s", account_url)
        # ThreadOffloadAsyncCredential: azure.identity.aio's CLI credential blocks the loop.
        client = _clients[account_url] = BlobServiceClient(
            account_url=account_url, credential=ThreadOffloadAsyncCredential()
        )
    return client.get_container_client(filesystem)


async def _iter_files(container: ContainerClient, prefix: str) -> AsyncIterator[BlobProperties]:
    """Yield files under ``prefix``, skipping Gen2 folder placeholders (``hdi_isfolder``)."""
    async for blob in container.list_blobs(name_starts_with=prefix, include=["metadata"]):
        if (blob.metadata or {}).get("hdi_isfolder") != "true":
            yield blob


async def _list_folder(container: ContainerClient, request: _Request, folder: str) -> _FolderResult:
    """Files under ``<prefix>/<folder>/`` last modified inside the business day."""
    start, end = request.window
    listing = f"{request.prefix}/{folder}/"
    result = _FolderResult(folder)
    scanned = 0
    try:
        async for blob in _iter_files(container, listing):
            scanned += 1
            if scanned > _MAX_LISTED:
                result.truncated = True
                logger.warning(
                    "ADLS listing of %r stopped after %d entries; results may be incomplete",
                    listing,
                    _MAX_LISTED,
                )
                break
            if _matches(blob, start, end, request.pattern):
                result.files.append(blob)
    except Exception as exc:  # noqa: BLE001 - reported to the model as a partial result
        result.error = _error_code(exc)
        logger.warning(
            "ADLS listing of %r failed (%s): %s", listing, type(exc).__name__, _truncate(exc)
        )
    result.files.sort(key=lambda blob: blob.last_modified, reverse=True)
    return result


def _matches(blob: BlobProperties, start: datetime, end: datetime, pattern: str) -> bool:
    modified = blob.last_modified
    if modified is None or not start <= modified < end:
        return False
    return _name_matches(blob.name.rsplit("/", 1)[-1], pattern)


def _name_matches(name: str, pattern: str) -> bool:
    """Wildcard match when the filter has ``*``/``?``/``[``, otherwise "name contains"."""
    if not pattern:
        return True
    name, pattern = name.lower(), pattern.lower()
    if _WILDCARDS.intersection(pattern):
        return fnmatch.fnmatch(name, pattern)
    return pattern in name


async def _path_exists(container: ContainerClient, prefix: str) -> bool:
    try:
        async for _ in container.list_blobs(name_starts_with=f"{prefix}/", results_per_page=1):
            return True
    except Exception:  # a listing failure is reported per folder instead
        logger.debug(
            "ADLS existence check under %r failed; treating the path as present",
            f"{prefix}/",
            exc_info=True,
        )
        return True
    return False


def _outcome(results: list[_FolderResult], exists: bool) -> str:
    """One-line result for the log: files found per landing folder."""
    if not exists:
        return "nothing exists under the path"
    return ", ".join(
        f"{result.folder}={'unreadable' if result.error else len(result.files)}"
        for result in results
    )


def _render(request: _Request, results: list[_FolderResult], exists: bool) -> str:
    lines = ["[adls-agent] File arrival check", *_header_lines(request)]
    newest = _newest(results)
    if newest is not None:
        folder, blob = newest
        lines += [
            f"  status       : {_FOLDER_STATUS[folder]}",
            f"  arrived      : {_format_time(blob.last_modified, request.zone)}",
            f"  file         : {blob.name}",
        ]
        unread = [result.folder for result in results if result.error or result.truncated]
        if unread:
            lines.append(
                f"  note         : Partial result — {', '.join(unread)} could not be fully read, "
                "so the file may also be there"
            )
    elif not exists:
        lines.append(
            f"  status       : Incorrect path — nothing exists under '{request.prefix}/'. "
            "The landing path given is wrong, not the file missing; ask the user to check the path."
        )
        return "\n".join(lines)
    elif any(result.error for result in results):
        lines.append(
            "  status       : Partial result — no file found in the folders that could be read"
        )
    elif any(result.truncated for result in results):
        lines.append(
            f"  status       : Partial result — no file found in the first {_MAX_LISTED} "
            "entries of a folder; the rest was not read"
        )
    else:
        matching = f"matching '{request.pattern}' " if request.pattern else ""
        lines.append(
            f"  status       : File not found for this date — no file {matching}under inbound, "
            f"error or archive was last modified on {request.day}"
        )
    lines += ["", "Folders checked:"]
    for result in results:
        lines += _folder_lines(result, request.zone)
    return "\n".join(lines)


def _header_lines(request: _Request) -> list[str]:
    lines = [
        f"  landing path : {request.prefix} (account {request.alias}, container {request.filesystem})",
        f"  business date: {request.day} ({request.zone.key})",
        f"  SLA given    : {request.sla or 'none in the question'}",
    ]
    if request.pattern:
        mode = "matches" if _WILDCARDS.intersection(request.pattern) else "contains"
        lines.append(f"  file filter  : name {mode} '{request.pattern}' (case-insensitive)")
    return lines


def _folder_lines(result: _FolderResult, zone: ZoneInfo) -> list[str]:
    label = f"  {result.folder}"
    if result.error:
        return [f"{label}: could not be read ({result.error})"]
    if not result.files:
        if result.truncated:
            return [f"{label}: none in the first {_MAX_LISTED} entries (the rest was not read)"]
        return [f"{label}: none"]
    shown = result.files[:_MAX_FILES_SHOWN]
    lines = [f"{label}: {len(result.files)} file(s)"]
    for blob in shown:
        created = getattr(blob, "creation_time", None) or blob.last_modified
        lines.append(
            f"    - {blob.name} | {blob.size} bytes | created {_format_time(created, zone)} "
            f"| lastModified {_format_time(blob.last_modified, zone)}"
        )
    if len(result.files) > len(shown):
        lines.append(f"    … {len(result.files) - len(shown)} more not shown")
    if result.truncated:
        lines.append(
            f"    (listing stopped after {_MAX_LISTED} entries; results may be incomplete)"
        )
    return lines


def _newest(results: list[_FolderResult]) -> tuple[str, BlobProperties] | None:
    candidates = [(result.folder, result.files[0]) for result in results if result.files]
    return max(candidates, key=lambda item: item[1].last_modified, default=None)


def _format_time(moment: datetime, zone: ZoneInfo) -> str:
    local = moment.astimezone(zone)
    return f"{local:%Y-%m-%d %H:%M %Z} ({moment.astimezone(UTC):%H:%M} UTC)"


def _error_code(exc: Exception) -> str:
    """Short error for the user; the full text goes to the log only."""
    code = getattr(exc, "error_code", None) or type(exc).__name__
    status = getattr(exc, "status_code", None)
    code = getattr(code, "value", code)
    return f"{code}, HTTP {status}" if status else str(code)


def _truncate(text: object) -> str:
    """Collapse whitespace and cap length with a visible truncation marker."""
    text = " ".join(str(text).split())
    return text if len(text) <= _MAX_MSG else text[:_MAX_MSG] + " …[truncated]"
