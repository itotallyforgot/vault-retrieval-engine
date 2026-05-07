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
# Failure surfacing (OGR-71):
#   - reindex runs in a backgrounded subshell so commits stay fast
#   - on non-zero exit (engine broken, missing module, etc.):
#       1. write `.git/.engine-broken` marker with timestamp + tail of output,
#          so next `git status` / shell prompt can surface it
#       2. print a loud one-line warning to stderr (survives even when stdout
#          is captured by git)
#       3. emit a native desktop notification if available (notify-send /
#          osascript / BurntToast)
#   - on zero exit: clean up any prior marker file (engine recovered)
#   - the hook itself ALWAYS exits 0 — never block commits
#
# Designed to be a graceful no-op when:
#   - vault-engine is not on PATH (engine uninstalled)
#
# Safe to keep installed even after engine removal.

set -u

# Find the vault root via git (the hook runs from .git/hooks → .git is parent).
VAULT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
[ -z "$VAULT_ROOT" ] && exit 0

# Engine absent → silent no-op (vault stays usable in standalone mode).
command -v vault-engine >/dev/null 2>&1 || exit 0

MARKER="$VAULT_ROOT/.git/.engine-broken"

(
  # Capture combined stdout + stderr so we can report on failure.
  output=$(vault-engine --vault "$VAULT_ROOT" reindex 2>&1)
  rc=$?

  if [ "$rc" -ne 0 ]; then
    # 1. Persist marker for next git status / shell prompt.
    {
      printf '[%s] vault-engine reindex failed (exit=%d)\n' \
        "$(date -u +%FT%TZ)" "$rc"
      printf 'Vec store is now stale. Last 20 lines of output:\n\n'
      printf '%s\n' "$output" | tail -20
    } > "$MARKER" 2>/dev/null

    # 2. Loud stderr — survives even when caller captures stdout.
    {
      printf '\n'
      printf '! vault-engine reindex FAILED (exit=%d). Vec store is now stale.\n' "$rc"
      printf '  Marker: %s\n' "$MARKER"
      printf '  Investigate: cat "%s"\n' "$MARKER"
      printf '\n'
    } >&2

    # 3. Best-effort native desktop notification — never fail the hook on this.
    if command -v notify-send >/dev/null 2>&1; then
      notify-send -u critical 'vault-engine reindex failed' \
        "Marker at $MARKER" 2>/dev/null || true
    elif command -v osascript >/dev/null 2>&1; then
      osascript -e \
        "display notification \"Marker at $MARKER\" with title \"vault-engine reindex failed\"" \
        2>/dev/null || true
    elif command -v powershell.exe >/dev/null 2>&1; then
      # BurntToast is opt-in (not preinstalled); fail silently if absent.
      powershell.exe -NoProfile -Command \
        "if (Get-Module -ListAvailable BurntToast) { New-BurntToastNotification -Text 'vault-engine reindex failed','Marker at $MARKER' }" \
        2>/dev/null || true
    fi
  else
    # Engine recovered — drop any stale marker.
    rm -f "$MARKER" 2>/dev/null || true
  fi
) &

# Hook always succeeds — don't block commits.
exit 0
