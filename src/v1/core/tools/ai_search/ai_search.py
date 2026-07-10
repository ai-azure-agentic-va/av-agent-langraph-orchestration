import asyncio
import itertools
import logging
import re
import threading
from collections import OrderedDict
from datetime import date, datetime
from typing import Any, Iterable, List, Tuple

from azure.core.credentials import AzureKeyCredential, TokenCredential
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery

from langchain_core.tools import tool
from langchain_openai import AzureOpenAIEmbeddings
from langgraph.config import get_stream_writer

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


# Citation numbering must stay stable across EVERY ai_search_tool call within a
# single turn. The orchestrator re-runs searches, and if each call renumbered
# from [1] the markers in the final answer would collide — [1] could mean three
# different documents. We key one registry per LangGraph run (turn) so a given
# document keeps the same [n] no matter how many searches surface it, and the
# next turn starts fresh at [1].
_MAX_TRACKED_TURNS = 1024
_anon_counter = itertools.count()  # process-unique keys for unidentifiable docs


class _TurnCitations:
    """Cumulative [n] numbering for one turn (all searches in a run share it)."""

    def __init__(self) -> None:
        self._by_key: dict[str, int] = {}
        self._next = 1
        # Streamed source payload per citation number, kept so the end-of-turn
        # citation filter can resolve the [n] markers the model actually used
        # back to their full source records (see ``sources_for``).
        self._sources_by_index: dict[int, dict] = {}
        self.lock = threading.Lock()

    def assign(self, doc_key: str) -> Tuple[int, bool]:
        """Return ``(citation_index, is_new)`` for ``doc_key``.

        Allocates the next number the first time a document is seen this turn and
        reuses it on every later search, so the same source never gets two
        numbers and two sources never share one. ``is_new`` lets the caller emit
        each source to the UI exactly once (the UI appends).
        """

        idx = self._by_key.get(doc_key)
        if idx is not None:
            return idx, False
        idx = self._next
        self._by_key[doc_key] = idx
        self._next += 1
        return idx, True

    def record_source(self, index: int, source: dict) -> None:
        """Remember the streamed payload for ``index`` (first writer wins)."""

        with self.lock:
            self._sources_by_index.setdefault(index, source)

    def sources_for(self, indices: Iterable[int]) -> List[dict]:
        """Return the recorded sources for ``indices``, ascending and deduped.

        Unknown indices (a marker the search never produced) are skipped, so a
        hallucinated ``[n]`` can never conjure a source chip.
        """

        with self.lock:
            seen: set[int] = set()
            out: List[dict] = []
            for idx in sorted(indices):
                if idx in seen:
                    continue
                seen.add(idx)
                source = self._sources_by_index.get(idx)
                if source is not None:
                    out.append(source)
            return out


_citation_registries: "OrderedDict[str, _TurnCitations]" = OrderedDict()
_citation_registries_lock = threading.Lock()


def _turn_citations(run_id: str | None) -> _TurnCitations:
    """Per-turn citation registry, created on first use and LRU-evicted.

    Falls back to a throwaway registry (numbered per call, the old behaviour)
    when there is no run context — e.g. a unit call outside the graph.
    """

    if not run_id:
        return _TurnCitations()
    with _citation_registries_lock:
        reg = _citation_registries.get(run_id)
        if reg is None:
            reg = _TurnCitations()
            _citation_registries[run_id] = reg
            # Bound memory: drop the oldest turn once we exceed the cap.
            if len(_citation_registries) > _MAX_TRACKED_TURNS:
                _citation_registries.popitem(last=False)
        else:
            _citation_registries.move_to_end(run_id)
        return reg


def _peek_turn_citations(run_id: str | None) -> _TurnCitations | None:
    """Return the existing registry for ``run_id`` without creating one.

    Used by the end-of-turn citation filter: if no search ran this turn there is
    no registry and nothing to filter.
    """

    if not run_id:
        return None
    with _citation_registries_lock:
        reg = _citation_registries.get(run_id)
        if reg is not None:
            _citation_registries.move_to_end(run_id)
        return reg


# Inline citation markers as emitted in the answer: ``[1]``, ``[1][3]``, or a
# comma list like ``[1, 3]``. We capture the digits between the brackets and
# split them out below.
_CITATION_MARKER_RE = re.compile(r"\[([\d,\s]+)\]")


def cited_indices(text: str) -> List[int]:
    """Citation numbers actually referenced in ``text``, ascending and deduped."""

    found: set[int] = set()
    for group in _CITATION_MARKER_RE.findall(text or ""):
        for part in group.split(","):
            part = part.strip()
            if part.isdigit():
                found.add(int(part))
    return sorted(found)


def cited_sources_for_current_run(answer_text: str) -> List[dict]:
    """Sources whose ``[n]`` marker appears in ``answer_text`` for this turn.

    Resolves the current run's citation registry and keeps only the sources the
    model actually cited inline, in citation-number order. Returns ``[]`` when no
    search ran this turn (no registry) so the UI clears any stray chips. Markers
    that don't correspond to a retrieved source are ignored.
    """

    reg = _peek_turn_citations(_current_run_id())
    if reg is None:
        return []
    return reg.sources_for(cited_indices(answer_text))


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
    citations: _TurnCitations,
) -> Tuple[str, List[dict]]:
    """Embed the query, run a hybrid Azure AI Search, and shape the results.

    Runs in a worker thread (see ai_search_tool) because both the embeddings
    call and the Azure Search SDK used here are blocking. ``index_name`` and
    ``semantic_configuration`` are resolved from the caller's groups before the
    thread hop (contextvars don't cross it); the semantic config must be the one
    defined on ``index_name`` or Azure 400s with "Unknown semantic configuration".

    Returns a ``(grounding_text, new_sources)`` pair:
    - ``grounding_text`` is the numbered passages handed to the LLM. Each
      ``[n]`` matches a source's ``index`` so the model can cite by number, and
      that number is stable for the whole turn (``citations``) — re-finding a
      document in a later search reuses its existing number.
    - ``new_sources`` holds only the documents numbered for the FIRST time in
      this call, ready to stream to the UI (which appends them). Documents
      carried over from an earlier search this turn are already on screen, so
      they are omitted here but still appear in ``grounding_text``.

    Documents below the configured relevance floor are dropped entirely, so a
    query with no genuinely relevant hit yields no grounding and no source chips.
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

    # Collapse chunks to one entry per document, preserving first-seen
    # (citation) order. Each document accumulates its chunk contents for
    # grounding while the source metadata comes from its best-scoring chunk.
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
                # unidentifiable docs never collapse into one citation number.
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
    # "no results" grounding and the UI gets no stray source chips.
    relevant = [doc for doc in docs.values() if _passes_relevance(doc)]
    if not relevant:
        return "No results found in the knowledge base for this query.", []

    passages: List[str] = []
    new_sources: List[dict] = []
    for doc in relevant:
        contents = doc.pop("_contents", [])
        merged = "\n\n".join(contents)
        # Stable, turn-global citation number for this document.
        index, is_new = citations.assign(doc.pop("_regkey"))
        passages.append(
            f"[{index}] {doc['title']}\nURL: {doc['url'] or ''}\nCONTENT:\n{merged}"
        )

        # Emit each source to the UI only once per turn; re-found documents are
        # already on screen under the same number and only need grounding text.
        if is_new:
            source = {"index": index, **doc}
            if merged:
                # Short preview for the UI; the full content stays in grounding.
                source["preview"] = merged[:300]
            # Drop empty/None fields so the streamed array stays lean.
            cleaned = {k: v for k, v in source.items() if v not in (None, "")}
            # Retain the exact payload streamed now so the end-of-turn citation
            # filter can re-emit this same record if the model cites [index].
            citations.record_source(index, cleaned)
            new_sources.append(cleaned)

    return "\n\n---\n\n".join(passages), new_sources


def _emit(writer, payload: dict) -> None:
    """Best-effort custom-stream emit; never let telemetry break the tool."""

    if writer is None:
        return
    try:
        writer(payload)
    except Exception:  # pragma: no cover - streaming is non-essential
        logger.debug("ai_search_tool stream emit failed", exc_info=True)


@tool("ai_search_tool")
async def ai_search_tool(query: str, top_k: int | None = None) -> str:
    """
    Search the company knowledge base and return cited passages.
    Args:
         query (str): The search query.
         top_k (int, optional): The number of top results to return. Defaults to settings.ai_search_default_top_k.
    Returns:
         str: A formatted string containing the search results with cited passages.
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

    # Resolve the turn's citation registry here (in the run context) so [n]
    # markers stay stable across every search this turn; passed into the thread
    # because contextvars don't cross it.
    citations = _turn_citations(_current_run_id())

    _emit(writer, {"type": "search_start"})
    try:
        grounding_text, sources = await asyncio.to_thread(
            _run_search, query, top_k, index_name, semantic_configuration, citations
        )
    except Exception as exc:  # surface search failures to the model, not as a crash
        return f"Knowledge base search failed: {type(exc).__name__}: {exc}"

    # Stream only the sources newly numbered by this search; the UI appends them
    # under their turn-stable [n]. An empty list (no relevant hit, or every hit
    # already shown) appends nothing — so a "not found" answer shows no chips.
    _emit(writer, {"type": "search_complete", "sources": sources})
    return grounding_text
