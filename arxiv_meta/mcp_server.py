#!/usr/bin/env python3
"""MCP integration is intentionally disabled for this local-only service."""


def run_mcp_server() -> None:
    raise RuntimeError(
        "MCP runtime is disabled. This service is intended to run through "
        "`arxiv-meta build`, `arxiv-meta serve`, and `arxiv-meta smoke`."
    )
