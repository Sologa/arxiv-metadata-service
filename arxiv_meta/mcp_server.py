#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# MCP Server — Hermes Agent integration

# This file is not a package dependency; it dynamically checks for the mcp library at runtime

def run_mcp_server():
    """Start MCP stdio server"""
    try:
        import mcp.server.stdio
        import mcp.types as types
        from mcp.server import Server
    except ImportError:
        import os
        import subprocess
        import sys
        print("Installing mcp library...", file=sys.stderr)
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
                description="Search arXiv metadata (title, authors, abstract) using FTS5 full-text search. 2.69 million papers, millisecond response.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "FTS5 query term, e.g. 'neural operator', 'PINN physics-informed'"},
                        "limit": {"type": "integer", "description": "Number of results, default 50", "default": 50},
                        "year_from": {"type": "integer", "description": "Start year", "default": 2017},
                        "sort": {"type": "string", "enum": ["relevance", "date"], "description": "Sort method", "default": "relevance"},
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

        raise ValueError(f"Unknown tool: {name}")

    import anyio
    async def main():
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    anyio.run(main)
