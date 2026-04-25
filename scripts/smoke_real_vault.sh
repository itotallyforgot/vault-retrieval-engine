#!/usr/bin/env bash
# Smoke test against the real vault. Runs on PC.
set -euo pipefail

VAULT="${VAULT:-$USERPROFILE/Projects/Second-Brain}"
FIXTURES="$VAULT/_ops/eval/retrieval-fixtures.jsonl"

echo "[smoke] vault=$VAULT"
echo "[smoke] fixtures=$FIXTURES"

uv run vault-engine --vault "$VAULT" status
uv run vault-engine --vault "$VAULT" reindex
uv run vault-engine --vault "$VAULT" search "claude-code" -k 5
uv run vault-engine --vault "$VAULT" eval --fixtures "$FIXTURES" || {
    echo "[smoke] eval had failures; inspect output above"
    exit 1
}

echo "[smoke] OK"
