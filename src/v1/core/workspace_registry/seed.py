"""Seeding the workspace registry once from a per-environment YAML file.

The file (``DATABRICKS_WORKSPACES_SEED_FILE``) is a mapping of alias to entry. Each
entry is checked here first, so one bad entry is logged and left out without
stopping the others; the store then inserts only the aliases it has never had.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

import yaml

from v1.core.workspace_registry.checks import validated
from v1.core.workspace_registry.models import InvalidWorkspace
from v1.core.workspace_registry.store import WorkspaceStore

logger = logging.getLogger(__name__)


def load_seed_file(path: str | os.PathLike) -> list[tuple[str, dict[str, Any]]]:
    """The file's valid entries, in file order; ``[]`` (and one ERROR) if it can't be read."""

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        logger.error("databricks seed file %s not loaded: %s", path, exc)
        return []
    if not isinstance(data, Mapping):
        logger.error("databricks seed file %s not loaded: top level is not a mapping", path)
        return []
    entries: list[tuple[str, dict[str, Any]]] = []
    for alias, entry in data.items():
        try:
            validated(alias, entry)  # as the store will check it
        except InvalidWorkspace as exc:
            logger.error(
                "databricks seed entry %s skipped: %s is invalid or missing", alias, exc.field
            )
            continue
        entries.append((alias, dict(entry)))
    return entries


async def seed_from_file(store: WorkspaceStore, path: str | None) -> list[str]:
    """Insert the file's aliases the registry has never had; returns them."""

    if path is None:
        return []
    inserted = await store.seed(load_seed_file(path))
    logger.info("databricks seed inserted %s", inserted)
    return inserted


__all__ = ["load_seed_file", "seed_from_file"]
