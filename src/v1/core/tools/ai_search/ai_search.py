import asyncio
import itertools
import logging
import re
import threading
from collections import OrderedDict
from datetime import date, datetime
from typing import Annotated, Any, List, Optional

from azure.core.credentials import AzureKeyCredential, TokenCredential
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery

from langchain_core.tools import tool
from langchain_openai import AzureOpenAIEmbeddings
from langgraph.config import get_stream_writer
from langgraph.prebuilt import InjectedState

from v1.core.config import get_settings
from v1.utils.azure_credentials import (
    get_async_token_provider,
    get_azure_credential,
    get_token_provider,
)
from v1.utils.group_routing import groups_from_config, resolve_for_groups

logger = logging.getLogger(__name__)
settings = get_settings()


def _build_embeddings() -> AzureOpenAIEmbeddings:
    """Embeddings client; must match the model/dimensions used to populate the index."""

    kwargs = {
        "model": settings.embedding_deployment,
        "azure_endpoint": settings.endpoint,
        "openai_api_version": settings.azure_openai_embedding_version,
    }
    if settings.use_managed_identity:
        kwargs["azure_ad_token_provider"] = get_token_provider(settings.azure_openai_scope)
        kwargs["azure_ad_async_token_provider"] = get_async_token_provider(settings.azure_openai_scope)
    else:
        kwargs["api_key"] = settings.api_key
    return AzureOpenAIEmbeddings(**kwargs)


def _search_credential() -> AzureKeyCredential | TokenCredential:
    """Managed identity when enabled, otherwise the static admin/query key."""

    if settings.use_managed_identity:
        return get_azure_credential()
    return AzureKeyCredential(settings.azure_search_api_key)


# Built lazily on first search (not at import) so a missing/invalid Azure
# OpenAI endpoint or key surfaces as a tool error rather than crashing the
# whole agent package at import time. Double-checked lock mirrors
# _get_search_client; one embeddings client is reused for the process lifetime.
_embeddings: AzureOpenAIEmbeddings | None = None
_embeddings_lock = threading.Lock()


def _get_embeddings() -> AzureOpenAIEmbeddings:
    """Return the process-wide embeddings client, building once on first use."""

    global _embeddings
    client = _embeddings
    if client is None:
        with _embeddings_lock:
            client = _embeddings
            if client is None:
                client = _build_embeddings()
                _embeddings = client
    return client


# Azure SDK clients are safe to cache and reuse (they hold a pooled HTTP
# transport); building one per search opened a fresh connection pool on every
# call. Cache one client per index because the index is resolved per caller/group.
_search_clients: dict[str, SearchClient] = {}
_search_clients_lock = threading.Lock()


def _get_search_client(index_name: str) -> SearchClient:
    """Return the cached :class:`SearchClient` for ``index_name``, building once."""

    client = _search_clients.get(index_name)
    if client is None:
        with _search_clients_lock:
            client = _search_clients.get(index_name)
            if client is None:
                # connection_timeout/read_timeout cap how long a blocking search
                # can pin its to_thread worker; without them azure-core waits up
                # to its 300s default, which looks like a hang to the chat UI.
                client = SearchClient(
                    endpoint=settings.azure_search_endpoint,
                    index_name=index_name,
                    credential=_search_credential(),
                    connection_timeout=settings.azure_search_timeout_seconds,
                    read_timeout=settings.azure_search_timeout_seconds,
                )
                _search_clients[index_name] = client
    return client


def close_search_clients() -> None:
    """Close and drop all cached search clients (call on shutdown)."""

    with _search_clients_lock:
        for client in _search_clients.values():
            try:
                client.close()
            except Exception:  # pragma: no cover - best effort cleanup
                logger.debug("search client close failed", exc_info=True)
        _search_clients.clear()


def _first_value(result, *keys: str) -> str:
    """Return the first non-empty field among keys; index schemas vary."""

    for key in keys:
        value = result.get(key)
        if value:
            return str(value)
    return ""


def _to_iso(value: Any) -> str | None:
    """Coerce an Azure ``last_modified`` value into an ISO-8601 string.

    Edm.DateTimeOffset fields usually arrive already as ISO strings, but the
    SDK may hand back ``datetime``/``date`` objects too. Returns ``None`` when
    there is nothing usable so the field can be omitted from the source.
    """

    if not value:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _doc_key(result) -> str:
    """Stable per-document identity used to collapse multiple chunks.

    Prefer the resolvable URL (most unique), then the file name, title, or the
    chunk id as a last resort so distinct documents never merge.
    """

    return _first_value(result, "source_url", "url", "source", "file_name", "document_title", "id")


# Document numbering must stay stable across the WHOLE CONVERSATION, not just one
# turn. The orchestrator re-runs searches every turn, and referenced sources are
# append-only: a document keeps the same [n] however many turns surface it, and a
# genuinely new document gets the next unused number (6, 7, ...) rather than
# restarting at [1]. We key one registry per LangGraph run (turn) but SEED it from
# the conversation's accumulated `all_sources` state (see `ai_search_tool`), so the
# per-turn registry continues the running numbering instead of starting empty. That
# keeps the seed durable — it survives a process restart / replica change because
# `all_sources` is checkpointed graph state, not this in-memory cache.
_MAX_TRACKED_TURNS = 1024
_anon_counter = itertools.count()  # process-unique keys for unidentifiable docs


def source_key(doc: dict) -> str:
    """Stable identity for a source ACROSS turns (dedup + cumulative numbering).

    ``ai_search_tool`` stamps each document with the exact registry key it was
    numbered under (``source_key``); prefer it so a re-surfaced document reuses its
    conversation-level number. Fall back to the resolvable URL / file name / title
    for documents that predate the stamp, and finally the assigned index so a
    payload without any identity still de-dupes against itself.
    """

    return (
        doc.get("source_key")
        or doc.get("url")
        or doc.get("file_name")
        or doc.get("title")
        or f"_index_{doc.get('index')}"
    )

# A source document's own title/body can contain bracketed integers (e.g. a
# runbook's "[1242]" cross-reference). Once inlined into the grounding text they
# are textually identical to the leading ``[n]`` citation marker we prepend, so
# the model copies them verbatim and emits invalid citations that match no
# document and render as raw brackets in the UI. We rewrite any ``[n]`` inside
# the grounded title/content to ``(n)`` so the prepended marker is the ONLY
# citable ``[n]`` the model ever sees. See the passage builder in `_run_search`.
_CONTENT_BRACKET_NUM_RE = re.compile(r"\[(\d+)\]")


class _TurnDocuments:
    """Conversation-stable ``[n]`` numbering + the cumulative referenced-source set.

    Every search in a run shares one instance, SEEDED at the start of the turn from
    the conversation's accumulated sources (see :meth:`seed`). A document keeps the
    same number however many searches or turns surface it, and the accumulated list
    the tool streams/persists is the full running set (prior turns + this turn) — an
    idempotent, append-only replace.
    """

    def __init__(self) -> None:
        self._index_by_key: dict[str, int] = {}
        self._by_index: "dict[int, dict]" = {}
        self._next = 1
        self._seeded = False
        self._lock = threading.Lock()

    def seed(self, prior_documents: List[dict]) -> None:
        """Prime numbering from the conversation's accumulated ``all_sources``.

        Runs once per turn (idempotent via ``_seeded``) BEFORE the first search
        assigns any number, so this turn continues the running numbering: a source
        seen in an earlier turn reuses its number, and ``_next`` picks up after the
        highest number ever assigned. Also carries the prior documents into the set
        so :meth:`documents` emits the full cumulative list, not just this turn's.
        """

        with self._lock:
            if self._seeded:
                return
            self._seeded = True
            for doc in prior_documents or []:
                idx = doc.get("index")
                if not isinstance(idx, int):
                    continue
                key = source_key(doc)
                if key in self._index_by_key:
                    continue
                self._index_by_key[key] = idx
                self._by_index[idx] = doc
                if idx >= self._next:
                    self._next = idx + 1

    def assign(self, doc_key: str, document: dict) -> int:
        """Return the turn-stable ``[n]`` for ``doc_key``.

        Allocates the next number the first time a document is seen this turn and
        records its display payload; later searches that re-find the document
        reuse the same number and keep the first-recorded payload.
        """

        with self._lock:
            idx = self._index_by_key.get(doc_key)
            if idx is None:
                idx = self._next
                self._index_by_key[doc_key] = idx
                self._next += 1
                self._by_index[idx] = {"index": idx, **document}
            return idx

    def documents(self) -> List[dict]:
        """Every referenced source so far (earlier turns + this turn), by ``[n]``."""

        with self._lock:
            return [self._by_index[i] for i in sorted(self._by_index)]

    def indices(self) -> set[int]:
        """The ``[n]`` numbers assigned across the conversation so far."""

        with self._lock:
            return set(self._by_index)


_document_registries: "OrderedDict[str, _TurnDocuments]" = OrderedDict()
_document_registries_lock = threading.Lock()


def _turn_documents(run_id: str | None) -> _TurnDocuments:
    """Per-turn document registry, created on first use and LRU-evicted.

    Falls back to a throwaway registry (numbered per call) when there is no run
    context — e.g. a unit call outside the graph.
    """

    if not run_id:
        return _TurnDocuments()
    with _document_registries_lock:
        reg = _document_registries.get(run_id)
        if reg is None:
            reg = _TurnDocuments()
            _document_registries[run_id] = reg
            # Bound memory: drop the oldest turn once we exceed the cap.
            if len(_document_registries) > _MAX_TRACKED_TURNS:
                _document_registries.popitem(last=False)
        else:
            _document_registries.move_to_end(run_id)
        return reg


def assigned_citation_indices(run_id: str | None) -> set[int]:
    """The ``[n]`` numbers assigned so far for ``run_id`` (seeded cumulative set).

    Read-only peek for the citation guard: unlike :func:`_turn_documents` it never
    CREATES a registry, so a turn that ran no search returns an empty set here. The
    guard unions this with the conversation's persisted ``all_sources`` indices, so
    a follow-up turn that reuses an earlier turn's ``[n]`` without searching is still
    validated (and only genuinely invented markers are stripped).
    """

    if not run_id:
        return set()
    with _document_registries_lock:
        reg = _document_registries.get(run_id)
        if reg is None:
            return set()
        _document_registries.move_to_end(run_id)
    return reg.indices()


def _current_run_id() -> str | None:
    """Stable per-turn id from the LangGraph run config (``metadata.run_id``).

    ``run_id`` propagates into every node/tool's metadata for the life of one
    invocation, so it scopes citation numbering to a turn. Must be read inside
    the run context (not the worker thread, where contextvars don't reach).
    Returns ``None`` outside a graph run.
    """

    try:
        from langgraph.config import get_config

        config = get_config() or {}
    except Exception:
        return None
    run_id = (config.get("metadata") or {}).get("run_id")
    return str(run_id) if run_id else None


def _passes_relevance(doc: dict) -> bool:
    """Whether a deduped document clears the configured relevance floor.

    Prefers the semantic reranker score when present (only populated if a
    semantic configuration is set); otherwise gates on the raw search score.
    A threshold of ``0.0`` disables that gate, so the default keeps every result.
    """

    reranker = doc.get("reranker_score")
    if reranker is not None:
        min_reranker = settings.ai_search_min_reranker_score
        return min_reranker <= 0.0 or reranker >= min_reranker
    score = doc.get("score")
    min_score = settings.ai_search_min_score
    if min_score <= 0.0 or score is None:
        return True
    return score >= min_score


def _run_search(
    query: str,
    top_k: int,
    index_name: str,
    semantic_configuration: str | None,
    documents: _TurnDocuments,
) -> str:
    """Embed the query, run a hybrid Azure AI Search, and shape the results.

    Runs in a worker thread (see ai_search_tool) because both the embeddings
    call and the Azure Search SDK used here are blocking. ``index_name`` and
    ``semantic_configuration`` are resolved from the caller's groups before the
    thread hop (contextvars don't cross it); the semantic config must be the one
    defined on ``index_name`` or Azure 400s with "Unknown semantic configuration".

    Chunks are collapsed into one entry per document (``_doc_key``). Each
    document gets a turn-stable ``[n]`` from ``documents`` and is recorded there
    for streaming; the returned grounding text prefixes each document's merged
    content with its ``[n]`` so the model can cite by number. Documents below the
    configured relevance floor are dropped, so a query with no genuinely relevant
    hit yields the honest "no results" grounding.
    """

    # Reuse the cached client for this index (kept open for the process lifetime).
    search_client = _get_search_client(index_name)
    query_vector = _get_embeddings().embed_query(query)

    vector_query = VectorizedQuery(
        vector=query_vector,
        k_nearest_neighbors=top_k,
        fields=settings.azure_vector_field_name,
    )

    search_kwargs: dict[str, Any] = {
        "search_text": query,  # makes this hybrid: keyword + vector
        "vector_queries": [vector_query],
        "select": list(settings.azure_search_select_fields),
        "top": top_k,
    }
    # Opt into semantic ranking only when this index has a configured semantic
    # config; it populates @search.reranker_score so reranker-based relevance
    # gating can apply. The name must match a config defined on this index.
    if semantic_configuration:
        search_kwargs["query_type"] = "semantic"
        search_kwargs["semantic_configuration_name"] = semantic_configuration

    results = search_client.search(**search_kwargs)

    # Collapse chunks to one entry per document, preserving first-seen order.
    # Each document accumulates its chunk contents for grounding; scores are the
    # strongest seen across its chunks, other metadata comes from the first chunk.
    docs: "dict[str, dict]" = {}
    for r in results:
        real_key = _doc_key(r)
        key = real_key or f"_chunk_{len(docs)}"
        content = _first_value(r, "chunk_content", "content", "chunk", "text")
        score = r.get("@search.score")
        reranker = r.get("@search.reranker_score")

        doc = docs.get(key)
        if doc is None:
            docs[key] = {
                "title": _first_value(r, "document_title", "title", "file_name") or "Untitled",
                "url": _first_value(r, "source_url", "url", "source") or None,
                "file_name": _first_value(r, "file_name") or None,
                "breadcrumb": _first_value(r, "breadcrumb") or None,
                "source_type": _first_value(r, "source_type") or None,
                "page_number": r.get("page_number"),
                "updated_at": _to_iso(r.get("last_modified")),
                "score": score,
                "reranker_score": reranker,
                "_contents": [content] if content else [],
                # Turn-stable identity for cumulative numbering. Documents with
                # no usable identifier get a process-unique key so two distinct
                # unidentifiable docs never collapse into one number.
                "_regkey": real_key or f"_anon_{next(_anon_counter)}",
            }
        else:
            if content:
                doc["_contents"].append(content)
            # Keep the strongest score seen across the document's chunks.
            if score is not None and (doc["score"] is None or score > doc["score"]):
                doc["score"] = score
            if reranker is not None and (
                doc["reranker_score"] is None or reranker > doc["reranker_score"]
            ):
                doc["reranker_score"] = reranker

    # Keep only documents that clear the relevance floor; an empty result here
    # means the KB has nothing genuinely on-topic, so the model gets the honest
    # "no results" grounding and no documents are recorded for the UI.
    relevant = [doc for doc in docs.values() if _passes_relevance(doc)]
    if not relevant:
        return "No results found in the knowledge base for this query."

    passages: List[str] = []
    for doc in relevant:
        merged = "\n\n".join(doc.pop("_contents", []))
        regkey = doc.pop("_regkey")
        # Lean display payload for the UI (drop empties); add a short preview.
        display = {k: v for k, v in doc.items() if v not in (None, "")}
        if merged:
            display["preview"] = merged[:300]
        # Stamp the registry key so a re-surfaced document keeps its conversation
        # number across turns (the seed maps source_key -> index) and the
        # cumulative `all_sources` reducer de-dupes on it. UI ignores unknown keys.
        display["source_key"] = regkey
        # Conversation-stable [n] for this document; records it for the UI on first
        # sight (numbering continues from earlier turns via the turn's seed).
        index = documents.assign(regkey, display)
        # Neutralize any [n] the source's own title/body carries so the ONLY
        # citable [n] in the grounding is the leading marker we prepend — the
        # model can no longer copy a source cross-reference into a bogus citation.
        # (The UI display payload keeps the raw title/preview; only the model-
        # facing grounding text is rewritten.)
        safe_title = _CONTENT_BRACKET_NUM_RE.sub(r"(\1)", doc["title"])
        safe_content = _CONTENT_BRACKET_NUM_RE.sub(r"(\1)", merged)
        passages.append(
            f"[{index}] {safe_title}\nURL: {doc['url'] or ''}\nCONTENT:\n{safe_content}"
        )

    return "\n\n---\n\n".join(passages)


def _emit(writer, payload: dict) -> None:
    """Best-effort custom-stream emit; never let telemetry break the tool."""

    if writer is None:
        return
    try:
        writer(payload)
    except Exception:  # pragma: no cover - streaming is non-essential
        logger.debug("ai_search_tool stream emit failed", exc_info=True)


@tool("ai_search_tool", response_format="content_and_artifact")
async def ai_search_tool(
    query: str,
    top_k: int | None = None,
    state: Annotated[Optional[dict], InjectedState] = None,
) -> tuple[str, list[dict]]:
    """
    Search the company knowledge base and return numbered passages to cite.
    Args:
         query (str): The search query.
         top_k (int, optional): The number of top results to return. Defaults to settings.ai_search_default_top_k.
    Returns:
         tuple[str, list[dict]]: ``(grounding_text, documents)``. ``grounding_text`` is the
         numbered passages the model cites with ``[n]`` markers, numbered continuing from the
         conversation's earlier sources (append-only, never restarting at [1]). ``documents``
         is the full CUMULATIVE referenced-source set (earlier turns + this turn) and becomes
         the ToolMessage ``artifact`` — it is NOT shown to the model, but it IS persisted in
         the checkpoint, so a thread reopened from history (where the live custom ``documents``
         stream event never replays) can still rebuild "Referenced Sources" and link inline
         ``[n]`` markers. ``state`` is injected by LangGraph (not a model argument) so the tool
         can seed numbering from the conversation's accumulated ``all_sources``.
    """
    # Resolve the default here (not in the signature) so it tracks settings at
    # call time rather than being frozen at import.
    if top_k is None:
        top_k = settings.ai_search_default_top_k

    # Acquire the writer inside the graph-run context (not the worker thread,
    # where the contextvar would not propagate). Stays None outside a stream.
    try:
        writer = get_stream_writer()
    except Exception:
        writer = None

    # Resolve the index from the caller's groups here (inside the run context)
    # because contextvars don't propagate into the worker thread below.
    caller_groups = groups_from_config()
    index_name = resolve_for_groups(
        settings.tenant_group_index_mapping,
        caller_groups,
        settings.azure_ai_search_default_index,
    )
    # The semantic configuration is a property of the resolved index, not the
    # caller's group — look it up by index name, falling back to the global
    # default for any index not explicitly mapped. Sending the wrong index's
    # config name 400s ("Unknown semantic configuration") and silently kills RAG.
    semantic_configuration = (
        settings.ai_search_index_semantic_config_mapping.get(index_name)
        or settings.ai_search_semantic_configuration
    )
    # Diagnostic: confirms groups reach the tool and which index/config was chosen.
    logger.debug(
        "ai_search index routing: group_count=%d index=%s semantic_config=%s (default=%s)",
        len(caller_groups),
        index_name,
        semantic_configuration,
        settings.azure_ai_search_default_index,
    )

    # Resolve the turn's document registry here (in the run context) so [n]
    # numbers stay stable across every search this turn; passed into the thread
    # because contextvars don't cross it. Seed it (once per turn) from the
    # conversation's accumulated sources so numbering CONTINUES across turns —
    # a source seen earlier keeps its number and new ones get the next unused
    # number. `state` is injected by LangGraph; it is absent for a direct/unit
    # call, in which case numbering simply starts fresh.
    prior_sources = state.get("all_sources") if isinstance(state, dict) else None
    documents = _turn_documents(_current_run_id())
    documents.seed(prior_sources if isinstance(prior_sources, list) else [])

    try:
        grounding_text = await asyncio.to_thread(
            _run_search, query, top_k, index_name, semantic_configuration, documents
        )
    except Exception as exc:  # surface search failures to the model, not as a crash
        # response_format="content_and_artifact" requires EVERY return to be a
        # (content, artifact) tuple; no documents were retrieved on failure.
        return f"Knowledge base search failed: {type(exc).__name__}: {exc}", []

    # The CUMULATIVE referenced-source set (earlier turns' documents, carried in via
    # the seed, PLUS this turn's hits), numbered append-only 1..n, goes out two ways:
    # (1) the custom stream event drives the LIVE "Referenced Sources" UI as it
    # streams, and (2) it is returned as the tool `artifact`, which persists on the
    # ToolMessage in the checkpoint so a thread reopened from history — where the
    # stream event never replays — can rebuild the same list and inline [n] links.
    # Emitting the whole accumulated list each time makes the live event an
    # idempotent replace — no ordering/merge assumptions — and gives each answer its
    # own cumulative-as-of-that-turn snapshot.
    turn_documents = documents.documents()
    _emit(writer, {"type": "documents", "documents": turn_documents})
    return grounding_text, turn_documents
