#!/usr/bin/env python3
"""Benchmark batch candidate retrieval workloads."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from arxiv_meta.config import DEFAULT_DB_PATH
from arxiv_meta.search import ArxivSearch


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark arXiv candidate retrieval.")
    parser.add_argument("--input", required=True, help="JSONL review-candidate workload path")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="SQLite database path")
    parser.add_argument("--limit", type=int, default=20, help="Candidates per query")
    parser.add_argument("--max-rows", type=int, default=20, help="Maximum workload rows to read")
    parser.add_argument(
        "--category-mode",
        choices=("primary", "all", "none"),
        default="primary",
        help="Category filter mode for workload rows",
    )
    parser.add_argument("--include-details", action="store_true", help="Fetch full paper details")
    parser.add_argument("--output", help="Optional JSON output path")
    parser.add_argument("--summary-only", action="store_true", help="Print only the summary to stdout")
    return parser.parse_args()


def load_workload(path: Path, max_rows: int, category_mode: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if len(items) >= max_rows:
                break
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            query = row.get("query") or row.get("expected_title") or row.get("expected_title_norm") or row.get("title")
            if not query:
                continue
            item = {
                "request_id": str(len(items)),
                "query": query,
                "limit": row.get("limit"),
                "expected_source_id": row.get("source_id") or row.get("raw_id"),
            }
            if category_mode == "primary" and row.get("primary_category"):
                item["categories"] = [row["primary_category"]]
            elif category_mode == "all" and row.get("categories"):
                item["categories"] = row["categories"]
            items.append(item)
    return items


def summarize(
    workload_path: Path,
    db_path: Path,
    limit: int,
    max_rows: int,
    category_mode: str,
    include_details: bool,
    elapsed_seconds: float,
    items: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    expected_hits = 0
    rows_with_candidates = 0
    all_auto_accept_false = True
    for item, result in zip(items, results, strict=False):
        candidates = result.get("results", [])
        if candidates:
            rows_with_candidates += 1
        if any(candidate.get("auto_accept") is not False for candidate in candidates):
            all_auto_accept_false = False
        expected_source_id = item.get("expected_source_id")
        if expected_source_id and expected_source_id in [candidate["source_id"] for candidate in candidates]:
            expected_hits += 1

    return {
        "workload_path": str(workload_path),
        "db_path": str(db_path),
        "db_size_bytes": db_path.stat().st_size if db_path.exists() else 0,
        "rows_requested": max_rows,
        "rows_loaded": len(items),
        "limit": limit,
        "category_mode": category_mode,
        "include_details": include_details,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "rows_with_nonempty_candidates": rows_with_candidates,
        "rows_expected_source_in_candidates": expected_hits,
        "all_returned_auto_accept_false": all_auto_accept_false,
    }


def main() -> int:
    args = parse_args()
    workload_path = Path(args.input)
    db_path = Path(args.db)
    items = load_workload(workload_path, max_rows=args.max_rows, category_mode=args.category_mode)
    engine = ArxivSearch(db_path=db_path)

    started = time.perf_counter()
    results = engine.candidate_search_batch(
        items,
        default_limit=args.limit,
        include_details=args.include_details,
    )
    elapsed = time.perf_counter() - started

    summary = summarize(
        workload_path=workload_path,
        db_path=db_path,
        limit=args.limit,
        max_rows=args.max_rows,
        category_mode=args.category_mode,
        include_details=args.include_details,
        elapsed_seconds=elapsed,
        items=items,
        results=results,
    )
    payload = {"summary": summary, "results": results}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    printed = summary if args.summary_only else payload
    print(json.dumps(printed, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
