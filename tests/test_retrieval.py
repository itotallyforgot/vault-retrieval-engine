import hashlib
import socket
from pathlib import Path

import frontmatter
import httpx

from vault_engine.config import EngineConfig
from vault_engine.embedder import MockEmbedder
from vault_engine.indexer import Indexer
from vault_engine.pdf_ingester import add_pdf
from vault_engine.retrieval import Retrieval
from vault_engine.url_ingester import add_url

PDF_FIXTURE = Path(__file__).parent / "fixtures" / "two_page.pdf"


def _open_indexed(sample_vault: Path, tmp_path: Path) -> tuple[Indexer, Retrieval, EngineConfig]:
    cfg = EngineConfig(vault_path=sample_vault, cache_dir=tmp_path / "cache")
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    idx = Indexer(cfg=cfg, embedder=MockEmbedder(dim=cfg.embedding_dim))
    idx.open()
    idx.rebuild()
    r = Retrieval(cfg=cfg, indexer=idx, embedder=idx.embedder)
    return idx, r, cfg


def test_search_returns_chunks_for_matching_query(sample_vault: Path, tmp_path: Path):
    idx, r, _ = _open_indexed(sample_vault, tmp_path)
    try:
        hits = r.search("alpha", k=5)
        assert any(h.page_slug == "alpha" for h in hits)
        assert all(h.distance is not None for h in hits)
    finally:
        idx.close()


def test_expand_returns_full_page_body(sample_vault: Path, tmp_path: Path):
    idx, r, _ = _open_indexed(sample_vault, tmp_path)
    try:
        body = r.expand("alpha")
        assert body is not None
        assert "Alpha" in body
        assert "Details" in body
    finally:
        idx.close()


def test_source_returns_raw_file_when_present(sample_vault: Path, tmp_path: Path):
    idx, r, _ = _open_indexed(sample_vault, tmp_path)
    try:
        # The source page declares raw_path in frontmatter.
        text = r.source("2026-01-01-alpha-source")
        assert text is not None
        assert "Raw text body." in text
    finally:
        idx.close()


def test_source_returns_none_when_no_raw_path(sample_vault: Path, tmp_path: Path):
    idx, r, _ = _open_indexed(sample_vault, tmp_path)
    try:
        assert r.source("alpha") is None
    finally:
        idx.close()


def test_source_refuses_raw_path_escaping_vault(sample_vault: Path, tmp_path: Path):
    """E6: a crafted raw_path that traverses outside the vault root must NOT be
    read. raw_path is attacker-influenced frontmatter, so source() confines the
    resolved target to the vault (mirroring the write-path containment guard).
    """
    # Plant a secret OUTSIDE the vault, then a source page that tries to read it
    # via a traversal raw_path.
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET — must not leak", encoding="utf-8")
    # Vault root is tmp_path/vault, so a single ".." climbs to tmp_path where the
    # secret lives — outside the vault. That's the escape the guard must block.
    rel_to_secret = Path("..") / "secret.txt"
    (sample_vault / "wiki" / "sources" / "evil-source.md").write_text(
        "---\n"
        "title: Evil Source\n"
        "tags: [source]\n"
        f"raw_path: {rel_to_secret.as_posix()}\n"
        "---\n\n# Evil Source\n\nTries to escape the vault.\n",
        encoding="utf-8",
    )
    idx, r, _ = _open_indexed(sample_vault, tmp_path)
    try:
        # Sanity: the traversal really does point at the secret on disk.
        assert (sample_vault / rel_to_secret).resolve() == secret.resolve()
        # …but source() must refuse to read outside the vault root.
        assert r.source("evil-source") is None
    finally:
        idx.close()


def test_consolidation_candidates_flags_orphan_raw(sample_vault: Path, tmp_path: Path):
    idx, r, _ = _open_indexed(sample_vault, tmp_path)
    try:
        candidates = r.consolidation_candidates()
        assert "2026-01-01-alpha-raw" in candidates.orphan_pages
    finally:
        idx.close()


def test_graph_walk_from_seeds_returns_paths(sample_vault: Path, tmp_path: Path):
    idx, r, _ = _open_indexed(sample_vault, tmp_path)
    try:
        paths = r.graph_walk(seeds=["2026-01-01-alpha-source"], depth=2)
        slug_paths = [list(p) for p in paths]
        assert any(p == ["2026-01-01-alpha-source", "alpha", "beta"] for p in slug_paths)
    finally:
        idx.close()


def test_multi_hop_returns_paths_touching_multiple_seeds(sample_vault: Path, tmp_path: Path):
    idx, r, _ = _open_indexed(sample_vault, tmp_path)
    try:
        result = r.multi_hop(seed_query="alpha and beta", min_seeds_touched=2)
        # Should find at least one path through alpha->beta region.
        assert any("alpha" in p and "beta" in p for p in result.paths)
    finally:
        idx.close()


def test_source_reports_retained_original_for_pdf_page(sample_vault: Path, tmp_path: Path):
    """A page written by `add_pdf` carries `source_artifact`, not `raw_path`.

    `source` must report the retained original (path, media type, recorded
    hash, integrity) instead of claiming there is no raw source. The bytes are
    NOT dumped: the artifact is a PDF and would not decode as UTF-8.
    """
    page = add_pdf(sample_vault, PDF_FIXTURE, title_override="Two Page Paper")
    post = frontmatter.loads(page.read_text(encoding="utf-8"))
    idx, r, _ = _open_indexed(sample_vault, tmp_path)
    try:
        out = r.source(page.stem)
        assert out is not None, "source() must not report 'no raw source'"
        assert str(post["source_artifact"]) in out
        assert str(post["source_sha256"]) in out
        assert str(post["source_media_type"]) in out
        assert "integrity: ok" in out
    finally:
        idx.close()


def test_source_reports_retained_original_for_url_page(
    sample_vault: Path, tmp_path: Path, monkeypatch
):
    """The other half of the ingestion surface (ADR 0006 revisit trigger): a
    page written by `add_url` must report its retained original exactly as a
    PDF-ingested one does. No network: DNS is stubbed public and the GET is
    canned, so the SSRF guard passes without a socket."""
    raw = b"<html><head><title>Wire Article</title></head><body><article>"
    raw += b"<h1>Wire Article</h1><p>Enough prose for the extractor to keep.</p>"
    raw += b"</article></body></html>"

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))
        ],
    )
    monkeypatch.setattr(
        httpx.Client,
        "get",
        lambda self, url, **kw: httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=raw,
            request=httpx.Request("GET", url),
        ),
    )

    page = add_url(sample_vault, "https://example.com/wire-article")
    post = frontmatter.loads(page.read_text(encoding="utf-8"))
    idx, r, _ = _open_indexed(sample_vault, tmp_path)
    try:
        out = r.source(page.stem)
        assert out is not None, "source() must not report 'no raw source' for a URL page"
        assert str(post["source_artifact"]) in out
        assert "text/html" in out
        assert hashlib.sha256(raw).hexdigest() in out
        assert "integrity: ok" in out
    finally:
        idx.close()


def test_source_reports_tampered_retained_original(sample_vault: Path, tmp_path: Path):
    """The recorded `source_sha256` is only worth something if it is checked."""
    page = add_pdf(sample_vault, PDF_FIXTURE, title_override="Two Page Paper")
    post = frontmatter.loads(page.read_text(encoding="utf-8"))
    original = sample_vault / str(post["source_artifact"])
    original.write_bytes(original.read_bytes() + b"tampered")
    idx, r, _ = _open_indexed(sample_vault, tmp_path)
    try:
        out = r.source(page.stem)
        assert out is not None
        assert "MISMATCH" in out
    finally:
        idx.close()


def test_source_refuses_source_artifact_escaping_vault(sample_vault: Path, tmp_path: Path):
    """`source_artifact` is the same attacker-influenced frontmatter as
    `raw_path` and gets the same containment guard.
    """
    secret = tmp_path / "secret.pdf"
    secret.write_bytes(b"TOP SECRET")
    (sample_vault / "raw" / "evil-artifact.md").write_text(
        "---\n"
        "title: Evil Artifact\n"
        "tags: [source]\n"
        "source_artifact: ../secret.pdf\n"
        "source_sha256: deadbeef\n"
        "source_media_type: application/pdf\n"
        "---\n\n# Evil Artifact\n\nTries to escape the vault.\n",
        encoding="utf-8",
    )
    idx, r, _ = _open_indexed(sample_vault, tmp_path)
    try:
        assert (sample_vault / ".." / "secret.pdf").resolve() == secret.resolve()
        assert r.source("evil-artifact") is None
    finally:
        idx.close()


def test_source_reports_missing_and_unrecorded_originals(sample_vault: Path, tmp_path: Path):
    """The other two integrity verdicts: the retained original was deleted, and
    the page never recorded a hash to check it against.
    """
    (sample_vault / "raw" / "gone-artifact.md").write_text(
        "---\n"
        "title: Gone\n"
        "source_artifact: raw/_originals/gone.pdf\n"
        "source_sha256: deadbeef\n"
        "source_media_type: application/pdf\n"
        "---\n\n# Gone\n\nOriginal was deleted.\n",
        encoding="utf-8",
    )
    (sample_vault / "raw" / "_originals").mkdir(parents=True)
    (sample_vault / "raw" / "_originals" / "unhashed.pdf").write_bytes(b"%PDF-1.4\n")
    (sample_vault / "raw" / "unhashed-artifact.md").write_text(
        "---\n"
        "title: Unhashed\n"
        "source_artifact: raw/_originals/unhashed.pdf\n"
        "---\n\n# Unhashed\n\nNo hash was recorded.\n",
        encoding="utf-8",
    )
    idx, r, _ = _open_indexed(sample_vault, tmp_path)
    try:
        gone = r.source("gone-artifact")
        assert gone is not None and "MISSING" in gone
        unhashed = r.source("unhashed-artifact")
        assert unhashed is not None and "unverifiable" in unhashed
        assert "unknown" in unhashed, "media type is absent, so say so"
    finally:
        idx.close()
