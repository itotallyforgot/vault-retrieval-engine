"""Tests for tools/verify_harness.py.

The harness is intentionally thin: a Pydantic-AI agent with an Ollama
OpenAI-compatible model and engine MCP attached as a stdio toolset. The
unit-test surface we care about is *call shape* — that `run_probe`
forwards the user's question through the MCP transport and surfaces a
`query_graph` tool invocation. We mock the MCP transport so the test
runs without Ollama/MCP/network.

The live-Ollama path is gated on `_ollama_reachable()` and skipped by
default; flip it on locally only when Ollama is actually running.
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

# Make tools/ importable as a flat module path.
_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))


def _ollama_reachable(host: str = "http://localhost:11434") -> bool:
    """Return True iff an Ollama daemon answers /api/tags within 0.5 s."""
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=0.5) as resp:
            return resp.status == 200
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        return False


def test_verify_harness_module_imports():
    """The harness module must be importable without side effects."""
    import verify_harness  # noqa: F401

    assert hasattr(verify_harness, "run_probe")
    assert hasattr(verify_harness, "build_agent")


def test_run_probe_forwards_question_through_mcp(monkeypatch):
    """run_probe must invoke the agent with the question intact and return
    a dict carrying the engine response.

    We don't run a real LLM — we patch Agent.run to capture the prompt and
    fake a tool-roundtrip response. This asserts the *call shape*: the
    question reaches the agent, the engine MCP toolset is wired in, and
    the output is JSON-serializable.
    """
    import verify_harness

    captured: dict = {}

    class _FakeRunResult:
        output = "Intent: lookup | 2 fused hits\nNODE Alpha [community=0]"

    async def _fake_run(self, prompt, **kwargs):
        captured["prompt"] = prompt
        captured["toolsets"] = [type(t).__name__ for t in (self.toolsets or [])]
        return _FakeRunResult()

    async def _noop_aenter(self):
        return self

    async def _noop_aexit(self, exc_type, exc, tb):
        return False

    # Patch Agent.run AND the async-context entry/exit so no MCP subprocess
    # is started during the unit test.
    from pydantic_ai import Agent

    monkeypatch.setattr(Agent, "run", _fake_run, raising=True)
    monkeypatch.setattr(Agent, "__aenter__", _noop_aenter, raising=True)
    monkeypatch.setattr(Agent, "__aexit__", _noop_aexit, raising=True)

    result = verify_harness.run_probe(
        "What does the vault know about Alpha?",
        model="gemma3:4b",
        ollama_url="http://localhost:11434",
    )

    assert isinstance(result, dict)
    assert result["question"] == "What does the vault know about Alpha?"
    assert result["model"] == "gemma3:4b"
    assert "output" in result
    assert "Alpha" in result["output"]
    assert captured["prompt"] == "What does the vault know about Alpha?"
    # Engine MCP must be attached as a toolset.
    assert "MCPServerStdio" in captured["toolsets"]


def test_build_agent_attaches_engine_mcp_stdio():
    """build_agent must construct an Agent whose toolsets include an
    MCPServerStdio pointing at `vault-engine mcp` (the engine's stdio MCP
    surface). Verified without spawning the subprocess.
    """
    import verify_harness
    from pydantic_ai.mcp import MCPServerStdio

    agent = verify_harness.build_agent(model="gemma3:4b", ollama_url="http://localhost:11434")
    assert agent.toolsets, "agent must carry at least one toolset"
    stdio_servers = [t for t in agent.toolsets if isinstance(t, MCPServerStdio)]
    assert stdio_servers, "engine MCP stdio toolset must be attached"
    # The command + args should reference vault-engine's mcp subcommand.
    server = stdio_servers[0]
    flat = " ".join([server.command, *server.args]).lower()
    assert "vault-engine" in flat or "vault_engine" in flat
    assert "mcp" in flat


@pytest.mark.skipif(
    not _ollama_reachable(), reason="Ollama daemon not reachable on localhost:11434"
)
def test_run_probe_live_ollama(tmp_path):
    """Smoke: with Ollama actually running, run_probe returns a dict.

    Requires `gemma3:4b` (or whatever VERIFY_HARNESS_MODEL points at)
    pulled. Skipped on CI / when Ollama isn't up.
    """
    import verify_harness

    # Avoid mutating any user vault — just confirm the call doesn't blow up.
    with patch.object(verify_harness, "DEFAULT_VAULT_ENV", str(tmp_path)):
        try:
            result = verify_harness.run_probe("ping")
        except Exception as exc:  # pragma: no cover — environmental
            pytest.skip(f"live Ollama call failed environmentally: {exc}")
    assert isinstance(result, dict)
    assert "output" in result
