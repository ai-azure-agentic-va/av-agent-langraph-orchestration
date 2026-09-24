from __future__ import annotations

import asyncio
import functools
import logging
import threading
from typing import Annotated, Any, NotRequired

from deepagents import (
    DeepAgentState,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends.composite import CompositeBackend
from deepagents.backends.state import StateBackend
from deepagents.middleware.summarization import _DeepAgentsSummarizationMiddleware
from langchain.agents.middleware.context_editing import (
    ClearToolUsesEdit,
    ContextEditingMiddleware,
)
from langchain_openai import AzureChatOpenAI
from langgraph.errors import GraphBubbleUp

from v1.core.config import get_settings
from v1.core.middlewares.citation_guard import CitationGuardMiddleware
from v1.core.middlewares.model_error_guard import ModelErrorGuardMiddleware
from v1.core.middlewares.safety import SafetyGateMiddleware
from v1.core.middlewares.sliding_window import SlidingWindowFloorMiddleware
from v1.core.middlewares.source_accumulator import SourceAccumulatorMiddleware
from v1.core.middlewares.subagent_access import SubagentAccessMiddleware
from v1.core.middlewares.user_context import UserContextMiddleware
from v1.core.prompts import SYSTEM_PROMPT
from v1.core.skills import SKILLS_MOUNT, SKILLS_SOURCES, build_skills_backend
from v1.core.subagents import (
    ENABLED_SUBAGENTS,
    close_adf_resources,
    close_servicenow_resources,
)
from v1.core.tools import (
    ai_search_tool,
    close_search_clients,
)
from v1.core.tools.ai_search.ai_search import source_key
from v1.core.tools.url_reader import read_url_tool
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
    plain merge reducer, not a ``DeltaChannel``: the list is small and bounded by
    ``_MAX_ACCUMULATED_SOURCES``, so there is nothing to gain from delta encoding.
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
    """``DeepAgentState`` plus the cumulative ``all_sources`` channel. Nothing else.

    ``messages`` is deliberately NOT overridden here — it is inherited from
    ``DeepAgentState``, which annotates it with langgraph's ``DeltaChannel``
    (``snapshot_frequency=50``) to keep checkpoint growth O(N) instead of O(N²).
    That is the reducer deepagents' own built-ins (summarization, subagent writes)
    are written against, so leaving it alone keeps us on the supported path.

    History (do not re-add the override): this class used to force ``messages``
    back to the stable ``add_messages`` reducer, because through langgraph-api
    0.13.0 every ``/threads/{id}/state`` and ``/threads/{id}/history`` read of a
    thread past the 50-update snapshot boundary returned HTTP 400 — the platform
    checkpointer mishandled the delta replay and ``convert_to_messages`` raised
    ``Message dict must contain 'role' and 'content' keys``, so the UI
    (``useStream({ fetchStateHistory: true })``) rendered those conversations
    blank. The override made reads succeed but skipped langgraph's replay
    annotation entirely, so a thread's earlier messages were no longer rendered
    into the response — and writing a new turn onto that empty list persisted the
    empty array, which is how history was actually being lost. The platform bug is
    fixed in langgraph-api 0.14.1+; the Dockerfile pins 0.14.3 for it. The image
    bump and this removal must ship together.
    """

    # Append-only cumulative "Referenced Sources" for the WHOLE conversation: every
    # document ai_search has surfaced, numbered 1..n and de-duped by source_key.
    # SourceAccumulatorMiddleware writes it after each turn; ai_search reads it back
    # (InjectedState) to seed conversation-stable [n] numbering, and CitationGuard
    # validates markers against it. Durable (checkpointed graph state) so numbering
    # survives a process restart. NotRequired: a new thread has none until the first
    # search. Merged by `merge_sources`.
    all_sources: NotRequired[Annotated[list[dict], merge_sources]]


def build_azure_chat_model() -> AzureChatOpenAI:
    logger.info(
        "Building AzureChatOpenAI model with endpoint: %s, deployment: %s, api_version: %s, managed_identity: %s, max_tokens: %s, reasoning_effort: %s, temperature: %s",
        settings.endpoint,
        settings.chat_deployment,
        settings.api_version,
        settings.use_managed_identity,
        settings.ai_llm_default_max_tokens,
        settings.ai_llm_reasoning_effort,
        settings.ai_llm_default_temperature,
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
    # Reasoning effort. "none" turns gpt-5.x into a plain chat model (no hidden
    # reasoning tokens). Blank/unset omits the param entirely — REQUIRED for a
    # pre-reasoning deployment (gpt-4.1 on 2024-06-01 400s with "Unrecognized
    # request argument supplied: reasoning_effort" on ANY value, "none"
    # included), and the only way an env file can express it: pydantic reads an
    # empty `AI_LLM_REASONING_EFFORT=` as "", never as None.
    reasoning = (settings.ai_llm_reasoning_effort or "").strip() or None
    if reasoning is not None:
        kwargs["reasoning_effort"] = reasoning
    # Temperature and reasoning are MUTUALLY EXCLUSIVE on gpt-5.x (verified live
    # against gpt-5.2 on both 2024-10-21 and 2025-04-01-preview): with ANY real
    # reasoning effort (minimal/low/medium/high) the model rejects a non-default
    # temperature — "'temperature' does not support 0.2 ... Only the default (1)
    # value is supported" -> 400. A custom temperature is honoured ONLY when
    # reasoning is off (effort None or "none"). So send `temperature` only when
    # reasoning is off; when it's on, drop it (the model forces temperature=1
    # regardless) and warn if a non-default was configured, so a misconfig can
    # never 400 every call.
    reasoning_off = reasoning is None or str(reasoning).strip().lower() == "none"
    if settings.ai_llm_default_temperature is not None:
        if reasoning_off:
            kwargs["temperature"] = settings.ai_llm_default_temperature
        elif settings.ai_llm_default_temperature != 1:
            logger.warning(
                "Dropping AI_LLM_DEFAULT_TEMPERATURE=%s: reasoning_effort=%r is ON and "
                "gpt-5.x rejects any non-default temperature (it forces 1). Set "
                "AI_LLM_REASONING_EFFORT=none to use a custom temperature.",
                settings.ai_llm_default_temperature,
                reasoning,
            )
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
    _ensure_summary_failures_degrade()

    for name, setting_name in settings.unconfigured_capabilities:
        logger.warning(
            "Subagent %r NOT registered: %s is empty; its questions will fall "
            "through to ai_search_tool.",
            name,
            setting_name,
        )

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

    # Direct tools the orchestrator can call. read_url (fetch a page as Markdown)
    # is registered only when enabled — it is an SSRF surface, guarded in the tool.
    # Everything ServiceNow (incidents AND KB articles) lives on SERVICENOW_SUBAGENT.
    tools = [ai_search_tool]
    if settings.url_reader_enabled:
        tools.append(read_url_tool)

    agent = create_deep_agent(
        model=model,
        # Custom state schema ONLY to add the cumulative `all_sources` channel;
        # `messages` is deepagents' DeltaChannel default (see ChatAgentState).
        state_schema=ChatAgentState,
        tools=tools,
        subagents=ENABLED_SUBAGENTS,
        # Order matters: first entry = OUTERMOST, last = INNERMOST (closest to the
        # model). deepagents appends these AFTER its SummarizationMiddleware, so both
        # context-management layers below see the post-summarization effective view.
        middleware=[
            # Outermost: catch a model-call 400 (Azure content filter / bad request)
            # and return a graceful assistant message through the normal response
            # path, so the UI streams a readable reply instead of a raw 400 error.
            ModelErrorGuardMiddleware(),
            # Post-processes the fully assembled model response to strip citation
            # markers the model invented (numbers not in the conversation's
            # cumulative ai_search set), so no orphan [n] is persisted or shown raw.
            CitationGuardMiddleware(),
            # After each turn, persist this turn's ai_search documents into the
            # cumulative `all_sources` channel (append-only, de-duped). That state
            # seeds next turn's conversation-stable [n] numbering and backs the
            # collapsed Referenced Sources panel.
            SourceAccumulatorMiddleware(),
            SafetyGateMiddleware(),
            # Appends the signed-in user's name, email, and AD group names (from
            # the JWT principal) to the system prompt. Outer of SubagentAccess so
            # the ADF restriction note still lands last.
            UserContextMiddleware(),
            # Per-request gate: for callers in ADF_DISABLED_GROUPS appends a
            # restriction note and hard-blocks delegation to the disabled
            # subagent. Sits inner of deepagents' SubAgentMiddleware so it sees
            # the assembled request.
            SubagentAccessMiddleware(),
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
# NOTE: TodoListMiddleware is intentionally NOT excluded. deepagents 0.7.x (pinned
# in pyproject.toml — the base image ships no deepagents of its own) no longer adds
# TodoListMiddleware to the default stack, and excluding an absent middleware trips
# the strict validator and crashes boot ("excluded_middleware entries matched no
# middleware"). The todo tool is already forbidden by the orchestrator system
# prompt, so dropping the exclusion is safe either way.
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


# langchain 1.4.2 made SummarizationMiddleware._(a)create_summary RAISE once the
# summary model's retries are exhausted (1.3.x caught the error and returned an
# "Error generating summary" string instead). deepagents' summarization middleware
# delegates to that helper and sits OUTSIDE ModelErrorGuardMiddleware, so a
# transient summary-model failure (429, timeout, content filter on old history)
# would now fail the whole turn -- and only ever on the long threads that trigger
# summarization. Restore degrade-don't-die: log it and return a neutral placeholder
# so the turn still runs. The pre-summary history is still offloaded to
# /conversation_history/ and the summary message still carries that path, so
# grep-based recall keeps working.
_SUMMARY_FALLBACK = (
    "(Summary unavailable: the earlier part of this conversation could not be summarized.)"
)

_summary_guard_lock = threading.Lock()


def _ensure_summary_failures_degrade() -> None:
    """Patch deepagents' summary creation, once, to degrade instead of raise.

    Patches the class (not an instance) because ``create_deep_agent`` builds the
    middleware internally. Raises if deepagents renamed the methods, so a future
    bump fails at boot instead of silently losing the guard.
    """

    cls = _DeepAgentsSummarizationMiddleware
    with _summary_guard_lock:
        create = getattr(cls, "_create_summary", None)
        acreate = getattr(cls, "_acreate_summary", None)
        if create is None or acreate is None:
            raise RuntimeError(
                "deepagents _DeepAgentsSummarizationMiddleware no longer has "
                "_create_summary/_acreate_summary; re-check the summary-failure "
                "guard in v1.core.agent before bumping deepagents."
            )
        if getattr(acreate, "_degrades_on_failure", False):
            return

        @functools.wraps(create)
        def _create_summary(self: Any, messages_to_summarize: list[Any]) -> str:
            try:
                return create(self, messages_to_summarize)
            except GraphBubbleUp:
                raise
            except Exception:
                logger.warning(
                    "Conversation summary failed; continuing with a placeholder summary.",
                    exc_info=True,
                )
                return _SUMMARY_FALLBACK

        @functools.wraps(acreate)
        async def _acreate_summary(self: Any, messages_to_summarize: list[Any]) -> str:
            try:
                return await acreate(self, messages_to_summarize)
            except GraphBubbleUp:
                raise
            except Exception:
                logger.warning(
                    "Conversation summary failed; continuing with a placeholder summary.",
                    exc_info=True,
                )
                return _SUMMARY_FALLBACK

        _create_summary._degrades_on_failure = True  # type: ignore[attr-defined]
        _acreate_summary._degrades_on_failure = True  # type: ignore[attr-defined]
        cls._create_summary = _create_summary
        cls._acreate_summary = _acreate_summary


async def close_agent_resources() -> None:
    from v1.utils.azure_key_vault import aclose_default_kv

    # Each closer drops its own private client cache, so they are independent.
    # return_exceptions keeps one failing closer from stranding the others'
    # sockets and pool handles.
    closers = {
        "servicenow": close_servicenow_resources(),
        "adf": close_adf_resources(),
        "checkpointer": close_checkpointer(),
        # Shared async Key Vault client/credential (used by ServiceNow secret
        # resolution and any other aresolve_env_secret callers).
        "key vault": aclose_default_kv(),
    }
    results = await asyncio.gather(*closers.values(), return_exceptions=True)
    for name, result in zip(closers, results, strict=True):
        if isinstance(result, BaseException):
            logger.warning("Error closing %s resources", name, exc_info=result)

    close_search_clients()
