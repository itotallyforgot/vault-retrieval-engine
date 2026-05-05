#!/usr/bin/env bash
# vault-engine reindex — post-commit plug-in piece.
#
# Installed by `scripts/install-vault-overlays.sh` into a target vault's
# .githooks/post-commit.d/10-vault-engine.sh. The vault's .githooks/post-commit
# dispatcher invokes every executable in this directory after each commit;
# this script runs `vault-engine reindex` so the engine's vec store + graph
# stay current with vault contents.
#
# Numeric prefix (`10-`) controls dispatch order — lower runs earlier.
# Engine claims `10-` so future plug-ins can sequence around it.
#
# Designed to be a graceful no-op when:
#   - vault-engine is not on PATH (engine uninstalled)
#   - the engine's reindex command exits non-zero (silent in-background)
#
# Safe to keep installed even after engine removal: vault stays usable
# in standalone mode.

set -u

# Find the vault root via git (the hook runs from .git/hooks → .git is parent).
VAULT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
[ -z "$VAULT_ROOT" ] && exit 0

# Background reindex; mute output so the commit operation isn't slowed
# or noisy. Failure is non-fatal — vault is the source of truth.
if command -v vault-engine >/dev/null 2>&1; then
  ( vault-engine --vault "$VAULT_ROOT" reindex >/dev/null 2>&1 & ) || true
fi

exit 0
