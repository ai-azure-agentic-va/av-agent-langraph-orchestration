"""The sttm-agent: a compiled subagent that reads the analyst's message itself.

As a declarative subagent it saw only the task the orchestrator wrote, so what
its tools recorded rested on the orchestrator quoting the analyst's latest
message in full, and it did not always: a skill's instructions or a whole
message went missing, and an attached PDF never arrived. Registered as a fork
(``mode="fork"``), it is handed the conversation instead, and :func:`prepare`
takes the analyst's latest message from it in code: the typed text, a skill's
block and the text of each attached PDF (:mod:`v1.core.tools.sttm.documents`).
The STTM tools read that message from the run's config
(:data:`~v1.core.tools.sttm.tools.MESSAGE_KEY`). The model gets the task, the
message, the analyst's earlier messages and the sttm-agent's previous answer,
not the orchestrator's history.

The graph it runs is the one deepagents builds for a declarative subagent with
the same prompt, tools and middleware (:func:`build_sttm_graph`), set by
:func:`install` when the agent is built.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from deepagents._excluded_middleware import _apply_excluded_middleware
from deepagents._tools import _apply_tool_description_overrides
from deepagents.backends.protocol import BackendProtocol
from deepagents.graph import _apply_custom_middleware
from deepagents.middleware._prompt_caching import append_prompt_caching_middleware
from deepagents.middleware._tool_exclusion import _ToolExclusionMiddleware
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from deepagents.middleware.subagents import _FORK_TASK_PREAMBLE
from deepagents.middleware.summarization import create_summarization_middleware
from deepagents.profiles.harness.harness_profiles import (
    _apply_profile_prompt,
    _harness_profile_for_model,
)
from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import Runnable, RunnableConfig

from v1.core.middlewares.azure_links import AzureLinksMiddleware
from v1.core.prompts import STTM_SUBAGENT_PROMPT
from v1.core.tools.sttm import STTM_TOOLS
from v1.core.tools.sttm.documents import attachment_name, message_parts, read_message
from v1.core.tools.sttm.spec import without_attribute_tables
from v1.core.tools.sttm.tools import MESSAGE_KEY

NAME = "sttm-agent"
# Besides the latest message, the model gets the analyst's earlier messages (the
# most recent that fit) and the end of the sttm-agent's previous answer.
MAX_EARLIER_CHARS = 40_000
MAX_ANSWER_CHARS = 12_000


def _from_analyst(message: Any) -> bool:
    """Whether a message is one the analyst sent, not a summary of earlier ones."""

    return (
        isinstance(message, HumanMessage)
        and message.additional_kwargs.get("lc_source") != "summarization"
    )


def _earlier(messages: Sequence[HumanMessage]) -> str:
    """The analyst's earlier messages, oldest first: the most recent that fit.

    Attribute tables are left out (a recorded one is in the session), and an
    attached file is only named.
    """

    kept: list[str] = []
    used = 0
    for message in reversed(messages):
        texts, files = message_parts(message.content)
        body = without_attribute_tables("\n\n".join(texts)).strip()
        body = "\n".join([body, *(f"[attached: {attachment_name(file)}]" for file in files)])
        if not body.strip():
            continue
        if used + len(body) > MAX_EARLIER_CHARS:
            kept.append("(older messages left out)")
            break
        used += len(body)
        kept.append(f"<message>\n{body.strip()}\n</message>")
    return "\n".join(reversed(kept))


def _previous_answer(messages: Sequence[BaseMessage]) -> str:
    """The end of the sttm-agent's last answer in the conversation."""

    calls = {
        call["id"]
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
        if call["name"] == "task" and (call.get("args") or {}).get("subagent_type") == NAME
    }
    answer = next(
        (
            message.text
            for message in reversed(messages)
            if isinstance(message, ToolMessage) and message.tool_call_id in calls
        ),
        "",
    )
    return answer if len(answer) <= MAX_ANSWER_CHARS else "…" + answer[-MAX_ANSWER_CHARS:]


def prepare(state: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The sttm-agent's input from a fork's state, and the analyst's message for its tools.

    A fork's messages are the conversation, then the task (deepagents'
    ``_fork_messages``). The analyst's message is the last one they sent, read
    in full (:func:`read_message`), with the notes on any file not read; it
    keeps the message's id. A conversation without one gives an empty message,
    which records nothing.
    """

    messages = list(state.get("messages") or [])
    task = messages.pop().text.removeprefix(_FORK_TASK_PREAMBLE).strip() if messages else ""
    sent = [message for message in messages if _from_analyst(message)]
    text, notes = read_message(sent[-1].content) if sent else ("", [])
    message = {"id": (sent[-1].id if sent else None) or "", "text": text, "notes": notes}

    parts = [f"<task>\n{task}\n</task>"]
    if earlier := _earlier(sent[:-1]):
        parts.append(f"<earlier_messages>\n{earlier}\n</earlier_messages>")
    if answer := _previous_answer(messages):
        parts.append(f"<previous_answer>\n{answer}\n</previous_answer>")
    parts.append(f"<analyst_message>\n{text}\n</analyst_message>")
    if notes:
        parts.append("<notes>\n" + "\n".join(f"- {note}" for note in notes) + "\n</notes>")
    inner = {key: value for key, value in state.items() if key != "messages"}
    inner["messages"] = [HumanMessage("\n\n".join(parts))]
    return inner, message


def _with_message(config: RunnableConfig | None, message: Mapping[str, Any]) -> RunnableConfig:
    config = RunnableConfig(**(config or {}))
    config["configurable"] = {**(config.get("configurable") or {}), MESSAGE_KEY: message}
    return config


class SttmAgent(Runnable[dict[str, Any], dict[str, Any]]):
    """:func:`prepare`, then the graph :func:`install` set, in the same run."""

    graph: Runnable | None = None

    def _graph(self) -> Runnable:
        if self.graph is None:
            raise RuntimeError("The sttm-agent's graph is not installed: build the agent first.")
        return self.graph

    def invoke(
        self, input: dict[str, Any], config: RunnableConfig | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        inner, message = prepare(input)
        return self._graph().invoke(inner, _with_message(config, message), **kwargs)

    async def ainvoke(
        self, input: dict[str, Any], config: RunnableConfig | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        # Reading an attached PDF blocks.
        inner, message = await asyncio.to_thread(prepare, input)
        return await self._graph().ainvoke(inner, _with_message(config, message), **kwargs)


STTM_RUNNABLE = SttmAgent()


def build_sttm_graph(
    model: BaseChatModel, backend: BackendProtocol, state_schema: type
) -> Runnable:
    """The graph deepagents builds for a declarative subagent, for the sttm-agent.

    The same steps as ``create_deep_agent`` takes for a subagent spec with this
    prompt, these tools and AzureLinksMiddleware, so the stack matches the
    other subagents' (test_sttm_agent compares them): the filesystem,
    summarization and patch middleware, the harness profile's (access
    control), the profile's exclusions, then AzureLinksMiddleware ahead of the
    profile's.
    """

    profile = _harness_profile_for_model(model, None)
    middleware = [
        FilesystemMiddleware(
            backend=backend, custom_tool_descriptions=profile.tool_description_overrides
        ),
        create_summarization_middleware(model, backend),
        PatchToolCallsMiddleware(),
    ]
    core = {entry.name for entry in middleware}
    middleware.extend(profile.materialize_extra_middleware())
    append_prompt_caching_middleware(middleware)
    middleware = _apply_excluded_middleware(middleware, profile)
    # Its answer's Azure links are checked against its tools' results, as every
    # subagent's are.
    middleware = _apply_custom_middleware(
        middleware, [AzureLinksMiddleware(label="sttm-agent's answer")], core_names=core
    )
    middleware = _apply_excluded_middleware(middleware, profile)
    if profile.excluded_tools:
        middleware.append(_ToolExclusionMiddleware(excluded=profile.excluded_tools))
    return create_agent(
        model,
        system_prompt=_apply_profile_prompt(profile, STTM_SUBAGENT_PROMPT),
        tools=_apply_tool_description_overrides(STTM_TOOLS, profile.tool_description_overrides),
        middleware=middleware,
        name=NAME,
        response_format=None,
        state_schema=state_schema,
    )


def install(graph: Runnable) -> None:
    """Set the graph the sttm-agent runs (:func:`build_sttm_graph`)."""

    STTM_RUNNABLE.graph = graph


__all__ = ["NAME", "STTM_RUNNABLE", "SttmAgent", "build_sttm_graph", "install", "prepare"]
