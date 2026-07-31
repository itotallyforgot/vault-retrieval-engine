import pytest
from typer.testing import CliRunner

from vault_engine.cli import app
from vault_engine.embedder import MockEmbedder

runner = CliRunner()


def _run_serve(sample_vault, monkeypatch, argv):
    """Invoke `serve` with the network/server bits stubbed, returning the embedder it built."""
    import uvicorn

    import vault_engine.http_server as http_server_mod
    import vault_engine.service as service_mod

    captured: dict[str, object] = {}

    class _FakeService:
        def __init__(self, cfg, embedder=None):
            captured["embedder"] = embedder

        def start(self):
            pass

    monkeypatch.setattr(service_mod, "Service", _FakeService)
    monkeypatch.setattr(http_server_mod, "build_app", lambda svc, **kw: object())
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: None)

    result = runner.invoke(app, ["serve", "--vault", str(sample_vault), *argv])
    assert result.exit_code == 0, result.output
    return captured["embedder"]


@pytest.mark.parametrize(
    ("argv", "expect_mock"),
    [(["--embedder", "mock"], True), ([], False)],
)
def test_cli_serve_embedder_selection(sample_vault, monkeypatch, argv, expect_mock):
    """`serve --embedder mock` must actually mock.

    The top-level --mock-embedder flag never reaches `serve`: the Typer callback
    returns early for it, so `serve` previously built a bare Service(cfg) and
    silently loaded the real SentenceTransformer while claiming to be mocked.
    """
    embedder = _run_serve(sample_vault, monkeypatch, argv)
    assert isinstance(embedder, MockEmbedder) is expect_mock


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
