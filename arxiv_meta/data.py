# 数据下载与导入模块
#
# 下载委托给 hfpclawer.download.KaggleDownloader（通过 hfpclawer[arxiv] 引入）
# SQLite FTS5 索引构建保持本地实现。

import gzip
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
# 数据集下载（委托 hfpclawer）
# ════════════════════════════════════════════


def download_dataset(force: bool = False) -> Path:
    """从 Kaggle 下载 arXiv 元数据集（委托 hfpclawer KaggleDownloader）

    Args:
        force: 即使文件存在也重新下载

    Returns:
        JSONL 文件路径
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
        raise FileNotFoundError(f"KaggleDownloader 未生成 JSONL: {jsonl_path}")

    logger.info(f"数据集就绪: {jsonl_path} ({jsonl_path.stat().st_size / 1024 / 1024:.0f} MB)")
    return jsonl_path


# ════════════════════════════════════════════
# SQLite FTS5 索引构建
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
    """数据集构建器 — 解析 JSONL → SQLite FTS5"""

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
        logger.info(f"SQLite 就绪: {self.db_path}")

    def count(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM arxiv_meta").fetchone()[0]

    def build(self, jsonl_path: str, batch_size: int = 2000) -> int:
        """从 JSONL 文件构建 FTS5 索引

        Args:
            jsonl_path: JSON Lines 文件路径
            batch_size: 每批写入行数

        Returns:
            导入的论文总数
        """
        total = 0
        batch = []
        start = time.time()
        existing_count = self.count()
        logger.info(f"开始导入: {jsonl_path}")
        logger.info(f"当前已有: {existing_count:,} 篇")

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
            f"导入完成: total={total:,}, 新增={new_count:,}, "
            f"耗时={elapsed:.0f}s ({total/elapsed:.0f} 篇/s)"
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
            logger.info(f"导入 {total:,} 篇 ({rate:.0f} 篇/s, {elapsed:.0f}s)...")


# ════════════════════════════════════════════
# 便捷函数
# ════════════════════════════════════════════


def ensure_dataset() -> int:
    """确保数据集已下载并导入

    Returns:
        论文总数
    """
    jsonl_path = download_dataset()
    builder = ArxivMetaBuilder()
    if builder.count() == 0:
        total = builder.build(str(jsonl_path))
        return total
    else:
        return builder.count()
