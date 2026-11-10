"""Which of the caller's workspaces ``explain_run`` searches for a run. Pure: no I/O.

Only visible workspaces with job lookup on are ever chosen, one per hostname (the chat's
own workspace wins a shared host, else the first in ``visible`` order). A link goes to the
workspace on its host; a named workspace is used alone; a bare run ID tries the chat's
workspace first, then the rest. Refusals never name a workspace the caller can't see.
Malformed run refs aren't judged here: ``explain_run`` rejects them itself.
"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import chain
from typing import NamedTuple

from v1.core.databricks_workspaces import Workspace
from v1.core.tools.databricks.jobs.refs import hostname, link_host, same_host

JOBS_DISABLED_MESSAGE = "Job lookup isn't enabled for {label}."
HIDDEN_LINK_MESSAGE = "That run link is from a Databricks workspace you can't use here."
HIDDEN_NAMED_MESSAGE = "That isn't a Databricks workspace you can use here."
LINK_NOT_IN_NAMED_MESSAGE = "That run link isn't from {label}."


class Targets(NamedTuple):
    first: Workspace | None  # query this one first (None: start with the fan-out)
    others: tuple[Workspace, ...]  # fan-out set; empty for links and named workspaces
    refusal: str | None  # user-safe message; when set, query nothing
    # The visible workspace a refusal names (None for one that names none, or no refusal).
    refused_in: Workspace | None = None


def _refused(message: str, refused_in: Workspace | None = None) -> Targets:
    return Targets(None, (), message, refused_in)


def _on_host(host: str | None, workspaces: Sequence[Workspace]) -> Workspace | None:
    """The first of ``workspaces`` on this hostname, or None."""
    if not host:
        return None
    return next((ws for ws in workspaces if same_host(host, ws.host)), None)


def _candidates(current: Workspace, visible: Sequence[Workspace]) -> list[Workspace]:
    """Visible jobs-on workspaces, one per hostname, in ``visible`` order."""
    current_host = hostname(current.host) if current.jobs_enabled else None
    by_host: dict[str | None, Workspace] = {}
    for ws in visible:
        if not ws.jobs_enabled:
            continue
        host = hostname(ws.host)
        if host not in by_host:
            by_host[host] = ws
        if host == current_host and ws == current:
            by_host[host] = current
    return list(by_host.values())


def _named(name: str, visible: Sequence[Workspace]) -> Workspace | None:
    """The visible workspace whose alias is ``name`` (any case), else the first whose label
    is, or None. An alias wins: it is the unambiguous name."""
    wanted = name.strip().casefold()
    by_alias = (ws for ws in visible if ws.alias.casefold() == wanted)
    by_label = (ws for ws in visible if ws.label.casefold() == wanted)
    return next(chain(by_alias, by_label), None)


def run_targets(
    run: str, named: str | None, current: Workspace, visible: Sequence[Workspace]
) -> Targets:
    """Where to look for ``run``: the workspace to try first, the rest, or a refusal.
    A blank ``named`` (empty or whitespace) is no name."""
    candidates = _candidates(current, visible)
    link = link_host(run)

    if named is not None and named.strip():
        chosen = _named(named, visible)
        if chosen is None:
            return _refused(HIDDEN_NAMED_MESSAGE)
        if not chosen.jobs_enabled:
            return _refused(JOBS_DISABLED_MESSAGE.format(label=chosen.label), chosen)
        if link is not None and not same_host(link, chosen.host):
            return _refused(LINK_NOT_IN_NAMED_MESSAGE.format(label=chosen.label), chosen)
        return Targets(_on_host(hostname(chosen.host), candidates), (), None)

    if link is not None:
        target = _on_host(link, candidates)
        if target is not None:
            return Targets(target, (), None)
        jobs_off = _on_host(link, visible)
        if jobs_off is not None:
            return _refused(JOBS_DISABLED_MESSAGE.format(label=jobs_off.label), jobs_off)
        return _refused(HIDDEN_LINK_MESSAGE)

    if not candidates:
        return _refused(JOBS_DISABLED_MESSAGE.format(label=current.label), current)
    # Only the chat's own workspace goes first; with its jobs off, every host is fan-out.
    first = _on_host(hostname(current.host), candidates) if current.jobs_enabled else None
    return Targets(first, tuple(ws for ws in candidates if ws is not first), None)
