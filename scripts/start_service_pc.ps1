# Launches the engine in service mode, bound to the tailnet IP only.
# Requires: vault-engine installed, tailscale up, ENGINE_CONFIG env var pointing at TOML.
$ErrorActionPreference = "Stop"

# 1. Verify tailnet IP exists
$tailscaleScript = Join-Path $PSScriptRoot "tailscale_check.ps1"
& $tailscaleScript
if ($LASTEXITCODE -ne 0) { exit 1 }

# 2. Resolve config path
$cfg = $env:ENGINE_CONFIG
if (-not $cfg) { $cfg = Join-Path $env:USERPROFILE ".config\vault-engine\config.toml" }
if (-not (Test-Path $cfg)) {
    Write-Error "Config not found: $cfg"
    exit 1
}

# 3. Launch via uv
Write-Host "Starting vault-engine serve with config $cfg"
uv run vault-engine serve --config $cfg
