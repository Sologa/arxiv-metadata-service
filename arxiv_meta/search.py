#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Search engine — SQLite FTS5 wrapper

import logging
import os
import re
import sqlite3
import threading
from pathlib import Path

from arxiv_meta.config import get

logger = logging.getLogger("arxiv_meta.search")

DOI_RE = re.compile(r"10\.\d{4,}/[^\s]+")


class ArxivSearch:
    """arXiv FTS5 search engine

    Usage:
        engine = ArxivSearch()
        results = engine.search("neural operator", limit=50, year_from=2017)
        paper = engine.get_by_id("2001.08361")
    """

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = get("db.path", "data/arxiv_meta.db")
        if not os.path.isabs(db_path):
            db_path = str(Path(__file__).parent.parent / db_path)
        self.db_path = db_path
        self._lock = threading.Lock()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA cache_size=-80000")
        return conn

    def search(self, query: str, limit: int = 50, year_from: int = 0,
               year_to: int = 0, categories: list[str] = None,
               sort: str = "relevance") -> list[dict]:
        """Full-text search

        Args:
            query: FTS5 query syntax
            limit: Maximum number of results
            year_from: Start year
            year_to: End year
            categories: Category filter
            sort: "relevance" | "date"

        Returns:
            [{arxiv_id, title, authors, abstract, categories,
              doi, journal_ref, update_date, score}, ...]
        """
        results = []
        try:
            with self._lock, self._conn() as conn:
                if sort == "date":
                    sql = """SELECT f.arxiv_id, title, authors, abstract, categories,
                                    doi, journal_ref, update_date, rank
                             FROM arxiv_fts f
                             JOIN arxiv_meta m ON f.arxiv_id = m.arxiv_id
                             WHERE arxiv_fts MATCH ?
                             ORDER BY m.update_date DESC
                             LIMIT ?"""
                else:
                    sql = """SELECT arxiv_id, title, authors, abstract, categories,
                                    doi, journal_ref, update_date, rank
                             FROM arxiv_fts
                             WHERE arxiv_fts MATCH ?
                             ORDER BY rank
                             LIMIT ?"""
                rows = conn.execute(sql, (query, limit * 3)).fetchall()

            for r in rows:
                r = dict(r)
                update = (r.get("update_date") or "")
                year_str = update[:4]

                # Year filter
                if year_from and year_str:
                    try:
                        if int(year_str) < year_from:
                            continue
                    except ValueError:
                        pass
                if year_to and year_str:
                    try:
                        if int(year_str) > year_to:
                            continue
                    except ValueError:
                        pass

                # Category filter
                if categories:
                    cats = (r.get("categories") or "").split()
                    if not any(c in cats for c in categories):
                        continue

                results.append({
                    "arxiv_id": r["arxiv_id"],
                    "title": r["title"] or "",
                    "authors": r["authors"] or "",
                    "abstract": r["abstract"] or "",
                    "categories": (r["categories"] or "").split(),
                    "doi": r["doi"] or "",
                    "journal_ref": r["journal_ref"] or "",
                    "published_date": update,
                    "score": -r["rank"] if r["rank"] else 0,
                })
                if len(results) >= limit:
                    break

        except sqlite3.OperativeError as e:
            logger.warning(f"FTS5 query failed (index may be empty): {e}")

        return results

    def get_by_id(self, arxiv_id: str) -> dict | None:
        """Look up a single paper by arXiv ID"""
        try:
            with self._conn() as conn:
                r = conn.execute(
                    "SELECT * FROM arxiv_meta WHERE arxiv_id = ?",
                    (arxiv_id,),
                ).fetchone()
            if r:
                return dict(r)
        except sqlite3.OperativeError:
            pass
        return None

    def get_by_dois(self, dois: list[str]) -> dict[str, str]:
        """Batch lookup arXiv IDs by DOI

        Args:
            dois: List of DOIs

        Returns:
            {doi: arxiv_id, ...} — only matching entries
        """
        if not dois:
            return {}

        # Normalize DOIs (deduplicate, trim)
        clean_dois = []
        for d in dois:
            d = d.strip()
            if d:
                clean_dois.append(d)
        clean_dois = list(dict.fromkeys(clean_dois))  # deduplicate preserving order

        result = {}
        try:
            with self._conn() as conn:
                for doi in clean_dois:
                    r = conn.execute(
                        "SELECT arxiv_id FROM arxiv_meta WHERE doi = ?",
                        (doi,),
                    ).fetchone()
                    if r:
                        result[doi] = r[0]
        except sqlite3.OperativeError:
            pass
        return result

    def stats(self) -> dict:
        """Database statistics"""
        try:
            with self._conn() as conn:
                total = conn.execute(
                    "SELECT COUNT(*) FROM arxiv_meta"
                ).fetchone()[0]
                has_doi = conn.execute(
                    "SELECT COUNT(*) FROM arxiv_meta WHERE doi != ''"
                ).fetchone()[0]
                has_journal = conn.execute(
                    "SELECT COUNT(*) FROM arxiv_meta WHERE journal_ref != ''"
                ).fetchone()[0]
        except sqlite3.OperativeError:
            return {"total": 0, "has_doi": 0, "has_journal": 0}

        return {
            "total": total,
            "has_doi": has_doi,
            "has_journal": has_journal,
        }

    def ready(self) -> bool:
        """Check if the database is ready"""
        try:
            return self.stats()["total"] > 1000
        except Exception:
            return False
