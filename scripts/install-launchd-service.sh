#!/usr/bin/env bash
# install-launchd-service.sh — register vault-retrieval-engine as a macOS
# LaunchAgent (OGR-181, Mac counterpart to install-windows-service.ps1).
#
# What this script does:
#   1. Render overlays/launchd/com.vault-retrieval.engine.plist by
#      substituting __PLACEHOLDER__ tokens with values from flags / env.
#   2. Drop the rendered plist into ~/Library/LaunchAgents/.
#   3. Bootstrap it with `launchctl bootstrap gui/$(id -u)` so it starts
#      now AND on every login.
#
# Idempotent: if the agent is already loaded, it is booted-out first.
# Safe to re-run after editing the template.
#
# Usage:
#   ./scripts/install-launchd-service.sh --vault /path/to/vault \
#       [--token <token>] [--bind 127.0.0.1] [--port 7842] \
#       [--cache /path/to/cache] [--log-dir ~/Library/Logs/vault-retrieval-engine] \
#       [--uv /opt/homebrew/bin/uv] [--dry-run]
#
# Or via env vars (flags override):
#   VAULT_PATH                 — vault root (required if --vault not passed)
#   VAULT_ENGINE_HTTP_TOKEN    — HTTP bearer secret (required for non-loopback bind)
#   VAULT_ENGINE_BIND_ADDR     — defaults to 127.0.0.1 (loopback)
#   VAULT_ENGINE_HTTP_PORT     — defaults to 7842
#   VAULT_ENGINE_CACHE_DIR     — defaults to ~/.cache/vault-retrieval
#
# Notes:
#   - LaunchAgent runs as your user, NOT root. No sudo required.
#   - The plist references the engine repo via WorkingDirectory; do NOT
#     move or delete the repo while the agent is loaded.
#   - Logs land in $LOG_DIR/vault-engine-{stdout,stderr}.log (rotated by
#     the OS, not by launchd; consider newsyslog if size matters).

set -euo pipefail

# --- Defaults --------------------------------------------------------------
VAULT="${VAULT_PATH:-}"
TOKEN="${VAULT_ENGINE_HTTP_TOKEN:-}"
BIND="${VAULT_ENGINE_BIND_ADDR:-127.0.0.1}"
PORT="${VAULT_ENGINE_HTTP_PORT:-7842}"
CACHE="${VAULT_ENGINE_CACHE_DIR:-$HOME/.cache/vault-retrieval}"
LOG_DIR="${LOG_DIR:-$HOME/Library/Logs/vault-retrieval-engine}"
UV_BIN=""
DRY_RUN=0

# Repo root: this script lives in scripts/, repo root is one level up.
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE_PLIST="$REPO_DIR/overlays/launchd/com.vault-retrieval.engine.plist"
LABEL="com.vault-retrieval.engine"
DEST_PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"

# --- Arg parsing -----------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --vault)    VAULT="$2";    shift 2 ;;
    --token)    TOKEN="$2";    shift 2 ;;
    --bind)     BIND="$2";     shift 2 ;;
    --port)     PORT="$2";     shift 2 ;;
    --cache)    CACHE="$2";    shift 2 ;;
    --log-dir)  LOG_DIR="$2";  shift 2 ;;
    --uv)       UV_BIN="$2";   shift 2 ;;
    --dry-run)  DRY_RUN=1;     shift ;;
    -h|--help)
      sed -n '2,/^set/p' "$0" | sed -e 's/^# //' -e 's/^#//'
      exit 0
      ;;
    *)
      echo "error: unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

# --- Validate --------------------------------------------------------------
if [[ -z "$VAULT" ]]; then
  echo "error: --vault <path> required (or VAULT_PATH env)" >&2
  exit 2
fi

if [[ ! -d "$VAULT" ]]; then
  echo "error: vault path does not exist: $VAULT" >&2
  exit 2
fi

# Resolve to absolute path (avoids surprises when launchd later runs).
VAULT="$(cd "$VAULT" && pwd)"
CACHE="${CACHE/#~/$HOME}"
LOG_DIR="${LOG_DIR/#~/$HOME}"

# Auto-detect uv if not passed. Hard-fail with a useful hint rather than
# letting launchd silently fail with "ProgramArguments not executable".
if [[ -z "$UV_BIN" ]]; then
  UV_BIN="$(command -v uv || true)"
fi
if [[ -z "$UV_BIN" || ! -x "$UV_BIN" ]]; then
  echo "error: uv not found on PATH. Install via 'brew install uv' or pass --uv </path/to/uv>." >&2
  exit 2
fi

# Refuse to bind non-loopback without a token. The engine's http_server
# enforces this too, but we surface it at install time so the operator
# doesn't see a daemon-loop instead of a clear error.
#
# Important: --token is the HS256 SIGNING SECRET, not a pre-shared bearer.
# Clients must send a JWT signed with this secret (see docs/ios-shortcut.md
# and the README's HTTP/JSON section for the two-step generate-then-sign
# pattern).
if [[ "$BIND" != "127.0.0.1" && "$BIND" != "::1" && "$BIND" != "localhost" ]]; then
  if [[ -z "$TOKEN" ]]; then
    echo "error: --bind $BIND is non-loopback; --token (HS256 signing secret) is required to prevent unauthenticated remote access." >&2
    echo "  Generate the secret: uv run python -c \"import secrets; print(secrets.token_urlsafe(32))\"" >&2
    echo "  Then sign per-client JWTs with it (see docs/ios-shortcut.md)." >&2
    exit 2
  fi
fi

# Port must be a positive integer in valid range. Catches "--port abc".
if ! [[ "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then
  echo "error: --port must be an integer 1-65535 (got: $PORT)" >&2
  exit 2
fi

if [[ ! -r "$TEMPLATE_PLIST" ]]; then
  echo "error: template plist not found: $TEMPLATE_PLIST" >&2
  exit 2
fi

# --- Plan -------------------------------------------------------------------
echo "vault-retrieval-engine launchd installer"
echo "  vault:      $VAULT"
echo "  cache:      $CACHE"
echo "  bind:       $BIND"
echo "  port:       $PORT"
echo "  token:      $([[ -n "$TOKEN" ]] && echo "<set, $(echo -n "$TOKEN" | wc -c | tr -d ' ') chars>" || echo "<unset; loopback-only>")"
echo "  log dir:    $LOG_DIR"
echo "  uv binary:  $UV_BIN"
echo "  repo dir:   $REPO_DIR"
echo "  plist dest: $DEST_PLIST"
echo

# --- Render plist -----------------------------------------------------------
#
# We use a temp file so a partial write never lands at the destination.
# The substitutions go through python rather than sed because sed's escaping
# for arbitrary paths/tokens is a footgun — a literal '/' in the token
# would silently break sed. Python's str.replace is verbatim.

render_plist() {
  python3 - "$TEMPLATE_PLIST" \
    "__UV_BIN__"     "$UV_BIN" \
    "__REPO_DIR__"   "$REPO_DIR" \
    "__VAULT_PATH__" "$VAULT" \
    "__BIND_ADDR__"  "$BIND" \
    "__HTTP_PORT__"  "$PORT" \
    "__HTTP_TOKEN__" "$TOKEN" \
    "__CACHE_DIR__"  "$CACHE" \
    "__LOG_DIR__"    "$LOG_DIR" <<'PYEOF'
import html
import sys

src = sys.argv[1]
pairs = list(zip(sys.argv[2::2], sys.argv[3::2]))
with open(src, "r", encoding="utf-8") as f:
    text = f.read()
for placeholder, value in pairs:
    # XML-escape the value so '&', '<', '>' in tokens or paths do not
    # break the plist. Tokens are typically URL-safe-base64 (no XML
    # special chars) but paths can contain unusual characters.
    text = text.replace(placeholder, html.escape(value, quote=True))
sys.stdout.write(text)
PYEOF
}

if [[ $DRY_RUN -eq 1 ]]; then
  echo "[dry-run] Would render plist:"
  echo "---"
  render_plist
  echo "---"
  echo "[dry-run] Would write to: $DEST_PLIST"
  echo "[dry-run] Would mkdir: $LOG_DIR"
  echo "[dry-run] Would bootout (if loaded), then bootstrap gui/$(id -u) $DEST_PLIST"
  exit 0
fi

# --- Execute ----------------------------------------------------------------
mkdir -p "$LOG_DIR"
mkdir -p "$(dirname "$DEST_PLIST")"

tmp_plist="$(mktemp -t vault-engine-launchd.XXXXXX)"
trap 'rm -f "$tmp_plist"' EXIT
render_plist > "$tmp_plist"

# Validate plist syntax before installing. plutil exits non-zero on
# malformed plists. Catches placeholder typos cheaper than launchd does.
if ! plutil -lint "$tmp_plist" > /dev/null; then
  echo "error: rendered plist failed plutil lint. Bug in template or substitution." >&2
  plutil -lint "$tmp_plist" >&2 || true
  exit 1
fi

mv "$tmp_plist" "$DEST_PLIST"
trap - EXIT
# 600: the plist holds VAULT_ENGINE_HTTP_TOKEN in plaintext (same trust
# boundary as NSSM's AppEnvironmentExtra). Restrict to owner-read to
# match the secret's sensitivity.
chmod 600 "$DEST_PLIST"
echo "  -> wrote $DEST_PLIST"

# Bootout-then-bootstrap = idempotent reinstall. `bootout` is silent
# when the agent isn't loaded; we suppress its stderr to avoid noise
# in that case.
target="gui/$(id -u)"
if launchctl print "${target}/${LABEL}" > /dev/null 2>&1; then
  echo "  -> bootout existing ${target}/${LABEL}"
  launchctl bootout "${target}/${LABEL}" || true
fi

echo "  -> bootstrap ${target}/${LABEL}"
launchctl bootstrap "$target" "$DEST_PLIST"

# Give launchd a moment, then report status. If the engine fails to
# start (config error, port in use, etc.), the stderr log will name it.
sleep 1
if launchctl print "${target}/${LABEL}" > /dev/null 2>&1; then
  echo
  echo "LaunchAgent '${LABEL}' is loaded."
  echo "Logs:"
  echo "  stdout: $LOG_DIR/vault-engine-stdout.log"
  echo "  stderr: $LOG_DIR/vault-engine-stderr.log"
  echo
  echo "Health check (wait ~5 s for engine warm-up):"
  echo "  curl http://${BIND}:${PORT}/health"
  echo
  echo "Manage:"
  echo "  launchctl print ${target}/${LABEL}"
  echo "  launchctl kickstart -k ${target}/${LABEL}   # restart"
  echo "  ./scripts/uninstall-launchd-service.sh"
else
  echo "warning: bootstrap returned 0 but launchctl print can't see ${target}/${LABEL}." >&2
  echo "  Check $LOG_DIR/vault-engine-stderr.log for startup errors." >&2
  exit 1
fi
