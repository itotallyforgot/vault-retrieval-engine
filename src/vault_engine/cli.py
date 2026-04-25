"""vault-engine CLI."""
from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from vault_engine.config import EngineConfig
from vault_engine.embedder import (
    Embedder,
    MockEmbedder,
    SentenceTransformerEmbedder,
)
from vault_engine.indexer import Indexer
from vault_engine.retrieval import Retrieval

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()


_state: dict[str, object] = {}


@app.callback()
def main(
    ctx: typer.Context,
    vault: Path = typer.Option(..., "--vault", help="Path to the vault root."),
    cache: Path | None = typer.Option(None, "--cache", help="Cache directory."),
    mock_embedder: bool = typer.Option(
        False, "--mock-embedder", help="Use deterministic mock embedder (tests only)."
    ),
) -> None:
    cfg = EngineConfig(
        vault_path=vault,
        cache_dir=cache or EngineConfig(vault_path=vault).cache_dir,
    )
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    embedder: Embedder
    if mock_embedder:
        embedder = MockEmbedder(dim=cfg.embedding_dim)
    else:
        embedder = SentenceTransformerEmbedder(cfg.embedding_model)
    _state["cfg"] = cfg
    _state["embedder"] = embedder


def _open_indexer() -> Indexer:
    cfg: EngineConfig = _state["cfg"]  # type: ignore[assignment]
    embedder: Embedder = _state["embedder"]  # type: ignore[assignment]
    idx = Indexer(cfg=cfg, embedder=embedder)
    idx.open()
    return idx


@app.command()
def status() -> None:
    """Print engine state: vault, cache, doc count."""
    cfg: EngineConfig = _state["cfg"]  # type: ignore[assignment]
    idx = _open_indexer()
    try:
        from vault_engine.vault_reader import iter_pages
        pages = iter_pages(cfg.vault_path)
        console.print(f"[bold]vault[/bold]: {cfg.vault_path}")
        console.print(f"[bold]cache[/bold]: {cfg.cache_dir}")
        console.print(f"[bold]pages[/bold]: {len(pages)}")
        console.print(f"[bold]embedding model[/bold]: {cfg.embedding_model}")
    finally:
        idx.close()


@app.command()
def reindex(
    force: bool = typer.Option(
        False, "--force", help="Wipe the vec store and rebuild (use after model swap)."
    ),
) -> None:
    """Full cold rebuild of vec store + graph.

    --force wipes the vec store first; required after switching embedding models
    (different model_name or dim) to avoid the EmbeddingModelMismatch error.
    """
    cfg: EngineConfig = _state["cfg"]  # type: ignore[assignment]
    embedder: Embedder = _state["embedder"]  # type: ignore[assignment]
    idx = Indexer(cfg=cfg, embedder=embedder)
    idx.open(force_reset=force)
    try:
        report = idx.rebuild()
        console.print(f"pages: {report.pages_processed}")
        console.print(f"chunks indexed: {report.chunks_indexed}")
        console.print(f"chunks changed: {report.chunks_changed}")
        console.print(f"chunks unchanged: {report.chunks_unchanged}")
    finally:
        idx.close()


@app.command()
def search(query: str = typer.Argument(...), k: int = typer.Option(10, "-k")) -> None:
    """Top-k semantic search against the vault."""
    idx = _open_indexer()
    try:
        idx.rebuild()  # ensures fresh state when run ad-hoc; cheap at vault scale
        r = Retrieval(cfg=idx.cfg, indexer=idx, embedder=idx.embedder)
        for hit in r.search(query, k=k):
            console.print(
                f"[cyan]{hit.page_slug}[/cyan] "
                f"#{hit.chunk_idx} dist={hit.distance:.4f}"
            )
            console.print(hit.content[:200].replace("\n", " "))
            console.print("---")
    finally:
        idx.close()


@app.command()
def expand(slug: str = typer.Argument(...)) -> None:
    """Print the full body of a page."""
    idx = _open_indexer()
    try:
        r = Retrieval(cfg=idx.cfg, indexer=idx, embedder=idx.embedder)
        body = r.expand(slug)
        if body is None:
            console.print(f"[red]not found[/red]: {slug}")
            raise typer.Exit(code=1)
        console.print(body)
    finally:
        idx.close()


@app.command()
def source(slug: str = typer.Argument(...)) -> None:
    """Print the contents of the raw source linked from a wiki page."""
    idx = _open_indexer()
    try:
        r = Retrieval(cfg=idx.cfg, indexer=idx, embedder=idx.embedder)
        text = r.source(slug)
        if text is None:
            console.print(f"[yellow]no raw source for[/yellow]: {slug}")
            raise typer.Exit(code=1)
        console.print(text)
    finally:
        idx.close()


@app.command(name="eval")
def eval_cmd(
    fixtures: Path = typer.Option(
        ..., "--fixtures", help="Path to retrieval-fixtures.jsonl."
    ),
) -> None:
    """Run the eval fixture suite against the engine."""
    from vault_engine.eval import EvalRunner

    idx = _open_indexer()
    try:
        idx.rebuild()
        r = Retrieval(cfg=idx.cfg, indexer=idx, embedder=idx.embedder)
        runner = EvalRunner(cfg=idx.cfg, retrieval=r)
        report = runner.run(fixtures)
        console.print(f"total: {report.total}")
        console.print(f"[green]passed[/green]: {report.passed}")
        console.print(f"[red]failed[/red]: {report.failed}")
        for f in report.failures:
            console.print(f"  [red]{f.id}[/red] — {f.reason} ({f.latency_ms}ms)")
        if report.failed > 0:
            raise typer.Exit(code=1)
    finally:
        idx.close()


if __name__ == "__main__":
    app()
