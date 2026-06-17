#!/usr/bin/env python3
"""FastAPI service for local arXiv metadata queries."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from arxiv_meta.config import DEFAULT_HOST
from arxiv_meta.config import get as cfg_get
from arxiv_meta.search import (
    MAX_CANDIDATE_BATCH,
    MAX_DOI_BATCH,
    MAX_DOI_LENGTH,
    MAX_QUERY_LENGTH,
    ArxivSearch,
    QueryLimitError,
    SearchError,
)

logger = logging.getLogger("arxiv_meta.api")

_engine: ArxivSearch | None = None
_engine_db_path: str | None = None


def configure_engine(db_path: str | None = None) -> None:
    global _engine, _engine_db_path
    _engine = ArxivSearch(db_path=db_path)
    _engine_db_path = _engine.db_path


def get_engine() -> ArxivSearch:
    global _engine
    if _engine is None:
        configure_engine()
    assert _engine is not None
    return _engine


def error_response(status_code: int, error: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": error, "message": message})


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = get_engine()
    total = engine.paper_count()
    if engine.ready():
        logger.info("Search engine ready: %s papers", f"{total:,}")
    else:
        logger.warning("Database is empty or unavailable: %s", engine.db_path)
    yield


app = FastAPI(
    title="arXiv Metadata Service",
    description="Local-only arXiv metadata query API over a title-only FTS5 SQLite index.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(SearchError)
async def handle_search_error(request: Request, exc: SearchError) -> JSONResponse:
    return error_response(400, exc.error_code, str(exc))


class BatchDoiRequest(BaseModel):
    dois: list[str]


class BatchDoiResponse(BaseModel):
    results: dict[str, str]
    not_found: list[str]


class CandidateBatchItem(BaseModel):
    request_id: str | None = None
    query: str
    limit: int | None = None
    categories: list[str] | None = None
    update_date_from: str | None = None
    update_date_to: str | None = None
    include_details: bool = False


class CandidateBatchRequest(BaseModel):
    items: list[CandidateBatchItem]
    default_limit: int = 20
    include_details: bool = False


class BooleanSearchRequest(BaseModel):
    query_object: dict[str, Any]
    limit: int = 50
    sort: Literal["relevance", "date"] = "relevance"


class HealthResponse(BaseModel):
    status: str
    papers: int
    db_ready: bool


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    engine = get_engine()
    total = engine.paper_count()
    return HealthResponse(
        status="ok" if total > 0 else "empty",
        papers=total,
        db_ready=total > 0,
    )


@app.get("/stats")
def stats() -> dict[str, int | float]:
    engine = get_engine()
    summary = engine.stats()
    db_size = os.path.getsize(engine.db_path) if os.path.exists(engine.db_path) else 0
    return {
        "total_papers": summary["total"],
        "with_doi": summary["has_doi"],
        "with_journal_ref": summary["has_journal"],
        "db_size_mb": round(db_size / 1024 / 1024, 1),
    }


@app.get("/arxiv/{arxiv_id}")
def get_arxiv(arxiv_id: str):
    engine = get_engine()
    paper = engine.get_by_id(arxiv_id)
    if not paper:
        raise HTTPException(status_code=404, detail=f"arXiv ID {arxiv_id} not found")
    return paper


@app.get("/search")
def search(
    q: str = Query(..., min_length=1, description=f"Plain title keywords, max {MAX_QUERY_LENGTH} chars"),
    limit: int = Query(50, ge=1, description="Number of results, max 500"),
    cat: str = Query("", description="Comma-separated exact category tokens"),
    update_date_from: str | None = Query(None, description="Inclusive YYYY-MM-DD lower bound"),
    update_date_to: str | None = Query(None, description="Inclusive YYYY-MM-DD upper bound"),
    sort: Literal["relevance", "date"] = Query("relevance"),
):
    categories = [part.strip() for part in cat.split(",") if part.strip()] if cat else None
    engine = get_engine()
    results = engine.search(
        query=q,
        limit=limit,
        categories=categories,
        update_date_from=update_date_from,
        update_date_to=update_date_to,
        sort=sort,
    )
    return {
        "query": q,
        "limit": limit,
        "total": len(results),
        "results": results,
    }


@app.get("/candidate-search")
def candidate_search(
    q: str = Query(..., min_length=1, description="Unmatched title text for review candidates"),
    limit: int = Query(20, ge=1, description="Number of review candidates, max 500"),
    cat: str = Query("", description="Comma-separated exact category tokens"),
    update_date_from: str | None = Query(None, description="Inclusive YYYY-MM-DD lower bound"),
    update_date_to: str | None = Query(None, description="Inclusive YYYY-MM-DD upper bound"),
    include_details: bool = Query(False, description="Fetch full paper details after candidate retrieval"),
):
    categories = [part.strip() for part in cat.split(",") if part.strip()] if cat else None
    engine = get_engine()
    results = engine.candidate_search(
        query=q,
        limit=limit,
        categories=categories,
        update_date_from=update_date_from,
        update_date_to=update_date_to,
        include_details=include_details,
    )
    return {
        "query": q,
        "limit": limit,
        "mode": "candidate",
        "auto_accept": False,
        "total": len(results),
        "results": results,
    }


@app.post("/candidates-batch")
def candidates_batch(req: CandidateBatchRequest):
    if len(req.items) > MAX_CANDIDATE_BATCH:
        raise QueryLimitError(f"Maximum candidate batch size is {MAX_CANDIDATE_BATCH}.")

    engine = get_engine()
    items = [item.model_dump(exclude_none=True) for item in req.items]
    results = engine.candidate_search_batch(
        items,
        default_limit=req.default_limit,
        include_details=req.include_details,
    )
    return {
        "mode": "candidate_batch",
        "auto_accept": False,
        "total": len(results),
        "results": results,
    }


@app.post("/boolean-search")
def boolean_search(req: BooleanSearchRequest):
    engine = get_engine()
    results = engine.boolean_search(
        query_object=req.query_object,
        limit=req.limit,
        sort=req.sort,
    )
    return {
        "query_object": req.query_object,
        "limit": req.limit,
        "mode": "boolean",
        "compiled": engine.boolean_query_provenance(req.query_object),
        "total": len(results),
        "results": results,
    }


@app.post("/batch-doi", response_model=BatchDoiResponse)
def batch_doi(req: BatchDoiRequest) -> BatchDoiResponse:
    if len(req.dois) > MAX_DOI_BATCH:
        raise QueryLimitError(f"Maximum DOI batch size is {MAX_DOI_BATCH}.")
    if any(len(doi or "") > MAX_DOI_LENGTH for doi in req.dois):
        raise QueryLimitError(f"DOI must be at most {MAX_DOI_LENGTH} characters.")

    engine = get_engine()
    results = engine.get_by_dois(req.dois)
    not_found = [doi for doi in req.dois if (doi or "").strip() and doi not in results]
    return BatchDoiResponse(results=results, not_found=not_found)


def run_server(host: str | None = None, port: int | None = None, db_path: str | None = None) -> None:
    import uvicorn

    host = host or cfg_get("server.host", DEFAULT_HOST)
    port = int(port or cfg_get("server.port", 8110))
    configure_engine(db_path=db_path)

    logger.info("arXiv Metadata Service -> http://%s:%s", host, port)
    logger.info("API docs -> http://%s:%s/docs", host, port)
    uvicorn.run(app, host=host, port=port)
