#!/usr/bin/env bash
# demo-worktree-concurrency.sh
#
# Demonstrates the worktree-isolated concurrent agent execution policy
# (Linear ISSUE-N, markdown-vault internal-notes).
#
# Creates two scratch worktrees in a temp git repo, edits disjoint files
# in each (simulating two implementation agents working in parallel),
# merges both branches into a third, and verifies a clean merge with
# both edits present.
#
# Idempotent. Self-cleaning. Exits 0 on success.

set -euo pipefail

WORKDIR=$(mktemp -d -t worktree-demo.XXXXXX)
trap 'rm -rf "$WORKDIR"' EXIT

cd "$WORKDIR"
git init -q -b main
git config user.email "demo@example.com"
git config user.name "Worktree Demo"

# Seed with two disjoint files
mkdir -p src docs
cat > src/auth.py <<'PY'
def login(user, password):
    return user is not None
PY
cat > docs/README.md <<'MD'
# Demo Project
PY for auth.
MD
git add .
git commit -q -m "init: seed disjoint auth + README files"

# Confirm .worktrees/ is gitignored before parallel dispatch
echo ".worktrees/" > .gitignore
git add .gitignore && git commit -q -m "chore: gitignore worktree dir"

# Spawn TWO parallel worktrees on disjoint slices.
# Slice A — touches src/auth.py only.
# Slice B — touches docs/README.md only.
# Naming follows the policy: .worktrees/<scope>-<slice>, branch <scope>/<slice>.
git worktree add -q .worktrees/demo-slice-a -b demo/slice-a
git worktree add -q .worktrees/demo-slice-b -b demo/slice-b

# Simulate two agents working in parallel: each edits its own file in
# its own worktree, no shared writes.
(
  cd .worktrees/demo-slice-a
  cat > src/auth.py <<'PY'
def login(user, password):
    if not user or not password:
        raise ValueError("user and password required")
    return True
PY
  git add src/auth.py
  git commit -q -m "feat(auth): require both user + password"
) &
PID_A=$!

(
  cd .worktrees/demo-slice-b
  cat > docs/README.md <<'MD'
# Demo Project

Auth lives in `src/auth.py`. See ADR-001 for the credential model.
MD
  git add docs/README.md
  git commit -q -m "docs: expand README with auth pointer"
) &
PID_B=$!

wait $PID_A
wait $PID_B

# Integration: merge both into demo/integrate
git checkout -q -b demo/integrate
git merge -q --no-ff demo/slice-a -m "merge: slice-a"
git merge -q --no-ff demo/slice-b -m "merge: slice-b"

# Verify both edits present, no conflict markers, file integrity intact.
grep -q "user and password required" src/auth.py
grep -q "Auth lives in" docs/README.md
if grep -RInE '<{7}|={7}|>{7}' . --exclude-dir=.git --exclude-dir=.worktrees 2>/dev/null; then
  echo "FAIL: conflict markers found" >&2
  exit 1
fi

# Cleanup per policy. (Skipping the integrate branch since it's the
# current HEAD; demo/slice-a + demo/slice-b are merged so safe to remove.
# The temp repo itself is nuked by the EXIT trap.)
git worktree remove .worktrees/demo-slice-a
git worktree remove .worktrees/demo-slice-b
git branch -D demo/slice-a demo/slice-b >/dev/null

echo "PASS: two disjoint worktrees merged clean — ISSUE-N acceptance #2 satisfied."
