#!/usr/bin/env bash
# Scan staged files for blocked terms before they leak into a public repo.
# Used by pre-commit. Receives staged file paths as args.
#
# Maintenance:
#   - Edit BLOCKED_TERMS in this file for literal-string blocks (NDA names,
#     employer-internal terms, federal client names).
#   - Add personal-info regex patterns (phone, address fragments, alt emails)
#     to scripts/blocked-terms-personal.txt — gitignored, never committed.

set -euo pipefail

BLOCKED_TERMS=(
  # NDA-protected client work
  "REDACTED"
  "REDACTED"
  # Current employer
  "REDACTED"
  # Federal client lineage (sensitive contexts)
  "REDACTED"
  "REDACTED"
  "REDACTED"
  "REDACTED"
)

PERSONAL_PATTERNS_FILE="scripts/blocked-terms-personal.txt"

found_match=0

for file in "$@"; do
  [ -f "$file" ] || continue

  for term in "${BLOCKED_TERMS[@]}"; do
    if grep -inF "$term" "$file" >/dev/null 2>&1; then
      echo "BLOCKED: '$term' found in $file"
      grep -inF "$term" "$file" | head -3 | sed 's/^/  /'
      found_match=1
    fi
  done

  if [ -f "$PERSONAL_PATTERNS_FILE" ]; then
    while IFS= read -r pattern; do
      [[ -z "$pattern" ]] && continue
      [[ "$pattern" =~ ^# ]] && continue
      if grep -nE "$pattern" "$file" >/dev/null 2>&1; then
        echo "BLOCKED: personal-info pattern '$pattern' matched in $file"
        found_match=1
      fi
    done < "$PERSONAL_PATTERNS_FILE"
  fi
done

if [ "$found_match" -eq 1 ]; then
  echo ""
  echo "Sensitive terms detected. Redact before committing."
  echo "If false-positive, review and bypass with: git commit --no-verify"
  exit 1
fi
