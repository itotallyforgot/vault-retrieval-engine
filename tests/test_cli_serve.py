from typer.testing import CliRunner

from vault_engine.cli import app


runner = CliRunner()


def test_cli_help_lists_p2_subcommands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    out = result.stdout
    # P1 commands stay
    assert "index" in out  # matches "reindex"
    assert "search" in out  # P1 uses "search" (not "query")
    # P2 additions
    assert "serve" in out
    assert "mcp" in out
    assert "hook" in out


def test_cli_hook_install_dry_run(tmp_path):
    result = runner.invoke(app, ["hook", "install", "--vault", str(tmp_path), "--dry-run"])
    assert result.exit_code == 0
    assert "Would write" in result.stdout
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_cli_hook_install_writes_files(tmp_path):
    result = runner.invoke(app, ["hook", "install", "--vault", str(tmp_path)])
    assert result.exit_code == 0
    assert (tmp_path / ".claude" / "settings.json").exists()
    hooks_dir = tmp_path / ".claude" / "hooks"
    assert hooks_dir.exists()
    assert any(p.name.startswith("vault_query_hint") for p in hooks_dir.iterdir())


def test_cli_hook_install_idempotent(tmp_path):
    runner.invoke(app, ["hook", "install", "--vault", str(tmp_path)])
    result2 = runner.invoke(app, ["hook", "install", "--vault", str(tmp_path)])
    assert result2.exit_code == 0
    settings_text = (tmp_path / ".claude" / "settings.json").read_text()
    assert settings_text.count("vault_query_hint") <= 2
