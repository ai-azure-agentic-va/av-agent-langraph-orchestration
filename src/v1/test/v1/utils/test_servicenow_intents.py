"""Regression tests for the substring-matching root cause (PROD_DEPLOYMENT_TODO §5).

INTENT-1/5/7 all stem from contiguous-substring matching over a narrow haystack:
a multi-word query like 'missing data' matched nothing even when both words were
present. The client now tokenizes the needle and requires every token to appear
(AND-of-substrings). These tests pin that behavior with deterministic in-memory
incidents plus one guard against the bundled fixture.

Runs standalone (``python test_servicenow_intents.py``) or under pytest.
"""

from __future__ import annotations

import asyncio

from v1.core.tools.servicenow.tools import (
    ServiceNowToolInputError,
    normalize_cause,
)
from v1.utils.clients.servicenow import (
    ServiceNowClient,
    ServiceNowConfig,
    _contains_all_tokens,
)

_INCIDENTS = [
    {
        "number": "INC0000001",
        "short_description": "Core Banking row count mismatch on snapshot",
        "description": "Ingest left data missing for 2,400 records during load.",
        "cause": "upstream feed truncated",
        "close_notes": "Root cause: the upstream feed truncated mid-load; reran ingest.",
        "configuration_item": {"value": "ci1", "display_value": "Data Quality"},
        "state": {"value": "2", "display_value": "In Progress"},
    },
    {
        "number": "INC0000002",
        "short_description": "Transaction ledger daily ingest failed",
        "description": "PL-CB-04-TRANSACTION-LEDGER-DAILY-INGEST pipeline aborted.",
        "cause": "schema drift",
        "configuration_item": {"value": "ci2", "display_value": "PL-CB-04-TRANSACTION-LEDGER"},
        "state": {"value": "1", "display_value": "New"},
    },
    {
        "number": "INC0000003",
        "short_description": "Customer feed delayed",
        "description": "Vendor maintenance window pushed the customer file back.",
        "cause": "vendor delay",
        "configuration_item": {"value": "ci3", "display_value": "Data Quality"},
        "state": {"value": "7", "display_value": "Closed"},
    },
]


def _client() -> ServiceNowClient:
    return ServiceNowClient(ServiceNowConfig(mode="mock"), incidents=_INCIDENTS)


def _matching(**filters) -> set[str]:
    client = _client()
    envelope = asyncio.run(client.list_incidents(filters=filters, limit=50))
    return {
        (inc["number"]["display_value"] if isinstance(inc["number"], dict) else inc["number"])
        for inc in envelope["incidents"]
    }


# -- helper unit tests ---------------------------------------------------------


def test_contains_all_tokens_and_semantics() -> None:
    haystack = "ingest left data missing for 2,400 records"
    assert _contains_all_tokens("missing data", haystack)  # both tokens present
    assert _contains_all_tokens("data missing", haystack)  # order independent
    assert not _contains_all_tokens("missing absent", haystack)  # one token absent
    assert _contains_all_tokens("", haystack)  # empty needle matches


# -- description_contains: multi-word now matches ------------------------------


def test_multi_word_description_matches_when_tokens_present() -> None:
    # 'missing data' is not a contiguous phrase anywhere, but INC1's description
    # contains both 'missing' and 'data'.
    assert _matching(description_contains="missing data") == {"INC0000001"}


def test_multi_word_order_independent() -> None:
    assert _matching(description_contains="ledger transaction") == {"INC0000002"}
    assert _matching(description_contains="transaction ledger") == {"INC0000002"}


def test_token_absent_anywhere_matches_nothing() -> None:
    assert _matching(description_contains="missing nonexistentword") == set()


def test_single_word_still_matches() -> None:
    # 'data' appears in INC1 (description) and INC3 ('Data Quality' is CI, but
    # description_contains only scans description/short_description).
    assert "INC0000001" in _matching(description_contains="data")


def test_short_description_contains_matches_title_only() -> None:
    # Live-verified filter (2026-07-13): searches the TITLE, not the body — 'ledger'
    # is in INC2's title AND body, 'pipeline' only in its body. And it must be
    # forwarded to the real API, not dropped.
    from v1.utils.clients.servicenow import SUPPORTED_FILTERS

    assert "short_description_contains" in SUPPORTED_FILTERS
    assert _matching(short_description_contains="ledger ingest") == {"INC0000002"}
    assert _matching(short_description_contains="pipeline") == set()


def test_tokenization_applies_to_close_notes_contains() -> None:
    # 'feed truncated' -> tokens 'feed' + 'truncated', both in INC1's close notes.
    # (The instance exposes no cause substring filter; close_notes_contains is the supported
    # substring filter for resolution/close-note evidence.)
    assert _matching(close_notes_contains="feed truncated") == {"INC0000001"}


# -- guard against the bundled fixture (real-world improvement) ----------------


def test_bundled_fixture_missing_data_no_longer_empty() -> None:
    client = ServiceNowClient(ServiceNowConfig(mode="mock"))  # bundled 22-incident fixture
    envelope = asyncio.run(
        client.list_incidents(filters={"description_contains": "missing data"}, limit=50)
    )
    # Contiguous matching returned 0 here; tokenized matching finds the tickets
    # whose text contains both words.
    assert envelope["result_count"] >= 1


# -- historical resolution / similar-incident search (TC-042) ------------------


def _fixture_matching(**filters) -> set[str]:
    """Match against the bundled 22-incident fixture (INC3235130 / INC3011201)."""

    client = ServiceNowClient(ServiceNowConfig(mode="mock"))
    envelope = asyncio.run(client.list_incidents(filters=filters, limit=50))
    return {
        (inc["number"]["display_value"] if isinstance(inc["number"], dict) else inc["number"])
        for inc in envelope["incidents"]
    }


def test_similar_incident_overnarrowed_on_dataset_finds_nothing() -> None:
    # The second TC-042 trap: narrowing the search to the specific DATASET /
    # business service ('Deposit Account Master') or to short-description wording
    # copied from the source incident AND-drops the cross-dataset match. The agent
    # must search the broad SEGMENT ('Core Banking'), not the dataset.
    assert _fixture_matching(
        description_contains="Core Banking deposit account master", state="Closed"
    ) == set()
    assert _fixture_matching(
        description_contains="deposit account master", state="Closed"
    ) == set()


def test_similar_incident_by_datasource_finds_historical_match() -> None:
    # The recipe the subagent prompt now spells out: search by DATA SOURCE +
    # resolved/closed, WITHOUT over-constraining on the source incident's exact
    # cause. This surfaces INC3011201, whose resolution_notes answer the query.
    assert "INC3011201" in _fixture_matching(
        description_contains="Core Banking", state="Closed"
    )


# -- cause: lenient partial match in mock mode (SNCLIENT-CAUSE-PARTIAL) ---------


def test_cause_exact_match_still_works() -> None:
    # A full, exact cause label keeps matching exactly as before.
    assert _matching(cause="schema drift") == {"INC0000002"}
    assert _matching(cause="vendor delay") == {"INC0000003"}


def test_cause_partial_term_matches_full_label() -> None:
    # The live instance is exact-match, but mock mode is lenient so a user who
    # supplies only PART of a cause ('cluster', 'network') still surfaces the
    # incident whose stored cause is the full phrase. Here a single token of each
    # multi-word cause matches its incident.
    assert _matching(cause="truncated") == {"INC0000001"}  # 'upstream feed truncated'
    assert _matching(cause="drift") == {"INC0000002"}  # 'schema drift'
    assert _matching(cause="vendor") == {"INC0000003"}  # 'vendor delay'


def test_cause_multi_token_partial_is_and_of_tokens() -> None:
    # Every token of the supplied term must appear in the stored cause label, so a
    # subset of the phrase matches but an extra absent token drops it.
    assert _matching(cause="feed truncated") == {"INC0000001"}
    assert _matching(cause="truncated nonexistent") == set()


def test_cause_off_list_term_still_matches_nothing() -> None:
    # Leniency is partial-substring only — an unrelated term still finds nothing.
    assert _matching(cause="banana") == set()


# -- normalize_cause: partial USER input -> full VALID_CAUSES label -------------
# These exercise the UPSTREAM resolver (not the mock matcher above) — the layer
# that gives partial cause input effect in REAL mode by resolving a loose term to
# a full label before it reaches the wire.


def test_normalize_cause_exact_canonicalizes_casing() -> None:
    # A full label in any casing returns the canonical stored casing.
    assert normalize_cause("data quality") == "Data Quality"
    assert normalize_cause("SUBNET ISSUE") == "Subnet Issue"


def test_normalize_cause_partial_resolves_to_full_label() -> None:
    # A partial term unique to one label resolves to that full label, so REAL mode
    # can send the exact value the wire requires.
    assert normalize_cause("subnet") == "Subnet Issue"
    assert normalize_cause("network cluster") == "Network Cluster Issue"
    # Order-independent AND-of-tokens.
    assert normalize_cause("cluster network") == "Network Cluster Issue"


def test_normalize_cause_ambiguous_term_is_rejected_with_candidates() -> None:
    # A term whose tokens match 2+ labels must not silently pick one. 'network'
    # alone matches both Network labels -> raise and list them.
    try:
        normalize_cause("network")
    except ServiceNowToolInputError as exc:
        assert "Network Cluster Issue" in str(exc)
        assert "Network or Connectivity Issue" in str(exc)
    else:  # pragma: no cover - the assertion below reports the miss
        raise AssertionError("ambiguous 'network' should raise")


def test_normalize_cause_off_list_term_is_rejected() -> None:
    # A term matching NO label (exact or partial) is rejected with the full set.
    try:
        normalize_cause("banana")
    except ServiceNowToolInputError as exc:
        assert "Data Quality" in str(exc)  # full valid set is listed
    else:  # pragma: no cover
        raise AssertionError("off-list 'banana' should raise")


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
