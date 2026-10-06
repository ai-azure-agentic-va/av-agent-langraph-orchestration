
from __future__ import annotations
from functools import lru_cache
from typing import Annotated
from pydantic import Field, AliasChoices, BeforeValidator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from v1.utils.helper import _split_csv
StringList = Annotated[list[str], BeforeValidator(_split_csv), NoDecode]

class Settings(BaseSettings):
    """Runtime configuration for the demo backend."""
    postgress_url: str = Field(default="postgresql://postgres:postgres@localhost:5432/deepagent?sslmode=disable", alias="POSTGRESS_DATABASE_URL")
    persistence_backend: str = Field(default="memory", alias="PERSISTENCE_BACKEND")
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    agent_max_steps: int = Field(default=50, alias="AGENT_MAX_STEPS")

    tenant_group_index_mapping: dict[str, str] = Field(
        default_factory=dict,
        alias="TENANT_GROUP_INDEX_MAPPING",
        description=(
            "JSON object mapping Entra group IDs or names to Azure AI Search index names, "
            'for example: {"group-id": "search-index"}'
        ),
    )
    tenant_group_starter_prompts_mapping: dict[str, list[dict[str, str]]] = Field(
        default_factory=dict,
        alias="TENANT_GROUP_STARTER_PROMPTS_MAPPING",
        description=(
            "JSON object mapping Entra group IDs or names to a list of starter prompts "
            "({label, message}) shown in the chat UI for members of that group, e.g. "
            '{"group-id": [{"label": "...", "message": "..."}]}. Callers matching no '
            "mapped group get no starter prompts (there is no built-in fallback)."
        ),
    )

    #Azure Data Factory Config
    adf_factory_mapping: dict[str, dict[str, str]] = Field(
        default_factory=dict,
        alias="ADF_FACTORY_MAPPING",
        description=(
            "JSON object mapping a friendly factory alias to the coordinates of an "
            "Azure Data Factory the adf-agent may query, e.g. "
            '{"finance-dev": {"subscription_id": "...", "resource_group": "...", '
            '"factory_name": "..."}}. Empty (default) disables the ADF subagent '
            "entirely: it is not registered on the orchestrator and the system "
            "prompt carries no ADF routing text."
        ),
    )
    adf_default_factory: str | None = Field(
        default=None,
        alias="ADF_DEFAULT_FACTORY",
        description=(
            "Alias (a key of ADF_FACTORY_MAPPING) of the factory the ADF tools use "
            "when the caller does not name one. When exactly one factory is mapped "
            "it is the implicit default and this can stay unset; with several "
            "factories and no default the tools ask the model to pass factory=<alias>."
        ),
    )
    adf_disabled_groups: StringList = Field(
        default_factory=list,
        alias="ADF_DISABLED_GROUPS",
        description=(
            "Comma-separated Entra group object-ids or display names for which the "
            "Azure Data Factory subagent is DISABLED, matched the same way as "
            "TENANT_GROUP_INDEX_MAPPING keys. A caller whose groups intersect this set "
            "cannot delegate to the adf-agent; everyone else keeps it. Empty "
            "(default) leaves the ADF subagent enabled for everyone."
        ),
    )
    adf_write_enabled: bool = Field(
        default=False,
        alias="ADF_WRITE_ENABLED",
        description=(
            "Master switch for ADF mutations (trigger start/stop and pipeline rerun). "
            "False by default. Read-only ADF tools remain available when this is false."
        ),
    )
    adf_write_factory_allowlist: StringList = Field(
        default_factory=list,
        alias="ADF_WRITE_FACTORY_ALLOWLIST",
        description=(
            "Comma-separated ADF_FACTORY_MAPPING aliases on which mutation tools may "
            "operate. ADF_WRITE_ENABLED=true is not sufficient by itself: an empty "
            "allowlist denies every write, so each writable factory must be named "
            "explicitly."
        ),
    )
    #Openai Config
    # Reasoning effort for gpt-5.x deployments. "none" turns the model into a
    # plain (non-reasoning) chat model — no hidden reasoning tokens — AND is what
    # re-enables `temperature`: with reasoning ON these deployments 400 on any
    # non-default temperature, but with reasoning_effort="none" a custom
    # temperature is accepted. Set to a real effort ("minimal"/"low"/"medium"/
    # "high") to bring reasoning back. Leave BLANK (`AI_LLM_REASONING_EFFORT=`)
    # to omit the param entirely — mandatory on a pre-reasoning deployment such
    # as gpt-4.1, which 400s on the argument itself, "none" included.
    ai_llm_reasoning_effort: str | None = Field(
        default="none", alias="AI_LLM_REASONING_EFFORT"
    )
    # Sampling temperature sent to the chat model. Only honoured alongside
    # reasoning_effort="none" (see above); a reasoning-enabled gpt-5.x deployment
    # 400s on any non-default temperature. Leave unset (None) to omit it and use
    # the model default.
    ai_llm_default_temperature: float | None = Field(
        default=0.2, alias="AI_LLM_DEFAULT_TEMPERATURE"
    )
    # Cap on the model's output (completion) tokens per call. Passed to the chat
    # client as `max_tokens`, which langchain-openai serialises as
    # `max_completion_tokens` — the field gpt-5 / reasoning deployments require.
    ai_llm_default_max_tokens: int = Field(
        default=10000, alias="AI_LLM_DEFAULT_MAX_TOKENS"
    )

    # --- Safety gate --------------------------------------------------------
    # Layer 2b of the input safety gate (SafetyGateMiddleware): after the narrow
    # deterministic phrase filters, an LLM classifier with its own system prompt
    # judges whether the user's latest message is a prompt-extraction / jailbreak
    # attempt and blocks it with a clean refusal. It adds one short model call
    # before the agent runs, so it can be turned off where that latency/cost is
    # not wanted. The classifier FAILS OPEN: any error allows the request through
    # (Layer 1 prompt + Layer 2a phrase filters still apply), so a classifier
    # outage never takes the assistant down.
    safety_llm_classifier_enabled: bool = Field(
        default=True,
        alias="SAFETY_LLM_CLASSIFIER_ENABLED",
        description=(
            "Enable the LLM-based prompt-injection/extraction classifier in "
            "SafetyGateMiddleware (Layer 2b). Set false to rely on the prompt "
            "hardening (Layer 1) and the deterministic phrase filters (Layer 2a) "
            "alone — e.g. to avoid the extra per-request model call."
        ),
    )
    safety_classifier_timeout_seconds: float = Field(
        default=6.0,
        alias="SAFETY_CLASSIFIER_TIMEOUT_SECONDS",
        description=(
            "Hard wall-clock cap on the Layer 2b classifier call. The classifier "
            "runs before every non-blocked turn, so without a cap a slow/hung "
            "chat endpoint would stall the first token of EVERY answer. On timeout "
            "the classifier fails open (allows the request)."
        ),
    )
    # Layer 3 — Azure AI Content Safety Prompt Shields. Runs FIRST in the gate
    # (before the 2a phrase filters and the 2b LLM classifier) as the first line
    # of defense. DISABLED by default: the shieldPrompt call is a Content Safety
    # data-plane action that requires the app's managed identity to hold the
    # "Cognitive Services User" role on the endpoint resource — the existing
    # "Cognitive Services OpenAI User" role does NOT cover it. Ship the code with
    # this off, then flip it on once that role is granted. Like 2b, it FAILS OPEN.
    safety_prompt_shields_enabled: bool = Field(
        default=False,
        alias="SAFETY_PROMPT_SHIELDS_ENABLED",
        description=(
            "Enable the Layer 3 Azure Content Safety Prompt Shields input gate. "
            "Requires the app identity to have the 'Cognitive Services User' role "
            "on the Content Safety endpoint. Fails open on any error/timeout."
        ),
    )
    content_safety_endpoint: str | None = Field(
        default=None,
        alias="AZURE_CONTENT_SAFETY_ENDPOINT",
        description=(
            "Azure AI Content Safety endpoint for Prompt Shields. When unset, "
            "falls back to the Azure OpenAI endpoint — a multi-service Azure AI "
            "Services resource serves Content Safety on the same host, so no "
            "separate endpoint is needed."
        ),
    )
    content_safety_api_key: str | None = Field(
        default=None,
        alias="AZURE_CONTENT_SAFETY_API_KEY",
        description=(
            "Static key for Content Safety, used only when managed identity is "
            "off. With managed identity on (the default) the app uses AAD instead."
        ),
    )
    safety_prompt_shields_timeout_seconds: float = Field(
        default=4.0,
        alias="SAFETY_PROMPT_SHIELDS_TIMEOUT_SECONDS",
        description=(
            "Wall-clock cap on the Layer 3 Prompt Shields call. On timeout it "
            "fails open (allows the request)."
        ),
    )

    # --- read_url tool ------------------------------------------------------
    url_reader_enabled: bool = Field(
        default=False,
        alias="URL_READER_ENABLED",
        description=(
            "Register the read_url tool (fetch a web page as Markdown). OFF by "
            "default: it is an egress/SSRF surface (hardened with IP pinning + an "
            "internal-address screen, but still reaches arbitrary public hosts). "
            "Enable per environment after validating, ideally alongside "
            "URL_READER_ALLOWED_DOMAINS to restrict egress."
        ),
    )
    url_reader_allowed_domains: StringList = Field(
        default_factory=list,
        alias="URL_READER_ALLOWED_DOMAINS",
        description=(
            "Optional comma-separated allowlist of domains read_url may fetch "
            "(suffix match, so 'example.com' also allows 'docs.example.com'). "
            "Empty = any PUBLIC host (the internal/private-IP screen still applies). "
            "Set this in locked-down environments to restrict egress."
        ),
    )
    url_reader_max_bytes: int = Field(
        default=2_000_000,
        alias="URL_READER_MAX_BYTES",
        description="Max bytes read_url downloads from a page before stopping.",
    )
    url_reader_max_chars: int = Field(
        default=20000,
        alias="URL_READER_MAX_CHARS",
        description="Max characters of Markdown read_url returns to the model (longer is truncated).",
    )
    url_reader_timeout_seconds: float = Field(
        default=10.0,
        alias="URL_READER_TIMEOUT_SECONDS",
        description="Per-request timeout for read_url fetches.",
    )
    url_reader_max_redirects: int = Field(
        default=3,
        alias="URL_READER_MAX_REDIRECTS",
        description="Max HTTP redirects read_url follows (each hop is re-screened for SSRF).",
    )
    url_reader_user_agent: str = Field(
        default="AgentURLReader/1.0",
        alias="URL_READER_USER_AGENT",
        description="User-Agent header read_url sends when fetching a page.",
    )

    # --- Long-conversation context controls -------------------------------
    # These knobs tune the layered defense that keeps long chats fast, cheap,
    # and within the model's context window. They only take effect because
    # ``v1.core.agent`` reads them and wires them into middleware — a value set
    # here (or in .env) is inert unless agent.py passes it through.
    context_edit_trigger_tokens: int = Field(
        default=120000,
        alias="CONTEXT_EDIT_TRIGGER_TOKENS",
        description=(
            "Selective retention. Once the request exceeds this many (approximate) "
            "tokens, ContextEditingMiddleware clears the bodies of OLDER tool results "
            "(ai_search grounding, ServiceNow detail cards) to a '[cleared]' placeholder "
            "in the model-facing view only — the persisted messages and their artifacts "
            "(e.g. citation 'Referenced Sources') are untouched. Set below the "
            "summarization trigger (~231k on gpt-5.1) so tool bloat is shed before a "
            "full compaction is paid for. The agent can re-query / re-fetch anything it "
            "still needs."
        ),
    )
    context_edit_keep_tool_results: int = Field(
        default=3,
        alias="CONTEXT_EDIT_KEEP_TOOL_RESULTS",
        description=(
            "How many of the most-recent tool results ContextEditingMiddleware keeps in "
            "full when it clears older ones. The live turn's fresh results are always "
            "preserved; only stale ones are cleared."
        ),
    )
    context_window_floor_fraction: float = Field(
        default=0.92,
        alias="CONTEXT_WINDOW_FLOOR_FRACTION",
        description=(
            "Sliding-window safety floor. The hard per-call ceiling, as a fraction of the "
            "model's max input tokens, that SlidingWindowFloorMiddleware enforces by "
            "trimming the OLDEST messages from the request view (never mutating state). "
            "Kept ABOVE the summarization trigger (0.85) so summarization normally fires "
            "first; the floor only catches mis-fires or a single oversized turn."
        ),
    )
    ai_llm_max_input_tokens: int | None = Field(
        default=None,
        alias="AI_LLM_MAX_INPUT_TOKENS",
        description=(
            "Absolute input-token budget used as the base for context_window_floor_fraction. "
            "Leave unset to derive it from the model profile (max_input_tokens, e.g. 272000 "
            "for gpt-5.1). Set it explicitly when the deployment name is custom and the "
            "profile does not resolve a limit — otherwise the floor would lose its base and "
            "silently degrade. INPUT tokens only; leave headroom for AI_LLM_DEFAULT_MAX_TOKENS "
            "completion output."
        ),
    )
    #Search Configs
    ai_search_default_top_k: int = 7
    ai_search_min_score: float = Field(
        default=0.0,
        alias="AI_SEARCH_MIN_SCORE",
        description=(
            "Minimum Azure AI Search relevance score (@search.score) a document must "
            "reach to be surfaced as a citation. Documents below this floor are dropped "
            "so 'no relevant info' answers do not show stray source chips. With hybrid "
            "(RRF) search these scores are small (~0.01-0.03); tune against real data. "
            "0.0 disables score-based gating. Ignored for a document when a semantic "
            "reranker score is available (see ai_search_min_reranker_score)."
        ),
    )
    ai_search_min_reranker_score: float = Field(
        default=0.0,
        alias="AI_SEARCH_MIN_RERANKER_SCORE",
        description=(
            "Minimum semantic reranker score (@search.reranker_score, range 0-4) a "
            "document must reach to be surfaced as a citation. Only applies when "
            "semantic ranking is enabled via ai_search_semantic_configuration. 0.0 "
            "disables reranker-based gating."
        ),
    )
    ai_search_semantic_configuration: str | None = Field(
        default=None,
        alias="AI_SEARCH_SEMANTIC_CONFIGURATION",
        description=(
            "Default semantic configuration name, used for indexes not listed in "
            "ai_search_index_semantic_config_mapping (typically the default index). "
            "When a semantic configuration applies, queries run with semantic ranking so "
            "@search.reranker_score is populated and ai_search_min_reranker_score can gate "
            "relevance. Leave unset to use plain hybrid (keyword + vector) search."
        ),
    )
    ai_search_index_semantic_config_mapping: dict[str, str] = Field(
        default_factory=dict,
        alias="INDEX_SEMANTIC_CONFIG_MAPPING",
        description=(
            "JSON object mapping an Azure AI Search index name to the semantic "
            'configuration defined ON that index, e.g. {"index-a": "config-a", '
            '"index-b": "config-b"}. The semantic config is a property of the index, so '
            "when groups are routed to different indexes (tenant_group_index_mapping) each "
            "index needs its own config name — sending one global name makes Azure 400 "
            "('Unknown semantic configuration') on any index that does not define it, which "
            "silently kills RAG for that group. The resolved index is looked up here first; "
            "an unmapped index falls back to ai_search_semantic_configuration."
        ),
    )
    #Azure Search Config
    azure_search_endpoint: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
        "AZURE_AI_SEARCH_ENDPOINT",
        "AZURE_SEARCH_ENDPOINT",
        ),
        description="The endpoint URL for the Azure Search service, e.g., https://my-search.search.windows.net",
    )
    azure_search_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
        "AZURE_AI_SEARCH_API_KEY",
        "AZURE_SEARCH_API_KEY",
        ),
        description="The API key for the Azure Search service."
    )
    azure_ai_search_default_index: str = Field(
        default="documents",
        validation_alias=AliasChoices(
        "AZURE_AI_SEARCH_DEFAULT_INDEX",
        "AZURE_SEARCH_DEFAULT_INDEX",
        ),
        description=(
            "The default Azure Search index name to query if no index is specified. "
            "This should match the name of the index you created and populated with your documents. "
            "You can override this on a per-query basis if you have multiple indexes."
        )
    )
    #TODO fix
    azure_vector_field_name: str = Field(
        default="content_vector",
        alias="AZURE_VECTOR_FIELD_NAME",
        description=(
            "The name of the vector field in your Azure Search index. This should match the field you used to store the document embeddings. The default is 'content_vector', which is a common choice"
            " but you may have named it differently when setting up your index."
        )
    )
    azure_search_select_fields: StringList = Field(
        default_factory=lambda: [
        "id",
        "document_title",
        "file_name",
        "source_url",
        "breadcrumb",
        "chunk_content",
        "page_number",
        "source_type",
        "last_modified",
        ],
        alias="AZURE_SEARCH_SELECT_FIELDS",
        description=(
            "Fields to select in Azure Search queries, matching the index schema. "
            "Every field listed here must exist in the target index or Azure Search "
            "rejects the query. Override when pointing at an index with a different schema."
        ),
    )
    azure_search_timeout_seconds: float = Field(
        default=30.0,
        alias="AZURE_SEARCH_TIMEOUT_SECONDS",
        description=(
            "Per-request connect and read timeout (seconds) for Azure AI Search "
            "calls. Caps how long a blocking search can pin its worker thread; the "
            "azure-core SDK default is 300s, long enough to look like a hang."
        ),
    )

    #Azure Openai Config
    endpoint: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
        "AZURE_OPENAI_ENDPOINT",
        "API_ENDPOINT",
        ),
        description="The base URL for the Azure OpenAI resource, e.g., https://my-resource.openai.azure.com/",
    )
    api_key: str | None = Field(
        default=None,
        alias="AZURE_OPENAI_API_KEY",
        description="The API key for authenticating with Azure OpenAI. Required if API_ENDPOINT is set.",
    )
    api_version: str = Field(
        default="2026-05-05",
        alias="AZURE_OPENAI_API_VERSION",
        description="The API version to use for Azure OpenAI requests.",
    )
    embedding_deployment: str | None = Field(
        default="text-embedding-3-large",
        validation_alias=AliasChoices(
            "AZURE_OPENAI_EMBEDDING_DEPLOYMENT",
            "AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT",
            "AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME"
        ),
        description="The deployment name for the embedding model.",
    )
    chat_deployment: str | None = Field(
        default="gpt-chat-latest",
        validation_alias=AliasChoices(
            "AZURE_OPENAI_CHAT_DEPLOYMENT",
            "AZURE_OPENAI_CHAT_DEPLOYMENT_NAME"
        ),
        description="The deployment name for the chat model.",
    )
    
    use_managed_identity: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "AZURE_USE_MANAGED_IDENTITY",
            "AZURE_OPENAI_USE_MANAGED_IDENTITY",
        ),
        description=(
            "Whether to authenticate to Azure OpenAI and Azure AI Search with a "
            "managed identity (DefaultAzureCredential) instead of static API keys."
        ),
    )
    azure_openai_scope: str = Field(
        default="https://cognitiveservices.azure.com/.default",
        alias="AZURE_OPENAI_SCOPE",
        description="The scope to use for Azure OpenAI authentication. Typically, this should not need to be changed unless you have a custom Azure setup.",
    )
    azure_openai_embedding_version: str = Field(
        default="2024-02-01",
        alias="AZURE_OPENAI_EMBEDDING_API_VERSION",
        description="The API version to use for Azure OpenAI embedding requests.",
    )

    # --- Optional capabilities ---------------------------------------------
    # An optional capability is wired end to end or not at all: its subagent
    # joins the roster in ``v1.core.subagents`` and its routing block is
    # appended to the orchestrator system prompt in ``v1.core.prompts``. Both
    # read the flags below, so the two cannot drift and the model is never told
    # about a capability whose backing resource is unconfigured (which would
    # surface as a delegation that fails at tool-call time).

    @property
    def mapped_group_keys(self) -> frozenset[str]:
        """Every Entra group object-id or display name this deployment maps to behavior.

        Union of the group-keyed knobs: ``tenant_group_index_mapping``,
        ``tenant_group_starter_prompts_mapping``, and ``adf_disabled_groups``.
        Auth exports ONLY a caller's intersection with this set (both forms of a
        matched group) into ``langgraph_auth_user`` — the platform persists run
        configs and returns them through its threads/runs read APIs, so the
        caller's full AD inventory must never ride along (see
        ``v1.utils.auth._mapped_groups``).
        """

        return frozenset(
            (
                *self.tenant_group_index_mapping,
                *self.tenant_group_starter_prompts_mapping,
                *self.adf_disabled_groups,
            )
        )

    @property
    def adf_enabled(self) -> bool:
        """Whether the ADF subagent has a factory to talk to."""

        return bool(self.adf_factory_mapping)

    @property
    def unconfigured_capabilities(self) -> tuple[tuple[str, str], ...]:
        """(subagent name, setting that would enable it) for each capability off here.

        A skipped subagent is otherwise invisible from the outside (the model
        just routes its questions to ai_search_tool), so ``v1.core.agent`` states
        the roster and what is missing once at build time.
        """

        return tuple(
            (name, setting_name)
            for name, setting_name, enabled in (
                ("adf-agent", "ADF_FACTORY_MAPPING", self.adf_enabled),
            )
            if not enabled
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
