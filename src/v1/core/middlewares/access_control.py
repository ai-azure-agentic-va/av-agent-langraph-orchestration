"""Group access control: one policy file, applied by one middleware.

``ACCESS_CONTROL_CONFIG_PATH`` points at a JSON file (schema version 1, see
``config/access-control.example.json``). It holds group policy only:
credentials, endpoints and resource coordinates stay in the environment and Key
Vault::

    {
      "version": 1,
      "global": {"blocked_tools": ["ls", "grep"], "blocked_subagents": []},
      "groups": {
        "<entra-group-object-id>": {
          "display_name": "TEAM-A",
          "priority": 200,
          "search_index": "team-a-index",
          "starter_prompts": [{"label": "...", "message": "..."}],
          "blocked_tools": [],
          "blocked_subagents": ["adf-agent"]
        }
      }
    }

Groups are keyed by Entra object id, which is what the token and Microsoft Graph
return. ``display_name`` is only a label: the user-context block shows it, and
the auth-off dev mode accepts it in ``x-dev-groups``. Who may sign in is the
frontend's decision, not this file's.

A caller in several groups is resolved by fixed rules, never by JSON order:

- blocked tools and subagents: ``global``, which applies to every caller,
  plus every matched group's lists (deny wins);
- search index and starter prompts: the highest-priority matched group that
  sets them. Groups that set different values need different priorities or the
  file is rejected, so no caller gets an arbitrary group's value. A caller in no
  such group searches ``AZURE_AI_SEARCH_DEFAULT_INDEX`` and gets no prompts.

:class:`AccessControlMiddleware` is the one place restrictions are enforced, for
tools and subagents alike. Three readers outside the agent's tool calls read the
file as well: auth keeps only the groups it configures
(:meth:`AccessPolicy.export_groups`), and ``/starter-prompts`` and
``ai_search_tool`` ask :func:`caller_access` for a caller's prompts and index. A
missing or invalid file, or a blocked name the backend does not have
(:func:`check_blocked_names`), fails startup: running without the file would
lift every restriction in it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from deepagents.middleware.subagents import (
    _describe_subagent_for_tool,
    _is_forked_compiled_subagent,
    _is_forked_subagent,
)
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import ToolMessage
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from v1.core.config import get_settings
from v1.utils.group_routing import groups_from_config

logger = logging.getLogger(__name__)

# deepagents exposes all subagents through a single tool named "task".
TASK_TOOL_NAME = "task"

_OBJECT_ID_RE = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z", re.IGNORECASE
)

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def is_object_id(value: str) -> bool:
    """Whether ``value`` has the shape of an Entra object id (a GUID)."""

    return bool(_OBJECT_ID_RE.match(value.strip()))


class AccessControlConfigError(RuntimeError):
    """The access-control file cannot be read or is invalid."""


# -- the file ------------------------------------------------------------------


class StarterPrompt(BaseModel):
    """One starter prompt: the button ``label`` and the ``message`` it sends."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: Name
    message: Name


class Blocked(BaseModel):
    """Tools, and subagents (``task`` targets), a caller may not use."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    blocked_tools: tuple[Name, ...] = ()
    blocked_subagents: tuple[Name, ...] = ()


class GroupPolicy(Blocked):
    """What members of one Entra group get, and what they may not use."""

    display_name: Name | None = None
    # Decides search_index and starter_prompts between matched groups; higher wins.
    priority: int = 0
    # None: this group does not decide the index.
    search_index: Name | None = None
    # None: this group does not decide the prompts. An empty list decides on none.
    starter_prompts: tuple[StarterPrompt, ...] | None = None


@dataclass(frozen=True)
class CallerAccess:
    """What one caller gets, resolved from their groups."""

    # Configured groups the caller is in, highest priority first.
    groups: tuple[str, ...]
    blocked_tools: frozenset[str]
    blocked_subagents: frozenset[str]
    # None: the default index applies.
    search_index: str | None
    starter_prompts: tuple[StarterPrompt, ...]


class AccessPolicy(BaseModel):
    """The access-control file."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    version: Literal[1]
    global_policy: Blocked = Field(default_factory=Blocked, alias="global")
    groups: dict[str, GroupPolicy] = Field(default_factory=dict)

    @field_validator("groups", mode="before")
    @classmethod
    def _keys_are_object_ids(cls, groups: Any) -> Any:
        if not isinstance(groups, Mapping):
            return groups  # the type check reports it
        keyed: dict[str, Any] = {}
        for key, group in groups.items():
            if not is_object_id(str(key)):
                raise ValueError(f"group key {key!r} is not an Entra group object id")
            canonical = str(key).strip().lower()
            if canonical in keyed:
                raise ValueError(f"group {canonical} is listed more than once")
            keyed[canonical] = group
        return keyed

    @model_validator(mode="after")
    def _unambiguous(self) -> AccessPolicy:
        names: dict[str, str] = {}
        for key, group in self.groups.items():
            if group.display_name is None:
                continue
            name = group.display_name
            if is_object_id(name):
                raise ValueError(f"groups.{key}.display_name {name!r} is an object id")
            if name.casefold() in names:
                raise ValueError(
                    f"display_name {name!r} is used by groups {names[name.casefold()]} and {key}"
                )
            names[name.casefold()] = key
        for field in ("search_index", "starter_prompts"):
            decided: dict[int, tuple[str, Any]] = {}
            for key, group in self.groups.items():
                value = getattr(group, field)
                if value is None:
                    continue
                other, other_value = decided.setdefault(group.priority, (key, value))
                if other_value != value:
                    raise ValueError(
                        f"groups {other} and {key} set different {field} at priority "
                        f"{group.priority}: give one of them a higher priority"
                    )
        return self

    def resolve(self, groups: Iterable[str]) -> CallerAccess:
        """What a caller in ``groups`` gets. Entries that are not configured ids are ignored."""

        matched = sorted(
            {key for group in groups if (key := group.strip().lower()) in self.groups},
            key=lambda key: (-self.groups[key].priority, key),
        )
        blocked_tools = set(self.global_policy.blocked_tools)
        blocked_subagents = set(self.global_policy.blocked_subagents)
        for key in matched:
            blocked_tools.update(self.groups[key].blocked_tools)
            blocked_subagents.update(self.groups[key].blocked_subagents)

        def deciding(field: str) -> Any:
            values = (getattr(self.groups[key], field) for key in matched)
            return next((value for value in values if value is not None), None)

        return CallerAccess(
            groups=tuple(matched),
            blocked_tools=frozenset(blocked_tools),
            blocked_subagents=frozenset(blocked_subagents),
            search_index=deciding("search_index"),
            starter_prompts=deciding("starter_prompts") or (),
        )

    def export_groups(
        self, group_ids: Iterable[str], names: Mapping[str, str | None] | None = None
    ) -> tuple[str, ...]:
        """The groups auth keeps on the principal: configured ids, each with a name.

        The principal is persisted with every run and returned by the platform's
        read APIs, so the caller's other directory groups must not ride along.
        The name, which the user-context block shows, is Graph's (``names``, by
        id) or else the file's ``display_name``. A GUID-shaped name is dropped:
        downstream it would read as the id of whichever group it imitates.
        """

        names = {key.strip().lower(): name for key, name in (names or {}).items()}
        keep: set[str] = set()
        for group_id in group_ids:
            key = group_id.strip().lower()
            if key not in self.groups:
                continue
            keep.add(key)
            name = (names.get(key) or "").strip()
            if not name or is_object_id(name):
                name = self.groups[key].display_name
            if name:
                keep.add(name)
        return tuple(sorted(keep))

    def dev_groups(self, values: Iterable[str]) -> tuple[str, ...]:
        """:meth:`export_groups` for the auth-off dev mode, which also takes a display_name."""

        by_name = {
            group.display_name.casefold(): key
            for key, group in self.groups.items()
            if group.display_name
        }
        return self.export_groups(by_name.get(value.strip().casefold(), value) for value in values)


def load_access_policy(path: str | None = None) -> AccessPolicy:
    """Read the file at ``path``, by default ``ACCESS_CONTROL_CONFIG_PATH``.

    Raises :class:`AccessControlConfigError` when the path is unset or the file
    cannot be read or is invalid.
    """

    if path is None:
        path = get_settings().access_control_config_path or ""
    path = path.strip()
    if not path:
        raise AccessControlConfigError(
            "ACCESS_CONTROL_CONFIG_PATH is not set. Point it at the access-control file "
            "(config/access-control.example.json shows the format)."
        )
    try:
        policy = AccessPolicy.model_validate_json(Path(path).expanduser().read_bytes())
    except (OSError, ValueError) as exc:  # pydantic's ValidationError is a ValueError
        raise AccessControlConfigError(
            f"Cannot load the access-control file ACCESS_CONTROL_CONFIG_PATH={path}: {exc}"
        ) from exc
    logger.info(
        "Access-control policy from %s: %d group(s); blocked for everyone: tools=%s subagents=%s",
        path,
        len(policy.groups),
        list(policy.global_policy.blocked_tools),
        list(policy.global_policy.blocked_subagents),
    )
    return policy


@lru_cache
def get_access_policy() -> AccessPolicy:
    """The process-wide policy, loaded once (a failed load is retried, not cached)."""

    return load_access_policy()


def caller_access(groups: Iterable[str] | None = None) -> CallerAccess:
    """What the policy gives the caller in ``groups``, by default this run's caller.

    Outside a run, or with no authenticated groups, only the global lists apply.
    """

    return get_access_policy().resolve(groups_from_config() if groups is None else groups)


def check_blocked_names(
    policy: AccessPolicy, *, tools: Iterable[str], subagents: Iterable[str]
) -> None:
    """Fail when ``policy`` blocks a tool or subagent this backend does not have.

    A typo would block nothing and leave the capability open. ``tools`` and
    ``subagents`` are every name the backend can expose, whether or not this
    deployment turns it on, so one file serves every deployment.
    """

    known = {"blocked_tools": frozenset(tools), "blocked_subagents": frozenset(subagents)}
    sections = [("global", policy.global_policy)]
    sections += [(f"groups.{key}", group) for key, group in policy.groups.items()]
    unknown = [
        f"{place}.{field}: {name!r}"
        for place, section in sections
        for field, names in known.items()
        for name in getattr(section, field)
        if name not in names
    ]
    if unknown:
        raise AccessControlConfigError(
            f"The access-control file blocks names this backend does not have "
            f"({'; '.join(unknown)}). Tools: {', '.join(sorted(known['blocked_tools']))}. "
            f"Subagents: {', '.join(sorted(known['blocked_subagents']))}."
        )


# -- the middleware ------------------------------------------------------------


def _tool_name(tool: Any) -> str | None:
    if isinstance(tool, Mapping):
        function = tool.get("function")
        if isinstance(function, Mapping):
            return function.get("name")
        return tool.get("name")
    return getattr(tool, "name", None)


def _without_line(text: str, line: str) -> str | None:
    """``text`` with ``line`` removed where it stands as whole lines; None if absent."""

    if f"\n{line}\n" in text:
        return text.replace(f"\n{line}\n", "\n", 1)
    if text.endswith(f"\n{line}"):
        return text[: -(len(line) + 1)]
    if text.startswith(f"{line}\n"):
        return text[len(line) + 1 :]
    return None


def _is_forked(spec: Mapping[str, Any]) -> bool:
    return _is_forked_subagent(spec) or _is_forked_compiled_subagent(spec)  # type: ignore[arg-type]


def _error(tool_call: Mapping[str, Any], content: str) -> ToolMessage:
    return ToolMessage(
        content=content,
        tool_call_id=tool_call.get("id") or "",
        name=tool_call.get("name"),
        status="error",
    )


class AccessControlMiddleware(AgentMiddleware):
    """Hide, and refuse, every tool and subagent the policy blocks for this caller.

    The compiled agent is one graph for every caller, so this runs on each model
    call and each tool call, from the caller's groups on the run config:

    1. What the model sees. A blocked tool is removed from the tools it is
       offered. Subagents are all reached through deepagents' one ``task``
       tool, which lists them one line each: a blocked subagent's line is cut
       from it, and ``task`` goes only when no subagent is left (or when
       ``task`` is itself a blocked tool).
    2. What runs. A call to a blocked tool, or a ``task`` call to a blocked
       subagent, gets the error a tool or subagent that does not exist gets.
       For a restricted caller an unknown name is answered here too, because
       the stock errors list every registered name, blocked ones included.

    Nothing is said to the model or the user about what is missing, and every
    name comes from the file, so blocking another tool or subagent, including
    one added later, is a configuration change. deepagents' ``ls`` and ``grep``
    are tools of their own, not shell commands (its one shell tool,
    ``execute``, exists only with a sandbox backend, which this agent does not
    use), so they are blocked like any other tool. ``read_file`` also opens the
    skills and the history summarization offloads: blocking it takes those away
    too.

    ``v1.core.agent`` adds an instance to every stack deepagents builds (the
    orchestrator and each declarative subagent) through the harness profile,
    inner of that stack's other middleware, so it is the last to change the
    model's tools and the last wrapper before a tool runs. A compiled subagent
    builds its own stack and has to add it there.

    ``task_subagents`` are the subagents behind the ``task`` tool, in the order
    deepagents lists them; deepagents' own helper rebuilds each one's line, so a
    blocked one's line can be cut out exactly. A stack without ``task`` ignores
    them.
    """

    def __init__(self, *, task_subagents: Iterable[Mapping[str, Any]] = ()) -> None:
        super().__init__()
        self._listing = {
            spec["name"]: _describe_subagent_for_tool(
                spec["name"], spec["description"], forked=_is_forked(spec)
            )
            for spec in task_subagents
        }

    def _allowed_subagents(self, blocked: frozenset[str]) -> list[str]:
        return [name for name in self._listing if name not in blocked]

    def _task_tool(self, tool: Any, blocked: frozenset[str]) -> Any | None:
        """``task`` without the blocked subagents' lines; None to hide it."""

        hidden = [name for name in self._listing if name in blocked]
        if not self._allowed_subagents(blocked):
            if not self._listing:
                logger.error(
                    "The task tool is offered without its subagent list, so it is "
                    "hidden from a caller with blocked subagents."
                )
            return None
        if not hidden:
            return tool
        description = getattr(tool, "description", None)
        for name in hidden:
            trimmed = None
            if isinstance(description, str):
                trimmed = _without_line(description, self._listing[name])
            if trimmed is None:
                logger.error(
                    "The task tool's description does not list subagent %s the way "
                    "deepagents renders it, so task is hidden from a caller blocked "
                    "from it rather than shown with it.",
                    name,
                )
                return None
            description = trimmed
        return tool.model_copy(update={"description": description})

    def _restricted(self, request: ModelRequest) -> ModelRequest:
        access = caller_access()
        if not (access.blocked_tools or access.blocked_subagents):
            return request
        tools = []
        for tool in request.tools:
            name = _tool_name(tool)
            if name in access.blocked_tools:
                continue
            if name == TASK_TOOL_NAME and access.blocked_subagents:
                tool = self._task_tool(tool, access.blocked_subagents)
                if tool is None:
                    continue
            tools.append(tool)
        return request.override(tools=tools)

    def _refusal(self, request: Any) -> ToolMessage | None:
        """The error for a call this caller may not make, or None to let it run."""

        access = caller_access()
        if not (access.blocked_tools or access.blocked_subagents):
            return None
        call = request.tool_call
        name = call.get("name")
        if name in access.blocked_tools:
            logger.warning(
                "Refused a call to %s, which the access-control policy blocks for this caller.",
                name,
            )
            return _error(call, f"Error: {name} is not a valid tool.")
        if request.tool is None:
            # Unknown to the agent. The stock error would list every registered tool.
            return _error(call, f"Error: {name} is not a valid tool.")
        if name != TASK_TOOL_NAME or not access.blocked_subagents:
            return None
        subagent_type = (call.get("args") or {}).get("subagent_type")
        allowed = self._allowed_subagents(access.blocked_subagents)
        if subagent_type in allowed:
            return None
        if subagent_type in access.blocked_subagents:
            logger.warning(
                "Refused task delegation to %s, which the access-control policy blocks "
                "for this caller.",
                subagent_type,
            )
        if not allowed:
            return _error(call, f"Error: {name} is not a valid tool.")
        # deepagents' wording for a subagent that does not exist, listing only these.
        allowed_types = ", ".join(f"`{subagent}`" for subagent in allowed)
        return _error(
            call,
            f"We cannot invoke subagent {subagent_type} because it does not exist, the "
            f"only allowed types are {allowed_types}",
        )

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        return handler(self._restricted(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return await handler(self._restricted(request))

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        refusal = self._refusal(request)
        return refusal if refusal is not None else handler(request)

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        refusal = self._refusal(request)
        return refusal if refusal is not None else await handler(request)


__all__ = [
    "TASK_TOOL_NAME",
    "AccessControlConfigError",
    "AccessControlMiddleware",
    "AccessPolicy",
    "CallerAccess",
    "GroupPolicy",
    "StarterPrompt",
    "caller_access",
    "check_blocked_names",
    "get_access_policy",
    "load_access_policy",
]
