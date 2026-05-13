#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# CLI entry point — arxiv-meta command

import logging
from pathlib import Path

import typer

from arxiv_meta.config import get, load_config

app = typer.Typer(name="arxiv-meta", help="arXiv Metadata Service CLI")

logger = logging.getLogger("arxiv_meta.cli")


@app.callback()
def main_callback(verbose: bool = typer.Option(False, "--verbose", "-v")):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


@app.command()
def download(
    force: bool = typer.Option(False, "--force", "-f", help="Force re-download"),
):
    """Download arXiv metadata dataset from Kaggle"""
    from arxiv_meta.data import download_dataset
    path = download_dataset(force=force)
    typer.echo(f"✅ Download complete: {path}")
    size_mb = path.stat().st_size / 1024 / 1024
    typer.echo(f"   Size: {size_mb:.0f} MB")
    typer.echo("   Next: arxiv-meta build")


@app.command()
def build(
    jsonl: str = typer.Option("", "--jsonl", "-j", help="JSONL file path, defaults to config path"),
    batch_size: int = typer.Option(2000, "--batch", "-b", help="Batch size"),
):
    """Build SQLite FTS5 index"""
    from arxiv_meta.data import ArxivMetaBuilder
    if not jsonl:
        jsonl = get("data.jsonl", "data/arxiv_metadata.jsonl")
    jsonl_path = Path(jsonl)
    if not jsonl_path.is_absolute():
        jsonl_path = Path(__file__).parent.parent / jsonl_path
    if not jsonl_path.exists():
        typer.echo(f"❌ JSONL file not found: {jsonl_path}")
        typer.echo("   First run: arxiv-meta download")
        raise typer.Exit(1)

    builder = ArxivMetaBuilder()
    total = builder.build(str(jsonl_path), batch_size=batch_size)
    stats = builder.count()
    typer.echo(f"✅ Import complete: {total:,} papers")
    typer.echo(f"   Database total: {stats:,} papers")
    db_path = builder.db_path
    size_mb = Path(db_path).stat().st_size / 1024 / 1024
    typer.echo(f"   Database size: {size_mb:.0f} MB")
    typer.echo("   Next: arxiv-meta serve")


@app.command()
def serve(
    host: str = typer.Option(None, "--host", "-h", help="Listen address"),
    port: int = typer.Option(None, "--port", "-p", help="Port"),
):
    """Start FastAPI server"""
    from arxiv_meta.server import run_server
    run_server(host=host, port=port)


@app.command()
def update():
    """Update dataset (re-download + incremental import)"""
    from arxiv_meta.data import ArxivMetaBuilder, download_dataset
    from arxiv_meta.search import ArxivSearch

    # Check current state
    engine = ArxivSearch()
    old_stats = engine.stats()
    typer.echo(f"📊 Current: {old_stats['total']:,} papers")

    # Download latest
    jsonl_path = download_dataset(force=True)
    typer.echo("📥 Downloaded latest dataset")

    # Incremental import (INSERT OR IGNORE automatically skips existing)
    builder = ArxivMetaBuilder()
    builder.build(str(jsonl_path))
    final_count = builder.count()
    new_added = final_count - old_stats["total"]
    typer.echo("✅ Update complete")
    typer.echo(f"   New: {new_added:,} papers")
    typer.echo(f"   Total: {final_count:,} papers")


@app.command()
def config():
    """View current configuration"""
    import json
    cfg = load_config()
    typer.echo(json.dumps(cfg, indent=2, default=str))


@app.command()
def mcp():
    """Start MCP Server (Hermes Agent integration)"""
    from arxiv_meta.mcp_server import run_mcp_server
    run_mcp_server()
