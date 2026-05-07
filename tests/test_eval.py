import json
from pathlib import Path

from vault_engine.config import EngineConfig
from vault_engine.embedder import MockEmbedder
from vault_engine.eval import EvalRunner, FixtureRow
from vault_engine.indexer import Indexer
from vault_engine.reranker import FusedHit
from vault_engine.retrieval import Retrieval


def test_fixture_row_parse_strict():
    raw = {
        "id": "x",
        "query": "alpha",
        "expected_pages": ["alpha"],
        "min_citation_depth": 1,
        "mode": "lookup",
        "max_latency_ms": 1000,
        "forbidden_pages": ["orphan"],
        "expected_citations": ["2026-01-01-alpha-source"],
        "track": "verification",
        "top_k": 5,
    }
    row = FixtureRow.from_dict(raw)
    assert row.id == "x"
    assert row.expected_pages == ["alpha"]
    assert row.forbidden_pages == ["orphan"]
    assert row.expected_citations == ["2026-01-01-alpha-source"]
    assert row.track == "verification"
    assert row.top_k == 5


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
            json.dumps(
                {
                    "id": "lookup-alpha",
                    "query": "alpha",
                    "expected_pages": ["alpha"],
                    "min_citation_depth": 1,
                    "mode": "lookup",
                    "max_latency_ms": 5000,
                }
            )
            + "\n"
        )
        runner = EvalRunner(cfg=cfg, retrieval=r)
        report = runner.run(fixture_path)
        assert report.total == 1
        assert report.passed == 1
        assert report.failed == 0
        assert report.by_mode["lookup"].passed == 1
        assert report.by_track["lookup"].passed == 1
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
            json.dumps(
                {
                    "id": "lookup-missing",
                    "query": "alpha",
                    "expected_pages": ["nonexistent-page"],
                    "min_citation_depth": 1,
                    "mode": "lookup",
                    "max_latency_ms": 5000,
                }
            )
            + "\n"
        )
        runner = EvalRunner(cfg=cfg, retrieval=r)
        report = runner.run(fixture_path)
        assert report.failed == 1
        assert "lookup-missing" in [f.id for f in report.failures]
    finally:
        idx.close()


def test_eval_runner_fails_when_declared_mode_regresses(sample_vault: Path, tmp_path: Path):
    cfg = EngineConfig(vault_path=sample_vault, cache_dir=tmp_path / "cache")
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    idx = Indexer(cfg=cfg, embedder=MockEmbedder(dim=cfg.embedding_dim))
    idx.open()
    try:
        idx.rebuild()
        r = Retrieval(cfg=cfg, indexer=idx, embedder=idx.embedder)
        fixture_path = tmp_path / "fix.jsonl"
        fixture_path.write_text(
            json.dumps(
                {
                    "id": "intent-regression",
                    "query": "alpha",
                    "expected_pages": ["alpha"],
                    "min_citation_depth": 1,
                    "mode": "semantic",
                    "max_latency_ms": 5000,
                }
            )
            + "\n"
        )
        runner = EvalRunner(cfg=cfg, retrieval=r)
        report = runner.run(fixture_path)
        assert report.failed == 1
        assert report.failures[0].reason.startswith("wrong intent:")
    finally:
        idx.close()


def test_eval_runner_fails_when_citation_depth_is_insufficient(sample_vault: Path, tmp_path: Path):
    cfg = EngineConfig(vault_path=sample_vault, cache_dir=tmp_path / "cache")
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    idx = Indexer(cfg=cfg, embedder=MockEmbedder(dim=cfg.embedding_dim))
    idx.open()
    try:
        idx.rebuild()
        r = Retrieval(cfg=cfg, indexer=idx, embedder=idx.embedder)
        fixture_path = tmp_path / "fix.jsonl"
        fixture_path.write_text(
            json.dumps(
                {
                    "id": "citation-depth-regression",
                    "query": "alpha",
                    "expected_pages": ["alpha"],
                    "min_citation_depth": 3,
                    "mode": "lookup",
                    "max_latency_ms": 5000,
                }
            )
            + "\n"
        )
        runner = EvalRunner(cfg=cfg, retrieval=r)
        report = runner.run(fixture_path)
        assert report.failed == 1
        assert report.failures[0].reason.startswith("insufficient citation depth:")
    finally:
        idx.close()


def test_eval_runner_fails_when_expected_citation_is_missing(sample_vault: Path, tmp_path: Path):
    cfg = EngineConfig(vault_path=sample_vault, cache_dir=tmp_path / "cache")
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    idx = Indexer(cfg=cfg, embedder=MockEmbedder(dim=cfg.embedding_dim))
    idx.open()
    try:
        idx.rebuild()
        r = Retrieval(cfg=cfg, indexer=idx, embedder=idx.embedder)
        fixture_path = tmp_path / "fix.jsonl"
        fixture_path.write_text(
            json.dumps(
                {
                    "id": "citation-correctness",
                    "query": "alpha",
                    "expected_pages": ["alpha"],
                    "expected_citations": ["missing-source"],
                    "min_citation_depth": 0,
                    "mode": "lookup",
                    "max_latency_ms": 5000,
                }
            )
            + "\n"
        )
        runner = EvalRunner(cfg=cfg, retrieval=r)
        report = runner.run(fixture_path)
        assert report.failed == 1
        assert report.failures[0].reason.startswith("missing expected citations:")
    finally:
        idx.close()


def test_eval_runner_fails_when_forbidden_page_is_retrieved(
    monkeypatch, sample_vault: Path, tmp_path: Path
):
    cfg = EngineConfig(vault_path=sample_vault, cache_dir=tmp_path / "cache")
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    idx = Indexer(cfg=cfg, embedder=MockEmbedder(dim=cfg.embedding_dim))
    idx.open()
    try:
        idx.rebuild()
        r = Retrieval(cfg=cfg, indexer=idx, embedder=idx.embedder)
        fixture_path = tmp_path / "fix.jsonl"
        fixture_path.write_text(
            json.dumps(
                {
                    "id": "negative-retrieval",
                    "query": "alpha",
                    "expected_pages": ["alpha"],
                    "forbidden_pages": ["orphan"],
                    "min_citation_depth": 0,
                    "mode": "lookup",
                    "top_k": 2,
                    "max_latency_ms": 5000,
                }
            )
            + "\n"
        )
        runner = EvalRunner(cfg=cfg, retrieval=r)
        monkeypatch.setattr(
            runner.router,
            "dispatch",
            lambda query, top_k: {
                "intent": "lookup",
                "fused_hits": [
                    FusedHit(doc_id="alpha", rrf_score=1.0),
                    FusedHit(doc_id="orphan", rrf_score=0.5),
                ],
            },
        )
        report = runner.run(fixture_path)
        assert report.failed == 1
        assert report.failures[0].reason == "forbidden pages retrieved: ['orphan']"
    finally:
        idx.close()


def test_eval_report_groups_latency_by_mode_and_track(sample_vault: Path, tmp_path: Path):
    cfg = EngineConfig(vault_path=sample_vault, cache_dir=tmp_path / "cache")
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    idx = Indexer(cfg=cfg, embedder=MockEmbedder(dim=cfg.embedding_dim))
    idx.open()
    try:
        idx.rebuild()
        r = Retrieval(cfg=cfg, indexer=idx, embedder=idx.embedder)
        fixture_path = tmp_path / "fix.jsonl"
        rows = [
            {
                "id": "lookup-alpha",
                "query": "alpha",
                "expected_pages": ["alpha"],
                "min_citation_depth": 0,
                "mode": "lookup",
                "max_latency_ms": 5000,
            },
            {
                "id": "source-alpha",
                "query": "alpha source provenance",
                "expected_pages": ["alpha", "2026-01-01-alpha-source"],
                "expected_citations": ["2026-01-01-alpha-source"],
                "min_citation_depth": 1,
                "mode": "hybrid",
                "track": "verification",
                "max_latency_ms": 5000,
            },
        ]
        fixture_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        runner = EvalRunner(cfg=cfg, retrieval=r)
        report = runner.run(fixture_path)
        assert report.failed == 0
        assert report.by_mode["lookup"].passed == 1
        assert report.by_mode["hybrid"].passed == 1
        assert report.by_track["verification"].passed == 1
    finally:
        idx.close()


def test_eval_runner_measures_citation_depth_on_expected_pages_only(
    monkeypatch, sample_vault: Path, tmp_path: Path
):
    cfg = EngineConfig(vault_path=sample_vault, cache_dir=tmp_path / "cache")
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    idx = Indexer(cfg=cfg, embedder=MockEmbedder(dim=cfg.embedding_dim))
    idx.open()
    try:
        idx.rebuild()
        r = Retrieval(cfg=cfg, indexer=idx, embedder=idx.embedder)
        fixture_path = tmp_path / "fix.jsonl"
        fixture_path.write_text(
            json.dumps(
                {
                    "id": "orphan-citation-depth",
                    "query": "orphan",
                    "expected_pages": ["orphan"],
                    "min_citation_depth": 1,
                    "mode": "semantic",
                    "max_latency_ms": 5000,
                }
            )
            + "\n"
        )
        runner = EvalRunner(cfg=cfg, retrieval=r)
        monkeypatch.setattr(
            runner.router,
            "dispatch",
            lambda query, top_k: {
                "intent": "semantic",
                "fused_hits": [
                    FusedHit(doc_id="orphan", rrf_score=1.0),
                    FusedHit(doc_id="alpha", rrf_score=0.5),
                ],
            },
        )
        report = runner.run(fixture_path)
        assert report.failed == 1
        assert report.failures[0].reason.startswith("insufficient citation depth:")
    finally:
        idx.close()
