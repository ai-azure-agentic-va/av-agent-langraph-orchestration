
from __future__ import annotations
from functools import lru_cache
import json
import os
from typing import Annotated
from pydantic import Field, AliasChoices
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from v1.utils.helper import _split_csv
from pydantic import (
    AnyHttpUrl,
    BeforeValidator
)
StringList = Annotated[list[str], BeforeValidator(_split_csv), NoDecode]

class Settings(BaseSettings):
    """Runtime configuration for the demo backend.

    CORS_ORIGINS is intentionally stored as a plain string because pydantic-settings
    expects complex env values like list[str] to be JSON. A comma-separated value is
    friendlier for Docker Compose, so we parse it ourselves through cors_origin_list.
    """
    postgress_url: str = Field(default="postgresql://postgres:postgres@localhost:5432/deepagent?sslmode=disable", alias="POSTGRESS_DATABASE_URL")
    persistence_backend: str = Field(default="memory", alias="PERSISTENCE_BACKEND")
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    api_bearer_token: str = Field(
        default="dev-token-change-me",
        alias="API_BEARER_TOKEN",
        description="A token used to authenticate API requests. In production, use a secure, randomly generated token and keep it secret.",
    )
    app_name: str = "DeepAgent CopilotKit AG-UI Demo"
    agent_name: str = Field(default="deepagent-demo", alias="AGENT_NAME")
    agent_description: str = Field(
        default="A LangGraph DeepAgent demo exposed through CopilotKit AG-UI.",
        alias="AGENT_DESCRIPTION",
        description="A brief description of the agent's purpose and capabilities.",
    )
    cors_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173",
        alias="CORS_ORIGINS",
    )
    agent_max_steps: int = Field(default=50, alias="AGENT_MAX_STEPS")
    #Entra auth config
    entra_tenant_id: str | None = Field(default=None, alias="ENTRA_TENANT_ID")
    entra_client_id: str | None = Field(default=None, alias="ENTRA_CLIENT_ID")
    entra_audience: str | None = Field(default=None, alias="ENTRA_AUDIENCE")
    entra_issuer: AnyHttpUrl | None = Field(default=None, alias="ENTRA_ISSUER")
    entra_jwks_url: AnyHttpUrl | None = Field(default=None, alias="ENTRA_JWKS_URL")
    entra_required_scopes: StringList = Field(default_factory=list, alias="ENTRA_REQUIRED_SCOPES")
    entra_group_claim: str = Field(default="groups", alias="ENTRA_GROUP_CLAIM")

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
            "external callers: SERVICENOW_DISABLED_GROUPS=FIN-APP-EXT"
        ),
    )
    #Openai Config
    ai_llm_default_top_p: float = 0.95
    # Optional: some models (e.g. reasoning / gpt-5 chat deployments) reject any
    # temperature other than the default and 400 if one is sent. Leave unset to
    # omit `temperature` from the request entirely (uses the model default);
    # set AI_LLM_DEFAULT_TEMPERATURE only for deployments that accept it.
    ai_llm_default_temperature: float | None = Field(
        default=None, alias="AI_LLM_DEFAULT_TEMPERATURE"
    )
    ai_llm_default_tmax_token: int = 25000
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
        agent_description="The endpoint URL for the Azure Search service, e.g., https://my-search.search.windows.net",
    )
    azure_search_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
        "AZURE_AI_SEARCH_API_KEY",
        "AZURE_SEARCH_API_KEY",
        ),
        agent_description="The API key for the Azure Search service."
    )
    azure_ai_search_default_index: str = Field(
        default="documents",
        validation_alias=AliasChoices(
        "AZURE_AI_SEARCH_DEFAULT_INDEX",
        "AZURE_SEARCH_DEFAULT_INDEX",
        ),
        agent_description=(
            "The default Azure Search index name to query if no index is specified. "
            "This should match the name of the index you created and populated with your documents. "
            "You can override this on a per-query basis if you have multiple indexes."
        )
    )
    #TODO fix
    azure_vector_field_name: str = Field(
        default="content_vector",
        alias="AZURE_VECTOR_FIELD_NAME",
        agent_description=(
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
        agent_description=(
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
        agent_description="The base URL for the Azure OpenAI resource, e.g., https://my-resource.openai.azure.com/",
    )
    api_key: str | None = Field(
        default=None,
        alias="AZURE_OPENAI_API_KEY",
        agent_description="The API key for authenticating with Azure OpenAI. Required if API_ENDPOINT is set.",
    )
    api_version: str = Field(
        default="2026-05-05",
        alias="AZURE_OPENAI_API_VERSION",
        agent_description="The API version to use for Azure OpenAI requests.",
    )
    embedding_deployment: str | None = Field(
        default="text-embedding-3-large",
        validation_alias=AliasChoices(
            "AZURE_OPENAI_EMBEDDING_DEPLOYMENT",
            "AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT",
            "AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME"
        ),
        agent_description="The deployment name for the embedding model.",
    )
    chat_deployment: str | None = Field(
        default="gpt-chat-latest",
        validation_alias=AliasChoices(
            "AZURE_OPENAI_CHAT_DEPLOYMENT",
            "AZURE_OPENAI_CHAT_DEPLOYMENT_NAME"
        ),
        agent_description="The deployment name for the chat model.",
    )
    
    use_managed_identity: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "AZURE_USE_MANAGED_IDENTITY",
            "AZURE_OPENAI_USE_MANAGED_IDENTITY",
        ),
        agent_description=(
            "Whether to authenticate to Azure OpenAI and Azure AI Search with a "
            "managed identity (DefaultAzureCredential) instead of static API keys."
        ),
    )
    fallback_enabled: bool = Field(
        default=True,
        alias="AZURE_OPENAI_FALLBACK_ENABLED",
        agent_description="Whether to enable fallback for Azure OpenAI requests.",
    )
    fallback_dimensions: int = Field(
        default=1536,
        alias="AZURE_OPENAI_FALLBACK_DIMENSIONS",
        agent_description="The dimensions for fallback embeddings.",
    )
    azure_openai_scope: str = Field(
        default="https://cognitiveservices.azure.com/.default",
        alias="AZURE_OPENAI_SCOPE",
        agent_description="The scope to use for Azure OpenAI authentication. Typically, this should not need to be changed unless you have a custom Azure setup.",
    )
    azure_search_scope: str = Field(
        default="https://search.azure.com/.default",
        alias="AZURE_SEARCH_SCOPE",
        agent_description="The AAD scope to use for Azure AI Search authentication when using managed identity. Typically does not need to change.",
    )
    azure_openai_embedding_version: str = Field(
        default="2024-02-01",
        alias="AZURE_OPENAI_EMBEDDING_API_VERSION",
         agent_description="The API version to use for Azure OpenAI embedding requests.",
    )

    
    


    @property
    def cors_origin_list(self) -> list[str]:
        """Return CORS origins from either JSON-list or comma-separated env syntax."""
        value = self.cors_origins.strip()
        if not value:
            return []

        # Also support JSON arrays for users who prefer Pydantic's native style:
        # CORS_ORIGINS='["http://localhost:5173", "http://127.0.0.1:5173"]'
        if value.startswith("["):
            parsed = json.loads(value)
            if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
                raise ValueError("CORS_ORIGINS JSON value must be a list of strings")
            return [origin.strip() for origin in parsed if origin.strip()]

        return [origin.strip() for origin in value.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
