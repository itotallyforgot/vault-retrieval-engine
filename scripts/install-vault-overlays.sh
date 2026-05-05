#!/usr/bin/env bash
# install-vault-overlays.sh — drop engine plug-in overlays into a target vault.
#
# Designed for second-brain-template-shaped vaults. Idempotent: re-running
# is safe and reports what's already in place vs newly installed.
#
# Usage:
#   ./scripts/install-vault-overlays.sh --vault /path/to/vault [--dry-run]
#
# Overlays installed:
#   - skills/vault/synth.md   — engine-aware insight synthesis skill
#   - skills/vault/crawl.md   — engine-aware URL → raw/ scrape skill
#   - .githooks/post-commit   — auto-reindex hook (fires after every commit)
#
# Pre-requisites for full function (skills are scaffolding either way):
#   - Vault has a 'skills/vault/' bundle conforming to the second-brain pattern.
#   - Vault is git-tracked (.git/ exists) for the post-commit hook to fire.
#   - vault-engine is on PATH for the post-commit hook to do its job;
#     the hook is graceful no-op when not.
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
install_file "$overlay_dir/githooks/post-commit" "$vault/.githooks/post-commit" "755"

echo
if [[ $dry_run -eq 1 ]]; then
  echo "Dry run complete. Re-run without --dry-run to apply."
else
  echo "Overlays installed."
  echo "If the vault is git-tracked and you want the post-commit hook active:"
  echo "  git -C \"$vault\" config core.hooksPath .githooks"
fi
