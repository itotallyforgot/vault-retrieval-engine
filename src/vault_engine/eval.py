"""Eval rig.

Reads JSONL fixtures, runs each against the Router-shaped query surface, asserts
that expected pages appear, declared intent matches, citations are deep enough,
and latency is within budget.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vault_engine.citations import CitationAssembler
from vault_engine.config import EngineConfig
from vault_engine.retrieval import Retrieval, SearchHit
from vault_engine.router import QueryMode, Router


@dataclass
class FixtureRow:
    id: str
    query: str
    expected_pages: list[str]
    min_citation_depth: int
    mode: str
    max_latency_ms: int

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FixtureRow:
        return cls(
            id=str(raw["id"]),
            query=str(raw["query"]),
            expected_pages=[str(p) for p in raw["expected_pages"]],
            min_citation_depth=int(raw["min_citation_depth"]),
            mode=str(raw["mode"]),
            max_latency_ms=int(raw["max_latency_ms"]),
        )


@dataclass
class FailureRecord:
    id: str
    reason: str
    latency_ms: int


@dataclass
class EvalReport:
    total: int = 0
    passed: int = 0
    failed: int = 0
    failures: list[FailureRecord] = field(default_factory=list)


class EvalRunner:
    def __init__(self, cfg: EngineConfig, retrieval: Retrieval) -> None:
        self.cfg = cfg
        self.retrieval = retrieval
        self.router = Router(
            cfg=cfg,
            embedder=retrieval.embedder,
            vec_store=retrieval.indexer.vec,
            graph_store=retrieval.indexer.graph,
        )
        self.citations = CitationAssembler(cfg=cfg, retrieval=retrieval)

    def run(self, fixture_path: Path) -> EvalReport:
        report = EvalReport()
        for line in fixture_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = FixtureRow.from_dict(json.loads(line))
            report.total += 1
            ok, reason, latency = self._run_row(row)
            if ok:
                report.passed += 1
            else:
                report.failed += 1
                report.failures.append(FailureRecord(id=row.id, reason=reason, latency_ms=latency))
        return report

    def _run_row(self, row: FixtureRow) -> tuple[bool, str, int]:
        start = time.monotonic()
        try:
            result = self.router.dispatch(row.query, top_k=max(20, len(row.expected_pages) * 5))
        except Exception as exc:
            return False, f"exception: {exc!r}", int((time.monotonic() - start) * 1000)
        latency_ms = int((time.monotonic() - start) * 1000)
        if latency_ms > row.max_latency_ms:
            return False, f"latency exceeded: {latency_ms}ms > {row.max_latency_ms}ms", latency_ms

        intent = result.get("intent")
        if intent is None:
            return False, "missing intent in router result", latency_ms
        actual_mode = intent.value if isinstance(intent, QueryMode) else str(intent)
        if row.mode and actual_mode != row.mode:
            return False, f"wrong intent: expected {row.mode}, got {actual_mode}", latency_ms

        fused_hits = result.get("fused_hits", [])
        slugs = {h.doc_id for h in fused_hits}
        missing = [p for p in row.expected_pages if p not in slugs]
        if missing:
            return False, f"missing expected pages: {missing}", latency_ms

        expected_slugs = set(row.expected_pages)
        citation_hits = [
            SearchHit(
                page_slug=h.doc_id,
                chunk_idx=0,
                content=self.retrieval.expand(h.doc_id) or "",
                distance=h.rrf_score,
            )
            for h in fused_hits
            if h.doc_id in expected_slugs
        ]
        citations = self.citations.assemble(citation_hits)
        citation_depth = sum(1 for citation in citations if citation.raw_path is not None)
        if citation_depth < row.min_citation_depth:
            return (
                False,
                "insufficient citation depth: "
                f"expected >= {row.min_citation_depth}, got {citation_depth}",
                latency_ms,
            )
        return True, "ok", latency_ms
