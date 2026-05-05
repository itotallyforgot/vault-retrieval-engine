#!/usr/bin/env bash
# test-overlay-install.sh — smoke harness for install-vault-overlays.sh.
#
# Validates the four documented behaviors of the installer against
# synthetic vaults built with `mktemp -d` + `git init`. Each case
# is independent and self-cleaning; failure of one does not abort the
# others, so a single run reports every regression.
#
# Cases:
#   1. case-fresh             — empty vault; full clean install.
#   2. case-legacy-monolithic — vault has the pre-refactor monolithic
#                                hook. Installer auto-migrates,
#                                producing .legacy.bak + dispatcher.
#   3. case-custom-hook       — vault has a hand-edited post-commit.
#                                Installer must REFUSE to overwrite,
#                                still installs .d/ piece.
#   4. case-rerun-idempotent  — second run is a no-op (all [skip]).
#
# Exits 0 iff every case passes.

set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALLER="$REPO_ROOT/scripts/install-vault-overlays.sh"
OVERLAY_DIR="$REPO_ROOT/overlays"
DISPATCHER_SRC="$OVERLAY_DIR/githooks/post-commit"
ENGINE_PIECE_SRC="$OVERLAY_DIR/githooks/post-commit.d/10-vault-engine.sh"
SYNTH_SRC="$OVERLAY_DIR/skills/vault/synth.md"
CRAWL_SRC="$OVERLAY_DIR/skills/vault/crawl.md"

# Legacy monolithic hook content — captured from commit 3aa35ad
# (engine repo overlays/githooks/post-commit, before the dispatcher
# refactor in PR #13). Regenerate via:
#   git show 3aa35ad:overlays/githooks/post-commit
LEGACY_HOOK_CONTENT='#!/usr/bin/env bash
# vault-engine post-commit reindex hook (overlay).
#
# Installed by `scripts/install-vault-overlays.sh` into a target vault'\''s
# .githooks/post-commit. Fires `vault-engine reindex` after every commit
# in the vault so the engine'\''s vec store + graph stay current.
#
# Designed to be a graceful no-op when:
#   - vault-engine is not on PATH (engine uninstalled)
#   - the engine'\''s reindex command exits non-zero (silent in-background)
#
# This means the hook is safe to keep installed even after engine removal,
# and the vault stays usable in standalone mode.

set -u

# Find the vault root via git (the hook runs from .git/hooks).
VAULT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
[ -z "$VAULT_ROOT" ] && exit 0

# Background reindex; mute output so the commit operation isn'\''t slowed
# or noisy. Failure is non-fatal — vault is the source of truth.
if command -v vault-engine >/dev/null 2>&1; then
  ( vault-engine --vault "$VAULT_ROOT" reindex >/dev/null 2>&1 & ) || true
fi

exit 0'

# --- counters --------------------------------------------------------------
PASS=0
FAIL=0
FAIL_LINES=()

ok()   { PASS=$((PASS+1));  echo "  ✓ $1"; }
nope() { FAIL=$((FAIL+1));  echo "  ✗ $1" >&2; FAIL_LINES+=("$1"); }

assert_eq() {  # $1=label  $2=actual  $3=expected
  if [[ "$2" == "$3" ]]; then ok "$1"; else nope "$1 — expected $(printf '%q' "$3"), got $(printf '%q' "$2")"; fi
}

assert_file_eq() {  # $1=label  $2=path  $3=reference-path
  if cmp -s "$2" "$3"; then ok "$1"; else nope "$1 — $2 differs from $3"; fi
}

assert_file_present() {  # $1=label  $2=path
  if [[ -f "$2" ]]; then ok "$1"; else nope "$1 — missing $2"; fi
}

assert_file_absent() {  # $1=label  $2=path
  if [[ ! -e "$2" ]]; then ok "$1"; else nope "$1 — unexpected $2"; fi
}

assert_grep() {  # $1=label  $2=pattern  $3=file
  if grep -q -- "$2" "$3"; then ok "$1"; else nope "$1 — pattern $(printf '%q' "$2") missing in $3"; fi
}

# --- runner ----------------------------------------------------------------

run_installer() {
  # Prints all output (stdout + stderr) to a log file; returns rc.
  local vault="$1"
  local log="$2"
  bash "$INSTALLER" --vault "$vault" >"$log" 2>&1
  return $?
}

setup_vault() {
  # Create a synthetic vault: skills/vault dir + git init.
  local vault="$1"
  mkdir -p "$vault/skills/vault"
  ( cd "$vault" && git init -q && git config user.email t@t && git config user.name t )
}

# --- case 1: fresh vault ---------------------------------------------------

case_fresh() {
  echo "== case-fresh =="
  local v
  v=$(mktemp -d)
  setup_vault "$v"
  local log; log=$(mktemp)
  run_installer "$v" "$log"
  rc=$?
  assert_eq "exit code 0"             "$rc" "0"
  assert_file_eq  "synth.md installed"     "$v/skills/vault/synth.md"          "$SYNTH_SRC"
  assert_file_eq  "crawl.md installed"     "$v/skills/vault/crawl.md"          "$CRAWL_SRC"
  assert_file_eq  "dispatcher installed"   "$v/.githooks/post-commit"          "$DISPATCHER_SRC"
  assert_file_eq  "engine piece installed" "$v/.githooks/post-commit.d/10-vault-engine.sh" "$ENGINE_PIECE_SRC"
  assert_file_absent "no .legacy.bak (was no legacy)"  "$v/.githooks/post-commit.legacy.bak"
  rm -rf "$v" "$log"
}

# --- case 2: legacy monolithic hook ----------------------------------------

case_legacy_monolithic() {
  echo "== case-legacy-monolithic =="
  local v
  v=$(mktemp -d)
  setup_vault "$v"
  mkdir -p "$v/.githooks"
  printf '%s\n' "$LEGACY_HOOK_CONTENT" > "$v/.githooks/post-commit"
  chmod 755 "$v/.githooks/post-commit"
  local legacy_sha; legacy_sha=$(shasum -a 256 "$v/.githooks/post-commit" | awk '{print $1}')
  assert_eq "pre-seeded legacy SHA matches expected" \
    "$legacy_sha" "b68cfa92f1266193ecb47c88035ac1358c361c527dcf5b465bc4959bba02fb69"

  local log; log=$(mktemp)
  run_installer "$v" "$log"
  rc=$?
  assert_eq "exit code 0"                      "$rc" "0"
  assert_grep "[migrate] in installer log"     "\[migrate\]"           "$log"
  assert_file_present "post-commit.legacy.bak" "$v/.githooks/post-commit.legacy.bak"
  # legacy.bak content matches what we seeded (modulo trailing newline)
  local bak_sha; bak_sha=$(shasum -a 256 "$v/.githooks/post-commit.legacy.bak" | awk '{print $1}')
  assert_eq ".legacy.bak SHA matches seeded" \
    "$bak_sha" "b68cfa92f1266193ecb47c88035ac1358c361c527dcf5b465bc4959bba02fb69"
  assert_file_eq  "post-commit replaced with dispatcher" "$v/.githooks/post-commit"          "$DISPATCHER_SRC"
  assert_file_eq  "engine piece installed"               "$v/.githooks/post-commit.d/10-vault-engine.sh" "$ENGINE_PIECE_SRC"
  rm -rf "$v" "$log"
}

# --- case 3: custom hand-edited hook ---------------------------------------

case_custom_hook() {
  echo "== case-custom-hook =="
  local v
  v=$(mktemp -d)
  setup_vault "$v"
  mkdir -p "$v/.githooks"
  cat > "$v/.githooks/post-commit" <<'EOF'
#!/usr/bin/env bash
# I am a custom hook the operator hand-wrote.
echo "custom hook" >&2
EOF
  chmod 755 "$v/.githooks/post-commit"
  local original_sha; original_sha=$(shasum -a 256 "$v/.githooks/post-commit" | awk '{print $1}')

  local log; log=$(mktemp)
  run_installer "$v" "$log"
  rc=$?
  assert_eq "exit code 0"                       "$rc" "0"
  assert_grep "[skip] (custom file)"            "custom file"           "$log"
  assert_file_absent "no .legacy.bak"           "$v/.githooks/post-commit.legacy.bak"
  local after_sha; after_sha=$(shasum -a 256 "$v/.githooks/post-commit" | awk '{print $1}')
  assert_eq "custom post-commit untouched"      "$after_sha" "$original_sha"
  # Plug-in piece STILL installed — that's the contract.
  assert_file_eq "engine piece installed despite skip" \
    "$v/.githooks/post-commit.d/10-vault-engine.sh" "$ENGINE_PIECE_SRC"
  rm -rf "$v" "$log"
}

# --- case 4: re-run is idempotent ------------------------------------------

case_rerun_idempotent() {
  echo "== case-rerun-idempotent =="
  local v
  v=$(mktemp -d)
  setup_vault "$v"
  local log1; log1=$(mktemp)
  local log2; log2=$(mktemp)
  run_installer "$v" "$log1"
  run_installer "$v" "$log2"
  # Second run: every install_file should report [skip] and dispatcher
  # should also be [skip].
  if grep -E "^\s*\[(new|update|migrate)\]" "$log2" >/dev/null; then
    nope "second run wasn't fully idempotent — saw new/update/migrate in log:"
    grep -E "^\s*\[(new|update|migrate)\]" "$log2" >&2
  else
    ok "second run is fully idempotent (only [skip] entries)"
  fi
  # And cmp should still match originals.
  assert_file_eq  "dispatcher unchanged"   "$v/.githooks/post-commit"          "$DISPATCHER_SRC"
  assert_file_eq  "engine piece unchanged" "$v/.githooks/post-commit.d/10-vault-engine.sh" "$ENGINE_PIECE_SRC"
  rm -rf "$v" "$log1" "$log2"
}

# --- main ------------------------------------------------------------------

if [[ ! -x "$INSTALLER" ]]; then
  echo "ERROR: installer not found or not executable: $INSTALLER" >&2
  exit 2
fi
if [[ ! -f "$DISPATCHER_SRC" || ! -f "$ENGINE_PIECE_SRC" ]]; then
  echo "ERROR: overlay sources missing — is this engine repo on a slice-1+ branch?" >&2
  exit 2
fi

case_fresh
case_legacy_monolithic
case_custom_hook
case_rerun_idempotent

echo
echo "summary: $PASS passed, $FAIL failed"
if [[ $FAIL -gt 0 ]]; then
  echo "failures:" >&2
  for line in "${FAIL_LINES[@]}"; do echo "  - $line" >&2; done
  exit 1
fi
exit 0
