"""Shared pytest fixtures."""

from pathlib import Path

import pytest

from vault_engine.embedder import MockEmbedder
from vault_engine.stores.graph_store import GraphStore


@pytest.fixture
def sample_vault(tmp_path: Path) -> Path:
    """Build a tiny vault on disk for tests. Each test gets a fresh copy."""
    vault = tmp_path / "vault"
    (vault / "wiki" / "topics").mkdir(parents=True)
    (vault / "wiki" / "sources").mkdir(parents=True)
    (vault / "raw").mkdir(parents=True)

    (vault / "wiki" / "topics" / "alpha.md").write_text(
        "---\n"
        "title: Alpha\n"
        'aliases: ["alpha-thing"]\n'
        "tags: [topic]\n"
        'sources: ["[[2026-01-01-alpha-source]]"]\n'
        "last_updated: 2026-01-01\n"
        "---\n"
        "\n"
        "# Alpha\n"
        "\n"
        "Alpha references [[beta]] and is described by alpha-thing.\n"
        "\n"
        "## Details\n"
        "\n"
        "More detail about alpha.\n",
        encoding="utf-8",
    )
    (vault / "wiki" / "topics" / "beta.md").write_text(
        "---\n"
        "title: Beta\n"
        "aliases: []\n"
        "tags: [topic]\n"
        "sources: []\n"
        "last_updated: 2026-01-02\n"
        "---\n"
        "\n"
        "# Beta\n"
        "\n"
        "Beta only.\n",
        encoding="utf-8",
    )
    (vault / "wiki" / "sources" / "2026-01-01-alpha-source.md").write_text(
        "---\n"
        "title: Alpha Source\n"
        "tags: [source]\n"
        "sources: []\n"
        "last_updated: 2026-01-01\n"
        "raw_path: raw/2026-01-01-alpha-raw.md\n"
        "---\n"
        "\n"
        "# Alpha Source\n"
        "\n"
        "Source page for [[alpha]].\n",
        encoding="utf-8",
    )
    (vault / "raw" / "2026-01-01-alpha-raw.md").write_text(
        "---\ntitle: Alpha Raw\ningested: true\n---\n\nRaw text body.\n",
        encoding="utf-8",
    )
    return vault


@pytest.fixture
def mock_embedder() -> MockEmbedder:
    return MockEmbedder(dim=64)


@pytest.fixture
def empty_graph_store() -> GraphStore:
    return GraphStore()
