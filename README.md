# arXiv Metadata Service

本地 arXiv 全量元数据检索服务。基于 Kaggle 的 [arXiv Academic Paper Dataset](https://www.kaggle.com/datasets/Cornell-University/arxiv)（269 万篇论文，每周更新），构建 SQLite FTS5 全文检索引擎，提供 REST API。

## 架构

```
Kaggle 数据集（4.58GB JSONL，每周更新）
        │
        ▼
   download.py     ← 自动下载 + 解压
        │
        ▼
   SQLite FTS5     ← 毫秒级全文搜索（~600MB 索引）
        │
        ▼
   FastAPI          ← REST API（uvicorn）
        │
        ├── GET  /search?q=neural+operator&year_from=2020&limit=50
        ├── GET  /arxiv/{arxiv_id}
        ├── POST /batch-doi     # 批量按 DOI 查 arXiv ID
        ├── GET  /stats
        ├── GET  /health
        └── POST /update-raw    # 手动触发 Kaggle 下载
```

## 快速开始

```bash
# 1. 安装
git clone git@github.com:your/arxiv-metadata-service.git
cd arxiv-metadata-service
pip install -e .

# 2. 下载并导入数据集（首次需约 30 分钟，耗 ~15GB 磁盘）
arxiv-meta download      # 从 Kaggle 下载 JSONL.gz（~4.5G）
arxiv-meta import        # 解析 JSONL → SQLite FTS5（~600MB）

# 3. 启动 API 服务
arxiv-meta serve         # 默认 http://localhost:8110

# 4. 测试
curl "http://localhost:8110/search?q=neural+operator&limit=3"
curl "http://localhost:8110/arxiv/2001.08361"
```

## API 文档

| 端点 | 方法 | 说明 |
|------|------|------|
| `/search?q=&year_from=&year_to=&cat=&limit=&sort=` | GET | 全文搜索 |
| `/arxiv/{arxiv_id}` | GET | 按 ID 查单篇 |
| `/batch-doi` | POST | 批量 DOI → arXiv ID |
| `/stats` | GET | 数据统计 |
| `/health` | GET | 健康检查 |
| `/update-raw` | POST | 手动触发数据集更新 |

### GET /search

参数:
- `q` — FTS5 查询词（必填，如 `"neural operator"`、`"PINN physics-informed"`）
- `year_from` — 起始年份（默认 2017）
- `year_to` — 截止年份（默认不限）
- `cat` — 分类过滤（逗号分隔，如 `"cs.LG,math.NA"`）
- `limit` — 结果数（默认 50，最大 500）
- `sort` — `relevance`（默认）| `date`

返回:
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
  "published_date": "2020-01-23"
}
```

### POST /batch-doi

请求体:
```json
{
  "dois": ["10.1007/978-3-030-58589-1_1", "10.1038/s42256-022-00576-1"]
}
```

返回:
```json
{
  "results": {
    "10.1007/978-3-030-58589-1_1": "2001.08361",
    "10.1038/s42256-022-00576-1": "2109.05237"
  },
  "not_found": []
}
```

## 数据更新

Kaggle 数据集每周更新。服务自动检测更新：

```bash
# 手动触发
arxiv-meta update

# 查看当前数据版本
curl http://localhost:8110/stats
# 返回: {total: 2689088, version: "2025-03-13", ...}
```

### 增量更新策略

导入脚本使用 `INSERT OR IGNORE`，新数据集中的已有论文自动跳过。新论文在 batches 中分批写入 FTS5 索引。每次更新只增加新记录，无需重建索引。

## 配置

通过 `config.yaml` 或环境变量配置：

| 配置项 | 环境变量 | 默认值 |
|--------|---------|--------|
| `db.path` | `ARXIV_META_DB` | `data/arxiv_meta.db` |
| `server.host` | `ARXIV_META_HOST` | `0.0.0.0` |
| `server.port` | `ARXIV_META_PORT` | `8110` |
| `kaggle.dataset` | — | `Cornell-University/arxiv` |
| `data.dir` | `ARXIV_META_DATA` | `data/` |
| `data.jsonl` | — | `data/arxiv_metadata.jsonl` |

## 性能

- **搜索**: 100ms 内返回前 50 条（FTS5 索引，~600MB）
- **查单篇**: < 10ms（B-tree 主键查询）
- **批量 DOI**: < 50ms / 100 个 DOI
- **内存占用**: ~200MB（SQLite page cache）
- **磁盘**: ~600MB（SQLite DB）+ ~4.5G（原始 JSONL，可删除）

## 与其他项目集成

### hfpapers-crawler（包名: `hfpclawer`）[更新]

> 命名哲学: **claw**（利爪）≠ **crawl**（爬行）。
> `hfpclawer` = HF Papers + claw (爪) + er (者) = "用利爪精准抓取论文的智能工具"
> 通过 `hfpclawer[arxiv]` 可选依赖集成

在 `config.yaml` 中配置：

```yaml
sources:
  arxiv_remote:
    enable: true
    api_base: "http://localhost:8110"
    max_results: 100
```

`ArxivRemoteSource` 会替代本地 SQLite 搜索，把它当 REST API 调用。

### Hermes Agent

在 `~/.hermes/config.yaml` 中添加 MCP 服务器：

```yaml
mcp_servers:
  arxiv_meta:
    command: "arxiv-meta"
    args: ["mcp"]
    env:
      ARXIV_META_DB: "~/Gitlab/Agentic4Sci/arxiv-metadata-service/data/arxiv_meta.db"
```
