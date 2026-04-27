from typer.testing import CliRunner

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
