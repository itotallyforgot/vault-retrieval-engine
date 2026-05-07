#!/usr/bin/env bash
# Focused smoke coverage for scripts/check-blocked-terms.sh.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCANNER="$ROOT_DIR/scripts/check-blocked-terms.sh"
PERSONAL_PATTERNS_FILE="$ROOT_DIR/scripts/blocked-terms-personal.txt"

tmpdir="$(mktemp -d)"
backup_personal="$tmpdir/blocked-terms-personal.txt.backup"
had_personal=0

cleanup() {
  if [ "$had_personal" -eq 1 ]; then
    cp "$backup_personal" "$PERSONAL_PATTERNS_FILE"
  else
    rm -f "$PERSONAL_PATTERNS_FILE"
  fi
  rm -rf "$tmpdir"
}
trap cleanup EXIT

if [ -f "$PERSONAL_PATTERNS_FILE" ]; then
  had_personal=1
  cp "$PERSONAL_PATTERNS_FILE" "$backup_personal"
fi

assert_pass() {
  local name="$1"
  shift

  if ! output="$("$SCANNER" "$@" 2>&1)"; then
    printf 'FAIL: %s\n%s\n' "$name" "$output" >&2
    exit 1
  fi
}

assert_fail() {
  local name="$1"
  local expected="$2"
  shift 2

  if output="$("$SCANNER" "$@" 2>&1)"; then
    printf 'FAIL: %s unexpectedly passed\n' "$name" >&2
    exit 1
  fi

  if [[ "$output" != *"$expected"* ]]; then
    printf 'FAIL: %s did not report %s\n%s\n' "$name" "$expected" "$output" >&2
    exit 1
  fi
}

false_positive_file="$tmpdir/false-positive-substrings.txt"
true_positive_file="$tmpdir/true-positive-term.txt"
employer_term_file="$tmpdir/employer-term.txt"
personal_info_file="$tmpdir/personal-info.txt"
blocked_term="$(printf 'N%sA' "S")"
employer_term="$(printf 'G%sng' "o")"
personal_pattern="$(printf '555-%s' '[0-9]{4}')"
personal_value="$(printf '555-%s' '1212')"

cat > "$false_positive_file" <<'EOF'
The unsafe-host guard is wrapped in a transaction.
This copy mirrors the Apache License wording: where such license applies only.
EOF

printf 'This line mentions %s as a standalone sensitive term.\n' "$blocked_term" > "$true_positive_file"

printf 'This line mentions %s as a standalone sensitive term.\n' "$employer_term" > "$employer_term_file"

printf 'Call the private line at %s.\n' "$personal_value" > "$personal_info_file"

printf '%s\n' "$personal_pattern" > "$PERSONAL_PATTERNS_FILE"

assert_pass "blocked-term substrings are allowed" "$false_positive_file"
assert_pass "scanner can scan its own blocked-term definitions" "$SCANNER"
assert_fail "literal blocked term still fails" "BLOCKED: '$blocked_term'" "$true_positive_file"
assert_fail "employer/client blocked term still fails" "BLOCKED: '$employer_term'" "$employer_term_file"
assert_fail "personal-info regex still fails" "BLOCKED: personal-info pattern" "$personal_info_file"

printf 'smoke_check_blocked_terms.sh: OK\n'
