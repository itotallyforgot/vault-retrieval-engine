#!/usr/bin/env bash
# install-vault-overlays.sh — drop engine plug-in overlays into a target vault.
#
# Designed for markdown/Obsidian-style vaults. Idempotent: re-running
# is safe and reports what's already in place vs newly installed.
#
# Usage:
#   ./scripts/install-vault-overlays.sh --vault /path/to/vault [--dry-run]
#
# Overlays installed:
#   - skills/vault/synth.md                          — engine-aware insight synthesis skill
#   - skills/vault/crawl.md                          — engine-aware URL → raw/ scrape skill
#   - .githooks/post-commit                          — vault-owned dispatcher (only if absent
#                                                       or matches the legacy monolithic engine
#                                                       hook from before the .d/ refactor)
#   - .githooks/post-commit.d/10-vault-engine.sh     — engine's reindex piece
#
# Pre-requisites for full function (skills are scaffolding either way):
#   - Vault has a 'skills/vault/' bundle conforming to this layout convention.
#   - Vault is git-tracked (.git/ exists) for the post-commit hook to fire.
#   - vault-engine is on PATH for the engine plug-in to do its job;
#     the plug-in is a graceful no-op when not.
#
# Dispatcher safety:
#   - If the vault already has a custom (non-engine, non-dispatcher)
#     post-commit, this script REFUSES to overwrite. The operator is
#     told to manually adopt the dispatcher pattern; the engine plug-in
#     piece is still installed in .d/ so manual adoption is one-line.
#
# After install, point git at the vault's .githooks/ directory:
#   git -C <vault> config core.hooksPath .githooks

set -euo pipefail

vault=""
dry_run=0
overlay_dir="$(cd "$(dirname "$0")/../overlays" && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --vault)
      vault="$2"
      shift 2
      ;;
    --dry-run)
      dry_run=1
      shift
      ;;
    -h|--help)
      sed -n '2,/^set/p' "$0" | sed -e 's/^# //' -e 's/^#//'
      exit 0
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$vault" ]]; then
  echo "error: --vault <path> is required" >&2
  exit 2
fi

if [[ ! -d "$vault" ]]; then
  echo "error: vault path does not exist: $vault" >&2
  exit 2
fi

vault=$(cd "$vault" && pwd)

echo "Installing engine overlays into: $vault"
echo "Source overlays: $overlay_dir"
echo

install_file() {
  local src="$1"
  local dest="$2"
  local mode="${3:-644}"
  local dest_dir
  dest_dir="$(dirname "$dest")"

  if [[ -e "$dest" ]]; then
    if cmp -s "$src" "$dest"; then
      echo "  [skip] $dest (already current)"
      return
    fi
    echo "  [update] $dest"
  else
    echo "  [new] $dest"
  fi

  if [[ $dry_run -eq 1 ]]; then
    return
  fi

  mkdir -p "$dest_dir"
  cp "$src" "$dest"
  chmod "$mode" "$dest"
}

install_file "$overlay_dir/skills/vault/synth.md" "$vault/skills/vault/synth.md"
install_file "$overlay_dir/skills/vault/crawl.md" "$vault/skills/vault/crawl.md"

# --- post-commit dispatcher + engine plug-in piece -------------------------
#
# Two-part install:
#
#   1. The dispatcher (`.githooks/post-commit`) is owned by the vault, not
#      the engine. We install it only if absent OR if the existing file is
#      the legacy monolithic engine hook (from before the .d/ refactor).
#      Custom user-edited dispatchers are NEVER overwritten.
#
#   2. The engine plug-in (`.githooks/post-commit.d/10-vault-engine.sh`)
#      is the only thing the engine has full ownership of.
#
# The legacy detector compares against the SHA256 of the pre-refactor body.
# If a vault has been migrated and the dispatcher hand-edited, this becomes
# a "custom" file from our PoV and we leave it alone.

# SHA256 of the legacy monolithic post-commit body from origin/main before
# this dispatcher refactor. Used to safely auto-replace that exact file.
LEGACY_HOOK_SHA="b68cfa92f1266193ecb47c88035ac1358c361c527dcf5b465bc4959bba02fb69"

install_dispatcher() {
  local src="$overlay_dir/githooks/post-commit"
  local dest="$vault/.githooks/post-commit"

  if [[ ! -e "$dest" ]]; then
    install_file "$src" "$dest" "755"
    return
  fi

  if cmp -s "$src" "$dest"; then
    echo "  [skip] $dest (already dispatcher)"
    return
  fi

  # Could be the legacy monolithic engine hook → safe to replace.
  local existing_sha
  existing_sha=$(shasum -a 256 "$dest" 2>/dev/null | awk '{print $1}')
  if [[ "$existing_sha" == "$LEGACY_HOOK_SHA" ]]; then
    echo "  [migrate] $dest (replacing legacy monolithic hook with dispatcher)"
    if [[ $dry_run -eq 0 ]]; then
      cp "$dest" "$dest.legacy.bak"
      cp "$src" "$dest"
      chmod 755 "$dest"
    fi
    return
  fi

  # Anything else is user-customized → never clobber.
  echo "  [skip] $dest (custom file; refusing to overwrite)"
  echo "         To adopt the dispatcher pattern, see overlays/githooks/post-commit"
  echo "         and ensure your hook walks .githooks/post-commit.d/*"
}

install_dispatcher
install_file "$overlay_dir/githooks/post-commit.d/10-vault-engine.sh" \
             "$vault/.githooks/post-commit.d/10-vault-engine.sh" "755"

echo
if [[ $dry_run -eq 1 ]]; then
  echo "Dry run complete. Re-run without --dry-run to apply."
else
  echo "Overlays installed."
  echo "If the vault is git-tracked and you want the post-commit hook active:"
  echo "  git -C \"$vault\" config core.hooksPath .githooks"
fi
