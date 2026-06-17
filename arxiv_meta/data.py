#!/usr/bin/env python3
"""Streaming JSONL import and SQLite schema management."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable

from arxiv_meta.config import get as cfg_get
from arxiv_meta.config import resolve_path

logger = logging.getLogger("arxiv_meta.data")

SCHEMA_VERSION = "2"

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS papers (
  rowid INTEGER PRIMARY KEY,
  arxiv_id TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  title_norm TEXT,
  authors TEXT,
  abstract TEXT,
  categories_raw TEXT,
  primary_category TEXT,
  doi TEXT,
  journal_ref TEXT,
  update_date TEXT,
  first_version_date TEXT,
  source_snapshot_path TEXT,
  imported_at TEXT DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_papers_arxiv_id ON papers(arxiv_id);
CREATE INDEX IF NOT EXISTS idx_papers_update_date ON papers(update_date);
CREATE INDEX IF NOT EXISTS idx_papers_primary_category ON papers(primary_category);
CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi);

CREATE TABLE IF NOT EXISTS paper_categories (
  paper_id TEXT NOT NULL,
  category TEXT NOT NULL,
  position INTEGER NOT NULL,
  is_primary INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (paper_id, category),
  FOREIGN KEY (paper_id) REFERENCES papers(arxiv_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_paper_categories_category ON paper_categories(category);
CREATE INDEX IF NOT EXISTS idx_paper_categories_paper_id ON paper_categories(paper_id);

CREATE VIRTUAL TABLE IF NOT EXISTS title_fts USING fts5(
  title,
  content='papers',
  content_rowid='rowid',
  tokenize='porter unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS paper_fts USING fts5(
  title,
  abstract,
  authors,
  content='papers',
  content_rowid='rowid',
  tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS import_runs (
  id INTEGER PRIMARY KEY,
  source_snapshot_path TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  records_seen INTEGER DEFAULT 0,
  records_imported INTEGER DEFAULT 0,
  json_decode_errors INTEGER DEFAULT 0,
  schema_version TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class ImportStats:
    source_snapshot_path: str
    db_path: str
    records_seen: int
    records_imported: int
    json_decode_errors: int
    started_at: str
    finished_at: str
    elapsed_seconds: float


@dataclass(frozen=True)
class OptimizeStats:
    db_path: str
    size_before_bytes: int
    size_after_bytes: int
    elapsed_seconds: float
    vacuum_run: bool = False


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_title(title: str) -> str:
    return " ".join((title or "").split())


def parse_first_version_date(record: dict[str, Any]) -> str | None:
    versions = record.get("versions") or []
    if not isinstance(versions, list) or not versions:
        return None
    created = versions[0].get("created") if isinstance(versions[0], dict) else None
    if not created:
        return None
    try:
        return parsedate_to_datetime(created).date().isoformat()
    except (TypeError, ValueError, IndexError):
        return str(created)


def category_tokens(categories_raw: str) -> list[str]:
    return [token for token in (categories_raw or "").split() if token]


def paper_tuple(record: dict[str, Any], source_snapshot_path: str) -> tuple[Any, ...] | None:
    arxiv_id = str(record.get("id") or "").strip()
    title = normalize_title(str(record.get("title") or ""))
    if not arxiv_id or not title:
        return None

    categories_raw = str(record.get("categories") or "").strip()
    categories = category_tokens(categories_raw)
    journal_ref = record.get("journal-ref", record.get("journal_ref", ""))

    return (
        arxiv_id,
        title,
        title.casefold(),
        str(record.get("authors") or ""),
        str(record.get("abstract") or ""),
        categories_raw,
        categories[0] if categories else None,
        str(record.get("doi") or "").strip(),
        str(journal_ref or ""),
        str(record.get("update_date") or "").strip(),
        parse_first_version_date(record),
        source_snapshot_path,
    )


def category_rows(paper: tuple[Any, ...]) -> Iterable[tuple[str, str, int, int]]:
    arxiv_id = paper[0]
    for idx, category in enumerate(category_tokens(paper[5] or "")):
        yield (arxiv_id, category, idx, 1 if idx == 0 else 0)


class ArxivMetaBuilder:
    """Build the local SQLite metadata database from a JSONL snapshot."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = str(resolve_path(db_path or cfg_get("db.path")))

    def _conn(self) -> sqlite3.Connection:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # Python 3.12's sqlite build cannot write rollback journal files on the
        # target ExFAT volume, while WAL/SHM can be unreliable there. The build
        # is a resettable single-writer operation, so an in-memory journal keeps
        # the generated DB path usable without SHM files.
        conn.execute("PRAGMA journal_mode=MEMORY")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-80000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init_db(self, reset: bool = False) -> None:
        if reset:
            self.remove_existing_database()
        with self._conn() as conn:
            conn.executescript(SCHEMA_SQL)

    def remove_existing_database(self) -> None:
        for suffix in ("", "-journal", "-wal", "-shm"):
            path = Path(f"{self.db_path}{suffix}")
            if path.exists():
                path.unlink()

    def count(self) -> int:
        try:
            with self._conn() as conn:
                return conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        except sqlite3.OperationalError:
            return 0

    def build(
        self,
        jsonl_path: str | Path,
        batch_size: int = 2000,
        reset: bool = True,
        staging_dir: str | Path | None = None,
    ) -> ImportStats:
        """Stream a JSONL snapshot into SQLite.

        The input file is read line by line. The full snapshot is never loaded
        into memory.
        """
        if staging_dir is not None:
            return self._build_with_staging(jsonl_path, batch_size=batch_size, staging_dir=staging_dir)

        return self._build_current_db(jsonl_path=jsonl_path, batch_size=batch_size, reset=reset)

    def _build_with_staging(
        self,
        jsonl_path: str | Path,
        batch_size: int,
        staging_dir: str | Path,
    ) -> ImportStats:
        target_db_path = Path(self.db_path)
        staging_root = resolve_path(staging_dir)
        staging_root.mkdir(parents=True, exist_ok=True)
        staging_path = staging_root / f"{target_db_path.name}.building.{os.getpid()}.sqlite"

        staging_builder = ArxivMetaBuilder(db_path=staging_path)
        try:
            stats = staging_builder._build_current_db(
                jsonl_path=jsonl_path,
                batch_size=batch_size,
                reset=True,
            )
            self.remove_existing_database()
            target_db_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staging_path, target_db_path)
            return ImportStats(
                source_snapshot_path=stats.source_snapshot_path,
                db_path=str(target_db_path),
                records_seen=stats.records_seen,
                records_imported=stats.records_imported,
                json_decode_errors=stats.json_decode_errors,
                started_at=stats.started_at,
                finished_at=stats.finished_at,
                elapsed_seconds=stats.elapsed_seconds,
            )
        finally:
            for suffix in ("", "-journal", "-wal", "-shm"):
                path = Path(f"{staging_path}{suffix}")
                if path.exists():
                    path.unlink()

    def _build_current_db(
        self,
        jsonl_path: str | Path,
        batch_size: int = 2000,
        reset: bool = True,
    ) -> ImportStats:
        """Build directly into ``self.db_path``."""
        jsonl = resolve_path(jsonl_path)
        if not jsonl.exists():
            raise FileNotFoundError(f"JSONL file not found: {jsonl}")

        self.init_db(reset=reset)

        started_at = utc_now()
        started_perf = time.perf_counter()
        records_seen = 0
        records_imported = 0
        json_decode_errors = 0

        with self._conn() as conn:
            run_id = conn.execute(
                """
                INSERT INTO import_runs (source_snapshot_path, started_at, schema_version)
                VALUES (?, ?, ?)
                """,
                (str(jsonl), started_at, SCHEMA_VERSION),
            ).lastrowid
            conn.commit()

            batch: list[tuple[Any, ...]] = []
            delete_existing_categories = not reset
            with jsonl.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    records_seen += 1
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        json_decode_errors += 1
                        continue

                    if not isinstance(record, dict):
                        continue

                    paper = paper_tuple(record, str(jsonl))
                    if paper is None:
                        continue
                    batch.append(paper)
                    if len(batch) >= batch_size:
                        records_imported += self._import_batch(
                            conn, batch, delete_existing_categories=delete_existing_categories
                        )
                        batch = []
                        self._log_progress(records_seen, records_imported, started_perf)

            if batch:
                records_imported += self._import_batch(
                    conn, batch, delete_existing_categories=delete_existing_categories
                )

            logger.info("Rebuilding FTS indexes")
            self.rebuild_title_fts(conn)
            self.rebuild_paper_fts(conn)
            records_imported = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
            finished_at = utc_now()
            elapsed_seconds = time.perf_counter() - started_perf

            conn.execute(
                """
                UPDATE import_runs
                SET finished_at = ?,
                    records_seen = ?,
                    records_imported = ?,
                    json_decode_errors = ?
                WHERE id = ?
                """,
                (finished_at, records_seen, records_imported, json_decode_errors, run_id),
            )
            conn.commit()

        logger.info(
            "Import complete: seen=%s imported=%s json_errors=%s elapsed=%.1fs",
            f"{records_seen:,}",
            f"{records_imported:,}",
            f"{json_decode_errors:,}",
            elapsed_seconds,
        )
        return ImportStats(
            source_snapshot_path=str(jsonl),
            db_path=self.db_path,
            records_seen=records_seen,
            records_imported=records_imported,
            json_decode_errors=json_decode_errors,
            started_at=started_at,
            finished_at=finished_at,
            elapsed_seconds=elapsed_seconds,
        )

    def _import_batch(
        self,
        conn: sqlite3.Connection,
        batch: list[tuple[Any, ...]],
        delete_existing_categories: bool,
    ) -> int:
        category_batch = [row for paper in batch for row in category_rows(paper)]
        conn.executemany(
            """
            INSERT OR REPLACE INTO papers (
              arxiv_id, title, title_norm, authors, abstract, categories_raw,
              primary_category, doi, journal_ref, update_date,
              first_version_date, source_snapshot_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            batch,
        )
        if category_batch:
            if delete_existing_categories:
                paper_ids = [(paper[0],) for paper in batch]
                conn.executemany("DELETE FROM paper_categories WHERE paper_id = ?", paper_ids)
            conn.executemany(
                """
                INSERT OR REPLACE INTO paper_categories
                  (paper_id, category, position, is_primary)
                VALUES (?, ?, ?, ?)
                """,
                category_batch,
            )
        conn.commit()
        return len(batch)

    def rebuild_title_fts(self, conn: sqlite3.Connection | None = None) -> None:
        if conn is None:
            with self._conn() as owned_conn:
                owned_conn.execute("INSERT INTO title_fts(title_fts) VALUES('rebuild')")
                owned_conn.commit()
        else:
            conn.execute("INSERT INTO title_fts(title_fts) VALUES('rebuild')")
            conn.commit()

    def rebuild_paper_fts(self, conn: sqlite3.Connection | None = None) -> None:
        if conn is None:
            with self._conn() as owned_conn:
                owned_conn.execute("INSERT INTO paper_fts(paper_fts) VALUES('rebuild')")
                owned_conn.commit()
        else:
            conn.execute("INSERT INTO paper_fts(paper_fts) VALUES('rebuild')")
            conn.commit()

    def rebuild_paper_fts_index(self) -> OptimizeStats:
        """Create or refresh the multi-field paper FTS index on an existing DB."""
        db_path = Path(self.db_path)
        if not db_path.exists():
            raise FileNotFoundError(self.db_path)

        size_before = db_path.stat().st_size
        started = time.perf_counter()
        self.init_db(reset=False)
        with self._conn() as conn:
            self.rebuild_paper_fts(conn)
        elapsed = time.perf_counter() - started
        size_after = db_path.stat().st_size
        return OptimizeStats(
            db_path=self.db_path,
            size_before_bytes=size_before,
            size_after_bytes=size_after,
            elapsed_seconds=elapsed,
            vacuum_run=False,
        )

    def optimize_title_fts(self) -> OptimizeStats:
        """Run FTS5 segment optimization without VACUUM."""
        db_path = Path(self.db_path)
        if not db_path.exists():
            raise FileNotFoundError(self.db_path)

        size_before = db_path.stat().st_size
        started = time.perf_counter()
        with self._conn() as conn:
            conn.execute("INSERT INTO title_fts(title_fts) VALUES('optimize')")
            conn.commit()
        elapsed = time.perf_counter() - started
        size_after = db_path.stat().st_size
        return OptimizeStats(
            db_path=self.db_path,
            size_before_bytes=size_before,
            size_after_bytes=size_after,
            elapsed_seconds=elapsed,
            vacuum_run=False,
        )

    def _log_progress(self, records_seen: int, records_imported: int, started_perf: float) -> None:
        if records_seen % 50000 != 0:
            return
        elapsed = time.perf_counter() - started_perf
        rate = records_seen / elapsed if elapsed else 0
        logger.info(
            "Imported %s/%s records at %.0f records/s",
            f"{records_imported:,}",
            f"{records_seen:,}",
            rate,
        )


def download_dataset(force: bool = False) -> Path:
    """Dataset download is intentionally disabled for the local-first service."""
    raise RuntimeError(
        "Automatic download is disabled. Build from a local JSONL snapshot with "
        "`arxiv-meta build --jsonl PATH --db PATH`."
    )


def ensure_dataset() -> int:
    """Compatibility helper that validates the configured DB exists."""
    builder = ArxivMetaBuilder()
    return builder.count()
