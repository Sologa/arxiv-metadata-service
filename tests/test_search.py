from __future__ import annotations

import pytest

from arxiv_meta.search import ArxivSearch, InvalidDateFilter, InvalidFTSQuery, QueryLimitError


def test_exact_arxiv_id_lookup(sample_db):
    engine = ArxivSearch(sample_db)

    paper = engine.get_by_id("0704.0001")

    assert paper is not None
    assert paper["arxiv_id"] == "0704.0001"
    assert paper["categories"] == ["cs.CL", "cs.AI"]
    assert engine.get_by_id("9999.99999") is None


def test_title_search_and_abstract_only_negative(sample_db):
    engine = ArxivSearch(sample_db)

    title_hits = engine.search("Quantum Widget")
    abstract_hits = engine.search("hiddenabstractneedle")

    assert [row["arxiv_id"] for row in title_hits] == ["0704.0001"]
    assert abstract_hits == []


def test_category_filter_is_exact_token(sample_db):
    engine = ArxivSearch(sample_db)

    assert engine.search("Graph", categories=["cs.CL"]) == []
    hits = engine.search("Graph", categories=["cs.CLG"])

    assert [row["arxiv_id"] for row in hits] == ["2401.00002"]


def test_update_date_filters_are_inclusive(sample_db):
    engine = ArxivSearch(sample_db)

    hits = engine.search(
        "Metadata",
        update_date_from="2024-01-02",
        update_date_to="2024-01-02",
    )

    assert [row["arxiv_id"] for row in hits] == ["2401.00001"]


def test_full_title_punctuation_is_sanitized(sample_db):
    engine = ArxivSearch(sample_db)

    hits = engine.search('Punctuation: Title - Boundary (Quoted "Example")')
    en_dash_hits = engine.search("Punctuation: Title – Boundary — Quoted Example")

    assert [row["arxiv_id"] for row in hits] == ["2502.00001"]
    assert [row["arxiv_id"] for row in en_dash_hits] == ["2502.00001"]


def test_candidate_search_uses_subset_or_review_candidates(sample_db):
    engine = ArxivSearch(sample_db)

    strict_hits = engine.search("Taxonomy Metadata Unmatched")
    candidates = engine.candidate_search("Taxonomy Metadata Unmatched", limit=3)

    assert strict_hits == []
    assert candidates
    assert candidates[0]["source_id"] == "2401.00001"
    assert candidates[0]["review_candidate"] is True
    assert candidates[0]["auto_accept"] is False
    assert "taxonomy" in candidates[0]["shared_tokens"]
    assert "metadata" in candidates[0]["shared_tokens"]
    assert candidates[0]["evidence"]["retrieval_strategy"] == "fts5_or_review_candidate"


def test_candidate_search_supports_filters(sample_db):
    engine = ArxivSearch(sample_db)

    hits = engine.candidate_search(
        "Punctuation Example Missing",
        categories=["cs.CL"],
        update_date_from="2025-01-01",
        update_date_to="2025-12-31",
    )

    assert [row["source_id"] for row in hits] == ["2502.00001"]


def test_optimized_read_connection_uses_read_only_pragmas(sample_db):
    engine = ArxivSearch(
        sample_db,
        immutable=True,
        query_only=True,
        mmap_size=1048576,
        cache_size=-1234,
    )

    assert "mode=ro" in engine._database_uri()
    assert "immutable=1" in engine._database_uri()
    with engine._conn() as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        assert conn.execute("PRAGMA cache_size").fetchone()[0] == -1234
        assert conn.execute("PRAGMA mmap_size").fetchone()[0] == 1048576
        with pytest.raises(Exception):
            conn.execute("CREATE TABLE should_not_write (id INTEGER)")


def test_candidate_search_batch_reuses_one_connection(sample_db, monkeypatch):
    engine = ArxivSearch(sample_db)
    original_conn = engine._conn
    calls = 0

    def counted_conn():
        nonlocal calls
        calls += 1
        return original_conn()

    monkeypatch.setattr(engine, "_conn", counted_conn)

    batch = engine.candidate_search_batch(
        [
            {"query": "Taxonomy Metadata Unmatched", "limit": 3},
            {"query": "Punctuation Example Missing", "limit": 3, "categories": ["cs.CL"]},
        ]
    )

    assert calls == 1
    assert [item["query"] for item in batch] == [
        "Taxonomy Metadata Unmatched",
        "Punctuation Example Missing",
    ]
    assert batch[0]["results"][0]["source_id"] == "2401.00001"
    assert batch[1]["results"][0]["source_id"] == "2502.00001"
    assert all(candidate["auto_accept"] is False for item in batch for candidate in item["results"])


def test_candidate_search_details_are_opt_in(sample_db):
    engine = ArxivSearch(sample_db)

    light = engine.candidate_search("Taxonomy Metadata Unmatched", limit=1)
    detailed = engine.candidate_search("Taxonomy Metadata Unmatched", limit=1, include_details=True)

    assert "details" not in light[0]
    assert detailed[0]["source_id"] == "2401.00001"
    assert detailed[0]["details"]["abstract"] == "A fixture record for metadata query behavior."
    assert detailed[0]["details"]["authors"] == "Carol Example"
    assert detailed[0]["auto_accept"] is False


def test_invalid_query_and_limits(sample_db):
    engine = ArxivSearch(sample_db)

    with pytest.raises(InvalidFTSQuery):
        engine.search("***")
    with pytest.raises(InvalidFTSQuery):
        engine.search("title:Quantum")
    with pytest.raises(InvalidFTSQuery):
        engine.search("Quantum*")
    with pytest.raises(InvalidFTSQuery):
        engine.search("^Quantum")
    with pytest.raises(InvalidFTSQuery):
        engine.search("NEAR(Quantum Widget, 5)")
    with pytest.raises(InvalidFTSQuery):
        engine.search("(Quantum OR Widget)")
    with pytest.raises(QueryLimitError):
        engine.search("x" * 257)
    with pytest.raises(QueryLimitError):
        engine.search("Quantum", limit=501)
    with pytest.raises(InvalidDateFilter):
        engine.search("Quantum", update_date_from="2024-99-01")
