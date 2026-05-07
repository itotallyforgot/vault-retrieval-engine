#!/usr/bin/env bash
# Smoke coverage for dispatcher-aware overlay installation.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALLER="$ROOT_DIR/scripts/install-vault-overlays.sh"
DISPATCHER="$ROOT_DIR/overlays/githooks/post-commit"
ENGINE_PLUGIN="$ROOT_DIR/overlays/githooks/post-commit.d/10-vault-engine.sh"
LEGACY_HOOK="$ROOT_DIR/tests/fixtures/legacy-monolithic-post-commit"

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
assert_same "$DISPATCHER" "$absent_vault/.githooks/post-commit"
assert_same "$ENGINE_PLUGIN" "$absent_vault/.githooks/post-commit.d/10-vault-engine.sh"
test -x "$absent_vault/.githooks/post-commit"
test -x "$absent_vault/.githooks/post-commit.d/10-vault-engine.sh"

legacy_vault="$(make_vault legacy)"
mkdir -p "$legacy_vault/.githooks"
cp "$LEGACY_HOOK" "$legacy_vault/.githooks/post-commit"
"$INSTALLER" --vault "$legacy_vault" >/dev/null
assert_same "$DISPATCHER" "$legacy_vault/.githooks/post-commit"
test -f "$legacy_vault/.githooks/post-commit.legacy.bak"
assert_same "$ENGINE_PLUGIN" "$legacy_vault/.githooks/post-commit.d/10-vault-engine.sh"

custom_vault="$(make_vault custom)"
mkdir -p "$custom_vault/.githooks"
printf '#!/usr/bin/env bash\nprintf custom\n' > "$custom_vault/.githooks/post-commit"
"$INSTALLER" --vault "$custom_vault" > "$tmpdir/custom.out"
if ! grep -q "custom file; refusing to overwrite" "$tmpdir/custom.out"; then
  printf 'installer did not report custom dispatcher refusal\n' >&2
  cat "$tmpdir/custom.out" >&2
  exit 1
fi
grep -q "printf custom" "$custom_vault/.githooks/post-commit"
assert_same "$ENGINE_PLUGIN" "$custom_vault/.githooks/post-commit.d/10-vault-engine.sh"

printf 'smoke_install_vault_overlays_dispatcher.sh: OK\n'
