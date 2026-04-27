"""Run eval against a warm cache without rebuilding.

P1 indexer.rebuild() re-encodes every page on every call regardless of
checksum match (encode runs before upsert's skip-check). Over 339 pages
that takes 10+ min on mxbai-embed-large. For diagnostic eval against an
already-warm vec_store, skip rebuild entirely — the cached vectors are
sufficient for retrieval.search() because the only model call is for the
query string itself.

Usage:
    uv run python scripts/eval_no_rebuild.py \\
        --vault E:/Projects/second-brain \\
        --fixtures E:/Projects/second-brain/_ops/eval/retrieval-fixtures.jsonl
"""

from __future__ import annotations

import argparse
from pathlib import Path

from vault_engine.config import EngineConfig
from vault_engine.embedder import SentenceTransformerEmbedder
from vault_engine.eval import EvalRunner
from vault_engine.indexer import Indexer
from vault_engine.retrieval import Retrieval


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", type=Path, required=True)
    ap.add_argument("--fixtures", type=Path, required=True)
    args = ap.parse_args()

    cfg = EngineConfig(vault_path=args.vault)
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    embedder = SentenceTransformerEmbedder(cfg.embedding_model)
    idx = Indexer(cfg=cfg, embedder=embedder)
    idx.open()
    try:
        # Skip idx.rebuild() — assume cache is warm from a prior `vault-engine reindex`.
        r = Retrieval(cfg=idx.cfg, indexer=idx, embedder=idx.embedder)
        runner = EvalRunner(cfg=idx.cfg, retrieval=r)
        report = runner.run(args.fixtures)
        print(f"total: {report.total}")
        print(f"passed: {report.passed}")
        print(f"failed: {report.failed}")
        for f in report.failures:
            print(f"  {f.id} -- {f.reason} ({f.latency_ms}ms)")
        return 0 if report.failed == 0 else 1
    finally:
        idx.close()


if __name__ == "__main__":
    raise SystemExit(main())
