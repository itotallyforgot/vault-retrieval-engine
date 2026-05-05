"""vault-engine — local semantic retrieval over personal markdown vaults.

Public package. The CLI entrypoint is ``vault_engine.cli:app``. Most
consumers should use the CLI or one of the service surfaces (MCP stdio,
HTTP/JSON) rather than importing modules directly.

The architecture follows from a local-only constraint: no external API,
no telemetry, all retrieval/embedding/storage on local disk. Citation
chains carry every result back to its source pages for auditable
retrieval.
"""
