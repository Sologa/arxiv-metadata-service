#!/usr/bin/env python3
"""Command-line interface for the local arXiv metadata service."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import sys
from pathlib import Path

import typer

from arxiv_meta.config import DEFAULT_DB_PATH, DEFAULT_JSONL_PATH, DEFAULT_HOST
from arxiv_meta.config import get as cfg_get
from arxiv_meta.config import load_config
from arxiv_meta.search import ArxivSearch, InvalidFTSQuery

app = typer.Typer(name="arxiv-meta", help="Local arXiv metadata query service")
logger = logging.getLogger("arxiv_meta.cli")


@app.callback()
def main_callback(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s")


@app.command()
def build(
    jsonl: str = typer.Option(DEFAULT_JSONL_PATH, "--jsonl", "-j", help="Local arXiv JSONL snapshot"),
    db: str = typer.Option(DEFAULT_DB_PATH, "--db", "-d", help="SQLite database output path"),
    batch_size: int = typer.Option(2000, "--batch", "-b", min=1, help="Rows per write batch"),
    reset: bool = typer.Option(True, "--reset/--no-reset", help="Rebuild the database from scratch"),
    staging_dir: str | None = typer.Option(
        None,
        "--staging-dir",
        help="Optional directory for building before copying to --db",
    ),
) -> None:
    """Build the SQLite database from a local JSONL snapshot."""
    from arxiv_meta.data import ArxivMetaBuilder

    builder = ArxivMetaBuilder(db_path=db)
    stats = builder.build(
        jsonl_path=jsonl,
        batch_size=batch_size,
        reset=reset,
        staging_dir=staging_dir,
    )
    db_size = Path(stats.db_path).stat().st_size / 1024 / 1024
    typer.echo("Import complete")
    typer.echo(f"  source: {stats.source_snapshot_path}")
    typer.echo(f"  db: {stats.db_path}")
    typer.echo(f"  records_seen: {stats.records_seen}")
    typer.echo(f"  records_imported: {stats.records_imported}")
    typer.echo(f"  json_decode_errors: {stats.json_decode_errors}")
    typer.echo(f"  elapsed_seconds: {stats.elapsed_seconds:.2f}")
    typer.echo(f"  db_size_mb: {db_size:.1f}")


@app.command()
def serve(
    host: str = typer.Option(None, "--host", help="Listen address"),
    port: int = typer.Option(None, "--port", "-p", help="Port"),
    db: str = typer.Option(None, "--db", "-d", help="SQLite database path"),
) -> None:
    """Start the local HTTP API."""
    from arxiv_meta.server import run_server

    run_server(host=host or cfg_get("server.host", DEFAULT_HOST), port=port, db_path=db)


@app.command()
def config() -> None:
    """Print resolved configuration."""
    typer.echo(json.dumps(load_config(), indent=2, ensure_ascii=False))


@app.command()
def smoke(
    db: str = typer.Option(DEFAULT_DB_PATH, "--db", "-d", help="SQLite database path"),
) -> None:
    """Run a local DB smoke check without opening a network listener."""
    engine = ArxivSearch(db_path=db)
    total = engine.paper_count()
    if total <= 0:
        typer.echo(f"Database is empty or unavailable: {engine.db_path}")
        raise typer.Exit(1)

    paper = engine.get_by_id("0704.0001")
    token = "diphoton" if paper and "diphoton" in paper["title"].casefold() else None
    if paper is None or token is None:
        with sqlite3.connect(engine.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT arxiv_id, title
                FROM papers
                WHERE title IS NOT NULL AND title != ''
                ORDER BY arxiv_id
                LIMIT 1
                """
            ).fetchone()

        if row is None:
            typer.echo("No searchable paper found.")
            raise typer.Exit(1)

        paper = engine.get_by_id(row["arxiv_id"])
        if not paper:
            typer.echo(f"Exact lookup failed for {row['arxiv_id']}")
            raise typer.Exit(1)
        tokens = re.findall(r"[A-Za-z0-9.-]{6,}", row["title"])
        token = max(tokens, key=len) if tokens else row["title"].split()[0]

    try:
        matches = engine.search(token, limit=5)
    except InvalidFTSQuery as exc:
        typer.echo(f"Smoke search query failed: {exc}")
        raise typer.Exit(1) from exc

    if not matches:
        typer.echo(f"Title search returned no matches for {token!r}")
        raise typer.Exit(1)

    typer.echo("Smoke OK")
    typer.echo(f"  papers: {total}")
    typer.echo(f"  exact_id: {paper['arxiv_id']}")
    typer.echo(f"  title_query: {token}")
    typer.echo(f"  search_results: {len(matches)}")


@app.command()
def candidates(
    title: str = typer.Argument(..., help="Unmatched title text"),
    db: str = typer.Option(DEFAULT_DB_PATH, "--db", "-d", help="SQLite database path"),
    limit: int = typer.Option(20, "--limit", "-n", min=1, help="Number of review candidates"),
    cat: str = typer.Option("", "--cat", help="Comma-separated exact category tokens"),
    update_date_from: str | None = typer.Option(None, "--update-date-from", help="Inclusive YYYY-MM-DD lower bound"),
    update_date_to: str | None = typer.Option(None, "--update-date-to", help="Inclusive YYYY-MM-DD upper bound"),
) -> None:
    """Return FTS5 review candidates for an unmatched title."""
    categories = [part.strip() for part in cat.split(",") if part.strip()] if cat else None
    engine = ArxivSearch(db_path=db)
    results = engine.candidate_search(
        query=title,
        limit=limit,
        categories=categories,
        update_date_from=update_date_from,
        update_date_to=update_date_to,
    )
    typer.echo(
        json.dumps(
            {
                "query": title,
                "mode": "candidate",
                "auto_accept": False,
                "total": len(results),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command()
def candidates_batch(
    input_path: Path = typer.Argument(..., help="JSONL or JSON file with candidate query items"),
    db: str = typer.Option(DEFAULT_DB_PATH, "--db", "-d", help="SQLite database path"),
    limit: int = typer.Option(20, "--limit", "-n", min=1, help="Default candidates per item"),
    include_details: bool = typer.Option(
        False,
        "--include-details",
        help="Fetch full paper details after lightweight candidate retrieval",
    ),
) -> None:
    """Run multiple candidate searches with one SQLite connection."""
    items = load_candidate_items(input_path)
    engine = ArxivSearch(db_path=db)
    results = engine.candidate_search_batch(
        items,
        default_limit=limit,
        include_details=include_details,
    )
    typer.echo(
        json.dumps(
            {
                "mode": "candidate_batch",
                "auto_accept": False,
                "total": len(results),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command()
def boolean_search(
    input_path: Path = typer.Argument(..., help="JSON file with a normalized Boolean query object"),
    db: str = typer.Option(DEFAULT_DB_PATH, "--db", "-d", help="SQLite database path"),
    limit: int | None = typer.Option(None, "--limit", "-n", min=1, help="Override result limit"),
    sort: str = typer.Option("relevance", "--sort", help="relevance or date"),
) -> None:
    """Run a normalized Boolean query object against the local SQLite backend."""
    request = load_boolean_search_request(input_path)
    query_object = request["query_object"]
    effective_limit = int(limit or request.get("limit") or 50)
    effective_sort = str(request.get("sort") or sort)

    engine = ArxivSearch(db_path=db)
    results = engine.boolean_search(
        query_object=query_object,
        limit=effective_limit,
        sort=effective_sort,
    )
    typer.echo(
        json.dumps(
            {
                "query_object": query_object,
                "limit": effective_limit,
                "mode": "boolean",
                "compiled": engine.boolean_query_provenance(query_object),
                "total": len(results),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command()
def optimize_fts(
    db: str = typer.Option(DEFAULT_DB_PATH, "--db", "-d", help="SQLite database path"),
) -> None:
    """Run FTS5 optimize maintenance without VACUUM."""
    from arxiv_meta.data import ArxivMetaBuilder

    stats = ArxivMetaBuilder(db_path=db).optimize_title_fts()
    typer.echo("FTS optimize complete")
    typer.echo(f"  db: {stats.db_path}")
    typer.echo(f"  size_before_mb: {stats.size_before_bytes / 1024 / 1024:.1f}")
    typer.echo(f"  size_after_mb: {stats.size_after_bytes / 1024 / 1024:.1f}")
    typer.echo(f"  elapsed_seconds: {stats.elapsed_seconds:.2f}")
    typer.echo(f"  vacuum_run: {str(stats.vacuum_run).lower()}")


@app.command()
def rebuild_paper_fts(
    db: str = typer.Option(DEFAULT_DB_PATH, "--db", "-d", help="SQLite database path"),
) -> None:
    """Create or rebuild the multi-field paper FTS index on an existing DB."""
    from arxiv_meta.data import ArxivMetaBuilder

    stats = ArxivMetaBuilder(db_path=db).rebuild_paper_fts_index()
    typer.echo("Paper FTS rebuild complete")
    typer.echo(f"  db: {stats.db_path}")
    typer.echo(f"  size_before_mb: {stats.size_before_bytes / 1024 / 1024:.1f}")
    typer.echo(f"  size_after_mb: {stats.size_after_bytes / 1024 / 1024:.1f}")
    typer.echo(f"  elapsed_seconds: {stats.elapsed_seconds:.2f}")
    typer.echo(f"  vacuum_run: {str(stats.vacuum_run).lower()}")


@app.command()
def download() -> None:
    """Explain that download is not part of the default local workflow."""
    typer.echo("Download is disabled. Use `arxiv-meta build --jsonl PATH --db PATH`.")
    raise typer.Exit(2)


def load_candidate_items(path: Path) -> list[dict]:
    if str(path) == "-":
        return candidate_items_from_lines(sys.stdin.read().splitlines())
    if not path.exists():
        raise typer.BadParameter(f"Input file not found: {path}")
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        payload = json.loads(text)
        if isinstance(payload, dict):
            payload = payload.get("items", payload.get("queries"))
        if not isinstance(payload, list):
            raise typer.BadParameter("JSON input must be a list or an object with items/queries.")
        return [normalize_candidate_item(item) for item in payload if item]
    return candidate_items_from_lines(text.splitlines())


def candidate_items_from_lines(lines: list[str]) -> list[dict]:
    items: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        items.append(normalize_candidate_item(json.loads(line)))
    return items


def normalize_candidate_item(item: dict) -> dict:
    result = dict(item)
    if "query" not in result:
        for key in ("q", "expected_title", "expected_title_norm", "title"):
            if result.get(key):
                result["query"] = result[key]
                break
    if "categories" not in result and result.get("primary_category"):
        result["categories"] = [result["primary_category"]]
    return result


def load_boolean_search_request(path: Path) -> dict:
    if not path.exists():
        raise typer.BadParameter(f"Input file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise typer.BadParameter("Boolean search input must be a JSON object.")
    if "query_object" in payload:
        query_object = payload["query_object"]
        if not isinstance(query_object, dict):
            raise typer.BadParameter("query_object must be a JSON object.")
        return dict(payload)
    return {"query_object": payload}
