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
