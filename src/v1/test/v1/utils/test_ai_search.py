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


def test_safe_url_encodes_markdown_breaking_chars() -> None:
    # Spaces and the Markdown link delimiters ( ) [ ] { } are percent-encoded;
    # ordinary URL-legal characters (scheme, slashes, query, fragment) are kept.
    out = ais._safe_url("https://x/a b(c)[d]{e}.md?q=1&r=2#frag")
    assert " " not in out and "(" not in out and ")" not in out
    assert "[" not in out and "]" not in out and "{" not in out and "}" not in out
    assert out.startswith("https://x/")
    assert "%20" in out  # space
    assert "?q=1&r=2#frag" in out  # query/fragment preserved


def test_safe_url_is_idempotent_on_already_encoded() -> None:
    # A correctly single-encoded URL is unchanged (no double-encoding of %20).
    url = "https://x/Shared%20Documents/Jul%2024.md"
    assert ais._safe_url(url) == url
    assert ais._safe_url(ais._safe_url(url)) == url  # idempotent


def test_safe_url_preserves_percent_encoded_reserved() -> None:
    # An already-encoded SharePoint-style URL must NOT be decoded: an encoded
    # slash/plus inside a component would change the target if turned literal.
    url = "https://t.sharepoint.com/AllItems.aspx?id=%2Fsites%2FFoo%2FQ1%2BQ2.docx"
    assert ais._safe_url(url) == url


def test_safe_url_preserves_encoded_folder_special_chars() -> None:
    # Folder/page names with special characters arrive as escapes. Decoding any
    # of them would make it a live delimiter: %26 -> "&" splits the query, %23 ->
    # "#" truncates at a fragment, %2B -> "+" reads as a space, %25 -> "%" forms
    # a bogus escape. All must survive byte-for-byte.
    urls = [
        "https://t.sharepoint.com/sites/S/Shared%20Documents/R%26D%20%23%201/a%2Bb%25.md",
        "https://t.sharepoint.com/AllItems.aspx?id=%2Fsites%2FS%2FR%26D%2FQ1%20%23%202",
        # Real ADO wiki shape: a literal hyphen in a page name is %2D, and the
        # whole pagePath value is encoded again, so %252D is correct, not a bug.
        (
            "https://dev.azure.com/Org/My%20Project/_wiki/wikis/W.wiki"
            "?pagePath=%2FW%2F2026%2F2026-%252D-Q1%2FJan-25"
        ),
    ]
    for url in urls:
        assert ais._safe_url(url) == url


def test_safe_url_encodes_raw_folder_special_chars() -> None:
    # A raw (unencoded) URL: spaces / parens / braces / non-ASCII get encoded, and
    # a "%" that does not begin an escape becomes %25 so it cannot form one.
    out = ais._safe_url("https://x/Team (Ops)/100% Done/Café {v2}.md")
    assert out == "https://x/Team%20%28Ops%29/100%25%20Done/Caf%C3%A9%20%7Bv2%7D.md"
    assert ais._safe_url(out) == out  # idempotent
    # Angle brackets (which would end an <...> link destination) are encoded too.
    assert ais._safe_url("https://x/a<b>.md") == "https://x/a%3Cb%3E.md"


def test_safe_url_keeps_authority_verbatim() -> None:
    # The host is never percent-encoded (an IDN or [IPv6] host would break).
    assert ais._safe_url("https://bücher.example/a b") == "https://bücher.example/a%20b"
    assert ais._safe_url("http://[::1]:8080/a b") == "http://[::1]:8080/a%20b"
    assert ais._safe_url("") == ""


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
    # The artifact URL is link-safe; the identity key stays the RAW url so a
    # source keeps its conversation-level [n] across the change.
    assert jul["url"] == "https://x/Shared%20Documents/Jul%2024.md"
    assert jul["source_key"] == "https://x/Shared Documents/Jul 24.md"


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
    assert docs.documents()[0]["url"] == "https://x/Policy%20A.md"


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
