from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from arxiv_meta import server
from arxiv_meta.data import ArxivMetaBuilder
from arxiv_meta.search import ArxivSearch, InvalidFTSQuery, SearchError


@pytest.fixture()
def boolean_jsonl() -> Path:
    return Path(__file__).parents[1] / "fixtures" / "boolean_backend" / "arxiv_boolean_sample.jsonl"


@pytest.fixture()
def boolean_db(tmp_path: Path, boolean_jsonl: Path) -> Path:
    db_path = tmp_path / "boolean_backend.sqlite"
    ArxivMetaBuilder(db_path=db_path).build(boolean_jsonl, batch_size=2)
    return db_path


def test_paper_fts_indexes_abstract_and_authors(boolean_db: Path):
    with sqlite3.connect(boolean_db) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
            )
        }
        abstract_rows = conn.execute(
            """
            SELECT p.arxiv_id
            FROM paper_fts
            JOIN papers p ON p.rowid = paper_fts.rowid
            WHERE paper_fts MATCH 'abstract : "taxonomyneedle"'
            """
        ).fetchall()
        author_rows = conn.execute(
            """
            SELECT p.arxiv_id
            FROM paper_fts
            JOIN papers p ON p.rowid = paper_fts.rowid
            WHERE paper_fts MATCH 'authors : "booleanauthorneedle"'
            """
        ).fetchall()

    assert "paper_fts" in tables
    assert [row[0] for row in abstract_rows] == ["2601.00002"]
    assert [row[0] for row in author_rows] == ["2601.00004"]


def test_rebuild_paper_fts_can_upgrade_existing_database(boolean_db: Path):
    with sqlite3.connect(boolean_db) as conn:
        conn.execute("DROP TABLE paper_fts")
        conn.commit()

    ArxivMetaBuilder(boolean_db).rebuild_paper_fts_index()

    with sqlite3.connect(boolean_db) as conn:
        rows = conn.execute(
            """
            SELECT p.arxiv_id
            FROM paper_fts
            JOIN papers p ON p.rowid = paper_fts.rowid
            WHERE paper_fts MATCH 'abstract : "taxonomyneedle"'
            """
        ).fetchall()

    assert [row[0] for row in rows] == ["2601.00002"]


def test_boolean_backend_combines_text_or_with_category_filter(boolean_db: Path):
    engine = ArxivSearch(boolean_db)

    results = engine.boolean_search(
        {
            "op": "AND",
            "children": [
                {
                    "op": "OR",
                    "children": [
                        {"field": "title", "match": {"type": "phrase", "value": "Taxonomy Survey"}},
                        {"field": "abstract", "match": {"type": "term", "value": "taxonomyneedle"}},
                    ],
                },
                {"field": "category", "match": {"type": "term", "value": "cs.CL"}},
            ],
        },
        limit=10,
    )

    assert {row["arxiv_id"] for row in results} == {"2601.00001", "2601.00002"}
    assert all("score" in row for row in results)


def test_boolean_backend_supports_text_prefix_match(boolean_db: Path):
    engine = ArxivSearch(boolean_db)

    results = engine.boolean_search(
        {"field": "title", "match": {"type": "prefix", "value": "taxonom"}},
        limit=10,
    )

    assert [row["arxiv_id"] for row in results] == ["2601.00001"]


def test_boolean_backend_accepts_trailing_star_in_prefix_value(boolean_db: Path):
    engine = ArxivSearch(boolean_db)

    results = engine.boolean_search(
        {"field": "title", "match": {"type": "prefix", "value": "taxonom*"}},
        limit=10,
    )

    assert [row["arxiv_id"] for row in results] == ["2601.00001"]


def test_boolean_backend_supports_metadata_or_and_date_range(boolean_db: Path):
    engine = ArxivSearch(boolean_db)

    results = engine.boolean_search(
        {
            "op": "AND",
            "children": [
                {
                    "op": "OR",
                    "children": [
                        {"field": "category", "match": {"type": "term", "value": "cs.AI"}},
                        {"field": "primary_category", "match": {"type": "term", "value": "stat.ML"}},
                    ],
                },
                {"field": "update_date", "range": {"gte": "2026-01-03", "lte": "2026-01-05"}},
            ],
        },
        limit=10,
        sort="date",
    )

    assert [row["arxiv_id"] for row in results] == ["2601.00004", "2601.00002"]


def test_boolean_backend_supports_andnot(boolean_db: Path):
    engine = ArxivSearch(boolean_db)

    results = engine.boolean_search(
        {
            "op": "ANDNOT",
            "children": [
                {"field": "all", "match": {"type": "term", "value": "fixture"}},
                {"field": "category", "match": {"type": "term", "value": "cs.CV"}},
            ],
        },
        limit=10,
        sort="date",
    )

    assert [row["arxiv_id"] for row in results] == ["2601.00004", "2601.00001"]


def test_boolean_backend_all_field_reaches_authors(boolean_db: Path):
    engine = ArxivSearch(boolean_db)

    results = engine.boolean_search(
        {"field": "all", "match": {"type": "term", "value": "booleanauthorneedle"}},
        limit=5,
    )

    assert [row["arxiv_id"] for row in results] == ["2601.00004"]


def test_boolean_backend_rejects_unsupported_fields_and_match_types(boolean_db: Path):
    engine = ArxivSearch(boolean_db)

    with pytest.raises(SearchError):
        engine.boolean_search({"field": "venue", "match": {"type": "term", "value": "ACL"}})
    with pytest.raises(SearchError):
        engine.boolean_search({"field": "title", "match": {"type": "regex", "value": "tax.*"}})
    with pytest.raises(SearchError):
        engine.boolean_search({"field": "title", "match": {"type": "prefix", "value": "ta*xo"}})


def test_existing_plain_search_still_rejects_raw_grouped_boolean(boolean_db: Path):
    engine = ArxivSearch(boolean_db)

    with pytest.raises(InvalidFTSQuery):
        engine.search('(Taxonomy OR Graph)')


def test_existing_plain_search_still_rejects_raw_prefix(boolean_db: Path):
    engine = ArxivSearch(boolean_db)

    with pytest.raises(InvalidFTSQuery):
        engine.search("taxonom*")


def test_boolean_search_api_uses_normalized_query_object(boolean_db: Path):
    server.configure_engine(str(boolean_db))
    client = TestClient(server.app)

    response = client.post(
        "/boolean-search",
        json={
            "query_object": {
                "op": "AND",
                "children": [
                    {"field": "abstract", "match": {"type": "term", "value": "taxonomyneedle"}},
                    {"field": "category", "match": {"type": "term", "value": "cs.AI"}},
                ],
            },
            "limit": 5,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "boolean"
    assert body["compiled"]["operators"] == ["AND"]
    assert body["compiled"]["fields"] == ["abstract", "category"]
    assert body["compiled"]["text_fields"] == ["abstract"]
    assert body["compiled"]["metadata_fields"] == ["category"]
    assert body["total"] == 1
    assert body["results"][0]["arxiv_id"] == "2601.00002"


def test_boolean_search_api_supports_prefix_match(boolean_db: Path):
    server.configure_engine(str(boolean_db))
    client = TestClient(server.app)

    response = client.post(
        "/boolean-search",
        json={
            "query_object": {"field": "title", "match": {"type": "prefix", "value": "taxonom"}},
            "limit": 5,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["results"][0]["arxiv_id"] == "2601.00001"
