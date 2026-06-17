from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from arxiv_meta.cli import app
from arxiv_meta.data import ArxivMetaBuilder


@pytest.fixture()
def boolean_jsonl() -> Path:
    return Path(__file__).parents[1] / "fixtures" / "boolean_backend" / "arxiv_boolean_sample.jsonl"


@pytest.fixture()
def boolean_db(tmp_path: Path, boolean_jsonl: Path) -> Path:
    db_path = tmp_path / "boolean_backend.sqlite"
    ArxivMetaBuilder(db_path=db_path).build(boolean_jsonl, batch_size=2)
    return db_path


def test_boolean_search_cli_reads_normalized_query_file(tmp_path: Path, boolean_db: Path):
    query_path = tmp_path / "query.json"
    query_path.write_text(
        json.dumps(
            {
                "query_object": {
                    "op": "AND",
                    "children": [
                        {"field": "abstract", "match": {"type": "term", "value": "taxonomyneedle"}},
                        {"field": "category", "match": {"type": "term", "value": "cs.AI"}},
                    ],
                },
                "limit": 5,
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["boolean-search", str(query_path), "--db", str(boolean_db)])

    assert result.exit_code == 0
    body = json.loads(result.stdout)
    assert body["mode"] == "boolean"
    assert body["compiled"]["fields"] == ["abstract", "category"]
    assert body["total"] == 1
    assert body["results"][0]["arxiv_id"] == "2601.00002"
