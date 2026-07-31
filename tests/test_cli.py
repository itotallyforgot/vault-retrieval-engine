import json

from typer.testing import CliRunner

import vault_engine.cli as cli
from vault_engine.cli import app

runner = CliRunner()


def test_cli_help_lists_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("reindex", "status", "search", "expand", "source", "eval"):
        assert cmd in result.stdout


def test_cli_status_runs_against_sample_vault(sample_vault, tmp_path):
    result = runner.invoke(
        app,
        [
            "--vault",
            str(sample_vault),
            "--cache",
            str(tmp_path / "cache"),
            "--mock-embedder",
            "status",
        ],
    )
    assert result.exit_code == 0
    assert "vault" in result.stdout.lower()


def test_cli_eval_embedder_mock_does_not_initialize_default_model(
    monkeypatch, sample_vault, tmp_path
):
    def fail_default_model(*args, **kwargs):
        raise AssertionError("default embedder should not initialize for eval --embedder mock")

    monkeypatch.setattr(cli, "SentenceTransformerEmbedder", fail_default_model)
    fixture_path = tmp_path / "fixtures.jsonl"
    fixture_path.write_text(
        json.dumps(
            {
                "id": "lookup-alpha",
                "query": "alpha",
                "expected_pages": ["alpha"],
                "min_citation_depth": 0,
                "mode": "lookup",
                "max_latency_ms": 5000,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "--vault",
            str(sample_vault),
            "--cache",
            str(tmp_path / "cache"),
            "eval",
            "--fixtures",
            str(fixture_path),
            "--embedder",
            "mock",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "passed: 1" in result.stdout


def test_cli_add_routes_local_pdf_to_pdf_ingester(tmp_path):
    from pathlib import Path

    vault = tmp_path / "vault"
    vault.mkdir()
    fixture = Path(__file__).parent / "fixtures" / "two_page.pdf"
    result = runner.invoke(app, ["add", str(fixture), "--vault", str(vault)])
    assert result.exit_code == 0, result.stdout
    assert "two-page.md" in result.stdout
    assert (vault / "raw" / "_originals").is_dir()


def test_cli_add_reports_pdf_error_without_traceback(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    result = runner.invoke(app, ["add", str(tmp_path / "missing.pdf"), "--vault", str(vault)])
    assert result.exit_code == 1
    assert "Error:" in result.stderr


def test_cli_add_reports_relative_path_through_symlinked_vault(tmp_path):
    """A symlinked vault root (/tmp -> /private/tmp on macOS) used to crash the
    relative-path echo, because both adapters return a resolved path."""
    from pathlib import Path

    real = tmp_path / "real_vault"
    real.mkdir()
    link = tmp_path / "vault_link"
    link.symlink_to(real, target_is_directory=True)
    fixture = Path(__file__).parent / "fixtures" / "two_page.pdf"

    result = runner.invoke(app, ["add", str(fixture), "--vault", str(link)])
    assert result.exit_code == 0, result.stdout
    assert result.stdout.startswith("Wrote raw/")


def test_cli_add_reports_pdf_pages_skipped(tmp_path):
    """A PDF with image-only pages writes `## p. 1` then `## p. 3`; the gap has
    to be surfaced, same as `status` / `reindex` surface unreadable pages."""
    from tests.test_pdf_ingester import _pdf_file

    vault = tmp_path / "vault"
    vault.mkdir()
    doc = _pdf_file(tmp_path, [["Page one."], [], ["Page three."]], name="gappy.pdf")

    result = runner.invoke(app, ["add", str(doc), "--vault", str(vault)])
    assert result.exit_code == 0, result.stdout
    assert "pages skipped (unreadable): 1" in result.stdout
    assert "page 2: no extractable text layer" in result.stdout


def test_cli_honors_cache_dir_env_var(monkeypatch, sample_vault, tmp_path):
    """README documents VAULT_ENGINE_CACHE_DIR; `status` used to ignore it.

    The callback built an EngineConfig directly instead of calling
    load_config, so only `serve` and `mcp` ever read the env var.
    """
    env_cache = tmp_path / "env-cache"
    monkeypatch.setenv("VAULT_ENGINE_CACHE_DIR", str(env_cache))

    result = runner.invoke(app, ["--vault", str(sample_vault), "--mock-embedder", "status"])
    assert result.exit_code == 0, result.stdout
    # rich hard-wraps long paths at the console width, so match on the
    # unwrapped output rather than the raw stdout.
    assert str(env_cache.resolve()) in result.stdout.replace("\n", "")
    assert (env_cache / "embeddings.db").exists()


def test_cli_explicit_cache_flag_beats_env_var(monkeypatch, sample_vault, tmp_path):
    monkeypatch.setenv("VAULT_ENGINE_CACHE_DIR", str(tmp_path / "env-cache"))
    flag_cache = tmp_path / "flag-cache"

    result = runner.invoke(
        app,
        ["--vault", str(sample_vault), "--cache", str(flag_cache), "--mock-embedder", "status"],
    )
    assert result.exit_code == 0, result.stdout
    assert str(flag_cache.resolve()) in result.stdout.replace("\n", "")


def test_cli_reindex_reports_pruned_pages(sample_vault, tmp_path):
    cache = tmp_path / "cache"
    args = ["--vault", str(sample_vault), "--cache", str(cache), "--mock-embedder", "reindex"]

    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.stdout
    assert "pages pruned (gone from vault): 0" in first.stdout

    (sample_vault / "wiki" / "topics" / "beta.md").unlink()
    second = runner.invoke(app, args)
    assert second.exit_code == 0, second.stdout
    assert "pages pruned (gone from vault): 1" in second.stdout
