"""Tests for group access control (``v1.core.middlewares.access_control``).

- The file: schema version 1, no unknown keys, groups keyed by Entra object id
  (any case), display names unique and not GUID-shaped, and groups on the same
  priority must agree on the search index and the starter prompts. An unset
  path, or a missing or invalid file, fails the load.
- A caller's groups: every matched group's blocked lists are added to the
  global ones (deny wins); the search index and the starter prompts come from
  the highest-priority group that sets them.
- The groups auth keeps: configured ids only, each with a name; a group named
  after another group, or after its id, is never taken for it.
- The readers outside the agent: ``ai_search_tool`` searches the caller's index
  and ``/starter-prompts`` returns the caller's prompts.
- AccessControlMiddleware, driven with stand-in requests: a blocked tool is not
  offered, and a blocked subagent's line is cut from the ``task`` description,
  which goes only when no subagent is left (or when it cannot cut the line). A
  call to a blocked tool or subagent is refused in the words used for one that
  does not exist, and so is an unknown name for a restricted caller.
- ``check_blocked_names`` rejects a blocked name the backend does not have.

The end-to-end tests build the REAL agent (``_build_agent_sync``) around the
stand-in model from ``test_patch_tool_calls`` and check the policy holds in the
orchestrator and in a subagent, and that the agent has no general-purpose
subagent.

Runs standalone (``python test_access_control.py``) or under pytest.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_args

from deepagents.middleware.filesystem import FsToolName
from deepagents.middleware.subagents import (
    GENERAL_PURPOSE_SUBAGENT,
    TASK_TOOL_DESCRIPTION,
    _describe_subagent_for_tool,
    _is_forked_compiled_subagent,
    _is_forked_subagent,
)
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool, tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field, ValidationError
from test_patch_tool_calls import _AzureStandIn, _user

import v1.api.routes.starter_prompts as starter_prompts_module
import v1.core.agent as agent_mod
import v1.core.middlewares.access_control as ac
import v1.core.tools.ai_search.ai_search as ais
from v1.core.config import get_settings
from v1.core.middlewares.access_control import (
    TASK_TOOL_NAME,
    AccessControlConfigError,
    AccessControlMiddleware,
    AccessPolicy,
    check_blocked_names,
    get_access_policy,
    load_access_policy,
)
from v1.core.middlewares.safety import SAFETY_CLASSIFIER_SYSTEM_PROMPT
from v1.core.subagents import ADF_SUBAGENT, ADLS_SUBAGENT, SERVICENOW_SUBAGENT, STTM_SUBAGENT

_REPO_ROOT = Path(__file__).resolve().parents[5]

# Hex letters in every id, so the case-insensitivity checks mean something.
_TEAM_A = "a1b2c3d4-1111-4111-8111-11111111111a"
_TEAM_B = "b2c3d4e5-2222-4222-8222-22222222222b"
_TEAM_C = "c3d4e5f6-3333-4333-8333-33333333333c"
_STRANGER = "f9e8d7c6-9999-4999-8999-99999999999f"

_PROMPT_A = {"label": "A", "message": "Prompt for team A"}
_PROMPT_B = {"label": "B", "message": "Prompt for team B"}

_GP = GENERAL_PURPOSE_SUBAGENT["name"]
_SERVICENOW = SERVICENOW_SUBAGENT["name"]
_ADF = ADF_SUBAGENT["name"]
_ADLS = ADLS_SUBAGENT["name"]
_STTM = STTM_SUBAGENT["name"]


# -- helpers ---------------------------------------------------------------------


def _policy(groups: dict | None = None, global_policy: dict | None = None) -> AccessPolicy:
    data: dict = {"version": 1, "groups": groups or {}}
    if global_policy is not None:
        data["global"] = global_policy
    return AccessPolicy.model_validate(data)


def _invalid(data: dict, substring: str) -> None:
    try:
        AccessPolicy.model_validate(data)
    except ValidationError as exc:
        assert substring in str(exc), f"expected {substring!r} in {exc}"
        return
    raise AssertionError(f"expected a validation error containing {substring!r}")


def _config_error(fn: Callable[[], Any]) -> str:
    try:
        fn()
    except AccessControlConfigError as exc:
        return str(exc)
    raise AssertionError("expected AccessControlConfigError")


def _exported(*groups: str) -> tuple[str, ...]:
    """An export as auth produces it (sorted)."""

    return tuple(sorted(groups))


@contextmanager
def _config_file(content: str | dict) -> Iterator[str]:
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "access-control.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content if isinstance(content, str) else json.dumps(content))
        yield path


@contextmanager
def _configured_path(value: str) -> Iterator[None]:
    """Run with ACCESS_CONTROL_CONFIG_PATH set to ``value`` and nothing cached."""

    saved = os.environ.get("ACCESS_CONTROL_CONFIG_PATH")
    os.environ["ACCESS_CONTROL_CONFIG_PATH"] = value
    get_settings.cache_clear()
    get_access_policy.cache_clear()
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("ACCESS_CONTROL_CONFIG_PATH", None)
        else:
            os.environ["ACCESS_CONTROL_CONFIG_PATH"] = saved
        get_settings.cache_clear()
        get_access_policy.cache_clear()


@contextmanager
def _caller(policy: AccessPolicy, *groups: str) -> Iterator[None]:
    """Run with ``policy`` in force for a caller in ``groups``."""

    saved = (ac.get_access_policy, ac.groups_from_config)
    ac.get_access_policy = lambda: policy
    ac.groups_from_config = lambda: groups
    try:
        yield
    finally:
        ac.get_access_policy, ac.groups_from_config = saved


@contextmanager
def _logged(level: int = logging.WARNING) -> Iterator[list[str]]:
    messages: list[str] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    sink = _Sink(level=level)
    ac.logger.addHandler(sink)
    try:
        yield messages
    finally:
        ac.logger.removeHandler(sink)


# -- the file --------------------------------------------------------------------


def test_the_file_needs_schema_version_1() -> None:
    _invalid({"groups": {}}, "version")
    _invalid({"version": 2, "groups": {}}, "version")
    assert AccessPolicy.model_validate({"version": 1}).groups == {}


def test_the_file_rejects_unknown_keys() -> None:
    # A misspelt restriction must not be silently ignored.
    _invalid({"version": 1, "group": {}}, "Extra inputs are not permitted")
    _invalid({"version": 1, "groups": {_TEAM_A: {"blocked_tool": ["x"]}}}, "blocked_tool")
    # Login is the frontend's decision; the backend file has no such field.
    _invalid({"version": 1, "groups": {_TEAM_A: {"login_allowed": True}}}, "login_allowed")
    _invalid({"version": 1, "global": {"search_index": "x"}}, "search_index")


def test_the_file_rejects_empty_names() -> None:
    _invalid({"version": 1, "global": {"blocked_tools": [" "]}}, "blocked_tools")
    _invalid(
        {"version": 1, "groups": {_TEAM_A: {"starter_prompts": [{"label": "x", "message": ""}]}}},
        "message",
    )


def test_groups_are_keyed_by_object_id_in_any_case() -> None:
    policy = AccessPolicy.model_validate({"version": 1, "groups": {_TEAM_A.upper(): {}}})
    assert list(policy.groups) == [_TEAM_A]
    _invalid({"version": 1, "groups": {"TEAM-A": {}}}, "is not an Entra group object id")
    _invalid({"version": 1, "groups": {" ": {}}}, "is not an Entra group object id")
    _invalid({"version": 1, "groups": {_TEAM_A.upper(): {}, _TEAM_A: {}}}, "listed more than once")


def test_a_display_name_names_one_group_and_is_not_an_id() -> None:
    _invalid({"version": 1, "groups": {_TEAM_A: {"display_name": _TEAM_B}}}, "is an object id")
    _invalid(
        {
            "version": 1,
            "groups": {_TEAM_A: {"display_name": "TEAM-A"}, _TEAM_B: {"display_name": "team-a"}},
        },
        f"is used by groups {_TEAM_A} and {_TEAM_B}",
    )


def test_groups_on_one_priority_must_agree_on_the_index_and_the_prompts() -> None:
    """Otherwise a caller in both would get whichever group happened to come first."""

    _invalid(
        {
            "version": 1,
            "groups": {
                _TEAM_A: {"priority": 100, "search_index": "index-a"},
                _TEAM_B: {"priority": 100, "search_index": "index-b"},
            },
        },
        "set different search_index at priority 100",
    )
    _invalid(
        {
            "version": 1,
            "groups": {
                _TEAM_A: {"starter_prompts": [_PROMPT_A]},
                _TEAM_B: {"starter_prompts": []},
            },
        },
        "set different starter_prompts at priority 0",
    )
    # The same value on one priority, or a different value on another, is fine.
    _policy(
        {
            _TEAM_A: {"priority": 100, "search_index": "shared"},
            _TEAM_B: {"priority": 100, "search_index": "shared"},
            _TEAM_C: {"priority": 200, "search_index": "index-c"},
        }
    )


# -- loading ---------------------------------------------------------------------


def test_load_reads_the_file() -> None:
    with _config_file({"version": 1, "groups": {_TEAM_A: {"search_index": "index-a"}}}) as path:
        assert load_access_policy(path).resolve([_TEAM_A]).search_index == "index-a"


def test_load_fails_when_the_path_is_unset() -> None:
    """Running without the file would lift every restriction in it."""

    assert "ACCESS_CONTROL_CONFIG_PATH is not set" in _config_error(lambda: load_access_policy(" "))
    with _configured_path(""):
        assert "ACCESS_CONTROL_CONFIG_PATH is not set" in _config_error(get_access_policy)


def test_load_fails_when_the_file_is_missing_or_invalid() -> None:
    missing = os.path.join(tempfile.gettempdir(), "no-such-dir", "access-control.json")
    assert "Cannot load the access-control file" in _config_error(
        lambda: load_access_policy(missing)
    )
    for content in ("{not json", {"version": 1, "groups": {_TEAM_A: {"blocked_tool": []}}}):
        with _config_file(content) as path:
            message = _config_error(lambda path=path: load_access_policy(path))
        assert "Cannot load the access-control file" in message, message


def test_a_failed_load_is_not_cached() -> None:
    """Fixing a broken file needs no restart: the next caller loads it."""

    with _config_file({"version": 1, "global": {"blocked_tools": ["ls"]}}) as path:
        with _configured_path(path + ".missing"):
            assert "Cannot load the access-control file" in _config_error(get_access_policy)
            os.environ["ACCESS_CONTROL_CONFIG_PATH"] = path
            get_settings.cache_clear()  # the policy's cache is left alone
            assert get_access_policy().global_policy.blocked_tools == ("ls",)


# -- a caller's groups -----------------------------------------------------------


def test_every_matched_groups_blocks_add_to_the_global_ones() -> None:
    """Deny wins: no group can grant back what another group or the global list blocks."""

    policy = _policy(
        {
            _TEAM_A: {"priority": 200, "display_name": "TEAM-A", "blocked_subagents": [_ADF]},
            _TEAM_B: {"priority": 100, "blocked_tools": ["read_url"]},
            _TEAM_C: {"priority": 300},
        },
        {"blocked_tools": ["ls"], "blocked_subagents": [_STTM]},
    )
    access = policy.resolve([_TEAM_A.upper(), _TEAM_B, _TEAM_C, _STRANGER, "TEAM-A"])
    assert access.blocked_tools == {"ls", "read_url"}
    assert access.blocked_subagents == {_STTM, _ADF}
    # Configured ids only (a display name is not one), highest priority first.
    assert access.groups == (_TEAM_C, _TEAM_A, _TEAM_B)
    # A caller in no configured group still gets both global lists.
    assert policy.resolve([]).blocked_tools == {"ls"}
    assert policy.resolve([]).blocked_subagents == {_STTM}


def test_the_search_index_follows_priority_not_file_order() -> None:
    policy = _policy(
        {
            _TEAM_B: {"priority": 100, "search_index": "index-b"},
            _TEAM_A: {"priority": 200, "search_index": "index-a"},
            _TEAM_C: {"priority": 300, "blocked_tools": ["read_url"]},
        }
    )
    assert policy.resolve([_TEAM_B, _TEAM_A]).search_index == "index-a"
    assert policy.resolve([_TEAM_B]).search_index == "index-b"
    # A group that sets no index does not decide, whatever its priority.
    assert policy.resolve([_TEAM_C, _TEAM_B]).search_index == "index-b"
    assert policy.resolve([_STRANGER]).search_index is None


def test_starter_prompts_follow_priority_and_an_empty_list_decides() -> None:
    policy = _policy(
        {
            _TEAM_A: {"priority": 200, "starter_prompts": [_PROMPT_A]},
            _TEAM_B: {"priority": 100, "starter_prompts": [_PROMPT_B]},
            _TEAM_C: {"priority": 300, "starter_prompts": []},
        }
    )
    assert [p.label for p in policy.resolve([_TEAM_A, _TEAM_B]).starter_prompts] == ["A"]
    # A higher-priority group that shows no prompts wins over the rest.
    assert policy.resolve([_TEAM_A, _TEAM_C]).starter_prompts == ()
    assert policy.resolve([_STRANGER]).starter_prompts == ()


# -- the groups auth keeps -------------------------------------------------------


def test_export_keeps_configured_ids_each_with_a_name() -> None:
    policy = _policy({_TEAM_A: {"display_name": "TEAM-A"}, _TEAM_B: {}})
    # The file's name, or Graph's when it has one; ids in lower case.
    assert policy.export_groups([_TEAM_A.upper(), _STRANGER]) == _exported(_TEAM_A, "TEAM-A")
    assert policy.export_groups([_TEAM_A], {_TEAM_A.upper(): "Team A (renamed)"}) == _exported(
        _TEAM_A, "Team A (renamed)"
    )
    assert policy.export_groups([_TEAM_B]) == (_TEAM_B,)


def test_a_group_named_after_a_configured_group_or_its_id_gets_nothing() -> None:
    """Anyone can create a group: only the configured object id matches."""

    policy = _policy({_TEAM_A: {"display_name": "TEAM-A", "search_index": "index-a"}})
    impostors = {_STRANGER: "TEAM-A", _TEAM_C: _TEAM_A, _TEAM_B: _TEAM_A.upper()}
    assert policy.export_groups(list(impostors), impostors) == ()
    assert policy.resolve(["TEAM-A"]).groups == ()


def test_a_guid_shaped_name_is_replaced_by_the_files_name() -> None:
    """A member of team A whose group is named after team B's id must not read as team B."""

    policy = _policy(
        {
            _TEAM_A: {"priority": 100, "display_name": "TEAM-A", "search_index": "index-a"},
            _TEAM_B: {"priority": 200, "display_name": "TEAM-B", "search_index": "index-b"},
        }
    )
    exported = policy.export_groups([_TEAM_A], {_TEAM_A: _TEAM_B})
    assert exported == _exported(_TEAM_A, "TEAM-A")
    assert policy.resolve(exported).search_index == "index-a"


def test_dev_groups_take_object_ids_or_configured_display_names() -> None:
    policy = _policy({_TEAM_A: {"display_name": "TEAM-A"}, _TEAM_B: {"display_name": "TEAM-B"}})
    assert policy.dev_groups(["team-a", "UNKNOWN"]) == _exported(_TEAM_A, "TEAM-A")
    assert policy.dev_groups([_TEAM_B.upper()]) == _exported(_TEAM_B, "TEAM-B")


# -- the readers outside the agent: ai_search_tool and /starter-prompts -----------


def _search_calls(policy: AccessPolicy, *caller_groups: str) -> list[dict]:
    """The Azure Search calls ai_search_tool makes for a caller in ``caller_groups``."""

    calls: list[dict] = []

    def fake_run_search(query, top_k, index_name, semantic_configuration, documents, section=None):
        calls.append({"index": index_name, "semantic_configuration": semantic_configuration})
        return "grounding"

    settings = ais.settings
    saved = (
        ac.get_access_policy,
        ais.groups_from_config,
        ais._run_search,
        settings.azure_ai_search_default_index,
        settings.ai_search_index_semantic_config_mapping,
        settings.ai_search_semantic_configuration,
    )
    ac.get_access_policy = lambda: policy
    ais.groups_from_config = lambda: caller_groups
    ais._run_search = fake_run_search
    settings.azure_ai_search_default_index = "default-index"
    settings.ai_search_index_semantic_config_mapping = {"index-a": "semantic-a"}
    settings.ai_search_semantic_configuration = "semantic-default"
    try:
        asyncio.run(ais.ai_search_tool.coroutine(query="vpn"))
    finally:
        (
            ac.get_access_policy,
            ais.groups_from_config,
            ais._run_search,
            settings.azure_ai_search_default_index,
            settings.ai_search_index_semantic_config_mapping,
            settings.ai_search_semantic_configuration,
        ) = saved
    return calls


def test_ai_search_keeps_each_group_on_its_own_index() -> None:
    policy = _policy(
        {
            _TEAM_A: {"priority": 200, "search_index": "index-a"},
            _TEAM_B: {"priority": 100, "search_index": "index-b"},
        }
    )
    assert _search_calls(policy, _TEAM_A) == [
        {"index": "index-a", "semantic_configuration": "semantic-a"}
    ]
    assert _search_calls(policy, _TEAM_B) == [
        {"index": "index-b", "semantic_configuration": "semantic-default"}
    ]
    assert _search_calls(policy, _TEAM_A, _TEAM_B)[0]["index"] == "index-a"
    assert _search_calls(policy)[0]["index"] == "default-index"


def _starter_prompts(policy: AccessPolicy, *caller_groups: str) -> list[dict]:
    router = starter_prompts_module.create_starter_prompts_router()
    endpoint = next(route.endpoint for route in router.routes)
    request = SimpleNamespace(user=SimpleNamespace(groups=list(caller_groups)))
    with _caller(policy):
        response = asyncio.run(endpoint(request))
    return [prompt.model_dump() for prompt in response.prompts]


def test_the_starter_prompts_route_returns_the_top_groups_prompts() -> None:
    policy = _policy(
        {
            _TEAM_A: {"priority": 200, "starter_prompts": [_PROMPT_A]},
            _TEAM_B: {"priority": 100, "starter_prompts": [_PROMPT_B]},
        }
    )
    assert _starter_prompts(policy, _TEAM_B, _TEAM_A) == [_PROMPT_A]
    assert _starter_prompts(policy, _TEAM_B) == [_PROMPT_B]
    assert _starter_prompts(policy) == []


# -- stand-ins for the middleware --------------------------------------------------


@tool("ls")
def _ls(path: str) -> str:
    """List files."""

    return "a.md"


@tool("grep")
def _grep(pattern: str) -> str:
    """Search files."""

    return "a.md:1"


@tool("read_file")
def _read_file(file_path: str) -> str:
    """Read a file."""

    return "contents"


@tool("ai_search_tool")
def _search(query: str) -> str:
    """Search the knowledge base."""

    return "[1] Doc"


def _task_tool(specs: list[dict]) -> StructuredTool:
    """A ``task`` tool described the way deepagents describes its subagents (a forked
    one's line ends with deepagents' note on it)."""

    listing = "\n".join(
        _describe_subagent_for_tool(
            spec["name"],
            spec["description"],
            forked=_is_forked_subagent(spec) or _is_forked_compiled_subagent(spec),
        )
        for spec in specs
    )
    return StructuredTool.from_function(
        name=TASK_TOOL_NAME,
        func=lambda description, subagent_type: "delegated",
        description=TASK_TOOL_DESCRIPTION.format(available_agents=listing),
    )


_ROSTER = [SERVICENOW_SUBAGENT, ADF_SUBAGENT]


@dataclass
class _ModelRequest:
    tools: list

    def override(self, **overrides: Any) -> _ModelRequest:
        return replace(self, **overrides)


@dataclass
class _ToolCallRequest:
    tool_call: dict
    tool: Any


def _model_request(roster: list[dict] = _ROSTER) -> _ModelRequest:
    return _ModelRequest(tools=[_ls, _grep, _read_file, _search, _task_tool(roster)])


def _offered(request: _ModelRequest, roster: list[dict] = _ROSTER) -> _ModelRequest:
    """The request AccessControlMiddleware passes on to the model."""

    seen: dict[str, _ModelRequest] = {}

    def handler(forwarded: _ModelRequest) -> str:
        seen["request"] = forwarded
        return "answered"

    middleware = AccessControlMiddleware(task_subagents=roster)
    assert middleware.wrap_model_call(request, handler) == "answered"
    return seen["request"]


def _names(request: _ModelRequest) -> list[str]:
    return [tool.name for tool in request.tools]


def _task_description(request: _ModelRequest) -> str:
    (task,) = [tool for tool in request.tools if tool.name == TASK_TOOL_NAME]
    return task.description


def _call(name: str, call_id: str = "call_1", **args: Any) -> _ToolCallRequest:
    known = {tool.name: tool for tool in (_ls, _grep, _read_file, _search)}
    known[TASK_TOOL_NAME] = _task_tool(_ROSTER)
    return _ToolCallRequest(
        tool_call={"name": name, "args": args, "id": call_id}, tool=known.get(name)
    )


def _delegation(subagent_type: str, call_id: str = "call_1") -> _ToolCallRequest:
    return _call(TASK_TOOL_NAME, call_id, description="Do the work", subagent_type=subagent_type)


def _run(request: _ToolCallRequest, roster: list[dict] = _ROSTER) -> Any:
    middleware = AccessControlMiddleware(task_subagents=roster)
    return middleware.wrap_tool_call(request, lambda _request: "ran")


def _arun(request: _ToolCallRequest, roster: list[dict] = _ROSTER) -> Any:
    async def handler(_request: _ToolCallRequest) -> str:
        return "ran"

    middleware = AccessControlMiddleware(task_subagents=roster)
    return asyncio.run(middleware.awrap_tool_call(request, handler))


def _assert_refused(result: Any, content: str, call_id: str = "call_1") -> None:
    assert isinstance(result, ToolMessage), result
    assert result.status == "error"
    assert result.tool_call_id == call_id
    assert result.content == content, result.content


# -- what the model is offered --------------------------------------------------


def test_global_blocked_tools_are_hidden_from_every_caller() -> None:
    policy = _policy({_TEAM_A: {}}, {"blocked_tools": ["ls", "grep"]})
    for groups in ((), (_STRANGER,), (_TEAM_A,)):
        with _caller(policy, *groups):
            names = _names(_offered(_model_request()))
        assert names == ["read_file", "ai_search_tool", TASK_TOOL_NAME], (groups, names)


def test_global_blocked_subagents_are_hidden_from_every_caller() -> None:
    policy = _policy({_TEAM_A: {}}, {"blocked_subagents": [_ADF]})
    for groups in ((), (_STRANGER,), (_TEAM_A,)):
        with _caller(policy, *groups):
            listing = _task_description(_offered(_model_request()))
        assert f"- {_ADF}:" not in listing and f"- {_SERVICENOW}:" in listing, groups


def test_a_groups_blocked_tools_are_hidden_only_from_its_members() -> None:
    policy = _policy({_TEAM_A: {"blocked_tools": ["ai_search_tool"]}, _TEAM_B: {}})
    with _caller(policy, _TEAM_A):
        assert "ai_search_tool" not in _names(_offered(_model_request()))
    with _caller(policy, _TEAM_B):
        assert "ai_search_tool" in _names(_offered(_model_request()))


def test_every_matched_groups_blocks_apply_whatever_their_priority() -> None:
    """Deny wins: a higher-priority group that blocks nothing lifts nothing."""

    policy = _policy(
        {
            _TEAM_A: {"priority": 300, "blocked_tools": ["read_file"]},
            _TEAM_B: {"priority": 100, "blocked_tools": ["ai_search_tool"]},
            _STRANGER: {"priority": 900},
        },
        {"blocked_tools": ["ls"], "blocked_subagents": [_ADF]},
    )
    with _caller(policy, _TEAM_A, _TEAM_B, _STRANGER):
        request = _offered(_model_request())
    assert _names(request) == ["grep", TASK_TOOL_NAME]
    assert f"- {_ADF}:" not in _task_description(request)


def test_an_unrestricted_caller_gets_the_request_unchanged() -> None:
    policy = _policy({_TEAM_A: {"blocked_subagents": [_ADF]}, _TEAM_B: {}})
    with _caller(policy, _TEAM_B):
        original = _model_request()
        assert _offered(original) is original


def test_outside_a_run_the_global_lists_still_apply() -> None:
    saved = ac.get_access_policy
    ac.get_access_policy = lambda: _policy({}, {"blocked_tools": ["ls"]})
    try:
        names = _names(_offered(_model_request()))  # the real groups_from_config: ()
    finally:
        ac.get_access_policy = saved
    assert "ls" not in names and "grep" in names


def test_a_blocked_subagents_line_is_cut_from_the_task_description() -> None:
    policy = _policy({_TEAM_A: {"blocked_subagents": [_ADF]}})
    with _caller(policy, _TEAM_A):
        request = _offered(_model_request())
    # Exactly what deepagents would have written without adf-agent.
    expected = _task_tool([SERVICENOW_SUBAGENT]).description
    assert _task_description(request) == expected
    assert _names(request)[-1] == TASK_TOOL_NAME


def test_the_first_listed_subagent_can_be_cut_too() -> None:
    policy = _policy({_TEAM_A: {"blocked_subagents": [_SERVICENOW]}})
    with _caller(policy, _TEAM_A):
        request = _offered(_model_request())
    assert _task_description(request) == _task_tool([ADF_SUBAGENT]).description


def test_task_is_unchanged_when_no_listed_subagent_is_blocked() -> None:
    """A blocked subagent this deployment does not register costs nothing."""

    policy = _policy({_TEAM_A: {"blocked_subagents": [_ADLS]}})
    with _caller(policy, _TEAM_A):
        original = _model_request()
        (task,) = [tool for tool in original.tools if tool.name == TASK_TOOL_NAME]
        assert task in _offered(original).tools


def test_task_is_hidden_when_no_subagent_remains() -> None:
    policy = _policy({_TEAM_A: {"blocked_subagents": [_SERVICENOW, _ADF]}})
    with _caller(policy, _TEAM_A):
        assert TASK_TOOL_NAME not in _names(_offered(_model_request()))


def test_task_is_hidden_when_it_is_a_blocked_tool() -> None:
    policy = _policy({}, {"blocked_tools": [TASK_TOOL_NAME]})
    with _caller(policy):
        assert TASK_TOOL_NAME not in _names(_offered(_model_request()))


def test_task_is_hidden_when_its_description_does_not_list_a_blocked_subagent() -> None:
    """Fail closed: a subagent that cannot be cut from the listing is not offered."""

    policy = _policy({_TEAM_A: {"blocked_subagents": [_ADF]}})
    request = _model_request()
    request.tools[-1] = request.tools[-1].model_copy(update={"description": "Delegate work."})
    with _caller(policy, _TEAM_A), _logged(logging.ERROR) as errors:
        names = _names(_offered(request))
    assert TASK_TOOL_NAME not in names and "read_file" in names
    assert len(errors) == 1 and "adf-agent" in errors[0], errors


def test_task_offered_without_a_roster_is_hidden_from_a_restricted_caller() -> None:
    policy = _policy({_TEAM_A: {"blocked_subagents": [_ADF]}})
    with _caller(policy, _TEAM_A), _logged(logging.ERROR) as errors:
        names = _names(_offered(_model_request(), roster=[]))
    assert TASK_TOOL_NAME not in names
    assert len(errors) == 1, errors


def test_tools_given_as_dicts_are_filtered_too() -> None:
    policy = _policy({}, {"blocked_tools": ["ls"]})
    request = _ModelRequest(
        tools=[
            {"type": "function", "function": {"name": "ls", "description": "List."}},
            {"name": "grep", "description": "Search."},
            _read_file,
        ]
    )
    with _caller(policy):
        tools = _offered(request).tools
    assert tools == request.tools[1:]


# -- what runs ------------------------------------------------------------------


def test_a_call_to_a_blocked_tool_is_refused_and_never_runs() -> None:
    policy = _policy({_TEAM_A: {"blocked_tools": ["ai_search_tool"]}}, {"blocked_tools": ["ls"]})
    with _caller(policy, _TEAM_A), _logged() as warnings:
        for run in (_run, _arun):
            _assert_refused(run(_call("ls")), "Error: ls is not a valid tool.")
            _assert_refused(
                run(_call("ai_search_tool", "call_2", query="x")),
                "Error: ai_search_tool is not a valid tool.",
                "call_2",
            )
            assert run(_call("read_file", file_path="/a.md")) == "ran"
    assert len(warnings) == 4 and all("access-control policy" in m for m in warnings), warnings


def test_a_task_call_to_a_blocked_subagent_is_refused_and_never_runs() -> None:
    policy = _policy({_TEAM_A: {"blocked_subagents": [_ADF]}})
    refusal = (
        "We cannot invoke subagent adf-agent because it does not exist, the only "
        f"allowed types are `{_SERVICENOW}`"
    )
    with _caller(policy, _TEAM_A), _logged() as warnings:
        for run in (_run, _arun):
            _assert_refused(run(_delegation(_ADF)), refusal)
            assert run(_delegation(_SERVICENOW)) == "ran"
    warning = (
        "Refused task delegation to adf-agent, which the access-control policy "
        "blocks for this caller."
    )
    assert warnings == [warning, warning], warnings


def test_the_same_calls_run_for_an_unrestricted_caller() -> None:
    policy = _policy({_TEAM_A: {"blocked_tools": ["ls"], "blocked_subagents": [_ADF]}})
    with _caller(policy, _TEAM_B):
        for run in (_run, _arun):
            assert run(_call("ls", path="/")) == "ran"
            assert run(_delegation(_ADF)) == "ran"
            # An unknown name gets the stock handling, as before.
            assert run(_call("made_up")) == "ran"


def test_an_unknown_tool_is_refused_without_listing_the_registered_tools() -> None:
    """The stock error lists every registered tool, blocked ones included."""

    policy = _policy({}, {"blocked_tools": ["ls"]})
    with _caller(policy):
        for run in (_run, _arun):
            _assert_refused(run(_call("made_up")), "Error: made_up is not a valid tool.")


def test_an_unknown_subagent_type_lists_only_the_allowed_types() -> None:
    policy = _policy({_TEAM_A: {"blocked_subagents": [_ADF]}})
    with _caller(policy, _TEAM_A), _logged() as warnings:
        _assert_refused(
            _run(_delegation("made-up-agent")),
            "We cannot invoke subagent made-up-agent because it does not exist, the "
            f"only allowed types are `{_SERVICENOW}`",
        )
    assert warnings == []


def test_a_task_call_is_refused_when_no_subagent_remains() -> None:
    policy = _policy({_TEAM_A: {"blocked_subagents": [_SERVICENOW, _ADF]}})
    with _caller(policy, _TEAM_A):
        _assert_refused(_run(_delegation(_SERVICENOW)), "Error: task is not a valid tool.")


def test_a_blocked_subagent_this_deployment_lacks_is_refused_as_unknown() -> None:
    policy = _policy({_TEAM_A: {"blocked_subagents": [_ADLS]}})
    with _caller(policy, _TEAM_A):
        _assert_refused(
            _run(_delegation(_ADLS)),
            "We cannot invoke subagent adls-agent because it does not exist, the only "
            f"allowed types are `{_SERVICENOW}`, `{_ADF}`",
        )


# -- the startup check ----------------------------------------------------------


def _known() -> tuple[set[str], set[str]]:
    return agent_mod._capability_names()


def _check(policy: AccessPolicy) -> None:
    tools, subagents = _known()
    check_blocked_names(policy, tools=tools, subagents=subagents)


def _rejected(policy: AccessPolicy) -> str:
    return _config_error(lambda: _check(policy))


def test_the_catalog_has_every_tool_and_subagent_the_backend_can_expose() -> None:
    tools, subagents = _known()
    assert subagents == {_SERVICENOW, _ADF, _ADLS, _STTM}
    assert {"ls", "grep", "read_file", "task", "ai_search_tool", "read_url"} <= tools
    assert {"servicenow_search_knowledge", "find_pipelines", "check_file_arrival"} <= tools
    assert "sttm_start_session" in tools


def test_a_blocked_tool_the_backend_does_not_have_is_rejected() -> None:
    message = _rejected(_policy({_TEAM_A: {"blocked_tools": ["read_url_tool"]}}))
    assert f"groups.{_TEAM_A}.blocked_tools: 'read_url_tool'" in message
    assert "read_url" in message  # the known names are listed


def test_a_blocked_subagent_the_backend_does_not_have_is_rejected() -> None:
    message = _rejected(_policy({}, {"blocked_subagents": ["adf"]}))
    assert "global.blocked_subagents: 'adf'" in message


def test_a_tool_name_is_not_a_subagent_name() -> None:
    message = _rejected(_policy({_TEAM_A: {"blocked_subagents": ["ai_search_tool"]}}))
    assert "'ai_search_tool'" in message


def test_names_of_subagents_this_deployment_does_not_register_are_accepted() -> None:
    """One file serves every deployment, whichever subagents it configures."""

    _check(_policy({_TEAM_A: {"blocked_subagents": [_ADF, _ADLS], "blocked_tools": ["execute"]}}))


def test_the_shipped_files_load_and_block_only_names_the_backend_has() -> None:
    """The example, and the real policy where present (the public mirror has none)."""

    for name in ("access-control.example.json", "access-control.json"):
        path = _REPO_ROOT / "config" / name
        if not path.exists():
            print(f"     (skipped: no config/{name})")
            continue
        policy = load_access_policy(str(path))
        assert policy.groups and all(group.display_name for group in policy.groups.values())
        _check(policy)


def test_the_shipped_files_block_every_file_tool_but_read_file() -> None:
    """read_file opens the skills and the history offloads; the other file tools
    are blocked for everyone. ``execute`` exists only with a sandbox backend."""

    file_tools = set(get_args(FsToolName)) - {"execute"}
    for name in ("access-control.example.json", "access-control.json"):
        path = _REPO_ROOT / "config" / name
        if not path.exists():
            print(f"     (skipped: no config/{name})")
            continue
        blocked = set(load_access_policy(str(path)).global_policy.blocked_tools)
        assert file_tools - blocked == {"read_file"}, (name, sorted(file_tools - blocked))


def test_the_build_fails_on_a_blocked_name_the_backend_does_not_have() -> None:
    saved = agent_mod.get_access_policy
    agent_mod.get_access_policy = lambda: _policy({}, {"blocked_tools": ["lss"]})
    try:
        agent_mod._build_agent_sync(InMemorySaver())
    except AccessControlConfigError as exc:
        assert "global.blocked_tools: 'lss'" in str(exc)
    else:
        raise AssertionError("the build accepted a blocked name the backend does not have")
    finally:
        agent_mod.get_access_policy = saved


# -- the real agent -------------------------------------------------------------


class _RecordingStandIn(_AzureStandIn):
    """The stand-in model, noting each call's tools as {name: description}."""

    offered: list[dict[str, str]] = Field(default_factory=list)

    def _note(self, messages: list, tools: list | None) -> None:
        first = messages[0] if messages else None
        if isinstance(first, SystemMessage) and first.content == SAFETY_CLASSIFIER_SYSTEM_PROMPT:
            return
        functions = [entry["function"] for entry in tools or ()]
        self.offered.append({fn["name"]: fn.get("description", "") for fn in functions})

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self._note(messages, kwargs.get("tools"))
        return super()._generate(messages, stop, run_manager, **kwargs)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self._note(messages, kwargs.get("tools"))
        return await super()._agenerate(messages, stop, run_manager, **kwargs)


def _recording_model(replies: list[AIMessage]) -> _RecordingStandIn:
    return _RecordingStandIn(
        azure_endpoint="https://dummy.openai.azure.com",
        azure_deployment="gpt-test",
        api_version="2024-10-21",
        api_key="dummy-key",
        disable_streaming=True,
        replies=replies,
    )


@contextmanager
def _agent(
    model: _RecordingStandIn, policy: AccessPolicy, roster: list[dict] | None = None
) -> Iterator[Any]:
    """The real agent with ``roster`` as its subagents (by default ServiceNow and ADF)."""

    # The model stays patched for the whole test: the safety classifier fetches
    # it on every turn. adf-agent is registered only when ADF is configured.
    saved = (
        agent_mod.get_azure_chat_model,
        agent_mod.get_access_policy,
        agent_mod.ENABLED_SUBAGENTS,
        ac.get_access_policy,
    )
    agent_mod.get_azure_chat_model = lambda: model
    agent_mod.get_access_policy = lambda: policy
    agent_mod.ENABLED_SUBAGENTS = [SERVICENOW_SUBAGENT, ADF_SUBAGENT] if roster is None else roster
    ac.get_access_policy = lambda: policy
    try:
        yield agent_mod._build_agent_sync(InMemorySaver())
    finally:
        (
            agent_mod.get_azure_chat_model,
            agent_mod.get_access_policy,
            agent_mod.ENABLED_SUBAGENTS,
            ac.get_access_policy,
        ) = saved


def _calls(*calls: tuple[str, dict, str]) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id} for name, args, call_id in calls],
    )


def _delegate(subagent_type: str, call_id: str) -> tuple[str, dict, str]:
    return (TASK_TOOL_NAME, {"description": "Do the work", "subagent_type": subagent_type}, call_id)


def _run_turn(graph: Any, groups: list[str]) -> list[Any]:
    config = {
        "configurable": {
            "thread_id": "access-control",
            "langgraph_auth_user": {"identity": "caller", "groups": groups},
        }
    }
    result = asyncio.run(graph.ainvoke(_user("Help me with my work."), config))
    return result["messages"]


_E2E_POLICY = _policy(
    {_TEAM_B: {"blocked_tools": ["write_file"], "blocked_subagents": [_ADF]}},
    {"blocked_tools": ["ls", "grep"]},
)


def test_the_policy_holds_in_every_stack_of_the_real_agent() -> None:
    """A restricted caller: the orchestrator and the ServiceNow subagent are each
    offered no blocked tool, adf-agent is not listed, and the stray calls the
    model makes anyway are refused before they run."""

    model = _recording_model(
        [
            _calls(
                _delegate(_ADF, "call_adf"),
                ("ls", {"path": "/"}, "call_ls"),
                ("made_up", {}, "call_made_up"),
            ),
            _calls(_delegate(_SERVICENOW, "call_servicenow")),
            AIMessage(content="ServiceNow report."),
            AIMessage(content="All done."),
        ]
    )
    with _agent(model, _E2E_POLICY) as graph:
        messages = _run_turn(graph, [_TEAM_B])

    assert messages[-1].content == "All done."
    assert model.replies == []
    orchestrator, subagents = [], []
    for offered in model.offered:
        (orchestrator if TASK_TOOL_NAME in offered else subagents).append(offered)
    assert len(orchestrator) == 3 and len(subagents) == 1, model.offered
    (servicenow,) = subagents
    assert "servicenow_list_tickets" in servicenow
    for offered in model.offered:
        assert not {"ls", "grep", "write_file"} & set(offered), sorted(offered)
        assert "read_file" in offered
        assert "find_pipelines" not in offered  # adf-agent never ran
    for offered in orchestrator:
        listing = offered[TASK_TOOL_NAME]
        assert f"- {_ADF}:" not in listing and f"- {_SERVICENOW}:" in listing

    results = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    assert results["call_adf"].content == (
        "We cannot invoke subagent adf-agent because it does not exist, the only "
        f"allowed types are `{_SERVICENOW}`"
    )
    assert results["call_ls"].content == "Error: ls is not a valid tool."
    assert results["call_made_up"].content == "Error: made_up is not a valid tool."
    assert results["call_servicenow"].content == "ServiceNow report."


def test_the_global_list_blocks_a_subagent_for_a_caller_in_no_group() -> None:
    """The real agent: adf-agent, blocked in ``global``, is not listed for a caller
    the file has no group for, and the delegation the model makes anyway is
    refused before it runs."""

    model = _recording_model([_calls(_delegate(_ADF, "call_adf")), AIMessage(content="Done.")])
    with _agent(model, _policy({_TEAM_A: {}}, {"blocked_subagents": [_ADF]})) as graph:
        messages = _run_turn(graph, [])

    assert len(model.offered) == 2, model.offered  # the orchestrator's two calls
    for offered in model.offered:
        listing = offered[TASK_TOOL_NAME]
        assert f"- {_ADF}:" not in listing and f"- {_SERVICENOW}:" in listing
    (result,) = [m for m in messages if isinstance(m, ToolMessage)]
    assert result.content == (
        "We cannot invoke subagent adf-agent because it does not exist, the only "
        f"allowed types are `{_SERVICENOW}`"
    )


def test_a_caller_with_no_restriction_keeps_every_capability() -> None:
    model = _recording_model([AIMessage(content="Hello.")])
    with _agent(model, _policy({_TEAM_B: {"blocked_tools": ["write_file"]}})) as graph:
        _run_turn(graph, [_TEAM_A])

    (offered,) = model.offered
    assert {"ls", "grep", "write_file", "read_file", "ai_search_tool"} <= set(offered)
    expected = _task_tool([SERVICENOW_SUBAGENT, ADF_SUBAGENT])
    assert offered[TASK_TOOL_NAME] == expected.description
    # Every tool the orchestrator offers can be named in an access-control file.
    assert set(offered) <= _known()[0], set(offered) - _known()[0]


def test_the_agent_has_no_general_purpose_subagent() -> None:
    """Every delegation goes to a subagent this backend defines: deepagents' own
    is not listed, and a call to it runs nothing."""

    model = _recording_model([_calls(_delegate(_GP, "call_gp")), AIMessage(content="Done.")])
    with _agent(model, _policy()) as graph:
        messages = _run_turn(graph, [_TEAM_A])

    assert len(model.offered) == 2, model.offered  # the orchestrator's two calls
    assert f"- {_GP}:" not in model.offered[0][TASK_TOOL_NAME]
    (result,) = [m for m in messages if isinstance(m, ToolMessage)]
    assert result.content == (
        f"We cannot invoke subagent {_GP} because it does not exist, the only "
        f"allowed types are `{_SERVICENOW}`, `{_ADF}`"
    )


def _main() -> int:
    checks = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for check in checks:
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - standalone runner reports all
            failures += 1
            print(f"FAIL {check.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {check.__name__}")
    print(f"\n{len(checks) - failures}/{len(checks)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
