"""Regression tests for the AI Search request timeout (PROD_DEPLOYMENT_TODO §1).

AISEARCH-3: the cached ``SearchClient`` must be built with an explicit
connect/read timeout so a hung upstream cannot pin its ``to_thread`` worker for
the azure-core default of 300s.

Runs standalone (``python test_ai_search.py``) or under pytest.
"""

from __future__ import annotations

import os

from azure.core.credentials import AzureKeyCredential

import v1.core.tools.ai_search.ai_search as ais
from v1.core.config import Settings


def test_timeout_config_default_and_override() -> None:
    prev = os.environ.pop("AZURE_SEARCH_TIMEOUT_SECONDS", None)
    try:
        assert Settings(_env_file=None).azure_search_timeout_seconds == 30.0
        os.environ["AZURE_SEARCH_TIMEOUT_SECONDS"] = "5"
        assert Settings(_env_file=None).azure_search_timeout_seconds == 5.0
    finally:
        os.environ.pop("AZURE_SEARCH_TIMEOUT_SECONDS", None)
        if prev is not None:
            os.environ["AZURE_SEARCH_TIMEOUT_SECONDS"] = prev


def test_search_client_built_with_timeout() -> None:
    captured: dict = {}

    class FakeSearchClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def close(self):
            pass

    orig_client = ais.SearchClient
    orig_cred = ais._search_credential
    orig_cache = dict(ais._search_clients)
    orig_timeout = ais.settings.azure_search_timeout_seconds
    orig_endpoint = ais.settings.azure_search_endpoint
    try:
        ais.SearchClient = FakeSearchClient
        ais._search_credential = lambda: AzureKeyCredential("fake")
        ais._search_clients.clear()
        ais.settings.azure_search_timeout_seconds = 12.5
        ais.settings.azure_search_endpoint = "https://example.search.windows.net"

        client = ais._get_search_client("idx-1")

        assert isinstance(client, FakeSearchClient)
        assert captured["index_name"] == "idx-1"
        assert captured["connection_timeout"] == 12.5
        assert captured["read_timeout"] == 12.5
        # Cached: a second call reuses the client and does not rebuild.
        captured.clear()
        assert ais._get_search_client("idx-1") is client
        assert captured == {}
    finally:
        ais.SearchClient = orig_client
        ais._search_credential = orig_cred
        ais._search_clients.clear()
        ais._search_clients.update(orig_cache)
        ais.settings.azure_search_timeout_seconds = orig_timeout
        ais.settings.azure_search_endpoint = orig_endpoint


def test_simple_phrase_query_quotes_and_escapes() -> None:
    assert ais._simple_phrase_query("Jul 24") == '"Jul 24"'
    # Embedded double-quote is backslash-escaped inside the phrase.
    assert ais._simple_phrase_query('a"b') == '"a\\"b"'


def test_reduce_section_path_collapses_repeated_leaf() -> None:
    assert (
        ais._reduce_section_path("Policies > Underwriting > Jul 24 > Jul 24.md")
        == "Policies > Underwriting > Jul 24"
    )
    # Repeat without a file extension is also collapsed to its node (the repeated
    # leaf is dropped, the node is kept).
    assert ais._reduce_section_path("A > B > Report > Report") == "A > B > Report"
    # A non-repeated leaf is preserved; whitespace/separators are normalised.
    assert ais._reduce_section_path("  A >B>  C.md ") == "A > B > C.md"
    # A single segment is returned unchanged.
    assert ais._reduce_section_path("Jul 24") == "Jul 24"
    # A version-like dotted node is NOT misread as a file extension:
    #  - extensionless repeat of a dotted node still collapses (under-recall guard)
    assert (
        ais._reduce_section_path("A > Report v1.0 > Report v1.0") == "A > Report v1.0"
    )
    #  - a distinct file whose name resembles its folder + a version dot does NOT
    #    collapse the whole folder (over-recall guard)
    assert ais._reduce_section_path("A > v2 > v2.0") == "A > v2 > v2.0"
    # A real page-file leaf on a dotted node still collapses to the node.
    assert ais._reduce_section_path("A > Report v1.0 > Report v1.0.md") == "A > Report v1.0"


def test_is_section_descendant_exact_prefix_only() -> None:
    section = "A > B > C"
    assert ais._is_section_descendant("A > B > C", section) is True  # node itself
    assert ais._is_section_descendant("A > B > C > C.md", section) is True  # page
    assert ais._is_section_descendant("A > B > C > Child > Child.md", section) is True
    # Sibling that merely shares a name segment is NOT a descendant.
    assert ais._is_section_descendant("A > B > Camping > x.md", section) is False
    assert ais._is_section_descendant(None, section) is False


class _FakeClient:
    def __init__(self, results, captured):
        self._results = results
        self._captured = captured

    def search(self, **kwargs):
        self._captured.update(kwargs)
        return list(self._results)


def test_run_search_section_mode_filters_prefix_ignores_floor() -> None:
    captured: dict = {}
    results = [
        {
            "document_title": "Jul 24",
            "source_url": "https://x/Shared Documents/Jul 24.md",
            "breadcrumb": "Policies > Underwriting > Jul 24 > Jul 24.md",
            "chunk_content": "parent body",
            "@search.score": 0.01,  # tiny: would be dropped if the floor applied
        },
        {
            "document_title": "Rates",
            "source_url": "https://x/Rates.md",
            "breadcrumb": "Policies > Underwriting > Jul 24 > Rates > Rates.md",
            "chunk_content": "child body",
            "@search.score": 0.001,
        },
        {
            "document_title": "Aug 24",
            "source_url": "https://x/Aug 24.md",
            "breadcrumb": "Policies > Underwriting > Aug 24 > Aug 24.md",
            "chunk_content": "other month",
            "@search.score": 5.0,  # high score, but wrong branch → excluded
        },
    ]
    orig_client = ais._get_search_client
    orig_min_score = ais.settings.ai_search_min_score
    try:
        ais._get_search_client = lambda index_name: _FakeClient(results, captured)
        ais.settings.ai_search_min_score = 100.0  # would drop everything if applied
        docs = ais._TurnDocuments()
        text = ais._run_search(
            query="unused in section mode",
            top_k=3,
            index_name="idx",
            semantic_configuration="sem-config",  # must be ignored in section mode
            documents=docs,
            section="Policies > Underwriting > Jul 24 > Jul 24.md",
        )
    finally:
        ais._get_search_client = orig_client
        ais.settings.ai_search_min_score = orig_min_score

    # Lexical breadcrumb query only: reduced-path phrase, no vector, no top, no semantic.
    assert captured.get("search_fields") == ["breadcrumb"]
    assert captured.get("search_text") == '"Policies > Underwriting > Jul 24"'
    assert "vector_queries" not in captured
    assert "top" not in captured
    assert "query_type" not in captured
    # Kept the section page + descendant; excluded the sibling branch despite its
    # higher score (floor is ignored, but the exact-prefix filter still applies).
    assert "[1] Jul 24" in text
    assert "Rates" in text
    assert "Aug 24" not in text
    # BREADCRUMB line is emitted so the model can request a section follow-up.
    assert "BREADCRUMB: Policies > Underwriting > Jul 24 > Jul 24.md" in text
    # The source URL is NOT surfaced in the model-facing grounding text (emitting
    # it only tempted the model to paste the raw location into its answer); it
    # rides on the `documents` artifact the UI uses to build "Referenced Sources"
    # and to link the [n] markers.
    assert "URL:" not in text
    assert "https://x/" not in text
    jul = next(d for d in docs.documents() if d["title"] == "Jul 24")
    assert jul["url"] == "https://x/Shared Documents/Jul 24.md"


def test_run_search_normal_mode_uses_vector_and_emits_breadcrumb() -> None:
    captured: dict = {}
    results = [
        {
            "document_title": "Policy A",
            "source_url": "https://x/Policy A.md",
            "breadcrumb": "Policies > A > A.md",
            "chunk_content": "body",
            "@search.score": 9.0,
        }
    ]

    class _FakeEmbeddings:
        def embed_query(self, query):
            return [0.1, 0.2, 0.3]

    orig_client = ais._get_search_client
    orig_emb = ais._get_embeddings
    orig_min_score = ais.settings.ai_search_min_score
    try:
        ais._get_search_client = lambda index_name: _FakeClient(results, captured)
        ais._get_embeddings = lambda: _FakeEmbeddings()
        ais.settings.ai_search_min_score = 0.0  # keep everything
        docs = ais._TurnDocuments()
        text = ais._run_search(
            query="policy A",
            top_k=5,
            index_name="idx",
            semantic_configuration=None,
            documents=docs,
        )
    finally:
        ais._get_search_client = orig_client
        ais._get_embeddings = orig_emb
        ais.settings.ai_search_min_score = orig_min_score

    # Normal mode is the hybrid path: vector query + explicit top, no breadcrumb scoping.
    assert "vector_queries" in captured
    assert captured.get("top") == 5
    assert "search_fields" not in captured
    # Same passage shape (leading [n] + BREADCRUMB, no URL line) as section mode.
    assert "[1] Policy A" in text
    assert "BREADCRUMB: Policies > A > A.md" in text
    # Source URL stays out of the grounding text; it rides on the documents artifact.
    assert "URL:" not in text
    assert "https://x/" not in text
    assert docs.documents()[0]["url"] == "https://x/Policy A.md"


def _main() -> int:
    checks = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for check in checks:
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - standalone runner reports all
            failures += 1
            print(f"FAIL {check.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {check.__name__}")
    print(f"\n{len(checks) - failures}/{len(checks)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
