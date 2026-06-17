from __future__ import annotations

from pathlib import Path

import pytest

from arxiv_meta.data import ArxivMetaBuilder


@pytest.fixture()
def sample_jsonl() -> Path:
    return Path(__file__).parent / "fixtures" / "arxiv_sample.jsonl"


@pytest.fixture()
def sample_db(tmp_path: Path, sample_jsonl: Path) -> Path:
    db_path = tmp_path / "arxiv_sample.sqlite"
    builder = ArxivMetaBuilder(db_path=db_path)
    builder.build(sample_jsonl, batch_size=2)
    return db_path
