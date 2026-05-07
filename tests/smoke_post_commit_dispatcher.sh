#!/usr/bin/env bash
# Smoke coverage for the overlay post-commit dispatcher.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DISPATCHER="$ROOT_DIR/overlays/githooks/post-commit"

tmpdir="$(mktemp -d)"
cleanup() {
  rm -rf "$tmpdir"
}
trap cleanup EXIT

git -C "$tmpdir" init -q
mkdir -p "$tmpdir/.githooks/post-commit.d"
cp "$DISPATCHER" "$tmpdir/.githooks/post-commit"
chmod 755 "$tmpdir/.githooks/post-commit"

cat > "$tmpdir/.githooks/post-commit.d/10-first.sh" <<'HOOK'
#!/usr/bin/env bash
printf 'first\n' >> "$VAULT_ROOT/order.log"
HOOK

cat > "$tmpdir/.githooks/post-commit.d/20-fail.sh" <<'HOOK'
#!/usr/bin/env bash
printf 'fail\n' >> "$VAULT_ROOT/order.log"
exit 42
HOOK

cat > "$tmpdir/.githooks/post-commit.d/30-last.sh" <<'HOOK'
#!/usr/bin/env bash
printf 'last\n' >> "$VAULT_ROOT/order.log"
HOOK

cat > "$tmpdir/.githooks/post-commit.d/40-not-executable.sh" <<'HOOK'
#!/usr/bin/env bash
printf 'not-executable\n' >> "$VAULT_ROOT/order.log"
HOOK

cat > "$tmpdir/.githooks/post-commit.d/.50-hidden.sh" <<'HOOK'
#!/usr/bin/env bash
printf 'hidden\n' >> "$VAULT_ROOT/order.log"
HOOK

chmod 755 \
  "$tmpdir/.githooks/post-commit.d/10-first.sh" \
  "$tmpdir/.githooks/post-commit.d/20-fail.sh" \
  "$tmpdir/.githooks/post-commit.d/30-last.sh" \
  "$tmpdir/.githooks/post-commit.d/.50-hidden.sh"

if ! stderr="$(cd "$tmpdir" && ./.githooks/post-commit 2>&1)"; then
  printf 'dispatcher should exit 0 even when a plug-in fails\n%s\n' "$stderr" >&2
  exit 1
fi

expected_order="$(printf 'first\nfail\nlast\n')"
actual_order="$(cat "$tmpdir/order.log")"
if [[ "$actual_order" != "$expected_order" ]]; then
  printf 'unexpected dispatch order:\n%s\n' "$actual_order" >&2
  exit 1
fi

if [[ "$stderr" != *"post-commit: plug-in failed: 20-fail.sh (exit 42)"* ]]; then
  printf 'dispatcher did not report the failing plug-in exit code:\n%s\n' "$stderr" >&2
  exit 1
fi

printf 'smoke_post_commit_dispatcher.sh: OK\n'
