"""Pydantic-AI verify harness — Ollama-driven client for engine MCP.

Slice 4 of the three-pillars MVP. Proves a third harness (Ollama via
Pydantic-AI) can drive the same engine MCP surface that Claude Code and
Codex consume. Designed to be tiny: one `build_agent`, one `run_probe`,
and a CLI entry point that prints JSON.

Provider/model defaults are overridable via env vars and CLI flags so
the same module is reusable from `tests/test_mcp_connectivity.py`.

Env vars (all optional):
    VERIFY_HARNESS_MODEL    - Ollama model tag, default "gemma3:4b"
    VERIFY_HARNESS_OLLAMA   - Ollama base URL, default "http://localhost:11434"
    VERIFY_HARNESS_VAULT    - Path to vault root for engine MCP. Required at
                              run-time (no sensible cross-machine default).

Usage:
    python tools/verify_harness.py "What does the vault know about X?"
    python tools/verify_harness.py --model gemma3:4b --vault E:/Projects/second-brain "..."
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServerStdio
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.ollama import OllamaProvider

DEFAULT_MODEL = "gemma3:4b"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
# Sentinel used by tests via monkeypatch; production callers pass --vault.
DEFAULT_VAULT_ENV = os.environ.get("VERIFY_HARNESS_VAULT", "")


def _ollama_base_url(raw: str) -> str:
    """Normalize an Ollama base URL to the OpenAI-compatible /v1 suffix."""
    raw = raw.rstrip("/")
    if raw.endswith("/v1"):
        return raw
    return f"{raw}/v1"


def _engine_mcp_toolset(vault: str | None) -> MCPServerStdio:
    """Construct the engine MCP stdio toolset.

    We invoke the engine via `uv run vault-engine mcp --vault <path>` so
    the subprocess inherits the current uv environment. Falls back to a
    bare `vault-engine` command when uv isn't on PATH (e.g. CI without
    the toolchain), which fails loudly at run-time rather than silently.
    """
    args = ["run", "vault-engine", "mcp"]
    if vault:
        args.extend(["--vault", vault])
    return MCPServerStdio("uv", args=args, timeout=30)


def build_agent(
    *,
    model: str = DEFAULT_MODEL,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    vault: str | None = None,
) -> Agent:
    """Build a Pydantic-AI agent: Ollama provider + engine MCP toolset.

    The Ollama provider talks to Ollama's OpenAI-compatible /v1 endpoint;
    we keep `OllamaProvider` for clarity but it's a thin wrapper over the
    OpenAI client.
    """
    provider = OllamaProvider(base_url=_ollama_base_url(ollama_url))
    llm = OpenAIChatModel(model, provider=provider)
    engine = _engine_mcp_toolset(vault)
    system_prompt = (
        "You are a verify probe. When the user asks a question, ALWAYS call "
        "the `query_graph` tool with the user's question verbatim and return "
        "the tool result as your answer. Do not add commentary."
    )
    return Agent(llm, toolsets=[engine], system_prompt=system_prompt)


def run_probe(
    question: str,
    *,
    model: str = DEFAULT_MODEL,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    vault: str | None = None,
) -> dict:
    """Run a single probe: build agent, ask question, return engine output.

    Returns a dict with `question`, `model`, `output` (raw text from the
    agent — typically the `query_graph` tool's stringified response).
    """
    if vault is None:
        vault = DEFAULT_VAULT_ENV or None
    agent = build_agent(model=model, ollama_url=ollama_url, vault=vault)

    async def _go() -> str:
        async with agent:
            result = await agent.run(question)
            return result.output

    output = asyncio.run(_go())
    return {
        "question": question,
        "model": model,
        "ollama_url": ollama_url,
        "output": output,
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Pydantic-AI verify harness for engine MCP.")
    p.add_argument("question", help="Question to send through the agent.")
    p.add_argument(
        "--model",
        default=os.environ.get("VERIFY_HARNESS_MODEL", DEFAULT_MODEL),
        help=f"Ollama model tag (default: {DEFAULT_MODEL})",
    )
    p.add_argument(
        "--ollama-url",
        default=os.environ.get("VERIFY_HARNESS_OLLAMA", DEFAULT_OLLAMA_URL),
        help=f"Ollama base URL (default: {DEFAULT_OLLAMA_URL})",
    )
    p.add_argument(
        "--vault",
        default=os.environ.get("VERIFY_HARNESS_VAULT", ""),
        help="Path to vault root passed to `vault-engine mcp --vault`.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    vault = args.vault or None
    if vault and not Path(vault).exists():
        print(f"vault path does not exist: {vault}", file=sys.stderr)
        return 2
    result = run_probe(
        args.question,
        model=args.model,
        ollama_url=args.ollama_url,
        vault=vault,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
