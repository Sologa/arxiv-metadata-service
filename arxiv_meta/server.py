# FastAPI 服务 — arXiv 元数据 REST API

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from arxiv_meta.search import ArxivSearch

logger = logging.getLogger("arxiv_meta.api")

# ─── 全局搜索引擎单例 ──────────────────────────
_engine: ArxivSearch | None = None


def get_engine() -> ArxivSearch:
    global _engine
    if _engine is None:
        _engine = ArxivSearch()
    return _engine


# ─── Lifespan ────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时检查数据库状态"""
    engine = get_engine()
    if engine.ready():
        stats = engine.stats()
        logger.info(f"📚 搜索引擎就绪: {stats['total']:,} 篇论文")
    else:
        logger.warning("⚠️  数据库为空！请先运行: arxiv-meta import")
        logger.warning("   或: arxiv-meta download && arxiv-meta build")
    yield


app = FastAPI(
    title="arXiv Metadata Service",
    description="本地 arXiv 全量元数据检索 API (269 万篇论文)",
    version="0.1.0",
    lifespan=lifespan,
)


# ─── 模型 ───────────────────────────────────


class BatchDoiRequest(BaseModel):
    dois: list[str]


class BatchDoiResponse(BaseModel):
    results: dict[str, str]  # doi -> arxiv_id
    not_found: list[str]


class HealthResponse(BaseModel):
    status: str
    papers: int
    db_ready: bool


# ─── API ────────────────────────────────────


@app.get("/search")
def search(
    q: str = Query(..., description="FTS5 查询词"),
    limit: int = Query(50, ge=1, le=500, description="结果数"),
    year_from: int = Query(0, ge=0, description="起始年份"),
    year_to: int = Query(0, ge=0, description="截止年份"),
    cat: str = Query("", description="分类过滤，逗号分隔"),
    sort: str = Query("relevance", regex="^(relevance|date)$"),
):
    """全文搜索 arXiv 元数据"""
    categories = cat.split(",") if cat else None
    engine = get_engine()
    results = engine.search(
        query=q,
        limit=limit,
        year_from=year_from,
        year_to=year_to,
        categories=categories,
        sort=sort,
    )
    return {
        "query": q,
        "limit": limit,
        "total": len(results),
        "results": results,
    }


@app.get("/arxiv/{arxiv_id}")
def get_arxiv(arxiv_id: str):
    """按 arXiv ID 查单篇"""
    engine = get_engine()
    paper = engine.get_by_id(arxiv_id)
    if not paper:
        raise HTTPException(status_code=404, detail=f"arXiv ID {arxiv_id} 未找到")
    return {
        "arxiv_id": paper["arxiv_id"],
        "title": paper["title"],
        "authors": paper["authors"],
        "abstract": paper["abstract"],
        "categories": (paper.get("categories") or "").split(),
        "doi": paper.get("doi") or "",
        "journal_ref": paper.get("journal_ref") or "",
        "published_date": paper.get("update_date") or "",
    }


@app.post("/batch-doi", response_model=BatchDoiResponse)
def batch_doi(req: BatchDoiRequest):
    """批量 DOI → arXiv ID 转换"""
    engine = get_engine()
    results = engine.get_by_dois(req.dois)
    not_found = [d for d in req.dois if d not in results]
    return BatchDoiResponse(results=results, not_found=not_found)


@app.get("/stats")
def stats():
    """数据库统计信息"""
    engine = get_engine()
    s = engine.stats()
    import os
    db_path = engine.db_path
    db_size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
    return {
        "total_papers": s["total"],
        "with_doi": s["has_doi"],
        "with_journal_ref": s["has_journal"],
        "db_size_mb": round(db_size / 1024 / 1024, 1),
        "ready": s["total"] > 1000,
    }


@app.get("/health", response_model=HealthResponse)
def health():
    """健康检查"""
    engine = get_engine()
    s = engine.stats()
    return HealthResponse(
        status="ok" if s["total"] > 0 else "empty",
        papers=s["total"],
        db_ready=s["total"] > 1000,
    )


# ─── 直接启动 ────────────────────────────────


def run_server(host: str = None, port: int = None):
    """启动 uvicorn 服务器"""
    import uvicorn
    if host is None:
        from arxiv_meta.config import get as cfg_get
        host = cfg_get("server.host", "0.0.0.0")
        port = int(cfg_get("server.port", 8110))
    logger.info(f"🌐 arXiv Metadata Service → http://{host}:{port}")
    logger.info(f"📚 API 文档 → http://{host}:{port}/docs")
    uvicorn.run(app, host=host, port=port)
