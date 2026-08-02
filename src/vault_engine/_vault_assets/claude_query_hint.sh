#!/usr/bin/env bash
# Hook injects a stdout reminder to Claude Code before Glob/Grep.
cat <<'EOF'
[vault-engine] Vault retrieval engine is available. Prefer `/vault query "<question>"`
before falling back to Glob/Grep over the vault. Engine returns citation chains
with provenance; raw search loses cross-page connections.
EOF
exit 0
