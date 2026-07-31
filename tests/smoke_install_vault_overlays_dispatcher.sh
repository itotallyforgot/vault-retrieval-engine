#!/usr/bin/env bash
# Smoke coverage for dispatcher-aware overlay installation.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALLER="$ROOT_DIR/scripts/install-vault-overlays.sh"
DISPATCHER="$ROOT_DIR/overlays/githooks/post-commit"
ENGINE_PLUGIN="$ROOT_DIR/overlays/githooks/post-commit.d/10-vault-engine.sh"
LEGACY_HOOK="$ROOT_DIR/tests/fixtures/legacy-monolithic-post-commit"
SYNTH_SKILL="$ROOT_DIR/overlays/skills/vault/synth.md"
CRAWL_SKILL="$ROOT_DIR/overlays/skills/vault/crawl.md"

tmpdir="$(mktemp -d)"
cleanup() {
  rm -rf "$tmpdir"
}
trap cleanup EXIT

make_vault() {
  local name="$1"
  local vault="$tmpdir/$name"
  mkdir -p "$vault"
  printf '%s\n' "$vault"
}

assert_same() {
  local expected="$1"
  local actual="$2"
  if ! cmp -s "$expected" "$actual"; then
    printf 'files differ:\n  expected=%s\n  actual=%s\n' "$expected" "$actual" >&2
    exit 1
  fi
}

absent_vault="$(make_vault absent)"
"$INSTALLER" --vault "$absent_vault" >/dev/null
assert_same "$SYNTH_SKILL" "$absent_vault/skills/vault/synth.md"
assert_same "$CRAWL_SKILL" "$absent_vault/skills/vault/crawl.md"
assert_same "$DISPATCHER" "$absent_vault/.githooks/post-commit"
assert_same "$ENGINE_PLUGIN" "$absent_vault/.githooks/post-commit.d/10-vault-engine.sh"
test -x "$absent_vault/.githooks/post-commit"
test -x "$absent_vault/.githooks/post-commit.d/10-vault-engine.sh"
# Nothing to migrate here, so no backup should be manufactured.
test ! -e "$absent_vault/.githooks/post-commit.legacy.bak"

legacy_vault="$(make_vault legacy)"
mkdir -p "$legacy_vault/.githooks"
cp "$LEGACY_HOOK" "$legacy_vault/.githooks/post-commit"
"$INSTALLER" --vault "$legacy_vault" > "$tmpdir/legacy.out"
if ! grep -q "\[migrate\]" "$tmpdir/legacy.out"; then
  printf 'installer did not report a legacy migration\n' >&2
  cat "$tmpdir/legacy.out" >&2
  exit 1
fi
assert_same "$DISPATCHER" "$legacy_vault/.githooks/post-commit"
# The backup must be the ORIGINAL hook, not just some file at that path.
assert_same "$LEGACY_HOOK" "$legacy_vault/.githooks/post-commit.legacy.bak"
assert_same "$ENGINE_PLUGIN" "$legacy_vault/.githooks/post-commit.d/10-vault-engine.sh"

custom_vault="$(make_vault custom)"
mkdir -p "$custom_vault/.githooks"
printf '#!/usr/bin/env bash\nprintf custom\n' > "$custom_vault/.githooks/post-commit"
cp "$custom_vault/.githooks/post-commit" "$tmpdir/custom-hook.orig"
"$INSTALLER" --vault "$custom_vault" > "$tmpdir/custom.out"
if ! grep -q "custom file; refusing to overwrite" "$tmpdir/custom.out"; then
  printf 'installer did not report custom dispatcher refusal\n' >&2
  cat "$tmpdir/custom.out" >&2
  exit 1
fi
# Byte-equality, not a substring grep: an append would pass a grep.
assert_same "$tmpdir/custom-hook.orig" "$custom_vault/.githooks/post-commit"
test ! -e "$custom_vault/.githooks/post-commit.legacy.bak"
assert_same "$ENGINE_PLUGIN" "$custom_vault/.githooks/post-commit.d/10-vault-engine.sh"

# Re-running the installer on an already-installed vault must be a pure no-op.
rerun_vault="$(make_vault rerun)"
"$INSTALLER" --vault "$rerun_vault" >/dev/null
"$INSTALLER" --vault "$rerun_vault" > "$tmpdir/rerun.out"
if grep -qE '\[new\]|\[update\]|\[migrate\]' "$tmpdir/rerun.out"; then
  printf 'second installer run was not idempotent\n' >&2
  cat "$tmpdir/rerun.out" >&2
  exit 1
fi

printf 'smoke_install_vault_overlays_dispatcher.sh: OK\n'
