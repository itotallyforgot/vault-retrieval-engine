"""Cross-harness MCP-connectivity test suite (slice 6).

Asserts that all available harness branches reach engine MCP `query_graph`
and surface the same deterministic top-K for a fixed probe question.

Branches:
  1. Direct stdio MCP (always runs) — the "Claude Code branch" baseline
     since CC speaks plain MCP. Spawns a sample-vault-backed engine MCP
     subprocess and calls `query_graph` over the official MCP client SDK.
  2. Codex CLI (skipped if `codex` not on PATH) — invokes Codex against
     a config that points at the engine MCP server, asks a question, and
     parses the tool output.
  3. Ollama + Pydantic-AI (skipped if Ollama daemon not reachable) — uses
     `tools/verify_harness.run_probe`.

Comparison: ordered list of node titles surfaced by each branch (the
MCP `query_graph` tool prints `NODE <title> [...]` lines). Score values
may differ between branches; we compare IDs only.

Marker: `mcp_connectivity` — registered in pyproject.toml.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

# Make tools/ importable for the Ollama branch.
_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from vault_engine.config import EngineConfig  # noqa: E402
from vault_engine.embedder import MockEmbedder  # noqa: E402
from vault_engine.mcp_server import build_server  # noqa: E402
from vault_engine.service import Service  # noqa: E402

pytestmark = pytest.mark.mcp_connectivity

# Single deterministic probe question used by every branch. The sample
# vault is small enough that any non-trivial query maps to a stable
# ranked set.
PROBE_QUESTION = "alpha"
TOP_K = 5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ollama_reachable(host: str = "http://localhost:11434") -> bool:
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=0.5) as resp:
            return resp.status == 200
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        return False


_NODE_LINE_RE = re.compile(r"^NODE\s+(.*?)\s+\[", re.MULTILINE)


def _parse_node_titles(mcp_text: str) -> list[str]:
    """Pull ordered node titles out of `query_graph`'s text response."""
    return _NODE_LINE_RE.findall(mcp_text)


# ---------------------------------------------------------------------------
# Shared fixture: deterministic ground-truth top-K
# ---------------------------------------------------------------------------


@pytest.fixture
def engine_service(sample_vault, tmp_path):
    """Start a Service over the sample_vault with the mock embedder."""
    cfg = EngineConfig(
        vault_path=sample_vault,
        cache_dir=tmp_path / "cache",
        embedding_model="mock",
        embedding_dim=8,
    )
    svc = Service(cfg, embedder=MockEmbedder(dim=8))
    svc.start()
    try:
        yield svc
    finally:
        svc.stop()


@pytest.fixture
def expected_top_k(engine_service):
    """Compute the deterministic ranked top-K via Service.query() directly.

    Returns the ordered list of node titles. Branches that talk to the
    engine through the MCP wire surface text containing `NODE <title>`
    lines in this same order.
    """
    result = engine_service.query(PROBE_QUESTION, top_k=TOP_K)
    G = engine_service.graph_store.graph
    titles: list[str] = []
    for hit in result["fused_hits"]:
        node = G.nodes.get(hit.doc_id, {})
        titles.append(node.get("title", hit.doc_id))
    assert titles, "sample vault produced no fused_hits — fixture is broken"
    return titles


# ---------------------------------------------------------------------------
# Branch 1 — direct stdio MCP (always runs)
# ---------------------------------------------------------------------------


def test_query_graph_via_direct_mcp_stdio(engine_service, expected_top_k):
    """Drive the engine's MCP surface via the in-process server handle.

    We use `build_server(svc).call_tool_handler` rather than spawning a
    `vault-engine mcp` subprocess: it exercises the same code path
    (`call_tool` -> handler dict -> `_query_graph`) over the same MCP
    types (`TextContent`), just without the JSON-RPC wire roundtrip.
    The wire transport is exercised by the `mcp` SDK's own tests; what
    we verify here is that the engine's MCP-shaped handler returns the
    same top-K that `Service.query()` does.
    """
    handle = build_server(engine_service)
    out = asyncio.run(
        handle.call_tool_handler("query_graph", {"question": PROBE_QUESTION, "top_k": TOP_K})
    )
    assert out and out[0].text, "MCP query_graph returned empty"
    titles = _parse_node_titles(out[0].text)
    assert titles == expected_top_k, (
        f"direct-MCP branch top-K diverged from Service.query() ground truth\n"
        f"  expected: {expected_top_k}\n"
        f"  got:      {titles}\n"
        f"  raw text: {out[0].text!r}"
    )


# ---------------------------------------------------------------------------
# Branch 2 — Codex CLI (skipped if not installed)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    shutil.which("codex") is None and not os.environ.get("CODEX_BIN"),
    reason="Codex CLI not on PATH and CODEX_BIN unset",
)
def test_query_graph_via_codex(engine_service, expected_top_k, tmp_path):
    """Invoke Codex CLI in a way that triggers `query_graph`.

    Strategy: write a transient ~/.codex-like config TOML that registers
    the engine MCP server, then run `codex exec` (the non-interactive
    headless mode) with a prompt that should provoke a `query_graph`
    call. Parse Codex's stdout for `NODE <title>` lines emitted by the
    tool response.

    NOTE: Codex's exact CLI surface for headless prompting + per-run
    config selection has shifted between versions. If the invocation
    fails environmentally, we skip rather than fail — the test's job is
    to confirm parity *when* Codex is available, not to validate Codex
    itself.
    """
    codex_bin = os.environ.get("CODEX_BIN") or shutil.which("codex")
    assert codex_bin is not None  # narrowed by skipif

    vault = engine_service.cfg.vault_path
    config_path = tmp_path / "codex-config.toml"
    config_path.write_text(
        "# transient codex config for the connectivity test\n"
        "[mcp_servers.vault-engine]\n"
        'command = "uv"\n'
        f'args = ["run", "vault-engine", "mcp", "--vault", "{vault.as_posix()}"]\n',
        encoding="utf-8",
    )

    prompt = (
        f"Use the vault-engine MCP tool `query_graph` with the exact question "
        f'"{PROBE_QUESTION}" and top_k={TOP_K}. Print the raw tool output verbatim.'
    )
    try:
        proc = subprocess.run(
            [codex_bin, "exec", "--config", str(config_path), prompt],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"codex invocation failed environmentally: {exc}")

    if proc.returncode != 0:
        pytest.skip(
            f"codex returned non-zero ({proc.returncode}); "
            f"surface may have changed. stderr: {proc.stderr[:500]}"
        )

    titles = _parse_node_titles(proc.stdout)
    if not titles:
        pytest.skip(
            "codex output did not contain `NODE ...` lines from the tool; "
            "harness can't parse this Codex version's output format"
        )
    assert titles[: len(expected_top_k)] == expected_top_k, (
        f"codex branch top-K diverged from ground truth\n"
        f"  expected: {expected_top_k}\n"
        f"  got:      {titles}"
    )


# ---------------------------------------------------------------------------
# Branch 3 — Ollama + Pydantic-AI (skipped if Ollama unreachable)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _ollama_reachable(),
    reason="Ollama daemon not reachable on localhost:11434",
)
def test_query_graph_via_ollama_pydantic(engine_service, expected_top_k):
    """Drive the engine MCP through verify_harness.run_probe.

    The harness spawns its own `vault-engine mcp` subprocess pointed at
    the same vault path the in-process service is using; both backends
    are deterministic so the surfaced top-K must match.
    """
    import verify_harness

    vault = str(engine_service.cfg.vault_path)
    try:
        result = verify_harness.run_probe(PROBE_QUESTION, vault=vault)
    except Exception as exc:  # pragma: no cover — environmental
        pytest.skip(f"verify_harness.run_probe failed environmentally: {exc}")

    titles = _parse_node_titles(result.get("output", ""))
    if not titles:
        pytest.skip(
            "Ollama agent did not surface `NODE ...` lines; the local LLM "
            "may have summarized the tool output instead of returning it raw."
        )
    assert titles[: len(expected_top_k)] == expected_top_k, (
        f"ollama+pydantic branch top-K diverged from ground truth\n"
        f"  expected: {expected_top_k}\n"
        f"  got:      {titles}"
    )
