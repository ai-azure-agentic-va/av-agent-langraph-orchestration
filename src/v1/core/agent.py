from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import threading

from deepagents import HarnessProfile, create_deep_agent, register_harness_profile
from deepagents.backends.composite import CompositeBackend
from deepagents.backends.state import StateBackend
from langchain_openai import AzureChatOpenAI
from v1.core.config import get_settings
from v1.utils.azure_credentials import get_async_token_provider, get_token_provider
from v1.core.tools import (
    ai_search_tool,
    close_search_clients,
)
from v1.core.skills import SKILLS_MOUNT, SKILLS_SOURCES, build_skills_backend
from v1.core.subagents import (
    ADF_SUBAGENT,
    ADLS_SUBAGENT,
    SERVICENOW_SUBAGENT,
    close_adf_resources,
    close_adls_resources,
    close_servicenow_resources,
)
from v1.core.middlewares.citations import CitationFilterMiddleware
from v1.core.middlewares.content_filter import ContentFilterRefusalMiddleware
from v1.core.middlewares.verdict_guard import VerdictGuardMiddleware
from v1.core.middlewares.safety import SafetyGateMiddleware
from v1.core.middlewares.subagent_access import SubagentAccessMiddleware
from v1.core.prompts import ADF_ROUTING_BLOCK, ADLS_ROUTING_BLOCK, SYSTEM_PROMPT
from v1.utils.checkpointer import close_checkpointer, get_checkpointer

logger = logging.getLogger(__name__)
settings = get_settings()


def build_azure_chat_model() -> AzureChatOpenAI:
    logger.info(
        "Building AzureChatOpenAI model with endpoint: %s, deployment: %s, api_version: %s, managed_identity: %s",
        settings.endpoint,
        settings.chat_deployment,
        settings.api_version,
        settings.use_managed_identity,
    )
    kwargs: dict[str, Any] = {
        "azure_endpoint": settings.endpoint,
        "azure_deployment": settings.chat_deployment,
        "api_version": settings.api_version,
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


def _build_agent_sync(checkpointer: Any) -> Any:
    model = get_azure_chat_model()
    _ensure_harness_profiles_registered()
    # The ADF and ADLS subagents are wired only when their backing resource is
    # configured; each system-prompt routing block is appended in lockstep, so a
    # deployment never hears about a capability it does not have.
    optional = (
        (ADF_SUBAGENT, ADF_ROUTING_BLOCK, bool(settings.adf_factory_mapping)),
        (ADLS_SUBAGENT, ADLS_ROUTING_BLOCK, bool(settings.adls_account_mapping)),
    )
    subagents = [SERVICENOW_SUBAGENT] + [sub for sub, _, on in optional if on]
    system_prompt = "\n\n".join(
        [SYSTEM_PROMPT] + [block for _, block, on in optional if on]
    )
    # A skipped subagent is otherwise invisible from the outside (the model just
    # routes its questions to ai_search_tool), so state the roster — and what
    # would re-enable a missing member — once at build time.
    logger.info(
        "Registered subagents: %s", ", ".join(sub["name"] for sub in subagents)
    )
    for sub, _, on in optional:
        if not on:
            logger.warning(
                "Subagent %r NOT registered: %s is empty; its questions will fall "
                "through to ai_search_tool.",
                sub["name"],
                "ADF_FACTORY_MAPPING" if sub is ADF_SUBAGENT else "ADLS_ACCOUNT_MAPPING",
            )

    def _create() -> Any:
        return create_deep_agent(
            model=model,
            tools=[
                ai_search_tool,
            ],
            subagents=subagents,
            middleware=[
                SafetyGateMiddleware(),
                # Converts an Azure content-filter 400 on the model call into a
                # normal refusal message instead of letting it kill the run (the
                # caller would otherwise get a run-level error and no message).
                ContentFilterRefusalMiddleware(),
                # Per-request gate: for callers in SERVICENOW_DISABLED_GROUPS /
                # ADF_DISABLED_GROUPS (e.g. external users) appends restriction
                # notes, hard-blocks delegation to the disabled subagents, and drops
                # the `task` tool entirely when every subagent is disabled. Sits
                # inner of deepagents' SubAgentMiddleware so it sees the assembled
                # request.
                SubagentAccessMiddleware(),
                # Runs after the answer to emit a `sources_final` event holding only
                # the sources the model cited inline (the streamed `search_complete`
                # chips include every retrieved doc, cited or not).
                CitationFilterMiddleware(),
                # Deterministic backstop for the no-verdict contract: replaces a
                # bare yes/no answer to a timeliness/completeness question with the
                # facts-plus-deferral format (prompts alone hold only per-sample).
                VerdictGuardMiddleware(),
            ],
            system_prompt=system_prompt,
            backend=build_backend(),
            skills=SKILLS_SOURCES,
            checkpointer=checkpointer,
        )

    try:
        agent = _create()
    except ValueError as exc:
        if _drop_stale_middleware_exclusions(exc) is None:
            raise
        agent = _create()
    # Enforce a hard step ceiling. Without a configured recursion_limit the
    # parent loop runs at the LangGraph default (25); wiring agent_max_steps here
    # makes AGENT_MAX_STEPS the single authoritative knob (and stops a runaway
    # tool loop from running unbounded). ``with_config`` returns a Pregel copy,
    # not a RunnableBinding, so the platform's downstream checkpointer/store
    # injection still works.
    return agent.with_config({"recursion_limit": settings.agent_max_steps})
# Middleware from the deepagents default stack we strip via the harness profile.
# ``create_deep_agent`` always builds each of these in; ``_apply_excluded_middleware``
# then drops the names listed here. We deliberately keep SummarizationMiddleware
# ENABLED (not listed) so long conversations and large tool results compact instead
# of overflowing the model's context window — the SDK wires it with model-aware
# trigger/keep thresholds and offloads evicted history to our backend.
#   - TodoListMiddleware: the orchestrator prompt forbids the todo tool.
#   - PatchToolCallsMiddleware: keeps tool-call payloads verbatim for determinism.
#   - AnthropicPromptCachingMiddleware: no-op for Azure OpenAI (Anthropic-only).
_EXCLUDED_MIDDLEWARE = frozenset(
    {
        "TodoListMiddleware",
        "PatchToolCallsMiddleware",
        "AnthropicPromptCachingMiddleware",
    }
)

# Provider keys for the models :func:`build_chat_model` returns: ``AzureChatOpenAI``
# resolves to ``"azure"``; the offline ``KeywordPlannerModel`` to
# ``"keywordplannermodel"`` (derived from its class name).
_HARNESS_PROFILE_KEYS = ("azure", "keywordplannermodel")

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


def _drop_stale_middleware_exclusions(exc: ValueError) -> frozenset[str] | None:
    """Re-register the harness profile without stale exclusions, or return None.

    ``create_deep_agent`` raises ``ValueError`` when an ``excluded_middleware``
    entry matches nothing in the assembled stack. That guard turned a deepagents
    version drift into a startup crash-loop (the 2026-08-02 outage: an image
    whose deepagents no longer built ``TodoListMiddleware``). Running with a
    stale exclusion's middleware back in the stack is degraded but working;
    refusing to boot is not — so parse the offenders out of the message, shrink
    the registered profile, and let the caller rebuild once. Returns the
    surviving exclusion set, or None when ``exc`` is some other ``ValueError``
    (the caller re-raises).
    """

    message = str(exc)
    if "matched no middleware" not in message:
        return None
    stale = set(re.findall(r"'([^']+)' \(string\)", message)) & _EXCLUDED_MIDDLEWARE
    # An unparseable or unrecognized message still means coverage failed; fail
    # open by dropping every exclusion rather than crash-looping.
    surviving = frozenset(_EXCLUDED_MIDDLEWARE - stale) if stale else frozenset()
    logger.warning(
        "excluded_middleware is stale for the installed deepagents (%s); "
        "rebuilding with exclusions %s. The dropped middleware is back in the "
        "stack — re-pin deepagents or update _EXCLUDED_MIDDLEWARE.",
        message,
        sorted(surviving) if surviving else "disabled entirely",
    )
    # Registration is union-merge, so shrinking requires clearing our keys from
    # the (private) registry first. If its shape ever changes, surface the
    # original error rather than a confusing secondary one.
    try:
        from deepagents.profiles.harness.harness_profiles import _HARNESS_PROFILES
        for key in _HARNESS_PROFILE_KEYS:
            _HARNESS_PROFILES.pop(key, None)
    except Exception:
        raise exc
    if surviving:
        profile = HarnessProfile(excluded_middleware=surviving)
        for key in _HARNESS_PROFILE_KEYS:
            register_harness_profile(key, profile)
    return surviving

async def close_agent_resources() -> None:
    from v1.utils.azure_credentials import aclose_async_azure_credential
    from v1.utils.azure_key_vault import aclose_default_kv

    await close_servicenow_resources()
    await close_adf_resources()
    await close_adls_resources()
    close_search_clients()
    await close_checkpointer()
    # Shared async Key Vault client/credential (used by ServiceNow secret
    # resolution and any other aresolve_env_secret callers).
    await aclose_default_kv()
    # Shared async DefaultAzureCredential (used by the ADF management clients);
    # closed after the clients that hold it.
    await aclose_async_azure_credential()
