"""Group-scoped routing helpers.

Map an authenticated caller's Entra groups to a per-group value — a search
index, a starter-prompt set, etc. — via a ``{group-id-or-name: value}`` mapping.

The caller's groups come from the principal that auth stamps as
``langgraph_auth_user`` (object-ids *and* display names; see
``v1.utils.auth`` / ``v1.utils.graph_groups``). Two call sites read it
differently, so this module exposes both extractors plus the shared first-match
resolver:

- :func:`groups_from_config` — inside a LangGraph run (tool/middleware context),
  where the principal lives on the run config.
- :func:`groups_from_request` — in a plain HTTP route, where it lives on
  Starlette's ``request.user``.

Both are best-effort: they return ``()`` rather than raise when there is no
authenticated principal, so callers degrade to their default value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, TypeVar

T = TypeVar("T")


def _coerce_groups(groups: Any) -> tuple[str, ...]:
    """Normalize a raw groups value into a tuple of non-empty strings."""

    if isinstance(groups, Sequence) and not isinstance(groups, str | bytes):
        return tuple(str(group) for group in groups if str(group).strip())
    return ()


def groups_from_config() -> tuple[str, ...]:
    """Caller groups from the LangGraph run config (tool/middleware context).

    Returns ``()`` outside a run context or when no user/groups are present.
    """

    try:
        from langgraph.config import get_config

        config = get_config()
    except Exception:  # not inside a graph run (e.g. a unit call)
        return ()
    configurable = (config or {}).get("configurable") or {}
    user = configurable.get("langgraph_auth_user")
    if isinstance(user, Mapping):
        groups = user.get("groups")
    else:
        groups = getattr(user, "groups", None)
    return _coerce_groups(groups)


def groups_from_request(request: Any) -> tuple[str, ...]:
    """Caller groups from an HTTP request (``request.user`` proxy).

    Returns ``()`` when there is no authenticated user (e.g. auth disabled).
    """

    try:
        groups = getattr(request.user, "groups", None)
    except Exception:  # no auth backend / unauthenticated connection
        return ()
    return _coerce_groups(groups)


def resolve_for_groups(mapping: Mapping[str, T], groups: Sequence[str], default: T) -> T:
    """Return the mapped value for the caller's groups, else ``default``.

    Walks ``mapping`` in declared order and returns the value for the first key
    (group object-id *or* display name) the caller belongs to — so admins
    control precedence via JSON ordering. Skips falsy values (empty index name /
    empty prompt list). Returns ``default`` when nothing matches or the caller
    has no groups.
    """

    if mapping and groups:
        member = set(groups)
        for key, value in mapping.items():
            if key in member and value:
                return value
    return default


__all__ = ["groups_from_config", "groups_from_request", "resolve_for_groups"]
