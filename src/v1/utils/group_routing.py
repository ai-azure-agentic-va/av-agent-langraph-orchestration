"""Where a caller's Entra groups are read from.

The caller's groups come from the principal that auth stamps as
``langgraph_auth_user``. Auth exports only the groups the access-control policy
configures (see ``v1.utils.auth._mapped_groups``), so the full AD inventory
never reaches run configs or the UI. What those groups mean (search index,
starter prompts, blocked tools and subagents) is decided by
``v1.core.middlewares.access_control``. Two call sites read the groups
differently, so this module exposes both extractors:

- :func:`groups_from_config` — inside a LangGraph run (tool/middleware context),
  where the principal lives on the run config.
- :func:`groups_from_request` — in a plain HTTP route, where it lives on
  Starlette's ``request.user``.

Both are best-effort: they return ``()`` rather than raise when there is no
authenticated principal, so callers degrade to their default value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


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


__all__ = ["groups_from_config", "groups_from_request"]
