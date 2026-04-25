import json
from pathlib import Path

from vault_engine.config import EngineConfig
from vault_engine.embedder import MockEmbedder
from vault_engine.eval import EvalRunner, FixtureRow
from vault_engine.indexer import Indexer
from vault_engine.retrieval import Retrieval


def test_fixture_row_parse_strict():
    raw = {
        "id": "x",
        "query": "alpha",
        "expected_pages": ["alpha"],
        "min_citation_depth": 1,
        "mode": "lookup",
        "max_latency_ms": 1000,
    }
    row = FixtureRow.from_dict(raw)
    assert row.id == "x"
    assert row.expected_pages == ["alpha"]


def test_eval_runner_passes_for_seeded_lookup(sample_vault: Path, tmp_path: Path):
    cfg = EngineConfig(vault_path=sample_vault, cache_dir=tmp_path / "cache")
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    idx = Indexer(cfg=cfg, embedder=MockEmbedder(dim=cfg.embedding_dim))
    idx.open()
    try:
        idx.rebuild()
        r = Retrieval(cfg=cfg, indexer=idx, embedder=idx.embedder)
        fixture_path = tmp_path / "fix.jsonl"
        fixture_path.write_text(
            json.dumps({
                "id": "lookup-alpha",
                "query": "alpha",
                "expected_pages": ["alpha"],
                "min_citation_depth": 1,
                "mode": "lookup",
                "max_latency_ms": 5000,
            }) + "\n"
        )
        runner = EvalRunner(cfg=cfg, retrieval=r)
        report = runner.run(fixture_path)
        assert report.total == 1
        assert report.passed == 1
        assert report.failed == 0
    finally:
        idx.close()


def test_eval_runner_records_failure(sample_vault: Path, tmp_path: Path):
    cfg = EngineConfig(vault_path=sample_vault, cache_dir=tmp_path / "cache")
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    idx = Indexer(cfg=cfg, embedder=MockEmbedder(dim=cfg.embedding_dim))
    idx.open()
    try:
        idx.rebuild()
        r = Retrieval(cfg=cfg, indexer=idx, embedder=idx.embedder)
        fixture_path = tmp_path / "fix.jsonl"
        fixture_path.write_text(
            json.dumps({
                "id": "lookup-missing",
                "query": "alpha",
                "expected_pages": ["nonexistent-page"],
                "min_citation_depth": 1,
                "mode": "lookup",
                "max_latency_ms": 5000,
            }) + "\n"
        )
        runner = EvalRunner(cfg=cfg, retrieval=r)
        report = runner.run(fixture_path)
        assert report.failed == 1
        assert "lookup-missing" in [f.id for f in report.failures]
    finally:
        idx.close()
