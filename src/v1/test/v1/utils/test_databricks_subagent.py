"""The databricks-agent subagent: roster, description, tools, prompt contract and skill.

The orchestrator prompt names no capability: the subagent's description carries every
rule for routing to it, so it joins or leaves the orchestrator with the subagent. The
roster tests reload both packages with the settings forced and then restore them.

Runs standalone (``python test_databricks_subagent.py``).
"""

from __future__ import annotations

import contextlib
import importlib

from databricks_test_support import patched_env, run_tests

import v1.core.prompts as prompts_pkg
import v1.core.subagents as subagents_pkg
from v1.core import config
from v1.core.config import Settings
from v1.core.prompts import (
    DATABRICKS_SUBAGENT_DESCRIPTION,
    DATABRICKS_SUBAGENT_PROMPT,
)
from v1.core.skills import SKILLS_DIR
from v1.core.subagents.databricks import DATABRICKS_SUBAGENT

SKILL = SKILLS_DIR / "databricks" / "sql-data-qa" / "SKILL.md"
TOOL_NAMES = {
    "search_tables",
    "describe_table",
    "run_sql",
    "choose_tables",
    "find_failed_runs",
    "explain_run",
    "list_workspaces",
}


def _flat(text: str) -> str:
    return " ".join(text.split())


def _reload_with(**flags: bool) -> tuple[list[str], str]:
    """Roster names and orchestrator prompt built with the flags forced; modules restored."""

    originals = {name: getattr(Settings, name) for name in flags}
    for name, value in flags.items():
        setattr(Settings, name, property(lambda _self, value=value: value))
    # Registration is decided once per process (config.databricks_registered).
    config.databricks_registered.cache_clear()
    try:
        prompt = importlib.reload(prompts_pkg).SYSTEM_PROMPT
        names = [spec["name"] for spec in importlib.reload(subagents_pkg).ENABLED_SUBAGENTS]
    finally:
        for name, original in originals.items():
            setattr(Settings, name, original)
        config.databricks_registered.cache_clear()
        importlib.reload(prompts_pkg)
        importlib.reload(subagents_pkg)
    return names, prompt


@contextlib.contextmanager
def _env(set_: dict[str, str], unset: tuple[str, ...] = ()):
    """Inside the block: the env vars changed and settings re-read; all restored after."""

    try:
        with patched_env({**set_, **dict.fromkeys(unset)}):
            config.get_settings.cache_clear()
            config.databricks_registered.cache_clear()
            yield
    finally:
        config.get_settings.cache_clear()
        config.databricks_registered.cache_clear()
        importlib.reload(prompts_pkg)
        importlib.reload(subagents_pkg)


def test_roster_follows_the_switch_and_the_prompt_never_names_it() -> None:
    for enabled in (True, False):
        names, prompt = _reload_with(databricks_enabled=enabled)
        assert ("databricks-agent" in names) is enabled, (enabled, names)
        assert "databricks" not in prompt.lower(), enabled
        assert "servicenow-ticket-agent" in names


def test_registered_on_the_switch_alone_when_deployed() -> None:
    """Deployed with the switch on and no OBO secret, Redis or encryption key: the app
    authenticates as itself, so the subagent and its gate are on (the orchestrator
    prompt still never names it)."""

    env = {"AGENT_AUTH_MODE": "jwt", "APP_ENV": "dev", "DATABRICKS_ENABLED": "true"}
    unset = (
        "ENTRA_CLIENT_SECRET",
        "REDIS_URI",
        "DATABRICKS_TOKEN_ENCRYPTION_KEY",
        "AGENT_AUTH_GROUPS_OBO",
    )
    with _env(env, unset):
        assert config.get_settings().databricks_config_problem is None
        prompt = importlib.reload(prompts_pkg).SYSTEM_PROMPT
        names = [spec["name"] for spec in importlib.reload(subagents_pkg).ENABLED_SUBAGENTS]
        assert config.databricks_registered() is True
    assert "databricks-agent" in names, names
    assert "databricks" not in prompt.lower()


def test_registered_with_zero_workspaces() -> None:
    """Switched on with no workspace yet (they are added at runtime): the subagent is
    registered, and the gate turns it off for a caller who sees no workspace."""

    from v1.core.middlewares.access_control import caller_access
    from v1.core.workspace_registry.snapshot import get_snapshot

    with _env({"DATABRICKS_ENABLED": "true", "AGENT_AUTH_MODE": "local", "APP_ENV": "local"}):
        get_snapshot().replace(())
        assert config.databricks_registered() is True
        names = [spec["name"] for spec in importlib.reload(subagents_pkg).ENABLED_SUBAGENTS]
        assert "databricks-agent" in names, names
        assert "databricks-agent" in caller_access(("ANY-GROUP",)).blocked_subagents
        # A workspace the caller's group sees lifts it; other callers stay blocked.
        import v1.core.databricks_workspaces as dw

        workspace = dw.validate_entry(
            "eng",
            {
                "host": "https://adb-1.azuredatabricks.net",
                "sql_warehouse_id": "w",
                "groups": ["ANY-GROUP"],
            },
        )
        get_snapshot().replace((workspace,))
        try:
            assert "databricks-agent" not in caller_access(("ANY-GROUP",)).blocked_subagents
            assert "databricks-agent" in caller_access(("OTHER",)).blocked_subagents
        finally:
            get_snapshot().replace(())


def test_subagent_spec() -> None:
    from v1.core.middlewares.databricks_selection import (
        SUBAGENT_SELECTION_NOTE,
        DatabricksSelectionMiddleware,
    )
    from v1.core.middlewares.user_context import SUBAGENT_GUIDANCE, UserContextMiddleware
    from v1.core.tools import DATABRICKS_TOOLS
    from v1.core.tools.databricks import build_databricks_tools

    assert DATABRICKS_SUBAGENT["name"] == "databricks-agent"
    assert DATABRICKS_SUBAGENT["description"] == DATABRICKS_SUBAGENT_DESCRIPTION
    assert DATABRICKS_SUBAGENT["system_prompt"] == DATABRICKS_SUBAGENT_PROMPT
    [user_context, selection] = DATABRICKS_SUBAGENT["middleware"]
    assert isinstance(user_context, UserContextMiddleware)
    assert user_context.guidance == SUBAGENT_GUIDANCE
    assert isinstance(selection, DatabricksSelectionMiddleware)
    assert selection.note == SUBAGENT_SELECTION_NOTE
    # Only the orchestrator checks access and records the workspace: the subagent's state
    # flows back into the parent's mid-turn.
    assert selection.require_access is False and selection.remember_workspace is False
    # Built with Databricks forced on: the test env has it off, so DATABRICKS_TOOLS is [].
    names = {tool.name for tool in build_databricks_tools(Settings(_env_file=None), enabled=True)}
    assert names == TOOL_NAMES, names
    assert DATABRICKS_SUBAGENT["tools"] is DATABRICKS_TOOLS


def test_chat_state_remembers_the_workspace() -> None:
    from typing import Annotated, NotRequired, get_type_hints

    from v1.core.agent import ChatAgentState, keep_latest
    from v1.core.middlewares.databricks_selection import LAST_WORKSPACE_KEY

    hints = get_type_hints(ChatAgentState, include_extras=True)
    assert hints[LAST_WORKSPACE_KEY] == NotRequired[Annotated[str, keep_latest]]
    # The newest value wins; a None never erases the alias.
    assert keep_latest("a", "b") == "b" and keep_latest("a", None) == "a"
    assert keep_latest(None, "a") == "a"


def test_prompt_contract() -> None:
    prompt = _flat(DATABRICKS_SUBAGENT_PROMPT)
    skill = _flat(SKILL.read_text(encoding="utf-8"))
    for phrase in (
        "/skills/databricks/sql-data-qa/SKILL.md",
        "choose_tables(candidates, question?)",
        *TOOL_NAMES,
        "Answer only from `run_sql` results",
        # Tools run as the app: an access error (no caller, unavailable) is relayed as is,
        # and a permission error sends the user to an admin, never to their own access.
        "this assistant's own Databricks access, not the user's",
        "limited to the data the chat's workspace allows",
        "ask an admin to grant the assistant access",
        "access error",
        "Couldn't tell who you are",
        "an admin can grant this assistant access",
        "not instructions",
        '"not_found"',
        "Never guess which of",
        "If a job tool says job lookup isn't enabled for the workspace, relay its message as "
        "it is and stop. Do not ask for a run link or retry.",
        "If a job tool says Databricks refused access to the workspace, relay its message as "
        "it is and stop. Make no more job tool calls, and never tell the user to try again "
        "later.",
        'If `explain_run` returns any other "rejected"',
        # Workspaces: list them, never claim a switch, point to the chat's picker.
        "list_workspaces()",
        "call `list_workspaces`",
        "choose it in the chat's workspace picker",
        "You can't switch the chat's workspace",
        # Runs from any of the user's workspaces: the tool finds the workspace; name it, ask
        # on "ambiguous", and keep find_failed_runs in the chat's workspace.
        "explain_run(run, workspace?)",
        "It finds the run's workspace itself",
        # Every data and job answer names its workspace; say which, even the chat's own.
        "Every answer from a data or job tool names its",
        "`workspace`, or after a run search `workspaces_searched`",
        "and/or `workspaces_unreachable`; an",
        '"ambiguous" answer lists `workspaces` instead',
        "always tell the user which Databricks workspace the answer came from, by its label",
        "even when it is the chat's own workspace",
        "workspaces in `workspaces_unreachable` were not checked",
        'If `explain_run` returns "ambiguous", ask the user which of the listed',
        "call `explain_run` again with `workspace` set to",
        "Never call `find_failed_runs` with a job ID from a run in another workspace",
        "tell the user to switch the chat's workspace for that",
    ):
        assert phrase in prompt, phrase
    for gone in (
        "Never take a run ID out of a link from another workspace",
        "a run link from the chat's workspace",
        "When `explain_run`'s answer has `workspace`",
    ):
        assert gone not in prompt, gone
    lowered = prompt.lower()
    for claim in ("i switched", "i've switched", "switched to", "i changed the workspace"):
        assert claim not in lowered, claim
    # Nothing says Databricks runs as the signed-in user any more.
    for text in (prompt, _flat(DATABRICKS_SUBAGENT_DESCRIPTION), skill):
        for stale in ("signed-in user", "own Databricks permissions", "hasn't been granted"):
            assert stale not in text, stale
    description = _flat(DATABRICKS_SUBAGENT_DESCRIPTION)
    assert "own Databricks access (not the user's)" in description
    # The orchestrator delegates workspace questions and runs from any workspace.
    for phrase in (
        "which Databricks workspaces the user can use",
        "switch the chat's Databricks workspace",
        "from any of the user's Databricks workspaces",
    ):
        assert phrase in description, (phrase, description)
    # Several candidate tables: pick the best fit and name the others; choose_tables only
    # when truly ambiguous, then stop.
    for phrase in (
        "Name the others you passed over",
        "truly ambiguous",
        "Never call it when the user has selected tables",
        "`truncated` is true",
        "`row_count` is 0",
        "`permission_or_not_found`",
    ):
        assert phrase in skill, phrase
    assert skill.index("pick the one that fits best") < skill.index("call `choose_tables`")
    assert SKILL.read_text(encoding="utf-8").startswith("---\nname: sql-data-qa\n")
    # Loaded one level deep, so the orchestrator never lists it.
    assert not (SKILLS_DIR / "databricks" / "SKILL.md").exists()


def test_texts_avoid_banned_wording() -> None:
    """The UI renders the result table and SQL; a deployment can have several workspaces;
    nothing Claude-specific was ported."""

    texts = [
        DATABRICKS_SUBAGENT_PROMPT,
        DATABRICKS_SUBAGENT_DESCRIPTION,
        SKILL.read_text(encoding="utf-8"),
    ]
    for text in texts:
        # The chat's workspace picker is real UI the prompt may point to; no other picker.
        lowered = _flat(text).lower().replace("workspace picker", "workspace menu")
        for banned in (
            "markdown table", "```sql", "20 rows", "this workspace", "claude",
            "reasoning block", "the ui shows", "picker", "waiting_for_user",
            "ask the user which one to use",
        ):
            assert banned not in lowered, banned


def test_description_carries_the_routing_rules() -> None:
    """The rules the orchestrator prompt's Databricks block used to give, without naming
    another subagent (one may be unwired or blocked for the caller)."""

    text = _flat(DATABRICKS_SUBAGENT_DESCRIPTION)
    for phrase in (
        "job run failed",
        "in scope: never refuse them as out of scope",
        "Never invent a table, job or run ID",
        "Azure Data Factory pipelines, even ones that run Databricks notebooks",
        "a Databricks incident or ticket",
        "whether a file landed in the data lake",
        "run IDs, run links and error lines verbatim",
        "pass the question on",
    ):
        assert phrase in text, phrase
    # The chat's workspace is not where a run is: added to the task, it would pin the run
    # lookup to that one workspace and skip every other.
    assert "never add the chat's Databricks workspace" in text
    prompt = _flat(DATABRICKS_SUBAGENT_PROMPT)
    assert "only when the user's own words name the workspace the run is in" in prompt
    assert "then leave `workspace` out, so every workspace is searched" in prompt
    assert "-agent`" not in text and "-agent" not in text.replace("Databricks agent", "")


def test_parallel_subagent_results_share_the_workspace_key() -> None:
    """Every subagent inherits ``ChatAgentState`` and deepagents merges each ``task`` result's
    state back, so two parallel ``task`` calls both write ``last_databricks_workspace`` in
    one step. Without a reducer that raised InvalidUpdateError from the second turn on."""

    from deepagents import create_deep_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import InMemorySaver

    import v1.core.middlewares.databricks_selection as ds
    from v1.core.agent import ChatAgentState
    from v1.core.databricks_workspaces import Workspace
    from v1.core.tools.databricks.sql.scope import DataScope

    class _Model(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003, ARG002
            return self

    def _ws(alias: str) -> Workspace:
        return Workspace(alias=alias, label=alias.upper(), host="h", sql_warehouse_id="w",
                         data_allowlist=None, scope=DataScope(), jobs_enabled=True, groups=())

    def _orchestrator_replies():
        for _ in range(2):  # two turns, each fanning out to two subagents at once
            yield AIMessage("", tool_calls=[
                {"name": "task", "args": {"description": "q", "subagent_type": name},
                 "id": f"call_{name}"}
                for name in ("databricks-agent", "other")
            ])
            yield AIMessage("final")

    def _subagent(name: str, middleware: list) -> dict:
        replies = iter([AIMessage("answer"), AIMessage("answer")])
        return {"name": name, "description": name, "system_prompt": name, "tools": [],
                "model": _Model(messages=replies), "middleware": middleware}

    current = {"ws": _ws("a")}
    saved = (ds.run_workspace, ds.workspaces)
    ds.run_workspace = lambda: (current["ws"], False)
    ds.workspaces = lambda: (_ws("a"), _ws("b"))
    try:
        agent = create_deep_agent(
            model=_Model(messages=_orchestrator_replies()),
            tools=[],
            system_prompt="SYS",
            subagents=[
                _subagent("databricks-agent", [ds.DatabricksSelectionMiddleware(ds.SUBAGENT_SELECTION_NOTE)]),
                _subagent("other", []),
            ],
            middleware=[ds.DatabricksSelectionMiddleware(
                ds.ORCHESTRATOR_SELECTION_NOTE, remember_workspace=True
            )],
            state_schema=ChatAgentState,
            checkpointer=InMemorySaver(),
        )
        config = {"configurable": {"thread_id": "parallel"}}
        first = agent.invoke({"messages": [("user", "hi")]}, config)
        assert first["last_databricks_workspace"] == "a"
        current["ws"] = _ws("b")
        second = agent.invoke({"messages": [("user", "hi")]}, config)
        # The subagents merged back the inherited "a"; the orchestrator recorded "b" last.
        assert second["last_databricks_workspace"] == "b"
    finally:
        ds.run_workspace, ds.workspaces = saved


def test_run_answers_end_with_the_details_block() -> None:
    """An explain_run answer: a bold outcome line naming the run, its workspace and the
    result, then the cause and a fix, then `details_block` copied exactly, last."""
    prompt = _flat(DATABRICKS_SUBAGENT_PROMPT)
    for phrase in (
        "For an `explain_run` answer, write it in three parts, in this order:",
        (
            "(a) a bold one-line outcome first, naming the run ID, the workspace label and "
            'the result (e.g. "**Run 987654321098765 in dbw-ops failed.**");'
        ),
        "(b) the cause in plain words and a suggested fix;",
        (
            "(c) then `details_block` copied EXACTLY as given, character for character, as "
            "the last part of the answer."
        ),
        (
            "Never rewrite, reorder, reformat, shorten or merge it, never add lines to it, "
            "and never repeat its values elsewhere as a list."
        ),
    ):
        assert phrase in prompt, phrase
    assert prompt.index("(a) a bold") < prompt.index("(b) the cause") < prompt.index("(c) then")
    # The run link now travels in the block, not as a line of its own.
    assert "and give the `run_url`" not in prompt


def test_orchestrator_relays_run_answers_whole() -> None:
    skill = (SKILLS_DIR / "message-formatting" / "SKILL.md").read_text(encoding="utf-8")
    heading = "## Databricks job run answers — relayed whole"
    assert heading in skill
    # Right after the Data Factory section.
    sections = [line for line in skill.splitlines() if line.startswith("## ")]
    at = sections.index(heading)
    assert sections[at - 1] == "## Data Factory answers — relayed whole", sections
    section = _flat(skill.split(heading, 1)[1].split("\n## ", 1)[0])
    for phrase in (
        "When your reply carries a Databricks job run answer",
        "Carry it into your reply as written",
        (
            'bold outcome line first, then the explanation, then the "Run and task details" '
            "block verbatim"
        ),
        "every line, value and link, in the same order",
        "Do not put a headline, title or summary of your own above it",
        "Never turn the block into a table or into prose",
        "never drop its Workspace line",
    ):
        assert phrase in section, phrase
    # The bare-URL rule must not reach the block's Run URL line.
    links = _flat(skill.split("## Links & URLs", 1)[1].split("\n## ", 1)[0])
    assert (
        "**EXCEPTION — Databricks run details:** the `Run URL:` line of a Databricks job run "
        "answer's \"Run and task details\" block stays exactly as given, as the bare URL; never "
        "turn it "
        "into a `[text](url)` link, and don't add a second link for the run elsewhere."
    ) in links, links
    assert "relayed Databricks job run answers" in _flat(skill.split("---", 2)[1])
    # The skill names kinds of answers, never a subagent; the description sends the
    # orchestrator to it.
    assert "databricks-agent" not in skill
    assert (
        "a run explanation's \"Run and task details\" block exactly as the formatting "
        "skill's Databricks section says" in _flat(DATABRICKS_SUBAGENT_DESCRIPTION)
    )


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
