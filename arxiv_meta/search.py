#!/usr/bin/env python3
"""SQLite query layer for the local arXiv metadata database."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from arxiv_meta.config import get
from arxiv_meta.config import resolve_path

logger = logging.getLogger("arxiv_meta.search")

MAX_QUERY_LENGTH = 256
MAX_LIMIT = 500
MAX_DOI_BATCH = 500
MAX_DOI_LENGTH = 256
MAX_CANDIDATE_BATCH = 200

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
COLUMN_FILTER_RE = re.compile(r"\b(?:title|title_fts|papers|arxiv_id|rowid):\S", re.IGNORECASE)
WILDCARD_RE = re.compile(r"(?<!\s)[^\s]+\*")
ANCHOR_RE = re.compile(r"(^|\s)\^[^\s]+")
NEAR_RE = re.compile(r"\bNEAR\s*\(", re.IGNORECASE)
GROUPED_BOOLEAN_RE = re.compile(r"\([^)]*\b(?:AND|OR|NOT)\b[^)]*\)", re.IGNORECASE)


class SearchError(ValueError):
    error_code = "search_error"


class InvalidFTSQuery(SearchError):
    error_code = "invalid_fts_query"


class InvalidBooleanQuery(SearchError):
    error_code = "invalid_boolean_query"


class InvalidDateFilter(SearchError):
    error_code = "invalid_date"


class QueryLimitError(SearchError):
    error_code = "request_too_large"


def validate_iso_date(value: str | None) -> str | None:
    if value in (None, ""):
        return None
    if not DATE_RE.match(value):
        raise InvalidDateFilter("Expected YYYY-MM-DD.")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidDateFilter("Expected YYYY-MM-DD.") from exc
    return value


def fts_query_from_user_text(query: str) -> str:
    """Convert plain user text into a conservative FTS5 query."""
    reject_advanced_fts_syntax(query)
    tokens = tokenize_title_query(query)
    if not tokens:
        raise InvalidFTSQuery("The title query could not be parsed.")
    return " ".join(f'"{token}"' for token in tokens)


def tokenize_boolean_text(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text:
        raise InvalidBooleanQuery("Text match value must not be empty.")
    if len(text) > MAX_QUERY_LENGTH:
        raise QueryLimitError(f"Text match value must be at most {MAX_QUERY_LENGTH} characters.")
    tokens = [token.casefold() for token in TOKEN_RE.findall(text)]
    if not tokens:
        raise InvalidBooleanQuery("Text match value could not be parsed.")
    return tokens


def fts_phrase(tokens: list[str]) -> str:
    return f'"{" ".join(tokens)}"'


def fts_terms(tokens: list[str]) -> str:
    return " ".join(f'"{token}"' for token in tokens)


def fts_prefix(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise InvalidBooleanQuery("Prefix match value must not be empty.")
    if len(text) > MAX_QUERY_LENGTH:
        raise QueryLimitError(f"Prefix match value must be at most {MAX_QUERY_LENGTH} characters.")
    if "*" in text.rstrip("*") or text.count("*") > 1:
        raise InvalidBooleanQuery("Prefix match only supports a single trailing '*'.")
    tokens = tokenize_boolean_text(text[:-1] if text.endswith("*") else text)
    if not tokens[-1]:
        raise InvalidBooleanQuery("Prefix match value could not be parsed.")
    prefix_terms = [f'"{token}"' for token in tokens[:-1]]
    prefix_terms.append(f"{tokens[-1]}*")
    return " ".join(prefix_terms)


def boolean_text_fts_query(field: str, match: dict[str, Any]) -> str:
    match_type = str(match.get("type") or "").casefold()
    value = match.get("value")

    if match_type == "phrase":
        tokens = tokenize_boolean_text(value)
        expression = fts_phrase(tokens)
    elif match_type == "term":
        tokens = tokenize_boolean_text(value)
        expression = fts_terms(tokens)
    elif match_type == "prefix":
        expression = fts_prefix(value)
    else:
        raise InvalidBooleanQuery(f"Unsupported text match type: {match_type or '<missing>'}.")

    if field == "all":
        return expression
    if field not in {"title", "abstract", "authors"}:
        raise InvalidBooleanQuery(f"Unsupported text field: {field}.")
    return f"{field} : ( {expression} )"


def reject_advanced_fts_syntax(query: str) -> None:
    """Reject clear FTS grammar while allowing ordinary pasted-title punctuation."""
    query = query or ""
    if CONTROL_CHAR_RE.search(query):
        raise InvalidFTSQuery("Control characters are not valid in title queries.")
    if COLUMN_FILTER_RE.search(query):
        raise InvalidFTSQuery("Advanced FTS column filters are not enabled.")
    if WILDCARD_RE.search(query):
        raise InvalidFTSQuery("Advanced FTS prefix syntax is not enabled.")
    if ANCHOR_RE.search(query):
        raise InvalidFTSQuery("Advanced FTS anchor syntax is not enabled.")
    if NEAR_RE.search(query):
        raise InvalidFTSQuery("Advanced FTS proximity syntax is not enabled.")
    if GROUPED_BOOLEAN_RE.search(query):
        raise InvalidFTSQuery("Advanced FTS boolean grouping is not enabled.")


def tokenize_title_query(
    query: str,
    enforce_limit: bool = True,
) -> list[str]:
    """Tokenize pasted title text into safe FTS token boundaries."""
    query = (query or "").strip()
    if not query:
        raise InvalidFTSQuery("Title query must not be empty.")
    if enforce_limit and len(query) > MAX_QUERY_LENGTH:
        raise QueryLimitError(f"Title query must be at most {MAX_QUERY_LENGTH} characters.")

    tokens = [token.casefold() for token in TOKEN_RE.findall(query)]
    if not tokens:
        raise InvalidFTSQuery("The title query could not be parsed.")
    return list(dict.fromkeys(tokens))


def fts_or_query_from_tokens(tokens: list[str]) -> str:
    if not tokens:
        raise InvalidFTSQuery("The title query could not be parsed.")
    return " OR ".join(f'"{token}"' for token in tokens)


def candidate_anchor_tokens(tokens: list[str], max_tokens: int = 8) -> list[str]:
    """Pick a bounded set of informative tokens for FTS candidate retrieval."""
    informative = [token for token in tokens if len(token) >= 4]
    pool = informative or tokens
    ranked = sorted(pool, key=lambda token: (-len(token), token))
    selected = ranked[:max_tokens]
    return list(dict.fromkeys(selected))


def candidate_phrase_anchors(tokens: list[str], max_phrases: int = 8) -> list[str]:
    """Build safe consecutive phrase anchors from query tokens."""
    phrases: list[str] = []
    for width in (3, 2):
        for idx in range(0, max(0, len(tokens) - width + 1)):
            phrase_tokens = tokens[idx : idx + width]
            if all(len(token) >= 3 for token in phrase_tokens):
                phrases.append(" ".join(phrase_tokens))
    return list(dict.fromkeys(phrases))[:max_phrases]


def normalize_limit(limit: int) -> int:
    if limit < 1:
        raise QueryLimitError("Limit must be at least 1.")
    if limit > MAX_LIMIT:
        raise QueryLimitError(f"Limit must be at most {MAX_LIMIT}.")
    return limit


def normalize_sort(sort: str) -> str:
    if sort not in {"relevance", "date"}:
        raise ValueError("sort must be 'relevance' or 'date'")
    return sort


def boolean_children(node: dict[str, Any]) -> list[dict[str, Any]]:
    children = node.get("children")
    if not isinstance(children, list) or not children:
        raise InvalidBooleanQuery("Boolean operator nodes must include children.")
    if not all(isinstance(child, dict) for child in children):
        raise InvalidBooleanQuery("Boolean operator children must be objects.")
    return children


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def candidate_item_query(item: dict[str, Any]) -> str:
    for key in ("query", "q", "expected_title", "expected_title_norm", "title"):
        value = item.get(key)
        if value:
            return str(value)
    raise InvalidFTSQuery("Candidate batch item must include query text.")


def candidate_item_categories(item: dict[str, Any]) -> list[str] | None:
    raw = item.get("categories")
    if isinstance(raw, list):
        categories = [str(category).strip() for category in raw if str(category).strip()]
        return categories or None
    if isinstance(raw, str) and raw.strip():
        categories = [part.strip() for part in raw.replace(",", " ").split() if part.strip()]
        return categories or None

    cat = item.get("cat")
    if isinstance(cat, str) and cat.strip():
        categories = [part.strip() for part in cat.split(",") if part.strip()]
        return categories or None

    primary_category = item.get("primary_category")
    if primary_category:
        return [str(primary_category).strip()]
    return None


def row_to_paper(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    categories_raw = data.pop("categories_raw", "") or ""
    categories_json = data.pop("categories_json", None)
    if categories_json:
        categories = json.loads(categories_json)
    else:
        categories = categories_raw.split()
    return {
        "arxiv_id": data.get("arxiv_id", ""),
        "title": data.get("title") or "",
        "authors": data.get("authors") or "",
        "abstract": data.get("abstract") or "",
        "categories": categories,
        "primary_category": data.get("primary_category") or "",
        "doi": data.get("doi") or "",
        "journal_ref": data.get("journal_ref") or "",
        "update_date": data.get("update_date") or "",
        "first_version_date": data.get("first_version_date") or "",
        "score": data.get("score", 0) or 0,
    }


def row_to_candidate(
    row: sqlite3.Row | dict[str, Any],
    query_tokens: list[str],
    anchor_tokens: list[str],
    phrase_anchors: list[str],
) -> dict[str, Any]:
    paper = row_to_paper(row)
    title_tokens = tokenize_title_query(
        paper["title"],
        enforce_limit=False,
    )
    title_token_set = set(title_tokens)
    shared_tokens = [token for token in query_tokens if token in title_token_set]
    return {
        "source_id": paper["arxiv_id"],
        "arxiv_id": paper["arxiv_id"],
        "title": paper["title"],
        "categories": paper["categories"],
        "update_date": paper["update_date"],
        "score": paper["score"],
        "matched_tokens": shared_tokens,
        "shared_tokens": shared_tokens,
        "evidence": {
            "retrieval_strategy": "fts5_or_review_candidate",
            "query_tokens": query_tokens,
            "anchor_tokens": anchor_tokens,
            "phrase_anchors": phrase_anchors,
            "title_tokens": title_tokens,
            "shared_token_count": len(shared_tokens),
            "auto_accept": False,
            "review_candidate": True,
        },
        "review_candidate": True,
        "auto_accept": False,
    }


class ArxivSearch:
    """Read-only search API for the SQLite metadata database."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        immutable: bool | None = None,
        query_only: bool | None = None,
        mmap_size: int | None = None,
        cache_size: int | None = None,
    ):
        self.db_path = str(resolve_path(db_path or get("db.path")))
        self.immutable = bool(get("db.read.immutable", True) if immutable is None else immutable)
        self.query_only = bool(get("db.read.query_only", True) if query_only is None else query_only)
        self.mmap_size = _optional_int(get("db.read.mmap_size", 0) if mmap_size is None else mmap_size)
        self.cache_size = _optional_int(
            get("db.read.cache_size", -80000) if cache_size is None else cache_size
        )

    def _database_uri(self) -> str:
        params: dict[str, str] = {"mode": "ro"}
        if self.immutable:
            params["immutable"] = "1"
        return f"file:{Path(self.db_path).as_posix()}?{urlencode(params)}"

    def _conn(self) -> sqlite3.Connection:
        if not Path(self.db_path).exists():
            raise FileNotFoundError(self.db_path)
        uri = self._database_uri()
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        self._configure_read_conn(conn)
        return conn

    def _configure_read_conn(self, conn: sqlite3.Connection) -> None:
        if self.cache_size is not None:
            conn.execute(f"PRAGMA cache_size={int(self.cache_size)}")
        if self.mmap_size is not None:
            conn.execute(f"PRAGMA mmap_size={int(self.mmap_size)}")
        if self.query_only:
            conn.execute("PRAGMA query_only=ON")

    def search(
        self,
        query: str,
        limit: int = 50,
        categories: list[str] | None = None,
        update_date_from: str | None = None,
        update_date_to: str | None = None,
        sort: str = "relevance",
    ) -> list[dict[str, Any]]:
        limit = normalize_limit(limit)
        fts_query = fts_query_from_user_text(query)
        update_date_from = validate_iso_date(update_date_from)
        update_date_to = validate_iso_date(update_date_to)
        if sort not in {"relevance", "date"}:
            raise ValueError("sort must be 'relevance' or 'date'")

        sql = """
            SELECT
              p.arxiv_id,
              p.title,
              p.authors,
              p.abstract,
              p.categories_raw,
              p.primary_category,
              p.doi,
              p.journal_ref,
              p.update_date,
              p.first_version_date,
              bm25(title_fts) AS score
            FROM title_fts
            JOIN papers p ON p.rowid = title_fts.rowid
            WHERE title_fts MATCH ?
        """
        params: list[Any] = [fts_query]

        clean_categories = [cat.strip() for cat in categories or [] if cat and cat.strip()]
        if clean_categories:
            placeholders = ", ".join("?" for _ in clean_categories)
            sql += f"""
              AND EXISTS (
                SELECT 1
                FROM paper_categories pc
                WHERE pc.paper_id = p.arxiv_id
                  AND pc.category IN ({placeholders})
              )
            """
            params.extend(clean_categories)

        if update_date_from:
            sql += " AND p.update_date >= ?"
            params.append(update_date_from)
        if update_date_to:
            sql += " AND p.update_date <= ?"
            params.append(update_date_to)

        if sort == "date":
            sql += " ORDER BY p.update_date DESC, score ASC"
        else:
            sql += " ORDER BY score ASC, p.update_date DESC"
        sql += " LIMIT ?"
        params.append(limit)

        try:
            with self._conn() as conn:
                rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            logger.warning("FTS5 query failed: %s", exc)
            raise InvalidFTSQuery("The title query could not be parsed.") from exc
        except FileNotFoundError:
            return []

        return [row_to_paper(row) for row in rows]

    def boolean_search(
        self,
        query_object: dict[str, Any],
        limit: int = 50,
        sort: str = "relevance",
    ) -> list[dict[str, Any]]:
        """Search with an already normalized Boolean query object.

        This method is intentionally separate from ``search()``. It does not
        parse raw query strings and it does not expose raw FTS5 syntax.
        """
        limit = normalize_limit(limit)
        sort = normalize_sort(sort)
        if not isinstance(query_object, dict) or not query_object:
            raise InvalidBooleanQuery("Query object must be a non-empty object.")

        try:
            with self._conn() as conn:
                row_scores = self._evaluate_boolean_node(conn, query_object)
                return self._rows_from_boolean_scores(conn, row_scores, limit=limit, sort=sort)
        except sqlite3.OperationalError as exc:
            logger.warning("Boolean query failed: %s", exc)
            raise InvalidBooleanQuery("The Boolean query could not be executed.") from exc
        except FileNotFoundError:
            return []

    def boolean_query_provenance(self, query_object: dict[str, Any]) -> dict[str, list[str]]:
        """Return a compact summary of fields/operators used by a normalized query."""
        if not isinstance(query_object, dict) or not query_object:
            raise InvalidBooleanQuery("Query object must be a non-empty object.")

        provenance: dict[str, list[str]] = {
            "operators": [],
            "fields": [],
            "text_fields": [],
            "metadata_fields": [],
            "range_fields": [],
        }
        self._collect_boolean_provenance(query_object, provenance)
        return {key: list(dict.fromkeys(values)) for key, values in provenance.items()}

    def _collect_boolean_provenance(
        self,
        node: dict[str, Any],
        provenance: dict[str, list[str]],
    ) -> None:
        if "op" in node:
            op = str(node.get("op") or "").upper()
            if not op:
                raise InvalidBooleanQuery("Boolean operator must not be empty.")
            provenance["operators"].append(op)
            for child in boolean_children(node):
                self._collect_boolean_provenance(child, provenance)
            return

        field = str(node.get("field") or "")
        if not field:
            raise InvalidBooleanQuery("Leaf query nodes must include field.")
        provenance["fields"].append(field)
        if field in {"title", "abstract", "authors", "all"}:
            provenance["text_fields"].append(field)
        elif "range" in node:
            provenance["range_fields"].append(field)
        else:
            provenance["metadata_fields"].append(field)

    def _evaluate_boolean_node(
        self,
        conn: sqlite3.Connection,
        node: dict[str, Any],
    ) -> dict[int, float]:
        if "op" in node:
            op = str(node.get("op") or "").upper()
            children = boolean_children(node)
            child_scores = [self._evaluate_boolean_node(conn, child) for child in children]
            if op == "AND":
                return self._intersect_boolean_scores(child_scores)
            if op == "OR":
                return self._union_boolean_scores(child_scores)
            if op == "ANDNOT":
                return self._subtract_boolean_scores(child_scores)
            raise InvalidBooleanQuery(f"Unsupported Boolean operator: {op or '<missing>'}.")

        field = str(node.get("field") or "")
        if not field:
            raise InvalidBooleanQuery("Leaf query nodes must include field.")

        if "match" in node:
            match = node.get("match")
            if not isinstance(match, dict):
                raise InvalidBooleanQuery("Match query node must include a match object.")
            return self._evaluate_match_leaf(conn, field, match)

        if "range" in node:
            range_filter = node.get("range")
            if not isinstance(range_filter, dict):
                raise InvalidBooleanQuery("Range query node must include a range object.")
            return self._evaluate_range_leaf(conn, field, range_filter)

        raise InvalidBooleanQuery("Leaf query nodes must include match or range.")

    def _evaluate_match_leaf(
        self,
        conn: sqlite3.Connection,
        field: str,
        match: dict[str, Any],
    ) -> dict[int, float]:
        if field in {"title", "abstract", "authors", "all"}:
            fts_query = boolean_text_fts_query(field, match)
            rows = conn.execute(
                """
                SELECT p.rowid AS rowid, bm25(paper_fts) AS score
                FROM paper_fts
                JOIN papers p ON p.rowid = paper_fts.rowid
                WHERE paper_fts MATCH ?
                """,
                (fts_query,),
            ).fetchall()
            return {int(row["rowid"]): float(row["score"] or 0) for row in rows}

        if field == "category":
            return self._evaluate_category_match(conn, match)
        if field == "primary_category":
            return self._evaluate_exact_paper_field(conn, "primary_category", match)
        if field == "arxiv_id":
            return self._evaluate_exact_paper_field(conn, "arxiv_id", match)
        if field in {"update_date", "first_version_date"}:
            return self._evaluate_exact_paper_field(conn, field, match)

        raise InvalidBooleanQuery(f"Unsupported field: {field}.")

    def _evaluate_category_match(
        self,
        conn: sqlite3.Connection,
        match: dict[str, Any],
    ) -> dict[int, float]:
        value = self._exact_match_value(match)
        rows = conn.execute(
            """
            SELECT p.rowid AS rowid
            FROM papers p
            WHERE EXISTS (
              SELECT 1
              FROM paper_categories pc
              WHERE pc.paper_id = p.arxiv_id
                AND pc.category = ?
            )
            """,
            (value,),
        ).fetchall()
        return {int(row["rowid"]): 0.0 for row in rows}

    def _evaluate_exact_paper_field(
        self,
        conn: sqlite3.Connection,
        field: str,
        match: dict[str, Any],
    ) -> dict[int, float]:
        value = self._exact_match_value(match)
        rows = conn.execute(
            f"""
            SELECT rowid
            FROM papers
            WHERE {field} = ?
            """,
            (value,),
        ).fetchall()
        return {int(row["rowid"]): 0.0 for row in rows}

    def _exact_match_value(self, match: dict[str, Any]) -> str:
        match_type = str(match.get("type") or "").casefold()
        if match_type != "term":
            raise InvalidBooleanQuery(f"Unsupported metadata match type: {match_type or '<missing>'}.")
        value = str(match.get("value") or "").strip()
        if not value:
            raise InvalidBooleanQuery("Metadata match value must not be empty.")
        if len(value) > MAX_QUERY_LENGTH:
            raise QueryLimitError(f"Metadata match value must be at most {MAX_QUERY_LENGTH} characters.")
        return value

    def _evaluate_range_leaf(
        self,
        conn: sqlite3.Connection,
        field: str,
        range_filter: dict[str, Any],
    ) -> dict[int, float]:
        if field not in {"update_date", "first_version_date"}:
            raise InvalidBooleanQuery(f"Unsupported range field: {field}.")
        gte = validate_iso_date(range_filter.get("gte"))
        lte = validate_iso_date(range_filter.get("lte"))
        if not gte and not lte:
            raise InvalidBooleanQuery("Range query must include gte or lte.")

        sql = f"SELECT rowid FROM papers WHERE {field} IS NOT NULL AND {field} != ''"
        params: list[Any] = []
        if gte:
            sql += f" AND {field} >= ?"
            params.append(gte)
        if lte:
            sql += f" AND {field} <= ?"
            params.append(lte)
        rows = conn.execute(sql, params).fetchall()
        return {int(row["rowid"]): 0.0 for row in rows}

    def _intersect_boolean_scores(self, child_scores: list[dict[int, float]]) -> dict[int, float]:
        if not child_scores:
            return {}
        current = dict(child_scores[0])
        for scores in child_scores[1:]:
            shared = current.keys() & scores.keys()
            current = {rowid: current[rowid] + scores[rowid] for rowid in shared}
            if not current:
                break
        return current

    def _union_boolean_scores(self, child_scores: list[dict[int, float]]) -> dict[int, float]:
        merged: dict[int, float] = {}
        for scores in child_scores:
            for rowid, score in scores.items():
                if rowid not in merged or score < merged[rowid]:
                    merged[rowid] = score
        return merged

    def _subtract_boolean_scores(self, child_scores: list[dict[int, float]]) -> dict[int, float]:
        if len(child_scores) < 2:
            raise InvalidBooleanQuery("ANDNOT requires at least two children.")
        base = dict(child_scores[0])
        excluded = set().union(*(scores.keys() for scores in child_scores[1:]))
        return {rowid: score for rowid, score in base.items() if rowid not in excluded}

    def _rows_from_boolean_scores(
        self,
        conn: sqlite3.Connection,
        row_scores: dict[int, float],
        limit: int,
        sort: str,
    ) -> list[dict[str, Any]]:
        if not row_scores:
            return []
        rowids = list(row_scores)
        placeholders = ", ".join("?" for _ in rowids)
        rows = conn.execute(
            f"""
            SELECT
              p.*,
              (
                SELECT json_group_array(category)
                FROM (
                  SELECT category
                  FROM paper_categories pc
                  WHERE pc.paper_id = p.arxiv_id
                  ORDER BY position
                )
              ) AS categories_json
            FROM papers p
            WHERE p.rowid IN ({placeholders})
            """,
            rowids,
        ).fetchall()

        papers = [row_to_paper(row) for row in rows]
        for paper, row in zip(papers, rows):
            paper["score"] = row_scores[int(row["rowid"])]

        if sort == "date":
            papers.sort(key=lambda item: (item["update_date"], item["arxiv_id"]), reverse=True)
        else:
            papers.sort(key=lambda item: (item["score"], item["update_date"], item["arxiv_id"]))
        return papers[:limit]

    def candidate_search(
        self,
        query: str,
        limit: int = 20,
        categories: list[str] | None = None,
        update_date_from: str | None = None,
        update_date_to: str | None = None,
        include_details: bool = False,
    ) -> list[dict[str, Any]]:
        try:
            with self._conn() as conn:
                return self._candidate_search_with_conn(
                    conn=conn,
                    query=query,
                    limit=limit,
                    categories=categories,
                    update_date_from=update_date_from,
                    update_date_to=update_date_to,
                    include_details=include_details,
                )
        except sqlite3.OperationalError as exc:
            logger.warning("FTS5 candidate query failed: %s", exc)
            raise InvalidFTSQuery("The title query could not be parsed.") from exc
        except FileNotFoundError:
            return []

    def candidate_search_batch(
        self,
        items: list[dict[str, Any]],
        default_limit: int = 20,
        include_details: bool = False,
    ) -> list[dict[str, Any]]:
        if len(items) > MAX_CANDIDATE_BATCH:
            raise QueryLimitError(f"Maximum candidate batch size is {MAX_CANDIDATE_BATCH}.")

        try:
            with self._conn() as conn:
                responses: list[dict[str, Any]] = []
                for index, item in enumerate(items):
                    query = candidate_item_query(item)
                    limit = normalize_limit(int(item.get("limit") or default_limit))
                    categories = candidate_item_categories(item)
                    item_include_details = bool(item.get("include_details", include_details))
                    results = self._candidate_search_with_conn(
                        conn=conn,
                        query=query,
                        limit=limit,
                        categories=categories,
                        update_date_from=item.get("update_date_from"),
                        update_date_to=item.get("update_date_to"),
                        include_details=item_include_details,
                    )
                    responses.append(
                        {
                            "request_id": item.get("request_id", str(index)),
                            "query": query,
                            "limit": limit,
                            "total": len(results),
                            "auto_accept": False,
                            "results": results,
                        }
                    )
                return responses
        except sqlite3.OperationalError as exc:
            logger.warning("FTS5 candidate batch query failed: %s", exc)
            raise InvalidFTSQuery("The title query could not be parsed.") from exc
        except FileNotFoundError:
            return []

    def _candidate_search_with_conn(
        self,
        conn: sqlite3.Connection,
        query: str,
        limit: int = 20,
        categories: list[str] | None = None,
        update_date_from: str | None = None,
        update_date_to: str | None = None,
        include_details: bool = False,
    ) -> list[dict[str, Any]]:
        """Return review candidates using subset/OR title-token retrieval.

        This method is intentionally not an acceptance decision. It returns
        provenance and token-overlap evidence for manual or downstream review.
        """
        limit = normalize_limit(limit)
        reject_advanced_fts_syntax(query)
        query_tokens = tokenize_title_query(query)
        anchor_tokens = candidate_anchor_tokens(query_tokens)
        phrase_anchors = candidate_phrase_anchors(query_tokens)
        update_date_from = validate_iso_date(update_date_from)
        update_date_to = validate_iso_date(update_date_to)

        sql = """
            SELECT
              p.arxiv_id,
              p.title,
              p.categories_raw,
              p.update_date,
              bm25(title_fts) AS score
            FROM title_fts
            JOIN papers p ON p.rowid = title_fts.rowid
            WHERE title_fts MATCH ?
        """
        clean_categories = [cat.strip() for cat in categories or [] if cat and cat.strip()]
        category_filter = set(clean_categories)
        filter_sql = ""
        filter_params: list[Any] = []
        if clean_categories:
            placeholders = ", ".join("?" for _ in clean_categories)
            filter_sql += f"""
              AND EXISTS (
                SELECT 1
                FROM paper_categories pc
                WHERE pc.paper_id = p.arxiv_id
                  AND pc.category IN ({placeholders})
              )
            """
            filter_params.extend(clean_categories)

        if update_date_from:
            filter_sql += " AND p.update_date >= ?"
            filter_params.append(update_date_from)
        if update_date_to:
            filter_sql += " AND p.update_date <= ?"
            filter_params.append(update_date_to)

        fetch_limit = min(max(limit * 2, 10), MAX_LIMIT)
        scan_limit = min(max(fetch_limit * 5, 100), MAX_LIMIT)
        phrase_sql = sql + filter_sql + " LIMIT ?"
        fallback_sql = sql + " LIMIT ?"

        by_id: dict[str, sqlite3.Row] = {}
        phrase_queries = [f'"{phrase}"' for phrase in phrase_anchors]
        fallback_tokens = [token for token in anchor_tokens if len(token) >= 6]
        if not fallback_tokens:
            fallback_tokens = anchor_tokens[:3]

        for match_query in phrase_queries:
            rows = conn.execute(
                phrase_sql,
                [match_query, *filter_params, fetch_limit],
            ).fetchall()
            for row in rows:
                existing = by_id.get(row["arxiv_id"])
                if existing is None or row["score"] < existing["score"]:
                    by_id[row["arxiv_id"]] = row
            if len(by_id) >= fetch_limit:
                break

        for token in fallback_tokens:
            if len(by_id) >= fetch_limit:
                break
            match_query = f'"{token}"'
            rows = conn.execute(fallback_sql, [match_query, scan_limit]).fetchall()
            for row in rows:
                if category_filter and not category_filter.intersection(
                    (row["categories_raw"] or "").split()
                ):
                    continue
                if update_date_from and (row["update_date"] or "") < update_date_from:
                    continue
                if update_date_to and (row["update_date"] or "") > update_date_to:
                    continue
                existing = by_id.get(row["arxiv_id"])
                if existing is None or row["score"] < existing["score"]:
                    by_id[row["arxiv_id"]] = row
            if len(by_id) >= fetch_limit:
                break

        candidates = [
            row_to_candidate(row, query_tokens, anchor_tokens, phrase_anchors)
            for row in by_id.values()
        ]
        candidates.sort(
            key=lambda item: (
                -len(item["shared_tokens"]),
                item["score"],
                item["update_date"],
                item["source_id"],
            )
        )
        candidates = candidates[:limit]
        if include_details:
            details_by_id = self._get_by_ids_with_conn(
                conn,
                [candidate["source_id"] for candidate in candidates],
            )
            for candidate in candidates:
                details = details_by_id.get(candidate["source_id"])
                if details:
                    candidate["details"] = details
        return candidates

    def get_by_id(self, arxiv_id: str) -> dict[str, Any] | None:
        try:
            with self._conn() as conn:
                row = self._get_by_id_with_conn(conn, arxiv_id)
        except (sqlite3.OperationalError, FileNotFoundError):
            return None
        return row_to_paper(row) if row else None

    def _get_by_id_with_conn(self, conn: sqlite3.Connection, arxiv_id: str) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT
              p.*,
              (
                SELECT json_group_array(category)
                FROM (
                  SELECT category
                  FROM paper_categories pc
                  WHERE pc.paper_id = p.arxiv_id
                  ORDER BY position
                )
              ) AS categories_json
            FROM papers p
            WHERE p.arxiv_id = ?
            """,
            (arxiv_id,),
        ).fetchone()

    def _get_by_ids_with_conn(
        self,
        conn: sqlite3.Connection,
        arxiv_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        clean_ids = list(dict.fromkeys(arxiv_id for arxiv_id in arxiv_ids if arxiv_id))
        if not clean_ids:
            return {}
        placeholders = ", ".join("?" for _ in clean_ids)
        rows = conn.execute(
            f"""
            SELECT
              p.*,
              (
                SELECT json_group_array(category)
                FROM (
                  SELECT category
                  FROM paper_categories pc
                  WHERE pc.paper_id = p.arxiv_id
                  ORDER BY position
                )
              ) AS categories_json
            FROM papers p
            WHERE p.arxiv_id IN ({placeholders})
            """,
            clean_ids,
        ).fetchall()
        return {row["arxiv_id"]: row_to_paper(row) for row in rows}

    def get_by_dois(self, dois: list[str]) -> dict[str, str]:
        if len(dois) > MAX_DOI_BATCH:
            raise QueryLimitError(f"Maximum DOI batch size is {MAX_DOI_BATCH}.")

        clean_dois: list[str] = []
        seen: set[str] = set()
        for doi in dois:
            clean = (doi or "").strip()
            if not clean:
                continue
            if len(clean) > MAX_DOI_LENGTH:
                raise QueryLimitError(f"DOI must be at most {MAX_DOI_LENGTH} characters.")
            key = clean.casefold()
            if key not in seen:
                clean_dois.append(clean)
                seen.add(key)

        if not clean_dois:
            return {}

        placeholders = ", ".join("?" for _ in clean_dois)
        params = [doi.casefold() for doi in clean_dois]
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    f"""
                    SELECT doi, arxiv_id
                    FROM papers
                    WHERE doi IS NOT NULL
                      AND lower(doi) IN ({placeholders})
                    """,
                    params,
                ).fetchall()
        except (sqlite3.OperationalError, FileNotFoundError):
            return {}

        found_by_lower = {row["doi"].casefold(): row["arxiv_id"] for row in rows}
        return {doi: found_by_lower[doi.casefold()] for doi in clean_dois if doi.casefold() in found_by_lower}

    def stats(self) -> dict[str, int]:
        try:
            with self._conn() as conn:
                total = self._paper_count(conn)
                has_doi = conn.execute(
                    "SELECT COUNT(*) FROM papers WHERE doi IS NOT NULL AND doi != ''"
                ).fetchone()[0]
                has_journal = conn.execute(
                    "SELECT COUNT(*) FROM papers WHERE journal_ref IS NOT NULL AND journal_ref != ''"
                ).fetchone()[0]
        except (sqlite3.OperationalError, FileNotFoundError):
            return {"total": 0, "has_doi": 0, "has_journal": 0}
        return {"total": total, "has_doi": has_doi, "has_journal": has_journal}

    def paper_count(self) -> int:
        try:
            with self._conn() as conn:
                return self._paper_count(conn)
        except (sqlite3.OperationalError, FileNotFoundError):
            return 0

    def _paper_count(self, conn: sqlite3.Connection) -> int:
        try:
            row = conn.execute(
                """
                SELECT records_imported
                FROM import_runs
                WHERE finished_at IS NOT NULL
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            if row and row[0]:
                return int(row[0])
        except sqlite3.OperationalError:
            pass
        return conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]

    def ready(self) -> bool:
        return self.paper_count() > 0
