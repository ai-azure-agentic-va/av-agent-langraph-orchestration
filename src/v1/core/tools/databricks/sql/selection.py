"""The tables the user picked for this chat, read from the run's config.

The web client sends them as ``configurable["databricks_tables"]``, with the alias of the
workspace they were picked under as ``configurable["databricks_tables_workspace"]``. A
selection applies only to that workspace: when the run resolved another one (workspaces in a
region often share a metastore, so the allowlist alone can't tell), or the caller's default
stood in for a missing chat workspace, it is ignored as if no tables were selected. The tables
workspace is read by the same rule as the chat's (``canonical_alias``): missing (an old UI) or
``default`` means the legacy workspace, and a blank or malformed one never matches.

A selection only ever narrows the workspace's `DataScope`: an empty one means the workspace's
scope, and an invalid one is rejected (never widened). The client is not trusted, so every
rule is checked here.
"""

import logging

from langgraph.config import get_config

from v1.core.databricks_workspaces import Workspace, WorkspaceUnavailable, canonical_alias
from v1.core.tools.databricks.sql.scope import OUTSIDE_SCOPE, DataScope, format_name, table_parts

# ``get_config`` is looked up on this module at call time so tests can replace it.

logger = logging.getLogger(__name__)

SELECTION_KEY = "databricks_tables"
TABLES_WORKSPACE_KEY = "databricks_tables_workspace"
MAX_SELECTED = 20
# Unity Catalog names are at most 255 characters per part; this leaves room for quoting.
MAX_NAME_CHARS = 800

SELECTION_REJECTED = (
    "The tables selected for this chat can't be used: {}. "
    "Change the selection in the Tables picker."
)


class SelectionInvalid(ValueError):
    """The run's table selection breaks a rule. ``str()`` is safe to show the user."""


def _configurable() -> dict:
    """The run's ``configurable``; empty outside a run."""
    try:
        config = get_config()
    except RuntimeError:  # not inside a graph run
        return {}
    return (config or {}).get("configurable") or {}


def _ignored_reason(configurable: dict, workspace: Workspace, legacy_fallback: bool) -> str | None:
    """Why the run's selection doesn't apply to ``workspace``, or None when it does."""
    if legacy_fallback:
        return "legacy_fallback"
    try:
        picked_in = canonical_alias(configurable.get(TABLES_WORKSPACE_KEY))
    except WorkspaceUnavailable:  # blank or malformed: never matches
        return "other_workspace"
    if picked_in != workspace.alias:  # None: no legacy workspace to belong to
        return "other_workspace"
    return None


def run_selection(workspace: Workspace, legacy_fallback: bool) -> list[str]:
    """The raw selected names for ``workspace``; ``[]`` outside a run, when none were sent,
    or when the selection belongs to another workspace (see the module docstring)."""
    configurable = _configurable()
    selected = configurable.get(SELECTION_KEY)
    if selected is None:
        return []
    if selected:
        reason = _ignored_reason(configurable, workspace, legacy_fallback)
        if reason is not None:
            # Ignored whatever it holds: none of it is used.
            logger.info("databricks_selection_ignored reason=%s", reason)
            return []
    if not isinstance(selected, list) or not all(isinstance(name, str) for name in selected):
        raise SelectionInvalid("the selection must be a list of table names")
    return selected


def selection_dropped(workspace: Workspace, legacy_fallback: bool) -> bool:
    """Whether the run sent a non-empty selection that :func:`run_selection` ignores for
    ``workspace`` (so the agents can be told no tables are selected). Logs nothing."""
    configurable = _configurable()
    if not configurable.get(SELECTION_KEY):
        return False
    return _ignored_reason(configurable, workspace, legacy_fallback) is not None


def narrowed_scope(selected: list[str], configured: DataScope) -> DataScope:
    """The selection as a one-off allowlist inside ``configured``, deduped in order."""
    if not selected:
        return configured
    if len(selected) > MAX_SELECTED:
        raise SelectionInvalid(f"select at most {MAX_SELECTED} tables")
    entries: list[tuple[str, ...]] = []
    for name in selected:
        if len(name) > MAX_NAME_CHARS or not name.isprintable():
            # Not echoed: the name itself is what's unsafe to show.
            raise SelectionInvalid(
                f"table names must be printable and at most {MAX_NAME_CHARS} characters"
            )
        parts = table_parts(name)
        if parts is None:
            raise SelectionInvalid(f"{name} is not a full catalog.schema.table name")
        if not configured.covers(*parts):
            raise SelectionInvalid(OUTSIDE_SCOPE.format(format_name(*parts)))
        if parts not in entries:
            entries.append(parts)
    return DataScope(entries=tuple(entries))


def run_scope(workspace: Workspace, legacy_fallback: bool) -> tuple[list[str], DataScope]:
    """This run's raw selection (see :func:`run_selection`) and its scope: the selection
    inside the workspace's allowlist, or the workspace's scope itself when there is none.

    Reads the selection once. The SQL tools and the selection note both use this, so they
    can't disagree on the rule. Raises :class:`SelectionInvalid`.
    """
    selected = run_selection(workspace, legacy_fallback)
    return selected, narrowed_scope(selected, workspace.scope)
