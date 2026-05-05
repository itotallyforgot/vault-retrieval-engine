# Launches the engine in service mode, bound to the tailnet IP only.
# Requires: vault-engine installed, tailscale up.
# Env vars (optional): VAULT (vault root), CACHE (cache dir).
$ErrorActionPreference = "Stop"

# 1. Verify tailnet IP exists
$tailscaleScript = Join-Path $PSScriptRoot "tailscale_check.ps1"
& $tailscaleScript
if ($LASTEXITCODE -ne 0) { exit 1 }

# 2. Resolve vault path
$vault = $env:VAULT
if (-not $vault) { $vault = Join-Path $env:USERPROFILE "Projects\markdown-vault" }
if (-not (Test-Path $vault)) {
    Write-Error "Vault not found: $vault"
    exit 1
}

# 3. Optional cache override
$cacheArgs = @()
if ($env:CACHE) {
    $cacheArgs = @("--cache", $env:CACHE)
}

# 4. Launch via uv (serve takes its own --vault; top-level callback skips setup
# when invoked_subcommand is "serve").
Write-Host "Starting vault-engine serve with vault $vault"
uv run vault-engine serve --vault $vault @cacheArgs
