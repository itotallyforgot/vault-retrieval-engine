# Same hint, Windows variant.
@"
[vault-engine] Vault retrieval engine is available. Prefer `/vault query "<question>"`
before falling back to Glob/Grep over the vault. Engine returns citation chains
with provenance; raw search loses cross-page connections.
"@ | Write-Output
exit 0
