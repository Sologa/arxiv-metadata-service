#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Data download and import module
#
# Download delegated to hfpclawer.download.KaggleDownloader (via hfpclawer[arxiv])
# SQLite FTS5 index building kept as local implementation.

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

from arxiv_meta.config import get as cfg_get

logger = logging.getLogger("arxiv_meta.data")


# ════════════════════════════════════════════
# Dataset download (delegated to hfpclawer)
# ════════════════════════════════════════════


def download_dataset(force: bool = False) -> Path:
    """Download arXiv metadata dataset from Kaggle (delegated to hfpclawer KaggleDownloader)

    Args:
        force: Re-download even if file exists

    Returns:
        JSONL file path
    """
    from hfpclawer.download.kaggle import KaggleDownloader

    data_dir = Path(cfg_get("data.dir", "data")).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)

    dl = KaggleDownloader(
        data_dir=str(data_dir),
    )

    dl.run(force=force)

    jsonl_path = dl.jsonl_path()
    if not jsonl_path.exists():
        raise FileNotFoundError(f"KaggleDownloader did not produce JSONL: {jsonl_path}")

    logger.info(f"Dataset ready: {jsonl_path} ({jsonl_path.stat().st_size / 1024 / 1024:.0f} MB)")
    return jsonl_path


# ════════════════════════════════════════════
# SQLite FTS5 Index Building
# ════════════════════════════════════════════

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS arxiv_fts USING fts5(
    arxiv_id UNINDEXED,
    title,
    authors,
    abstract,
    categories UNINDEXED,
    doi UNINDEXED,
    journal_ref UNINDEXED,
    update_date UNINDEXED,
    tokenize='porter unicode61'
);
"""

META_SCHEMA = """
CREATE TABLE IF NOT EXISTS arxiv_meta (
    arxiv_id TEXT PRIMARY KEY,
    title TEXT,
    authors TEXT,
    abstract TEXT,
    categories TEXT,
    doi TEXT,
    journal_ref TEXT,
    update_date TEXT,
    imported_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_arxiv_meta_date ON arxiv_meta(update_date);
CREATE INDEX IF NOT EXISTS idx_arxiv_meta_cat ON arxiv_meta(categories);
CREATE INDEX IF NOT EXISTS idx_arxiv_meta_doi ON arxiv_meta(doi);
"""


class ArxivMetaBuilder:
    """Dataset builder — parse JSONL → SQLite FTS5"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or cfg_get("db.path", "data/arxiv_meta.db")
        if not os.path.isabs(self.db_path):
            base = Path(__file__).parent.parent
            self.db_path = str(base / self.db_path)
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA cache_size=-80000")  # 80MB cache
        conn.execute("PRAGMA temp_store=MEMORY")
        return conn

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript(FTS_SCHEMA)
            conn.executescript(META_SCHEMA)
        logger.info(f"SQLite ready: {self.db_path}")

    def count(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM arxiv_meta").fetchone()[0]

    def build(self, jsonl_path: str, batch_size: int = 2000) -> int:
        """Build FTS5 index from JSONL file

        Args:
            jsonl_path: JSON Lines file path
            batch_size: Rows per batch write

        Returns:
            Total number of imported papers
        """
        total = 0
        batch = []
        start = time.time()
        existing_count = self.count()
        logger.info(f"Starting import: {jsonl_path}")
        logger.info(f"Existing: {existing_count:,} papers")

        with open(jsonl_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    paper = json.loads(line)
                    arxiv_id = paper.get("id", "")
                    if not arxiv_id:
                        continue
                    batch.append((
                        arxiv_id,
                        (paper.get("title", "") or "")[:500],
                        (paper.get("authors", "") or "")[:500],
                        (paper.get("abstract", "") or "")[:2000],
                        (paper.get("categories", "") or ""),
                        (paper.get("doi", "") or ""),
                        (paper.get("journal_ref", "") or "")[:200],
                        (paper.get("update_date", "") or ""),
                    ))
                    total += 1
                except json.JSONDecodeError:
                    continue

                if len(batch) >= batch_size:
                    self._import_batch(batch)
                    batch = []
                    self._log_progress(total, start)

        if batch:
            self._import_batch(batch)

        elapsed = time.time() - start
        new_count = self.count() - existing_count
        logger.info(
            f"Import complete: total={total:,}, new={new_count:,}, "
            f"time={elapsed:.0f}s ({total/elapsed:.0f} papers/s)"
        )
        return total

    def _import_batch(self, batch: list[tuple]):
        with self._lock, self._conn() as conn:
            conn.executemany(
                """INSERT OR IGNORE INTO arxiv_meta
                   (arxiv_id, title, authors, abstract, categories,
                    doi, journal_ref, update_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                batch,
            )
            conn.executemany(
                """INSERT OR IGNORE INTO arxiv_fts
                   (arxiv_id, title, authors, abstract, categories,
                    doi, journal_ref, update_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                batch,
            )
            conn.commit()

    def _log_progress(self, total: int, start: float):
        elapsed = time.time() - start
        rate = total / elapsed if elapsed > 0 else 0
        if total % 50000 == 0:
            logger.info(f"Imported {total:,} papers ({rate:.0f} papers/s, {elapsed:.0f}s)...")


# ════════════════════════════════════════════
# Convenience functions
# ════════════════════════════════════════════


def ensure_dataset() -> int:
    """Ensure the dataset is downloaded and imported

    Returns:
        Total number of papers
    """
    jsonl_path = download_dataset()
    builder = ArxivMetaBuilder()
    if builder.count() == 0:
        total = builder.build(str(jsonl_path))
        return total
    else:
        return builder.count()
