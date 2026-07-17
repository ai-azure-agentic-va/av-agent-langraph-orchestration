
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
    servicenow_disabled_groups: StringList = Field(
        default_factory=list,
        alias="SERVICENOW_DISABLED_GROUPS",
        description=(
            "Comma-separated Entra group object-ids or display names for which the "
            "ServiceNow ticket subagent is DISABLED. A caller whose groups intersect "
            "this set loses the `task` delegation tool and cannot look up / list / "
            "reference ServiceNow incidents; everyone else keeps it. Matched the same "
            "way as TENANT_GROUP_INDEX_MAPPING keys (object-id or display name). Empty "
            "(default) leaves ServiceNow enabled for everyone. Example for "
            "external callers: SERVICENOW_DISABLED_GROUPS=<your-external-group-name>"
        ),
    )
    #Openai Config
    # Optional: some models (e.g. reasoning / gpt-5 chat deployments) reject any
    # temperature other than the default and 400 if one is sent. Leave unset to
    # omit `temperature` from the request entirely (uses the model default);
    # set AI_LLM_DEFAULT_TEMPERATURE only for deployments that accept it.
    ai_llm_default_temperature: float | None = Field(
        default=None, alias="AI_LLM_DEFAULT_TEMPERATURE"
    )
    # Cap on the model's output (completion) tokens per call. Passed to the chat
    # client as `max_tokens`, which langchain-openai serialises as
    # `max_completion_tokens` — the field gpt-5 / reasoning deployments require.
    ai_llm_default_max_tokens: int = Field(
        default=10000, alias="AI_LLM_DEFAULT_MAX_TOKENS"
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
