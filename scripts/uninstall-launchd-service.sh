#!/usr/bin/env bash
# uninstall-launchd-service.sh — stop and remove the vault-retrieval-engine
# LaunchAgent on macOS (OGR-181, counterpart to uninstall-windows-service.ps1).
#
# Idempotent: safe to run when the agent is not loaded.
#
# Usage:
#   ./scripts/uninstall-launchd-service.sh [--keep-logs] [--keep-plist] [--dry-run]
#
# By default this:
#   1. boots out the LaunchAgent (if loaded)
#   2. removes ~/Library/LaunchAgents/com.vault-retrieval.engine.plist
#   3. preserves logs (~/Library/Logs/vault-retrieval-engine/) — pass
#      --no-keep-logs to delete them too.

set -euo pipefail

LABEL="com.vault-retrieval.engine"
DEST_PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="$HOME/Library/Logs/vault-retrieval-engine"
KEEP_LOGS=1
KEEP_PLIST=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep-logs)     KEEP_LOGS=1;   shift ;;
    --no-keep-logs)  KEEP_LOGS=0;   shift ;;
    --keep-plist)    KEEP_PLIST=1;  shift ;;
    --log-dir)       LOG_DIR="$2";  shift 2 ;;
    --dry-run)       DRY_RUN=1;     shift ;;
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

target="gui/$(id -u)"
LOG_DIR="${LOG_DIR/#~/$HOME}"

echo "vault-retrieval-engine launchd uninstaller"
echo "  target:     ${target}/${LABEL}"
echo "  plist:      $DEST_PLIST"
echo "  log dir:    $LOG_DIR (keep: $KEEP_LOGS)"
echo

run() {
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "  [dry-run] $*"
    return 0
  fi
  "$@"
}

# Boot out if loaded. `launchctl print` returns non-zero when the agent
# is not loaded — we use that as a clean existence check rather than
# parsing list output.
if launchctl print "${target}/${LABEL}" > /dev/null 2>&1; then
  echo "  -> bootout ${target}/${LABEL}"
  # bootout can return non-zero for "already shutting down" scenarios;
  # we tolerate that and proceed to plist removal.
  run launchctl bootout "${target}/${LABEL}" || true
else
  echo "  -> not loaded (skip bootout)"
fi

# Remove the plist file unless --keep-plist.
if [[ -f "$DEST_PLIST" ]]; then
  if [[ $KEEP_PLIST -eq 1 ]]; then
    echo "  -> keeping plist at $DEST_PLIST (--keep-plist)"
  else
    echo "  -> rm $DEST_PLIST"
    run rm -f "$DEST_PLIST"
  fi
else
  echo "  -> no plist at $DEST_PLIST (skip rm)"
fi

# Optionally remove logs.
if [[ $KEEP_LOGS -eq 0 ]]; then
  if [[ -d "$LOG_DIR" ]]; then
    echo "  -> rm -rf $LOG_DIR"
    run rm -rf "$LOG_DIR"
  else
    echo "  -> no log dir at $LOG_DIR (skip rm)"
  fi
else
  echo "  -> keeping logs at $LOG_DIR (default; pass --no-keep-logs to delete)"
fi

echo
echo "LaunchAgent '${LABEL}' removed."
