"""Turns what the user pasted (a run ID, a run link, a job ID) into numeric IDs. Pure: no I/O.

Links are accepted only from this workspace's host, so a pasted URL can't point the app at
another workspace. The job ID inside a run link is ignored: the job always comes from get-run.
"""

import re
from urllib.parse import urlsplit

_DIGITS = re.compile(r"[0-9]+")
_RUN_PATH = re.compile(r"/jobs/([0-9]+)/runs/([0-9]+)/?")
# Older UI links put the run in the fragment: https://<host>/?o=<org>#job/<job>/run/<run>.
_RUN_FRAGMENT = re.compile(r"job/([0-9]+)/run/([0-9]+)")

# Databricks IDs are positive signed 64-bit integers.
_MAX_ID = 2**63


def _to_id(digits: str) -> int | None:
    value = int(digits)
    return value if 1 <= value < _MAX_ID else None


def hostname(host: str | None) -> str | None:
    """The lowercase hostname of a stored workspace host (with or without a scheme), or None."""
    # Apps may inject the host with or without a scheme; "//" makes urlsplit read it as a host.
    if not host:
        return None
    return urlsplit(host if "://" in host else f"//{host}").hostname


def same_host(url_host: str, workspace_host: str | None) -> bool:
    """True when url_host is this workspace's hostname (case-insensitive)."""
    expected = hostname(workspace_host)
    return bool(url_host) and expected is not None and url_host.lower() == expected


def link_host(text: str) -> str | None:
    """The hostname (lowercase) of an https run link on any host; None for anything else,
    including a bare run ID. Callers compare it with :func:`same_host`."""
    try:
        parts = urlsplit(text.strip())
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname:
        return None
    if not (_RUN_PATH.fullmatch(parts.path) or _RUN_FRAGMENT.fullmatch(parts.fragment)):
        return None
    return parts.hostname


def parse_job_id(text: str) -> int | None:
    """The job ID if text is all digits; None means treat it as a job name."""
    text = text.strip()
    return _to_id(text) if _DIGITS.fullmatch(text) else None


def parse_run_ref(text: str, workspace_host: str | None) -> int | None:
    """The run ID from a bare ID or a run link on this workspace; None if it's neither."""
    text = text.strip()
    if _DIGITS.fullmatch(text):
        return _to_id(text)

    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme != "https" or not same_host(parts.hostname or "", workspace_host):
        return None

    match = _RUN_PATH.fullmatch(parts.path) or _RUN_FRAGMENT.fullmatch(parts.fragment)
    return _to_id(match.group(2)) if match else None
