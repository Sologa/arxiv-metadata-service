from __future__ import annotations

import sqlite3

from arxiv_meta.data import ArxivMetaBuilder


def test_schema_tables_exist(sample_db):
    with sqlite3.connect(sample_db) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
            )
        }

    assert {"papers", "paper_categories", "title_fts", "paper_fts", "import_runs"} <= tables


def test_import_records_categories_in_order(sample_db):
    with sqlite3.connect(sample_db) as conn:
        rows = conn.execute(
            """
            SELECT category, position, is_primary
            FROM paper_categories
            WHERE paper_id = '0704.0001'
            ORDER BY position
            """
        ).fetchall()

    assert rows == [("cs.CL", 0, 1), ("cs.AI", 1, 0)]


def test_import_run_audit_metadata(sample_db, sample_jsonl):
    with sqlite3.connect(sample_db) as conn:
        row = conn.execute(
            """
            SELECT source_snapshot_path, started_at, finished_at,
                   records_seen, records_imported, json_decode_errors, schema_version
            FROM import_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    assert row[0] == str(sample_jsonl)
    assert row[1]
    assert row[2]
    assert row[3] == 5
    assert row[4] == 5
    assert row[5] == 0
    assert row[6] == "2"


def test_title_fts_does_not_index_abstract(sample_db):
    with sqlite3.connect(sample_db) as conn:
        title_hits = conn.execute(
            """
            SELECT p.arxiv_id
            FROM title_fts
            JOIN papers p ON p.rowid = title_fts.rowid
            WHERE title_fts MATCH '"Quantum"'
            """
        ).fetchall()
        abstract_hits = conn.execute(
            """
            SELECT p.arxiv_id
            FROM title_fts
            JOIN papers p ON p.rowid = title_fts.rowid
            WHERE title_fts MATCH '"hiddenabstractneedle"'
            """
        ).fetchall()

    assert [row[0] for row in title_hits] == ["0704.0001"]
    assert abstract_hits == []


def test_staging_build_copies_target_database(tmp_path, sample_jsonl):
    target_db = tmp_path / "target.sqlite"
    staging_dir = tmp_path / "staging"

    stats = ArxivMetaBuilder(target_db).build(
        sample_jsonl,
        batch_size=2,
        staging_dir=staging_dir,
    )

    assert stats.db_path == str(target_db)
    assert target_db.exists()
    assert not list(staging_dir.glob("*.building.*.sqlite"))
    with sqlite3.connect(target_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 5
