"""ADLS tool for the adls-agent: reports when a file landed in a landing folder (read-only)."""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import re
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
_ET = ZoneInfo("America/New_York")
_SLA_ZONES = {
    **dict.fromkeys(("ET", "EST", "EDT", "EASTERN"), ("ET", _ET)),
    **dict.fromkeys(("CT", "CST", "CDT", "CENTRAL"), ("CT", ZoneInfo("America/Chicago"))),
    **dict.fromkeys(("PT", "PST", "PDT", "PACIFIC"), ("PT", ZoneInfo("America/Los_Angeles"))),
}
_SLA_TIME = re.compile(
    r"^\s*(\d{1,2})(?::?(\d{2}))?(?!\d)\s*(?:([ap])\.?\s*m\.?)?(?![a-z])\s*(.*?)\s*$",
    re.IGNORECASE,
)
_NOT_A_ZONE = frozenset({"ON", "AT", "BY", "FOR", "EACH", "EVERY", "DAILY", "THE", "TIME"})
# One cached client per storage endpoint.
_clients: dict[str, BlobServiceClient] = {}


class _InputError(ValueError):
    """Unusable input; the message is returned to the model as is."""


class _Refused(_InputError):
    """A path or setting the deployment refuses; logged as a warning."""


@dataclass(frozen=True)
class _Sla:
    given: str
    due: datetime
    label: str
    assumed_et: bool


@dataclass(frozen=True)
class _Request:
    alias: str
    endpoint: str
    filesystem: str
    prefix: str
    day: date
    zone: ZoneInfo
    sla: _Sla | None
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
    files: list[BlobProperties] = field(default_factory=list)  # newest first
    seen: bool = False  # the listing returned anything, folder placeholders included
    truncated: bool = False
    error: str = ""


@dataclass(frozen=True)
class _Account:
    alias: str
    endpoint: str
    paths: dict[str, tuple[str, ...]]  # container -> allowed folder prefixes; "*" = all
    legacy: str  # the "filesystem" container, read with ADLS_ALLOWED_PATH_PREFIXES


@tool
async def check_file_arrival(
    path: str,
    sla_time: str = "",
    date: str = "",
    file_pattern: str = "",
    account: str = "",
) -> str:
    """Check whether a file landed in an ADLS landing (LND) folder on a business date.

    Lists the inbound, error and archive folders under ``path`` and reports the
    files last modified on that date, with where and when each landed, and
    compares the arrival time against the SLA when one is given.

    Args:
        path: Landing folder from the user's question, e.g. ``lnd/loansys/daily_payments``
              or a full ``abfss://``, ``abfs://`` or ``https://`` URL. Required — never
              guess one.
        sla_time: Expected arrival time exactly as the user gave it, e.g. ``6:00 AM ET``,
                  ``18:00 CT`` or ``1800``. No timezone means ET; only ET, CT and PT
                  are accepted. Leave empty when the user gave none.
        date: Business date ``YYYY-MM-DD``, ``today`` or ``yesterday``. Empty = today.
        file_pattern: Optional file name filter, case-insensitive. Plain text such as
                      an object ID matches any file name that contains it; use
                      ``*`` or ``?`` only for a wildcard pattern, e.g. ``PMT_*.csv``.
        account: Storage account the user named (its alias or account name), e.g. ``lakedev``.
                 Leave empty when they named none; a full URL already names it.
    """
    started = perf_counter()
    try:
        request = _build_request(path, sla_time, date, file_pattern, account)
        logger.info(
            "ADLS file arrival check: account=%s path=%r date=%s sla=%r filter=%r container=%s",
            request.alias,
            request.prefix,
            request.day,
            request.sla.given if request.sla else "",
            request.pattern,
            request.filesystem,
        )
        container = _container(request.endpoint, request.filesystem)
        results = await asyncio.gather(
            *(_list_folder(container, request, folder) for folder in _FOLDER_STATUS)
        )
        # Anything listed proves the path exists; otherwise ask storage once more.
        exists = any(result.seen for result in results) or await _path_exists(
            container, request.prefix
        )
        logger.info(
            "ADLS file arrival check for %r returned in %.0f ms: %s",
            request.prefix,
            (perf_counter() - started) * 1000,
            _outcome(results, exists),
        )
        return _render(request, results, exists)
    except _InputError as exc:
        level = logging.WARNING if isinstance(exc, _Refused) else logging.INFO
        logger.log(level, "ADLS file arrival check refused for path %r: %s", path, exc)
        return str(exc)
    except Exception:  # one bad check must not end the agent's turn
        logger.exception("ADLS file arrival check failed unexpectedly for path %r", path)
        return "[adls-agent] The check failed unexpectedly, so nothing could be confirmed."


ADLS_TOOLS = [check_file_arrival]


# --- storage clients ----------------------------------------------------------


def _container(account_url: str, filesystem: str) -> ContainerClient:
    client = _clients.get(account_url)
    if client is None:
        logger.info("Creating ADLS blob client for %s", account_url)
        # ThreadOffloadAsyncCredential: azure.identity.aio's CLI credential blocks the loop.
        client = _clients[account_url] = BlobServiceClient(
            account_url=account_url, credential=ThreadOffloadAsyncCredential()
        )
    return client.get_container_client(filesystem)


async def close_adls_resources() -> None:
    """Close every cached blob client (idempotent)."""
    global _clients
    clients, _clients = list(_clients.items()), {}
    for key, client in clients:
        try:
            await client.close()
        except Exception:
            logger.warning("Error closing ADLS client for %s", key, exc_info=True)


# --- request parsing ----------------------------------------------------------


def _build_request(
    path: str, sla_time: str, day_text: str, pattern: str, account_name: str = ""
) -> _Request:
    host, url_container, relative = _split_path(path)
    account = _pick_account(account_name, host, url_container)
    if url_container and url_container not in account.paths:
        # abfss://lnd@... can name the lnd/ folder of a configured container.
        relative = f"{url_container}/{relative}"
    prefix, file_name = _clean_prefix(relative)
    container, prefix = _pick_container(account, prefix, url_container)
    zone = _zone()
    day = _resolve_date(day_text, zone)
    return _Request(
        alias=account.alias,
        endpoint=account.endpoint,
        filesystem=container,
        prefix=prefix,
        day=day,
        zone=zone,
        sla=_parse_sla(sla_time, day),
        pattern=(pattern or "").strip() or file_name,
    )


def _split_path(path: str) -> tuple[str, str, str]:
    """``(host, container, folder path)`` from a URL; a plain path has no host or container."""
    raw = (path or "").strip()
    if not raw:
        raise _InputError(
            "[adls-agent] No landing path was given. Ask the user for the landing "
            "folder, e.g. lnd/<source>/<feed>."
        )
    if "://" not in raw:
        return "", "", raw
    try:  # a copied placeholder like [account] is not a URL
        url = urlparse(raw)
        if url.scheme.lower() in ("abfss", "abfs", "wasbs", "wasb"):  # <container>@<host>/<path>
            container, _, host = url.netloc.partition("@")
            relative = url.path
        else:  # https://<host>/<container>/<path>
            host = url.netloc
            container, _, relative = url.path.lstrip("/").partition("/")
        if not host:
            raise ValueError("no host")
    except ValueError as exc:
        raise _InputError(
            f"[adls-agent] Could not read the landing path '{raw}': it is not a valid URL. "
            "Ask the user to check it. Nothing was read."
        ) from exc
    return host.lower(), container, unquote(relative).lstrip("/")


def _accounts() -> dict[str, _Account]:
    """Usable ADLS_ACCOUNT_MAPPING entries by alias: an account URL and a container."""
    accounts = {}
    for alias, entry in settings.adls_account_mapping.items():
        entry = entry if isinstance(entry, dict) else {}
        containers = entry.get("containers")
        paths = {
            name: _path_list(items)
            for key, items in (containers.items() if isinstance(containers, dict) else [])
            if (name := str(key).strip())
        }
        legacy = str(entry.get("filesystem") or "").strip()
        if legacy in paths:
            legacy = ""
        elif legacy:
            paths[legacy] = _path_list(settings.adls_allowed_path_prefixes)
        url = str(entry.get("account_url") or "").strip()
        if url and paths:
            accounts[alias] = _Account(alias, _blob_endpoint(url), paths, legacy)
        else:
            logger.warning(
                "ADLS_ACCOUNT_MAPPING entry '%s' needs account_url and a container", alias
            )
    return accounts


def _path_list(items: object) -> tuple[str, ...]:
    """Folder prefixes from a list or one comma-separated string."""
    if isinstance(items, str):
        items = items.split(",")
    if not isinstance(items, list | tuple):
        return ()
    return tuple(path for item in items if (path := str(item).strip().strip("/")))


def _pick_account(name: str, host: str, url_container: str) -> _Account:
    """The account the URL or ``name`` points at, else the default or the only one."""
    accounts = _accounts()
    if not accounts:
        raise _no_account()
    named = _named_account(accounts, name)
    if host:
        endpoint = _blob_endpoint(f"https://{host}")
        on_url = [account for account in accounts.values() if account.endpoint == endpoint]
        if not on_url:
            raise _Refused(
                f"[adls-agent] Wrong storage account: '{host}' is not a Data Lake account "
                "this agent reads. Nothing was read."
            )
        if named is None:
            # Several aliases can share one account; take the one with the URL's container.
            return next((a for a in on_url if url_container in a.paths), on_url[0])
        if named.endpoint != endpoint:
            raise _InputError(
                f"[adls-agent] The URL is on account '{on_url[0].alias}', not "
                f"'{name.strip()}'. Ask the user which one. Nothing was read."
            )
        return named
    if named is not None:
        return named
    if name.strip() and len(accounts) > 1:
        raise _InputError(
            f"[adls-agent] Unknown storage account '{name.strip()}'. Configured: "
            f"{', '.join(sorted(accounts))}. Nothing was read."
        )
    default = (settings.adls_default_account or "").strip()
    if default:
        if default not in accounts:
            raise _no_account()
        return accounts[default]
    if len(accounts) == 1:
        return next(iter(accounts.values()))
    raise _InputError(
        f"[adls-agent] Several storage accounts are configured ({', '.join(sorted(accounts))}) "
        "and none was named. Ask the user which one. Nothing was read."
    )


def _named_account(accounts: dict[str, _Account], name: str) -> _Account | None:
    """An account by its alias or storage account name, in any case."""
    wanted = name.strip().casefold()
    for account in accounts.values():
        storage_name = account.endpoint.split("://")[-1].split(".")[0]
        if wanted and wanted in (account.alias.casefold(), storage_name):
            return account
    return None


def _no_account() -> _Refused:
    return _Refused(
        "[adls-agent] No usable Data Lake account is configured (check "
        "ADLS_ACCOUNT_MAPPING and ADLS_DEFAULT_ACCOUNT). Nothing was read."
    )


def _blob_endpoint(account_url: str) -> str:
    """Normalize either ADLS endpoint form (.dfs or .blob) to the blob one."""
    url = account_url.strip().rstrip("/").lower()
    return url.replace(".dfs.core.windows.net", ".blob.core.windows.net")


def _clean_prefix(relative: str) -> tuple[str, str]:
    """The feed folder, and the file name if the path goes down to a file."""
    if "\\" in relative:
        raise _Refused("[adls-agent] Path not allowed: use forward slashes. Nothing was read.")
    segments = [segment for segment in relative.split("/") if segment]
    if any(segment in (".", "..") for segment in segments):
        raise _Refused(
            "[adls-agent] Path not allowed: '.' and '..' are not permitted. Nothing was read."
        )
    # Cut at the last inbound/error/archive; what follows may name a file.
    marks = [i for i, segment in enumerate(segments) if segment.lower() in _FOLDER_STATUS]
    cut = marks[-1] if marks else len(segments)
    segments, tail = segments[:cut], segments[cut + 1 :]
    if not segments:
        raise _InputError(
            "[adls-agent] The landing path names no feed folder. Ask the user for the "
            "landing folder, e.g. lnd/<source>/<feed>."
        )
    file_name = tail[-1] if tail and "." in tail[-1] else ""
    return "/".join(segments), file_name


def _pick_container(account: _Account, prefix: str, url_container: str) -> tuple[str, str]:
    """The container to read and the feed folder inside it."""
    if not any(account.paths.values()):
        if account.legacy:
            raise _Refused(
                "[adls-agent] No landing paths are allowed in this deployment "
                "(ADLS_ALLOWED_PATH_PREFIXES is empty). Nothing was read."
            )
        raise _Refused(
            f"[adls-agent] No landing paths are listed for account '{account.alias}' in "
            "ADLS_ACCOUNT_MAPPING. Nothing was read."
        )
    if url_container in account.paths:
        if _allows(account, url_container, prefix):
            return url_container, prefix
        raise _not_allowed(account, _shown(account, url_container, prefix))
    # A plain path may start with its container, e.g. lnd/loansys/daily_payments.
    head, _, rest = prefix.partition("/")
    if rest and head in account.paths and _allows(account, head, rest):
        return head, rest
    fits = sorted(name for name in account.paths if _allows(account, name, prefix))
    if len(fits) == 1:
        return fits[0], prefix
    if fits:
        raise _InputError(
            f"[adls-agent] The landing path '{prefix}' fits more than one container "
            f"({', '.join(fits)}) of account '{account.alias}'. Ask the user which "
            "container. Nothing was read."
        )
    if url_container:
        raise _Refused(
            f"[adls-agent] Container '{url_container}' is not one this agent reads. "
            "Nothing was read."
        )
    raise _not_allowed(account, prefix)


def _allows(account: _Account, container: str, path: str) -> bool:
    """Whether the container's prefixes cover the path; "*" counts only in "containers"."""
    whole = "*" if container != account.legacy else None
    return any(
        entry == whole or path == entry or path.startswith(entry + "/")
        for entry in account.paths[container]
    )


def _shown(account: _Account, container: str, path: str) -> str:
    """A path as users write it; the old "filesystem" container is never named."""
    return path if container == account.legacy else f"{container}/{path}"


def _not_allowed(account: _Account, path: str) -> _Refused:
    allowed = sorted(
        _shown(account, name, entry) for name, entries in account.paths.items() for entry in entries
    )
    more = f" and {len(allowed) - 10} more" if len(allowed) > 10 else ""
    return _Refused(
        f"[adls-agent] Path not allowed: '{path}' is outside the permitted landing folders "
        f"({', '.join(allowed[:10])}{more}). Nothing was read."
    )


def _zone() -> ZoneInfo:
    try:
        return ZoneInfo(settings.adls_business_timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise _Refused(
            f"[adls-agent] ADLS_BUSINESS_TIMEZONE '{settings.adls_business_timezone}' is not "
            "a valid timezone. Nothing was read."
        ) from exc


def _resolve_date(text: str, zone: ZoneInfo) -> date:
    today = datetime.now(zone).date()
    raw = re.sub(r"^(?:on|for)\s+|'s$|[.,!?]+$", "", (text or "").strip().lower()).strip()
    if raw in ("", "today", "todays"):
        return today
    if raw in ("yesterday", "yesterdays"):
        return today - timedelta(days=1)
    try:
        day = date.fromisoformat(raw)
    except ValueError as exc:
        raise _InputError(
            f"[adls-agent] Could not read the date '{text}'. Pass it as YYYY-MM-DD or 'today'."
        ) from exc
    if day > today:
        raise _InputError(
            f"[adls-agent] The date {day} is in the future; no file can have landed yet."
        )
    return day


def _parse_sla(text: str, day: date) -> _Sla | None:
    """The SLA time on the business date; no timezone means ET."""
    given = (text or "").strip()
    if not given:
        return None
    match = _SLA_TIME.match(given)
    due_time = _time_of_day(match) if match else None
    if match is None or due_time is None:
        raise _InputError(
            f"[adls-agent] Could not read the SLA '{given}'. Give it like 6:00 AM ET, "
            "18:00 CT or 1800 PT."
        )
    typed = match[4].split()[0].strip("(),.") if match[4] else ""
    word = typed.upper()
    if word and word not in _NOT_A_ZONE and word not in _SLA_ZONES:
        raise _InputError(
            f"[adls-agent] Timezone '{typed}' isn't supported. Please give the SLA in ET, CT or PT."
        )
    label, zone = _SLA_ZONES.get(word, ("ET", _ET))
    due = datetime.combine(day, due_time, tzinfo=zone)
    return _Sla(given=given, due=due, label=label, assumed_et=word not in _SLA_ZONES)


def _time_of_day(match: re.Match[str]) -> time | None:
    """The 24-hour time an ``_SLA_TIME`` match names, or None when out of range."""
    hour, minute, meridiem = int(match[1]), int(match[2] or 0), (match[3] or "").lower()
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == "p" else 0)
    return time(hour, minute) if hour <= 23 and minute <= 59 else None


# --- storage listing ----------------------------------------------------------


async def _list_folder(container: ContainerClient, request: _Request, folder: str) -> _FolderResult:
    """Files under ``<prefix>/<folder>/`` last modified inside the business day."""
    start, end = request.window
    listing = f"{request.prefix}/{folder}/"
    result, scanned = _FolderResult(folder), 0
    try:
        async for blob in container.list_blobs(name_starts_with=listing, include=["metadata"]):
            result.seen = True
            # Gen2 folder placeholders (hdi_isfolder) are not files.
            if (blob.metadata or {}).get("hdi_isfolder") == "true":
                continue
            scanned += 1
            if scanned > _MAX_LISTED:
                result.truncated = True
                logger.warning(
                    "ADLS listing of %r stopped after %d entries; results may be incomplete",
                    listing,
                    _MAX_LISTED,
                )
                break
            modified = blob.last_modified
            if modified is None or not start <= modified < end:
                continue
            if _name_matches(blob.name, request.pattern):
                result.files.append(blob)
    except Exception as exc:  # noqa: BLE001 - reported to the model as a partial result
        result.error = _error_code(exc)
        logger.warning(
            "ADLS listing of %r failed (%s): %s", listing, type(exc).__name__, _truncate(exc)
        )
    result.files.sort(key=lambda blob: blob.last_modified, reverse=True)
    return result


def _name_matches(name: str, pattern: str) -> bool:
    """File-name match: wildcard when the filter has ``*``/``?``/``[``, otherwise "contains"."""
    name, pattern = name.rsplit("/", 1)[-1].lower(), pattern.lower()
    if _WILDCARDS.intersection(pattern):
        return fnmatch.fnmatchcase(name, pattern)
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


# --- report -------------------------------------------------------------------


def _outcome(results: list[_FolderResult], exists: bool) -> str:
    """One-line result for the log: files found per landing folder."""
    if not exists:
        return "nothing exists under the path"
    return ", ".join(
        f"{result.folder}={'unreadable' if result.error else len(result.files)}"
        for result in results
    )


def _render(request: _Request, results: list[_FolderResult], exists: bool) -> str:
    lines = [
        "[adls-agent] File arrival check",
        _field(
            "landing path",
            f"{request.prefix} (account {request.alias}, container {request.filesystem})",
        ),
        _field("business date", f"{request.day} ({request.zone.key})"),
        _field("SLA given", _sla_line(request.sla)),
    ]
    if request.pattern:
        mode = "matches" if _WILDCARDS.intersection(request.pattern) else "contains"
        lines.append(_field("file filter", f"name {mode} '{request.pattern}' (case-insensitive)"))
    found = [result for result in results if result.files]
    if found:
        newest = max(found, key=lambda result: result.files[0].last_modified)
        blob = newest.files[0]
        lines += [
            _field("status", _FOLDER_STATUS[newest.folder]),
            _field("arrived", _format_time(blob.last_modified, request.zone, request.sla)),
        ]
        if request.sla:
            lines.append(_field("SLA status", _sla_status(blob.last_modified, request.sla)))
        lines.append(_field("file", blob.name))
        unread = [result.folder for result in results if result.error or result.truncated]
        if unread:
            lines.append(
                _field(
                    "note",
                    f"Partial result — {', '.join(unread)} could not be fully read, "
                    "so the file may also be there",
                )
            )
    else:
        lines.append(_field("status", _miss_status(request, results, exists)))
    if exists:
        lines += ["", "Folders checked:"]
        for result in results:
            lines += _folder_lines(result, request.zone)
    return "\n".join(lines)


def _field(label: str, value: str) -> str:
    return f"  {label:<13}: {value}"


def _miss_status(request: _Request, results: list[_FolderResult], exists: bool) -> str:
    """The status line when no file was found."""
    if not exists:
        return (
            f"Incorrect path — nothing exists under '{request.prefix}/'. "
            "The landing path given is wrong, not the file missing; ask the user to check the path."
        )
    if any(result.error for result in results):
        return "Partial result — no file found in the folders that could be read"
    if any(result.truncated for result in results):
        return (
            f"Partial result — no file found in the first {_MAX_LISTED} "
            "entries of a folder; the rest was not read"
        )
    matching = f"matching '{request.pattern}' " if request.pattern else ""
    return (
        f"File not found for this date — no file {matching}under inbound, "
        f"error or archive was last modified on {request.day}"
    )


def _folder_lines(result: _FolderResult, zone: ZoneInfo) -> list[str]:
    label = f"  {result.folder}"
    if result.error:
        return [f"{label}: could not be read ({result.error})"]
    if not result.files:
        if result.truncated:
            return [f"{label}: none in the first {_MAX_LISTED} entries (the rest was not read)"]
        return [f"{label}: none"]
    lines = [f"{label}: {len(result.files)} file(s)"]
    for blob in result.files[:_MAX_FILES_SHOWN]:
        created = getattr(blob, "creation_time", None) or blob.last_modified
        lines.append(
            f"    - {blob.name} | {blob.size} bytes | created {_format_time(created, zone)} "
            f"| lastModified {_format_time(blob.last_modified, zone)}"
        )
    if len(result.files) > _MAX_FILES_SHOWN:
        lines.append(f"    … {len(result.files) - _MAX_FILES_SHOWN} more not shown")
    if result.truncated:
        lines.append(
            f"    (listing stopped after {_MAX_LISTED} entries; results may be incomplete)"
        )
    return lines


def _sla_line(sla: _Sla | None) -> str:
    if sla is None:
        return "none in the question"
    read = f"{_clock(sla.due)} {sla.label}"
    if sla.label != "ET":
        read += f" ({_clock(sla.due.astimezone(_ET))} ET)"
    if sla.assumed_et:
        read += " (no timezone given, read as ET)"
    return f"{sla.given} → {read}"


def _sla_status(modified: datetime, sla: _Sla) -> str:
    if modified <= sla.due:
        return "On time"
    minutes = max(1, round((modified - sla.due).total_seconds() / 60))
    return f"Late by {minutes} min"


def _format_time(moment: datetime, zone: ZoneInfo, sla: _Sla | None = None) -> str:
    local = moment.astimezone(zone)
    also = f"; {moment.astimezone(sla.due.tzinfo):%H:%M %Z}" if sla and sla.label != "ET" else ""
    return f"{local:%Y-%m-%d %H:%M %Z} ({moment.astimezone(UTC):%H:%M} UTC{also})"


def _clock(moment: datetime) -> str:
    return f"{moment.hour % 12 or 12}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"
