# MCP Server — Hermes Agent 集成

# 这个文件不是包依赖，运行时动态检查 mcp 库

def run_mcp_server():
    """启动 MCP stdio server"""
    try:
        import mcp.server.stdio
        import mcp.types as types
        from mcp.server import Server
    except ImportError:
        import subprocess, sys, os
        print("安装 mcp 库...", file=sys.stderr)
        subprocess.run(
            [os.environ.get("PIP", sys.executable + " -m pip"), "install", "mcp"],
            check=True,
        )
        import mcp.server.stdio  # noqa: F811
        import mcp.types as types
        from mcp.server import Server

    from arxiv_meta.search import ArxivSearch

    engine = ArxivSearch()
    server = Server("arxiv-meta")

    @server.list_tools()
    async def handle_list_tools():
        return [
            types.Tool(
                name="arxiv_search",
                description="Search arXiv metadata (title, authors, abstract) using FTS5 full-text search. 269万篇论文，毫秒级响应。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "FTS5 查询词，如 'neural operator'、'PINN physics-informed'"},
                        "limit": {"type": "integer", "description": "结果数，默认 50", "default": 50},
                        "year_from": {"type": "integer", "description": "起始年份", "default": 2017},
                        "sort": {"type": "string", "enum": ["relevance", "date"], "description": "排序方式", "default": "relevance"},
                    },
                    "required": ["query"],
                },
            ),
            types.Tool(
                name="arxiv_get_paper",
                description="Get a single paper's full metadata by arXiv ID.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "arxiv_id": {"type": "string", "description": "arXiv ID, e.g. '2001.08361'"},
                    },
                    "required": ["arxiv_id"],
                },
            ),
            types.Tool(
                name="arxiv_batch_doi",
                description="Batch convert DOIs to arXiv IDs.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "dois": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of DOIs",
                        },
                    },
                    "required": ["dois"],
                },
            ),
            types.Tool(
                name="arxiv_stats",
                description="Get dataset statistics (total papers, DOI coverage, etc.)",
                inputSchema={"type": "object", "properties": {}},
            ),
        ]

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict):
        import json
        if name == "arxiv_search":
            results = engine.search(
                query=arguments["query"],
                limit=arguments.get("limit", 50),
                year_from=arguments.get("year_from", 0),
                sort=arguments.get("sort", "relevance"),
            )
            return [types.TextContent(type="text", text=json.dumps(results, ensure_ascii=False))]

        elif name == "arxiv_get_paper":
            paper = engine.get_by_id(arguments["arxiv_id"])
            if paper:
                return [types.TextContent(type="text", text=json.dumps(dict(paper), ensure_ascii=False))]
            return [types.TextContent(type="text", text=json.dumps({"error": "not found"}))]

        elif name == "arxiv_batch_doi":
            results = engine.get_by_dois(arguments["dois"])
            return [types.TextContent(type="text", text=json.dumps(results, ensure_ascii=False))]

        elif name == "arxiv_stats":
            return [types.TextContent(type="text", text=json.dumps(engine.stats(), ensure_ascii=False))]

        raise ValueError(f"未知工具: {name}")

    import anyio
    async def main():
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    anyio.run(main)
