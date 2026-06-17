from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

from arxiv_meta import server
from arxiv_meta.config import DEFAULT_HOST

ROOT = Path(__file__).resolve().parents[1]


def test_default_server_host_is_localhost(sample_db, monkeypatch):
    captured = {}

    def fake_run(app, host, port):
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setattr("uvicorn.run", fake_run)

    server.run_server(port=8119, db_path=str(sample_db))

    assert captured == {"host": DEFAULT_HOST, "port": 8119}


def test_public_bind_requires_explicit_override(sample_db, monkeypatch):
    captured = {}

    def fake_run(app, host, port):
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setattr("uvicorn.run", fake_run)

    server.run_server(host="0.0.0.0", port=8119, db_path=str(sample_db))

    assert captured == {"host": "0.0.0.0", "port": 8119}


def test_default_runtime_has_no_mcp_dependency_or_runtime_install():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    dependencies = "\n".join(pyproject["project"]["dependencies"])
    dev_dependencies = "\n".join(pyproject["dependency-groups"]["dev"])
    mcp_server = (ROOT / "arxiv_meta" / "mcp_server.py").read_text()
    cli = (ROOT / "arxiv_meta" / "cli.py").read_text()

    assert "mcp" not in dependencies.lower()
    assert "mcp" not in dev_dependencies.lower()
    assert "pip install" not in mcp_server
    assert "subprocess" not in mcp_server
    assert "@app.command()\ndef mcp" not in cli


def test_generated_files_are_ignored_but_uv_lock_is_tracked():
    ignored = [
        "._index",
        "data/arxiv.sqlite",
        "arxiv_oai_title_fts.sqlite",
        "arxiv_oai_title_fts.sqlite-wal",
        "arxiv_oai_title_fts.sqlite-shm",
    ]
    for path in ignored:
        result = subprocess.run(
            ["git", "check-ignore", "-q", path],
            cwd=ROOT,
            check=False,
        )
        assert result.returncode == 0, path

    lock_result = subprocess.run(
        ["git", "check-ignore", "-q", "uv.lock"],
        cwd=ROOT,
        check=False,
    )
    assert lock_result.returncode != 0
