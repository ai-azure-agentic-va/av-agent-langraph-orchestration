from __future__ import annotations

import asyncio
import logging
import threading
from typing import Annotated, Any, NotRequired, Required

from deepagents import (
    DeepAgentState,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends.composite import CompositeBackend
from deepagents.backends.state import StateBackend
from langchain.agents.middleware.context_editing import (
    ClearToolUsesEdit,
    ContextEditingMiddleware,
)
from langchain_core.messages import AnyMessage
from langchain_openai import AzureChatOpenAI
from langgraph.graph.message import add_messages

from v1.core.config import get_settings
from v1.core.middlewares.citation_guard import CitationGuardMiddleware
from v1.core.middlewares.safety import SafetyGateMiddleware
from v1.core.middlewares.servicenow_access import ServiceNowAccessMiddleware
from v1.core.middlewares.sliding_window import SlidingWindowFloorMiddleware
from v1.core.middlewares.source_accumulator import SourceAccumulatorMiddleware
from v1.core.prompts import SYSTEM_PROMPT
from v1.core.skills import SKILLS_MOUNT, SKILLS_SOURCES, build_skills_backend
from v1.core.subagents import SERVICENOW_SUBAGENT, close_servicenow_resources
from v1.core.tools import (
    ai_search_tool,
    close_search_clients,
)
from v1.core.tools.ai_search.ai_search import source_key
from v1.utils.azure_credentials import get_async_token_provider, get_token_provider
from v1.utils.checkpointer import close_checkpointer, get_checkpointer

logger = logging.getLogger(__name__)
settings = get_settings()


# Cap the cumulative referenced-source list so a very long thread's checkpoint does
# not grow without bound. Sources are append-only and an inline [n] can point at any
# earlier number, so there is no fully safe truncation; 500 is far above any
# realistic conversation (the UI collapses the list precisely because it can grow
# long). If ever exceeded, keep the most recently numbered sources.
_MAX_ACCUMULATED_SOURCES = 500


def merge_sources(existing: list[dict] | None, new: list[dict] | None) -> list[dict]:
    """Reducer for the append-only ``all_sources`` channel.

    Union the conversation's referenced sources with a turn's, de-duped by
    :func:`source_key`, keeping each document's conversation-stable ``index`` and
    ordering ascending by it. New payloads win (freshest metadata); the index does
    not shift because the tool assigns it once per source and re-emits it. This is a
    plain merge reducer — NEVER a ``DeltaChannel`` — so it does not reintroduce the
    replay-400 that :class:`ChatAgentState` exists to avoid.
    """

    combined: "dict[str, dict]" = {}
    for doc in existing or []:
        combined[source_key(doc)] = doc
    for doc in new or []:
        combined[source_key(doc)] = doc
    ordered = sorted(combined.values(), key=lambda d: d.get("index") or 0)
    if len(ordered) > _MAX_ACCUMULATED_SOURCES:
        ordered = ordered[-_MAX_ACCUMULATED_SOURCES:]
    return ordered


class ChatAgentState(DeepAgentState):
    """``DeepAgentState`` with the STANDARD ``add_messages`` channel on ``messages``.

    deepagents' ``DeepAgentState`` annotates ``messages`` with langgraph's *beta*
    ``DeltaChannel`` (``snapshot_frequency=50``) to shrink checkpoint growth from
    O(N²) to O(N). That channel does not store the message list — it stores only
    the per-superstep *writes* and periodic ``_DeltaSnapshot`` blobs, then
    rebuilds state by REPLAYING those writes through ``_messages_delta_reducer``
    on every read (``/threads/{id}/state`` and ``/threads/{id}/history``).

    Once a thread crosses the 50-update snapshot boundary (~10-16 turns of tool
    calls + subagent + summarization writes), a ``_DeltaSnapshot`` blob
    deserializes back as a bare ``{"value": [...]}`` dict and gets fed into the
    reducer as if it were a message write. ``convert_to_messages`` then raises
    ``ValueError: Message dict must contain 'role' and 'content' keys`` and the
    platform returns HTTP 400 for EVERY state/history read of that thread. The UI
    (``useStream({ fetchStateHistory: true })``) loads a thread via those
    endpoints, so the conversation renders blank and its history is unrecoverable
    even though new turns still stream fine over SSE (a separate code path that
    never replays history).

    Overriding ``messages`` back to the stable ``add_messages`` reducer swaps the
    ``DeltaChannel`` for a plain ``BinaryOperatorAggregate`` that stores the full
    list per checkpoint — the channel langgraph has always used and that the
    platform serializes without any replay/coercion step. All other
    ``DeepAgentState`` fields are inherited unchanged, so the rest of the
    deepagents contract (files, todos, summarization event) is preserved.
    """

    messages: Required[Annotated[list[AnyMessage], add_messages]]
    # Append-only cumulative "Referenced Sources" for the WHOLE conversation: every
    # document ai_search has surfaced, numbered 1..n and de-duped by source_key.
    # SourceAccumulatorMiddleware writes it after each turn; ai_search reads it back
    # (InjectedState) to seed conversation-stable [n] numbering, and CitationGuard
    # validates markers against it. Durable (checkpointed graph state) so numbering
    # survives a process restart. NotRequired: a new thread has none until the first
    # search. Merged by `merge_sources` (a plain reducer, not a DeltaChannel).
    all_sources: NotRequired[Annotated[list[dict], merge_sources]]


def build_azure_chat_model() -> AzureChatOpenAI:
    logger.info(
        "Building AzureChatOpenAI model with endpoint: %s, deployment: %s, api_version: %s, managed_identity: %s, max_tokens: %s",
        settings.endpoint,
        settings.chat_deployment,
        settings.api_version,
        settings.use_managed_identity,
        settings.ai_llm_default_max_tokens,
    )
    kwargs: dict[str, Any] = {
        "azure_endpoint": settings.endpoint,
        "azure_deployment": settings.chat_deployment,
        "api_version": settings.api_version,
        # Cap the completion length. langchain-openai maps `max_tokens` to the
        # API's `max_completion_tokens`, the field gpt-5 / reasoning deployments
        # accept (they 400 on the legacy `max_tokens`).
        "max_tokens": settings.ai_llm_default_max_tokens,
    }
    # Only send `temperature` when explicitly configured: reasoning / gpt-5 chat
    # deployments 400 on any non-default temperature, so omitting it lets the
    # model use its own default.
    if settings.ai_llm_default_temperature is not None:
        kwargs["temperature"] = settings.ai_llm_default_temperature
    if settings.use_managed_identity:
        # Provide both: the sync client uses the sync provider, the async client
        # (used by the LangGraph runtime) uses the thread-offloaded async one so the
        # blocking token acquisition never runs on the event loop.
        kwargs["azure_ad_token_provider"] = get_token_provider(settings.azure_openai_scope)
        kwargs["azure_ad_async_token_provider"] = get_async_token_provider(settings.azure_openai_scope)
    else:
        kwargs["api_key"] = settings.api_key

    return AzureChatOpenAI(**kwargs)


_chat_model: AzureChatOpenAI | None = None
_chat_model_lock = threading.Lock()


def get_azure_chat_model() -> AzureChatOpenAI:
    """Return the process-wide ``AzureChatOpenAI`` singleton, building it once.

    ``build_agent`` runs on every request, so constructing the model there spun
    up a new client (and a fresh HTTP connection pool) per request. The model is
    a stateless config wrapper over the OpenAI client and is safe to share, so we
    build it once and reuse it; ``create_deep_agent`` binds tools to a derived
    copy without mutating this instance.
    """

    global _chat_model
    if _chat_model is None:
        with _chat_model_lock:
            if _chat_model is None:
                _chat_model = build_azure_chat_model()
    return _chat_model


def build_backend() -> CompositeBackend:
    """Agent backend: in-memory by default, with the skills library on disk.

    The default ``StateBackend`` keeps the agent's file operations ephemeral
    and per-session (the right choice for an API-served graph). The skills
    library lives on disk and is mounted read-side at :data:`SKILLS_MOUNT` via
    a scoped ``FilesystemBackend`` so ``SkillsMiddleware`` (and the agent's
    ``read_file`` on a skill path) can resolve it. ``StateBackend`` holds no
    per-session data — it reads and writes the checkpointed ``files`` channel
    (keyed by ``thread_id``) via ``get_config()`` — so a single instance is safe
    to share; session isolation comes from the checkpointer, not the backend.
    """
    return CompositeBackend(
        default=StateBackend(),
        routes={SKILLS_MOUNT: build_skills_backend()},
    )


_agent: Any | None = None
_agent_lock = asyncio.Lock()


async def build_agent(config=None) -> Any:
    """Return the process-wide compiled agent, building it once.

    LangGraph re-invokes this factory on every run (a callable graph entry is
    always classified as a per-request factory), but nothing here varies per
    request: the model, checkpointer, backend, skills, tools and middleware are
    all process-global, and ``StateBackend`` keeps no per-session data. So we
    compile the graph once and hand back the same instance; under the LangGraph
    platform the per-run checkpointer/store are injected into a shallow copy
    downstream, so sharing the base graph is safe.
    """

    global _agent
    if _agent is None:
        async with _agent_lock:
            if _agent is None:
                # PERSISTENCE_BACKEND="memory" -> in-memory; else Postgres.
                checkpointer = await get_checkpointer(
                    settings.persistence_backend, settings.postgress_url
                )
                # ``create_deep_agent`` plus the skills/backend construction do
                # blocking filesystem work (FilesystemBackend path resolution,
                # SkillsMiddleware reading SKILL.md). Offload to a worker thread
                # so no blocking I/O runs on the event loop. Assign only on
                # success so a transient build failure is not cached.
                _agent = await asyncio.to_thread(_build_agent_sync, checkpointer)
    return _agent


# Fallback model input budget (gpt-5.1 / gpt-5 expose max_input_tokens=272000).
# Used only when neither AI_LLM_MAX_INPUT_TOKENS nor model.profile resolves a limit
# (e.g. a custom Azure deployment name), so the sliding-window floor always has a base.
_DEFAULT_MAX_INPUT_TOKENS = 272000


def _resolve_max_input_tokens(model: AzureChatOpenAI) -> int:
    """Absolute input-token budget for the sliding-window floor.

    Prefers the explicit ``AI_LLM_MAX_INPUT_TOKENS`` override, then the model's
    profile (``max_input_tokens``, which only resolves for recognized deployment
    names like ``gpt-5.1``), and finally a safe default. Computing this here — from
    the shared model instance — keeps ``SlidingWindowFloorMiddleware`` independent of
    whether the profile resolves at runtime.
    """

    if settings.ai_llm_max_input_tokens:
        return settings.ai_llm_max_input_tokens
    profile = getattr(model, "profile", None)
    if isinstance(profile, dict):
        profile_limit = profile.get("max_input_tokens")
        if isinstance(profile_limit, int) and profile_limit > 0:
            return profile_limit
    return _DEFAULT_MAX_INPUT_TOKENS


def _build_agent_sync(checkpointer: Any) -> Any:
    model = get_azure_chat_model()
    _ensure_harness_profiles_registered()

    # Long-conversation layered defense (see docs/research long-context strategy):
    # ContextEditingMiddleware performs SELECTIVE RETENTION — above
    # CONTEXT_EDIT_TRIGGER_TOKENS it clears the bodies of older tool results
    # (ai_search grounding, ServiceNow cards) to a placeholder in the model view
    # only, keeping the newest CONTEXT_EDIT_KEEP_TOOL_RESULTS intact; it deep-copies
    # the view so persisted messages/artifacts (e.g. citation sources) are untouched.
    # SlidingWindowFloorMiddleware is the hard SAFETY FLOOR — a last-resort per-call
    # ceiling that trims the oldest messages if a request still exceeds the window.
    floor_tokens = int(_resolve_max_input_tokens(model) * settings.context_window_floor_fraction)
    logger.info(
        "Context floor: max_input_tokens=%s, floor_fraction=%s -> floor_tokens=%s; "
        "context-edit trigger=%s keep=%s",
        _resolve_max_input_tokens(model),
        settings.context_window_floor_fraction,
        floor_tokens,
        settings.context_edit_trigger_tokens,
        settings.context_edit_keep_tool_results,
    )

    agent = create_deep_agent(
        model=model,
        # Use a state schema whose `messages` channel is the stable
        # `add_messages` reducer instead of the beta `DeltaChannel` default (see
        # ChatAgentState) — the DeltaChannel replay path 400s every state/history
        # read once a thread passes ~50 message writes, blanking the UI.
        state_schema=ChatAgentState,
        tools=[
            ai_search_tool,
        ],
        subagents=[
            SERVICENOW_SUBAGENT,
        ],
        # Order matters: first entry = OUTERMOST, last = INNERMOST (closest to the
        # model). deepagents appends these AFTER its SummarizationMiddleware, so both
        # context-management layers below see the post-summarization effective view.
        middleware=[
            # Outermost: post-processes the fully assembled model response to strip
            # citation markers the model invented (numbers not in the conversation's
            # cumulative ai_search set), so no orphan [n] is persisted or shown raw.
            CitationGuardMiddleware(),
            # After each turn, persist this turn's ai_search documents into the
            # cumulative `all_sources` channel (append-only, de-duped). That state
            # seeds next turn's conversation-stable [n] numbering and backs the
            # collapsed Referenced Sources panel.
            SourceAccumulatorMiddleware(),
            SafetyGateMiddleware(),
            # Per-request gate: for callers in SERVICENOW_DISABLED_GROUPS (e.g.
            # external-group members) strips the `task` delegation tool, appends a
            # restriction note, and hard-blocks ServiceNow delegation. Sits inner
            # of deepagents' SubAgentMiddleware so it sees the assembled request.
            ServiceNowAccessMiddleware(),
            # Selective retention: clear stale tool-result bodies before the model
            # call (view-only; persisted artifacts / citations preserved).
            ContextEditingMiddleware(
                edits=[
                    ClearToolUsesEdit(
                        trigger=settings.context_edit_trigger_tokens,
                        keep=settings.context_edit_keep_tool_results,
                    )
                ]
            ),
            # Sliding-window safety floor: innermost, last-resort hard ceiling.
            SlidingWindowFloorMiddleware(max_tokens=floor_tokens),
        ],
        system_prompt=SYSTEM_PROMPT,
        backend=build_backend(),
        skills=SKILLS_SOURCES,
        checkpointer=checkpointer,
    )
    # Enforce a hard step ceiling. Without a configured recursion_limit the
    # parent loop runs at the LangGraph default (25); wiring agent_max_steps here
    # makes AGENT_MAX_STEPS the single authoritative knob (and stops a runaway
    # tool loop from running unbounded). ``with_config`` returns a Pregel copy,
    # not a RunnableBinding, so the platform's downstream checkpointer/store
    # injection still works.
    return agent.with_config({"recursion_limit": settings.agent_max_steps})
# Middleware from the deepagents default stack we strip via the harness profile.
# ``create_deep_agent`` builds these in; ``_apply_excluded_middleware`` then drops
# the ones named here. deepagents' strict coverage validator ABORTS startup if any
# exclusion matches NOTHING in the assembled stack, so every entry here must be a
# middleware actually present at the DEPLOYED version. We deliberately keep
# SummarizationMiddleware ENABLED (not listed) so long conversations and large tool
# results compact instead of overflowing the model's context window.
#   - PatchToolCallsMiddleware: keeps tool-call payloads verbatim for determinism.
#   - AnthropicPromptCachingMiddleware: no-op for Azure OpenAI (Anthropic-only).
# NOTE: TodoListMiddleware is intentionally NOT excluded. The langgraph-api base
# image ships deepagents 0.7.4, which no longer adds TodoListMiddleware to the
# default stack (0.6.10 did). Excluding an absent middleware trips the strict
# validator and crashes boot ("excluded_middleware entries matched no middleware").
# The todo tool is already forbidden by the orchestrator system prompt, so dropping
# the exclusion is safe on both versions (absent on 0.7.4; present-but-unused on the
# 0.6.10 that uv.lock pins locally).
_EXCLUDED_MIDDLEWARE = frozenset(
    {
        "PatchToolCallsMiddleware",
        "AnthropicPromptCachingMiddleware",
    }
)

# Provider key for the model :func:`build_azure_chat_model` returns:
# ``AzureChatOpenAI`` resolves to ``"azure"`` (its LangSmith provider key).
_HARNESS_PROFILE_KEYS = ("azure",)

_harness_profiles_lock = threading.Lock()
_harness_profiles_registered = False


def _ensure_harness_profiles_registered() -> None:
    """Register the middleware exclusions once, before the agent is built.

    Registration mutates a process-global registry that ``create_deep_agent``
    consults when resolving the model's profile, so it must run before the
    build. Union-merge semantics make repeat calls idempotent; the
    lock-guarded flag keeps it to a single registration and avoids the SDK's
    per-merge log line.
    """

    global _harness_profiles_registered
    if _harness_profiles_registered:
        return
    with _harness_profiles_lock:
        if _harness_profiles_registered:
            return
        profile = HarnessProfile(excluded_middleware=_EXCLUDED_MIDDLEWARE)
        for key in _HARNESS_PROFILE_KEYS:
            register_harness_profile(key, profile)
        _harness_profiles_registered = True

async def close_agent_resources() -> None:
    from v1.utils.azure_key_vault import aclose_default_kv

    await close_servicenow_resources()
    close_search_clients()
    await close_checkpointer()
    # Shared async Key Vault client/credential (used by ServiceNow secret
    # resolution and any other aresolve_env_secret callers).
    await aclose_default_kv()
