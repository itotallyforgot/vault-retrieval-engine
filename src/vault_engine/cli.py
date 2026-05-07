"""vault-engine CLI."""

import json
import logging
import platform
import shutil
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


def _configure_logging(verbose: bool, quiet: bool) -> None:
    """Wire up Python logging based on CLI verbosity flags.

    Without this, every ``log.info`` and ``log.debug`` call in the engine
    is silently dropped because no handler is configured.
    """
    if verbose and quiet:
        # Mutually exclusive; quiet wins per principle of least surprise.
        verbose = False
    level = logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@app.callback()
def main(
    ctx: typer.Context,
    vault: Path | None = typer.Option(None, "--vault", help="Path to the vault root."),
    cache: Path | None = typer.Option(None, "--cache", help="Cache directory."),
    mock_embedder: bool = typer.Option(
        False, "--mock-embedder", help="Use deterministic mock embedder (tests only)."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable DEBUG-level logs."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress everything below WARNING."),
) -> None:
    _configure_logging(verbose=verbose, quiet=quiet)
    # Sub-commands that take their own --vault (or none at all) skip setup.
    # `hook` has no vault state. `serve`/`mcp` construct their own EngineConfig
    # from their own --vault flag so they can run as long-lived processes.
    # `eval` manages its own embedder selection, but still needs vault setup.
    if ctx.invoked_subcommand in ("hook", "serve", "mcp", "add"):
        return
    if vault is None:
        typer.echo("Error: --vault is required for this command.", err=True)
        raise typer.Exit(2)
    cfg = EngineConfig(
        vault_path=vault,
        cache_dir=cache or EngineConfig(vault_path=vault).cache_dir,
    )
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    _state["cfg"] = cfg
    if ctx.invoked_subcommand == "eval":
        return

    embedder: Embedder
    if mock_embedder:
        embedder = MockEmbedder(dim=cfg.embedding_dim)
    else:
        embedder = SentenceTransformerEmbedder(cfg.embedding_model)
    _state["embedder"] = embedder


def _open_indexer() -> Indexer:
    cfg: EngineConfig = _state["cfg"]  # type: ignore[assignment]
    embedder: Embedder = _state["embedder"]  # type: ignore[assignment]
    idx = Indexer(cfg=cfg, embedder=embedder)
    idx.open()
    return idx


# ---------------------------------------------------------------------------
# P1 commands (unchanged)
# ---------------------------------------------------------------------------


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
            console.print(f"[cyan]{hit.page_slug}[/cyan] #{hit.chunk_idx} dist={hit.distance:.4f}")
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
    fixtures: Path = typer.Option(..., "--fixtures", help="Path to retrieval-fixtures.jsonl."),
    embedder: str = typer.Option(
        "default", "--embedder", help="Embedder to use: 'default' (SentenceTransformer) or 'mock'."
    ),
    threshold: float = typer.Option(
        None,
        "--threshold",
        help="Pass-rate threshold (0.0-1.0). Exit code 1 if passed/total < threshold.",
    ),
) -> None:
    """Run the eval fixture suite against the engine."""
    from vault_engine.eval import EvalRunner

    cfg: EngineConfig = _state["cfg"]  # type: ignore[assignment]

    # Override embedder if specified
    active_embedder: Embedder
    if embedder == "mock":
        active_embedder = MockEmbedder(dim=cfg.embedding_dim)
    else:
        active_embedder = SentenceTransformerEmbedder(cfg.embedding_model)

    idx = Indexer(cfg=cfg, embedder=active_embedder)
    idx.open()
    try:
        idx.rebuild()
        r = Retrieval(cfg=idx.cfg, indexer=idx, embedder=idx.embedder)
        runner = EvalRunner(cfg=idx.cfg, retrieval=r)
        report = runner.run(fixtures)
        console.print(f"total: {report.total}")
        console.print(f"[green]passed[/green]: {report.passed}")
        console.print(f"[red]failed[/red]: {report.failed}")
        console.print("by mode:")
        for mode, bucket in sorted(report.by_mode.items()):
            console.print(
                f"  {mode}: {bucket.passed}/{bucket.total} "
                f"avg={bucket.avg_latency_ms}ms max={bucket.max_latency_ms}ms"
            )
        console.print("by track:")
        for track, bucket in sorted(report.by_track.items()):
            console.print(
                f"  {track}: {bucket.passed}/{bucket.total} "
                f"avg={bucket.avg_latency_ms}ms max={bucket.max_latency_ms}ms"
            )
        for f in report.failures:
            console.print(f"  [red]{f.id}[/red] — {f.reason} ({f.latency_ms}ms)")

        # Check pass-rate threshold if specified
        if threshold is not None and report.total > 0:
            pass_rate = report.passed / report.total
            if pass_rate < threshold:
                console.print(
                    f"[red]FAIL: pass-rate {pass_rate:.2%} < threshold {threshold:.2%}[/red]"
                )
                raise typer.Exit(code=1)

        # Fail if any tests failed (unless threshold check passed above)
        if report.failed > 0:
            raise typer.Exit(code=1)
    finally:
        idx.close()


# ---------------------------------------------------------------------------
# P2 commands: serve, mcp
# ---------------------------------------------------------------------------


@app.command()
def serve(
    vault: Path = typer.Option(..., "--vault", help="Path to vault root."),
    cache: Path | None = typer.Option(None, "--cache", help="Cache directory."),
) -> None:
    """Run the HTTP server long-lived (P2)."""
    import uvicorn

    from vault_engine.config import load_config
    from vault_engine.http_server import build_app
    from vault_engine.service import Service

    cfg = load_config(vault, cache)
    svc = Service(cfg)
    svc.start()
    application = build_app(svc, secret=cfg.http_token)
    uvicorn.run(application, host=cfg.http_bind_addr, port=cfg.http_port, log_level="info")


@app.command()
def mcp(
    vault: Path = typer.Option(..., "--vault", help="Path to vault root."),
    cache: Path | None = typer.Option(None, "--cache", help="Cache directory."),
) -> None:
    """Run the MCP stdio server."""
    from vault_engine.config import load_config
    from vault_engine.mcp_server import serve_stdio
    from vault_engine.service import Service

    cfg = load_config(vault, cache)
    svc = Service(cfg)
    svc.start()
    serve_stdio(svc)


@app.command()
def add(
    url: str = typer.Argument(..., help="URL of the article to fetch."),
    vault: Path = typer.Option(..., "--vault", help="Path to vault root."),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Replace an existing raw/<slug>.md instead of failing.",
    ),
    title: str | None = typer.Option(
        None,
        "--title",
        help="Override the auto-detected title (slug is derived from this).",
    ),
) -> None:
    """One-shot fetch + extract a URL into <vault>/raw/<slug>.md (P3 #5).

    The file lands with `ingested: false` frontmatter, ready for
    `/vault ingest <path>` (or batch ingest) to merge into the wiki. The
    engine deliberately does NOT touch wiki/ — splitting scrape from
    synthesis keeps engine work deterministic and lets the user review
    every fetch before it shapes topic pages.
    """
    from vault_engine.url_ingester import add_url

    if not vault.exists():
        typer.echo(f"Error: vault path does not exist: {vault}", err=True)
        raise typer.Exit(2)

    try:
        path = add_url(
            vault_path=vault,
            url=url,
            overwrite=overwrite,
            title_override=title,
        )
    except FileExistsError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from e

    rel = path.relative_to(vault) if path.is_absolute() else path
    typer.echo(f"Wrote {rel}")
    typer.echo("Next: run `/vault ingest <path>` (or batch ingest) to merge into the wiki.")


# ---------------------------------------------------------------------------
# P2 hook subcommand group
# ---------------------------------------------------------------------------

hook_app = typer.Typer(help="Vault hook management")
app.add_typer(hook_app, name="hook")


@hook_app.command("install")
def hook_install(
    vault: Path = typer.Option(..., help="Path to vault root"),
    dry_run: bool = typer.Option(False, help="Print intended writes, do nothing"),
) -> None:
    """Install PreToolUse hook into <vault>/.claude/settings.json (E1)."""
    repo_root = Path(__file__).resolve().parents[2]
    src_assets = repo_root / "_vault_assets"
    settings_path = vault / ".claude" / "settings.json"
    hooks_dir = vault / ".claude" / "hooks"

    is_windows = platform.system().lower().startswith("win")
    script_name = "vault_query_hint.ps1" if is_windows else "vault_query_hint.sh"
    src_script = src_assets / ("claude_query_hint.ps1" if is_windows else "claude_query_hint.sh")
    dst_script = hooks_dir / script_name

    settings_addition = json.loads((src_assets / "claude_settings_hook.json").read_text())
    for entry in settings_addition.get("hooks", {}).get("PreToolUse", []):
        entry["command"] = str(dst_script)

    if dry_run:
        typer.echo(f"Would write {settings_path}")
        typer.echo(f"Would write {dst_script}")
        return

    hooks_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_script, dst_script)
    if not is_windows:
        dst_script.chmod(0o755)

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(settings_path.read_text()) if settings_path.exists() else {}

    pre_tool = existing.setdefault("hooks", {}).setdefault("PreToolUse", [])
    new_entries = settings_addition["hooks"]["PreToolUse"]
    for new_entry in new_entries:
        if not any(
            e.get("matcher") == new_entry["matcher"] and e.get("command") == new_entry["command"]
            for e in pre_tool
        ):
            pre_tool.append(new_entry)

    settings_path.write_text(json.dumps(existing, indent=2))
    typer.echo(f"Installed PreToolUse hook -> {settings_path}")
    typer.echo(f"Hook script -> {dst_script}")


if __name__ == "__main__":
    app()
