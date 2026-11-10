"""The workbooks the sttm-agent builds, kept for a day for the analyst to download.

The workbook tool (``sttm_workbook`` in :mod:`.tools`) keeps each file it builds
in the platform's store, and the download route (:mod:`v1.api.routes.sttm_files`)
reads it back. A file is the signed-in caller's whose run built it: it is kept
under their identity, and its link works for them alone. The bytes go to the
store only, never into the conversation's state, messages or traces.

A file is kept 24 hours. A store that expires items deletes it then. On one that
cannot (``langgraph dev``'s), it is kept without an expiry, and the owner's
expired files are deleted the next time a file is kept for them. Either way, no
file is served after it expires.
"""

from __future__ import annotations

import base64
import binascii
import logging
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from langgraph.store.base import BaseStore

from v1.core.config import get_settings
from v1.core.tools.sttm.session import timestamp
from v1.core.tools.sttm.workbook import WORKBOOKS

logger = logging.getLogger(__name__)

NAMESPACE = "sttm_files"
KEEP = timedelta(hours=24)
ID_LENGTH = 16
# Upper-case letters without I, L and O, as session ids use: an id has no digits,
# so no card-number filter masks it, and none reads as another.
_ID_LETTERS = "ABCDEFGHJKMNPQRSTUVWXYZ"
# What follows a build's id in its files' ids: 'data-dictionary' or 'sttm'.
_WORKBOOK_PARTS = frozenset(workbook.replace("_", "-") for workbook in WORKBOOKS)
# The most of one owner's files looked at when their expired ones are deleted.
MAX_SWEPT = 500


@dataclass(frozen=True)
class KeptFile:
    """A file in the store, as its owner downloads it."""

    id: str
    name: str
    media_type: str
    expires_at: datetime
    content: bytes = field(repr=False)


def new_file_id() -> str:
    """A new build's id; its files' ids are :func:`workbook_file_id`."""

    return "".join(secrets.choice(_ID_LETTERS) for _ in range(ID_LENGTH))


def workbook_file_id(build_id: str, workbook: str) -> str:
    """The id of a build's ``workbook`` file (a key of WORKBOOKS), e.g. '…-data-dictionary'.

    A build's files share its id, so their links differ only where their labels do: a link
    copied with the other file's id still opens its own file.
    """

    return f"{build_id}-{workbook.replace('_', '-')}"


def is_file_id(value: str) -> bool:
    # A link given before a build's files shared an id has no workbook part.
    build_id, dash, workbook = value.partition("-")
    return (
        len(build_id) == ID_LENGTH
        and all(letter in _ID_LETTERS for letter in build_id)
        and (not dash or workbook in _WORKBOOK_PARTS)
    )


def download_url(file_id: str) -> str:
    """The link to a kept file, as the frontend reaches the download route."""

    return f"{get_settings().sttm_download_base_url.strip().rstrip('/')}/{file_id}"


def _namespace(owner: str) -> tuple[str, str]:
    # Identities are "user:<hash>": no period, which a namespace label cannot hold.
    return (NAMESPACE, owner)


def _expires_at(value: Mapping[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value["expires_at"]))
    except (KeyError, ValueError):
        return None


async def _sweep(store: BaseStore, owner: str, now: datetime) -> None:
    """Delete ``owner``'s expired files, for a store that cannot expire them itself."""

    namespace = _namespace(owner)
    try:
        items = await store.asearch(namespace, limit=MAX_SWEPT, refresh_ttl=False)
        for item in items:
            expires = _expires_at(item.value)
            if expires is None or expires <= now:
                await store.adelete(namespace, item.key)
    except Exception:  # a failed clean-up must not lose the file just kept
        logger.warning("Could not delete expired STTM files", exc_info=True)


async def keep_file(
    store: BaseStore,
    *,
    file_id: str,
    owner: str,
    name: str,
    media_type: str,
    content: bytes,
    now: datetime,
    thread_id: str | None,
    session_id: str,
    version: int,
) -> KeptFile:
    """Keep ``content`` for ``owner`` for :data:`KEEP`, under ``file_id``."""

    expires = now + KEEP
    value = {
        "owner": owner,
        "thread_id": thread_id,
        "session_id": session_id,
        "version": version,
        "name": name,
        "media_type": media_type,
        "size": len(content),
        "content": base64.b64encode(content).decode("ascii"),
        "created_at": timestamp(now),
        "expires_at": timestamp(expires),
    }
    namespace = _namespace(owner)
    try:
        await store.aput(namespace, file_id, value, index=False, ttl=KEEP.total_seconds() / 60)
    except NotImplementedError:  # this store cannot expire items
        await store.aput(namespace, file_id, value, index=False)
        await _sweep(store, owner, now)
    return KeptFile(
        id=file_id, name=name, media_type=media_type, expires_at=expires, content=content
    )


async def get_file(store: BaseStore, *, owner: str, file_id: str, now: datetime) -> KeptFile | None:
    """``owner``'s file ``file_id``, or None when there is none or it has expired."""

    if not owner or not is_file_id(file_id):
        return None
    item = await store.aget(_namespace(owner), file_id, refresh_ttl=False)
    if item is None or item.value.get("owner") != owner:
        return None
    expires = _expires_at(item.value)
    if expires is None or expires <= now:
        return None
    try:
        content = base64.b64decode(str(item.value["content"]), validate=True)
    except (KeyError, binascii.Error):
        logger.warning("STTM file %s has no readable content", file_id)
        return None
    return KeptFile(
        id=file_id,
        name=str(item.value.get("name") or f"{file_id}.xlsx"),
        media_type=str(item.value.get("media_type") or "application/octet-stream"),
        expires_at=expires,
        content=content,
    )


__all__ = [
    "ID_LENGTH",
    "KEEP",
    "NAMESPACE",
    "KeptFile",
    "download_url",
    "get_file",
    "is_file_id",
    "keep_file",
    "new_file_id",
    "workbook_file_id",
]
