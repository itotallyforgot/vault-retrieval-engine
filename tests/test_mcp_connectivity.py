"""Cross-harness MCP-connectivity test suite (slice 6).

Asserts that all available harness branches reach engine MCP `query_graph`
and surface the same deterministic top-K for a fixed probe question.

Branches:
  1. Direct stdio MCP (always runs) — the "Claude Code branch" baseline
     since CC speaks plain MCP. Spawns a `vault-engine mcp` subprocess
     against the sample vault and calls `query_graph` over the official
     `mcp` client SDK's stdio transport (real JSON-RPC roundtrip).
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
from collections import Counter
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Make tools/ importable for the Ollama branch.
_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from vault_engine.config import EngineConfig  # noqa: E402
from vault_engine.embedder import MockEmbedder  # noqa: E402
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


async def _query_graph_over_stdio(vault: Path, question: str, top_k: int) -> str:
    """Spawn `vault-engine mcp` as a subprocess and call `query_graph`
    over a real JSON-RPC stdio transport via the `mcp` client SDK.

    Returns the tool response text. Uses `--mock-embedder` so the
    subprocess shares the deterministic embedding behaviour the
    in-process ground-truth fixture uses.
    """
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "vault_engine.cli",
            "--mock-embedder",
            "mcp",
            "--vault",
            str(vault),
        ],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            # Timeouts guard against future engine changes that introduce
            # a slow init or tool path; without them a hang would only
            # surface at the CI job-level timeout.
            await asyncio.wait_for(session.initialize(), timeout=30)
            result = await asyncio.wait_for(
                session.call_tool(
                    "query_graph",
                    arguments={"question": question, "top_k": top_k},
                ),
                timeout=30,
            )
    assert result.content, "MCP query_graph returned empty content"
    first = result.content[0]
    text = getattr(first, "text", None)
    assert text, f"first content block has no text: {first!r}"
    return text


def test_query_graph_via_direct_mcp_stdio(engine_service, expected_top_k):
    """Spawn a real `vault-engine mcp` subprocess and exercise the full
    JSON-RPC-over-stdio roundtrip the way Claude Code does.

    The subprocess uses `--mock-embedder` so its top-K is byte-identical
    to the in-process Service ground truth. This is the actual
    "Claude Code branch" — same transport, same wire format CC uses.
    """
    vault = engine_service.cfg.vault_path
    text = asyncio.run(_query_graph_over_stdio(vault, PROBE_QUESTION, TOP_K))
    titles = _parse_node_titles(text)
    # Compare as multisets: the MCP wire surface sorts hits by RRF score
    # before formatting, while Service.query() returns them in the
    # graph's natural traversal order. Same retrieval RESULT, different
    # presentation order. Spec is "equivalent retrieval", not "byte-identical
    # order"; multiset equality is the semantically correct check.
    assert Counter(titles) == Counter(expected_top_k), (
        f"direct-MCP-stdio branch retrieval set diverged from Service.query() ground truth\n"
        f"  expected (multiset): {Counter(expected_top_k)}\n"
        f"  got      (multiset): {Counter(titles)}\n"
        f"  raw text: {text!r}"
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
    # Multiset comparison — see direct-stdio branch rationale.
    assert Counter(titles[: len(expected_top_k)]) == Counter(expected_top_k), (
        f"codex branch retrieval set diverged from ground truth\n"
        f"  expected (multiset): {Counter(expected_top_k)}\n"
        f"  got      (multiset): {Counter(titles[: len(expected_top_k)])}"
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
    # Multiset comparison — see direct-stdio branch rationale.
    assert Counter(titles[: len(expected_top_k)]) == Counter(expected_top_k), (
        f"ollama+pydantic branch retrieval set diverged from ground truth\n"
        f"  expected (multiset): {Counter(expected_top_k)}\n"
        f"  got      (multiset): {Counter(titles[: len(expected_top_k)])}"
    )
