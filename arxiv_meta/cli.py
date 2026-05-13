# CLI 入口 — arxiv-meta 命令

import logging
import typer
from pathlib import Path

from arxiv_meta.config import get, load_config

app = typer.Typer(name="arxiv-meta", help="arXiv 元数据服务 CLI")

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
    force: bool = typer.Option(False, "--force", "-f", help="强制重新下载"),
):
    """从 Kaggle 下载 arXiv 元数据集"""
    from arxiv_meta.data import download_dataset
    path = download_dataset(force=force)
    typer.echo(f"✅ 下载完成: {path}")
    size_mb = path.stat().st_size / 1024 / 1024
    typer.echo(f"   大小: {size_mb:.0f} MB")
    typer.echo(f"   下一步: arxiv-meta build")


@app.command()
def build(
    jsonl: str = typer.Option("", "--jsonl", "-j", help="JSONL 文件路径，默认使用 config 中的路径"),
    batch_size: int = typer.Option(2000, "--batch", "-b", help="批处理大小"),
):
    """构建 SQLite FTS5 索引"""
    from arxiv_meta.data import ArxivMetaBuilder
    if not jsonl:
        jsonl = get("data.jsonl", "data/arxiv_metadata.jsonl")
    jsonl_path = Path(jsonl)
    if not jsonl_path.is_absolute():
        jsonl_path = Path(__file__).parent.parent / jsonl_path
    if not jsonl_path.exists():
        typer.echo(f"❌ JSONL 文件不存在: {jsonl_path}")
        typer.echo("   先用 arxiv-meta download 下载")
        raise typer.Exit(1)

    builder = ArxivMetaBuilder()
    total = builder.build(str(jsonl_path), batch_size=batch_size)
    stats = builder.count()
    typer.echo(f"✅ 导入完成: {total:,} 篇")
    typer.echo(f"   数据库总计: {stats:,} 篇")
    db_path = builder.db_path
    size_mb = Path(db_path).stat().st_size / 1024 / 1024
    typer.echo(f"   数据库大小: {size_mb:.0f} MB")
    typer.echo(f"   下一步: arxiv-meta serve")


@app.command()
def serve(
    host: str = typer.Option(None, "--host", "-h", help="监听地址"),
    port: int = typer.Option(None, "--port", "-p", help="端口"),
):
    """启动 FastAPI 服务"""
    from arxiv_meta.server import run_server
    run_server(host=host, port=port)


@app.command()
def update():
    """更新数据集（重新下载 + 增量导入）"""
    from arxiv_meta.data import download_dataset, ArxivMetaBuilder
    from arxiv_meta.search import ArxivSearch

    # 检查当前状态
    engine = ArxivSearch()
    old_stats = engine.stats()
    typer.echo(f"📊 当前: {old_stats['total']:,} 篇论文")

    # 下载最新
    jsonl_path = download_dataset(force=True)
    typer.echo(f"📥 已下载最新数据集")

    # 增量导入（INSERT OR IGNORE 自动跳过已有的）
    builder = ArxivMetaBuilder()
    new_total = builder.build(str(jsonl_path))
    final_count = builder.count()
    new_added = final_count - old_stats["total"]
    typer.echo(f"✅ 更新完成")
    typer.echo(f"   新增: {new_added:,} 篇")
    typer.echo(f"   总计: {final_count:,} 篇")


@app.command()
def config():
    """查看当前配置"""
    import json
    cfg = load_config()
    typer.echo(json.dumps(cfg, indent=2, default=str))


@app.command()
def mcp():
    """启动 MCP Server（Hermes Agent 集成）"""
    from arxiv_meta.mcp_server import run_mcp_server
    run_mcp_server()
