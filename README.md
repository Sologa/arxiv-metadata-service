# arXiv Metadata Service

Local arXiv full metadata retrieval service. Based on Kaggle's [arXiv Academic Paper Dataset](https://www.kaggle.com/datasets/Cornell-University/arxiv) (2.69 million papers, updated weekly), builds a SQLite FTS5 full-text search engine with REST API.

## Install

```bash
# pip install from GitHub (no PyPI)
pip install git+https://github.com/diamond2nv/arxiv-metadata-service.git

# With MCP support (Hermes Agent integration)
pip install git+https://github.com/diamond2nv/arxiv-metadata-service.git#egg=arxiv-metadata-service[mcp]

# Local development
git clone git@github.com:diamond2nv/arxiv-metadata-service.git
cd arxiv-metadata-service
pip install -e ".[dev]"
```

Used as an optional dependency in hfpapers-crawler:
```bash
pip install hfpclawer[arxiv]
```

## Architecture

```
Kaggle Dataset (4.58GB JSONL, weekly updates)
        │
        ▼
   download.py     ← Auto download + decompress
        │
        ▼
   SQLite FTS5     ← Millisecond full-text search (~600MB index)
        │
        ▼
   FastAPI          ← REST API (uvicorn)
        │
        ├── GET  /search?q=neural+operator&year_from=2020&limit=50
        ├── GET  /arxiv/{arxiv_id}
        ├── POST /batch-doi     # Batch DOI to arXiv ID lookup
        ├── GET  /stats
        ├── GET  /health
        └── POST /update-raw    # Manually trigger Kaggle download
```

## Quick Start

```bash
# 1. Install
git clone git@github.com:your/arxiv-metadata-service.git
cd arxiv-metadata-service
pip install -e .

# 2. Download and import dataset (first time ~30 minutes, ~15GB disk space)
arxiv-meta download      # Download JSONL.gz from Kaggle (~4.5G)
arxiv-meta import        # Parse JSONL → SQLite FTS5 (~600MB)

# 3. Start API server
arxiv-meta serve         # Default http://localhost:8110

# 4. Test
curl "http://localhost:8110/search?q=neural+operator&limit=3"
curl "http://localhost:8110/arxiv/2001.08361"
```

## API Documentation

| Endpoint | Method | Description |
|------|------|------|
| `/search?q=&year_from=&year_to=&cat=&limit=&sort=` | GET | Full-text search |
| `/arxiv/{arxiv_id}` | GET | Look up single paper by ID |
| `/batch-doi` | POST | Batch DOI → arXiv ID |
| `/stats` | GET | Data statistics |
| `/health` | GET | Health check |
| `/update-raw` | POST | Manually trigger dataset update |

### GET /search

Parameters:
- `q` — FTS5 query term (required, e.g. `"neural operator"`, `"PINN physics-informed"`)
- `year_from` — Start year (default 2017)
- `year_to` — End year (default unlimited)
- `cat` — Category filter (comma-separated, e.g. `"cs.LG,math.NA"`)
- `limit` — Number of results (default 50, max 500)
- `sort` — `relevance` (default) | `date`

Response:
```json
{
  "total": 1234,
  "limit": 50,
  "results": [
    {
      "arxiv_id": "2001.08361",
      "title": "Fourier Neural Operator for Parametric PDEs",
      "authors": "Zongyi Li et al.",
      "abstract": "...",
      "categories": ["cs.LG", "math.NA"],
      "doi": "10.1007/978-3-030-58589-1_1",
      "journal_ref": "NeurIPS 2020",
      "published_date": "2020-10-22"
    }
  ]
}
```

### GET /arxiv/{arxiv_id}

```json
{
  "arxiv_id": "2001.08361",
  "title": "...",
  "authors": "Zongyi Li, Nikola Kovachki, Kamyar Azizzadenesheli...",
  "abstract": "...",
  "categories": ["cs.LG", "math.NA", "physics.comp-ph"],
  "doi": "10.1007/978-3-030-58589-1_1",
  "journal_ref": "NeurIPS 2020",
  "published_date": "2001-01-23"
}
```

### POST /batch-doi

Request body:
```json
{
  "dois": ["10.1007/978-3-030-58589-1_1", "10.1038/s42256-022-00576-1"]
}
```

Response:
```json
{
  "results": {
    "10.1007/978-3-030-58589-1_1": "2001.08361",
    "10.1038/s42256-022-00576-1": "2109.05237"
  },
  "not_found": []
}
```

## Data Updates

The Kaggle dataset is updated weekly. The service automatically detects updates:

```bash
# Manually trigger
arxiv-meta update

# View current data version
curl http://localhost:8110/stats
# Returns: {total: 2689088, version: "2025-03-13", ...}
```

### Incremental Update Strategy

The import script uses `INSERT OR IGNORE` — existing papers in the new dataset are automatically skipped. New papers are written to the FTS5 index in batches. Each update only adds new records without needing to rebuild the index.

## Configuration

Configure via `config.yaml` or environment variables:

| Config | Environment Variable | Default |
|--------|---------|--------|
| `db.path` | `ARXIV_META_DB` | `data/arxiv_meta.db` |
| `server.host` | `ARXIV_META_HOST` | `0.0.0.0` |
| `server.port` | `ARXIV_META_PORT` | `8110` |
| `kaggle.dataset` | — | `Cornell-University/arxiv` |
| `data.dir` | `ARXIV_META_DATA` | `data/` |
| `data.jsonl` | — | `data/arxiv_metadata.jsonl` |

## Performance

- **Search**: top 50 results in < 100ms (FTS5 index, ~600MB)
- **Single paper lookup**: < 10ms (B-tree primary key query)
- **Batch DOI**: < 50ms / 100 DOIs
- **Memory**: ~200MB (SQLite page cache)
- **Disk**: ~600MB (SQLite DB) + ~4.5G (raw JSONL, can be deleted)

## Integration with Other Projects

### hfpapers-crawler (package: `hfpclawer`) [Updated]

> Naming philosophy: **claw** (sharp claw) ≠ **crawl** (creep/crawl).
> `hfpclawer` = HF Papers + claw + er = "Intelligent tool for precisely grasping papers with sharp claws"
> Integrated via `hfpclawer[arxiv]` optional dependency

Configure in `config.yaml`:

```yaml
sources:
  arxiv_remote:
    enable: true
    api_base: "http://localhost:8110"
    max_results: 100
```

`ArxivRemoteSource` replaces local SQLite search and treats it as a REST API call.

### Hermes Agent

Add MCP server in `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  arxiv_meta:
    command: "arxiv-meta"
    args: ["mcp"]
    env:
      ARXIV_META_DB: "~/Gitlab/Agentic4Sci/arxiv-metadata-service/data/arxiv_meta.db"
```
