"""Each process's snapshot of the workspace registry, and the poller that refreshes it.

Requests read the snapshot (``databricks_workspaces.workspaces()``), never the store:
a store that is down leaves the last snapshot in place. Every replica polls the
store's ``registry_version`` and re-reads the workspaces only when it moved, so an
admin's change reaches every replica within ``DATABRICKS_REGISTRY_POLL_SECONDS``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

import v1.core.databricks_workspaces as dw
from v1.core.config import (
    databricks_registered,
    ignored_admin_entries,
    retired_databricks_settings,
)
from v1.core.databricks_workspaces import Workspace, validate_entry
from v1.core.workspace_registry.seed import seed_from_file
from v1.core.workspace_registry.store import WorkspaceStore, make_store

if TYPE_CHECKING:
    from v1.core.config import Settings

logger = logging.getLogger(__name__)

Listener = Callable[[frozenset[str]], None]


class RegistrySnapshot:
    """The registry's live workspaces, in order, as of ``version``."""

    def __init__(self) -> None:
        self._workspaces: tuple[Workspace, ...] = ()
        self._version: int | None = None
        self._listeners: list[Listener] = []
        # One refresh at a time. Made on first use in each event loop: a lock is bound
        # to the loop it is first awaited in, and tests run each case in a new loop.
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    def current(self) -> tuple[Workspace, ...]:
        return self._workspaces

    @property
    def version(self) -> int | None:
        """The ``registry_version`` the snapshot was read at; None before the first read."""

        return self._version

    def replace(self, workspaces: Sequence[Workspace], version: int | None = None) -> None:
        """Make ``workspaces`` the snapshot (tests set it directly). Listeners are not told."""

        self._workspaces = tuple(workspaces)
        self._version = version

    def on_change(self, listener: Listener) -> None:
        """Call ``listener`` after each refresh with the aliases that changed or went."""

        self._listeners.append(listener)

    def _refresh_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    async def refresh(self, store: WorkspaceStore) -> bool:
        """Re-read the store if its ``registry_version`` changed; whether it did.

        Refreshes run one at a time (a post-write refresh and the poller would
        otherwise interleave, and the slower could swap older rows back in), so each
        reads the store at least as new as the last. A version lower than the
        snapshot's (a restored or recreated database) is loaded too, with a WARNING.
        The version is read before the rows, so the rows are never older than the
        version recorded with them.
        """

        async with self._refresh_lock():
            return await self._refresh(store)

    async def _refresh(self, store: WorkspaceStore) -> bool:
        version = await store.registry_version()
        if version == self._version:
            return False
        if self._version is not None and version < self._version:
            # A restored or recreated database restarts the counter; its rows win.
            logger.warning(
                "registry version went backwards (%s → %s); reloading", self._version, version
            )
        loaded: list[Workspace] = []
        for row in await store.list():
            result = validate_entry(row.alias, row.fields())
            if isinstance(result, str):
                logger.error(
                    "databricks workspace %s skipped: %s is invalid or missing", row.alias, result
                )
                continue
            loaded.append(result)
        old = {w.alias: w for w in self._workspaces}
        self.replace(loaded, version)
        new = {w.alias: w for w in loaded}
        if set(old) != set(new):
            _check_legacy(frozenset(new))
        changed = frozenset(alias for alias, ws in old.items() if new.get(alias) != ws)
        if changed:
            for listener in self._listeners:
                try:
                    listener(changed)
                except Exception as exc:  # noqa: BLE001 - one listener must not stop the rest
                    logger.error(
                        "databricks registry change listener failed (%s)", type(exc).__name__
                    )
        return True


def _check_legacy(aliases: frozenset[str]) -> None:
    """Log when ``DATABRICKS_LEGACY_WORKSPACE`` is not among the loaded aliases."""

    legacy = dw.legacy_alias()
    if legacy and aliases and legacy not in aliases:
        logger.error(
            "DATABRICKS_LEGACY_WORKSPACE %s is not a configured workspace; "
            "chats without a workspace will be refused",
            legacy,
        )


_snapshot = RegistrySnapshot()
_store: WorkspaceStore | None = None
_poller: asyncio.Task[None] | None = None


def get_snapshot() -> RegistrySnapshot:
    """The process's snapshot."""

    return _snapshot


def on_change(listener: Listener) -> None:
    """Call ``listener`` with the aliases each refresh of the process's snapshot changed."""

    _snapshot.on_change(listener)


def get_store() -> WorkspaceStore | None:
    """The registry's store, once ``start_registry`` made it."""

    return _store


async def _prepare(store: WorkspaceStore, seed_file: str | None) -> bool:
    """Migrate and seed the store; whether both worked (a failure is logged)."""

    try:
        await store.migrate()
        await seed_from_file(store, seed_file)
    except Exception as exc:  # noqa: BLE001 - the app starts without the registry
        # Only the class: a connection error can carry the DSN.
        logger.error(
            "databricks workspace registry not ready (%s); retrying on the next poll",
            type(exc).__name__,
        )
        return False
    return True


async def _refresh(store: WorkspaceStore) -> None:
    try:
        await _snapshot.refresh(store)
    except Exception as exc:  # noqa: BLE001 - keep the last snapshot
        logger.error(
            "databricks workspace registry refresh failed (%s); keeping the last workspaces",
            type(exc).__name__,
        )


async def _poll(store: WorkspaceStore, seconds: float, seed_file: str | None, ready: bool) -> None:
    while True:
        await asyncio.sleep(seconds)
        if not ready:
            ready = await _prepare(store, seed_file)
            if not ready:
                continue
        await _refresh(store)


async def start_registry(settings: Settings) -> None:
    """Load the registry into this process's snapshot and keep it fresh. Never raises
    for the store: an unreachable one is logged, and the snapshot stays as it was."""

    global _store, _poller
    retired = retired_databricks_settings()
    if retired:
        logger.error(
            "%s are no longer read; workspaces come from DATABRICKS_WORKSPACES_SEED_FILE "
            "and the admin page",
            ", ".join(retired),
        )
    if not databricks_registered() or _store is not None:
        return
    ignored = ignored_admin_entries(settings.databricks_admin_groups)
    if ignored:
        # The count only: the entries are group names, which are not logged elsewhere.
        logger.error(
            "DATABRICKS_ADMIN_GROUPS: %d entries are not group object IDs and are ignored; "
            "only object IDs grant workspace admin",
            ignored,
        )
    seed_file = (settings.databricks_workspaces_seed_file or "").strip() or None
    store = make_store(settings)
    _store = store
    ready = await _prepare(store, seed_file)
    if ready:
        await _refresh(store)
    _poller = asyncio.create_task(
        _poll(store, settings.databricks_registry_poll_seconds, seed_file, ready),
        name="databricks-registry-poller",
    )


async def stop_registry() -> None:
    """Stop the poller and close the store. Safe to call more than once."""

    global _store, _poller
    poller, _poller = _poller, None
    if poller is not None:
        poller.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poller
    store, _store = _store, None
    if store is not None:
        try:
            await store.close()
        except Exception as exc:  # noqa: BLE001 - shutdown goes on
            logger.error("databricks workspace registry close failed (%s)", type(exc).__name__)


__all__ = [
    "RegistrySnapshot",
    "get_snapshot",
    "get_store",
    "on_change",
    "start_registry",
    "stop_registry",
]
