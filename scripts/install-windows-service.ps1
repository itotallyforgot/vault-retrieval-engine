<#
.SYNOPSIS
    Register vault-retrieval-engine as a Windows service via NSSM (P3 #2).

.DESCRIPTION
    Wraps `vault-engine serve` in a Windows service so the HTTP/MCP surface
    survives logout, sleep, and reboot. The service is configured with:
      - Auto-start at boot (SERVICE_AUTO_START)
      - Restart on failure with 30 s back-off
      - Stdout/stderr redirected to rotating log files (10 MB / file)
      - A useful description visible in `services.msc`

.PARAMETER VaultPath
    Absolute path to the second-brain vault root (the directory containing
    `wiki/`, `raw/`, etc.). Required.

.PARAMETER ServiceName
    Windows service name. Defaults to `vault-engine`. Pick something else
    only if you're running multiple engines side-by-side.

.PARAMETER LogDir
    Directory for stdout/stderr log files. Defaults to
    `$env:APPDATA\vault-retrieval\service-logs`.

.PARAMETER CacheDir
    Cache directory for embeddings.db + graph.pkl. Pass an absolute path
    that is readable + writable by `LocalSystem` (the service account) and
    by your user account (so `vault-engine reindex` can pre-warm it). The
    default `%APPDATA%\vault-retrieval` resolves to a different folder per
    account — `LocalSystem`'s `%APPDATA%` is
    `C:\Windows\System32\config\systemprofile\AppData\Roaming`, which is
    not the same cache your interactive shell uses. Without this param the
    service does a cold rebuild on first start. A shared E:\ path
    (e.g. `E:\Projects\.cache\vault-retrieval`) avoids that.

.PARAMETER VaultEngineExe
    Full path to `vault-engine.exe`. Auto-detected from PATH if omitted.

.PARAMETER Nssm
    Full path to `nssm.exe`. Auto-detected if omitted (PATH first, then
    `%LOCALAPPDATA%\Microsoft\WinGet\Packages\NSSM.NSSM_*\nssm-*\win64\`).

.PARAMETER BindAddr
    HTTP bind interface. Wired into the service via NSSM
    `AppEnvironmentExtra` as `VAULT_ENGINE_BIND_ADDR`, mirroring the
    macOS launchd plist (OGR-181). Defaults to `127.0.0.1` (loopback).
    Pass the Tailscale IP to expose over the tailnet.

.PARAMETER HttpPort
    HTTP listen port. Wired as `VAULT_ENGINE_HTTP_PORT`. Default 7842.

.PARAMETER HttpToken
    Bearer secret for `Authorization: Bearer <token>` on `/query` and
    `/graph/stats`. Wired as `VAULT_ENGINE_HTTP_TOKEN`. Required if
    `-BindAddr` is non-loopback — the engine refuses to bind without it.

.PARAMETER Start
    Start the service immediately after installation. Default: $true.

.PARAMETER DryRun
    Print every NSSM command that would run without executing any of them.
    Useful for previewing changes before elevating.

.EXAMPLE
    # Preview, no elevation required:
    .\install-windows-service.ps1 -VaultPath E:\Projects\second-brain -DryRun

.EXAMPLE
    # Real install (run from an elevated PowerShell):
    .\install-windows-service.ps1 -VaultPath E:\Projects\second-brain

.NOTES
    Service registration touches the SCM and requires Administrator. The
    script self-detects non-elevated context and emits a clear error rather
    than failing partway through. Uninstall via `uninstall-windows-service.ps1`.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$VaultPath,

    [string]$ServiceName = 'vault-engine',

    [string]$CacheDir,

    [string]$LogDir,

    [string]$VaultEngineExe,

    [string]$Nssm,

    [string]$BindAddr = '127.0.0.1',

    [int]$HttpPort = 7842,

    [string]$HttpToken,

    [bool]$Start = $true,

    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

function Resolve-Tool {
    param([string]$Name, [string]$Hint)
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Path }
    if ($Hint) {
        $found = Get-ChildItem $Hint -Recurse -Filter "$Name.exe" -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    throw "Could not locate $Name on PATH or under $Hint. Pass -$Name explicitly."
}

# --- Resolve tools and paths --------------------------------------------
if (-not $VaultEngineExe) {
    $VaultEngineExe = Resolve-Tool -Name 'vault-engine' -Hint $null
}
if (-not (Test-Path $VaultEngineExe)) {
    throw "vault-engine not found at: $VaultEngineExe"
}

if (-not $Nssm) {
    $Nssm = Resolve-Tool -Name 'nssm' `
        -Hint "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\NSSM.NSSM*"
}
if (-not (Test-Path $Nssm)) {
    throw "nssm not found at: $Nssm"
}

$VaultPath = (Resolve-Path -LiteralPath $VaultPath).Path
if (-not (Test-Path $VaultPath)) {
    throw "Vault path does not exist: $VaultPath"
}

if (-not $LogDir) {
    $LogDir = Join-Path $env:APPDATA 'vault-retrieval\service-logs'
}
if (-not $DryRun) {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
}

# Match the macOS install script: refuse non-loopback bind without a
# token. Engine's http_server.build_app enforces this too, but surfacing
# it at install time saves the operator a stderr-log read.
#
# Note: -HttpToken is the HS256 SIGNING SECRET, not a pre-shared bearer.
# Clients send JWTs signed with this secret. See README and docs/ios-shortcut.md.
$loopback = @('127.0.0.1', '::1', 'localhost')
if (($loopback -notcontains $BindAddr) -and (-not $HttpToken)) {
    throw "BindAddr '$BindAddr' is non-loopback; -HttpToken (HS256 signing secret) is required to prevent unauthenticated remote access. Generate one: uv run python -c `"import secrets; print(secrets.token_urlsafe(32))`""
}
if ($HttpPort -lt 1 -or $HttpPort -gt 65535) {
    throw "HttpPort must be 1-65535 (got $HttpPort)."
}

# --- Elevation check ----------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent() `
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin -and -not $DryRun) {
    Write-Error @"
This script must run from an elevated PowerShell to register a Windows
service. Either:
  1. Right-click PowerShell -> 'Run as administrator', then re-run.
  2. Use -DryRun to preview the commands without running them.
"@
    exit 2
}

# --- Service config -----------------------------------------------------
if ($CacheDir) {
    $CacheDir = (Resolve-Path -LiteralPath $CacheDir -ErrorAction SilentlyContinue).Path
    if (-not $CacheDir -or -not (Test-Path $CacheDir)) {
        throw "CacheDir does not exist: $CacheDir (create it first; the service account must be able to read+write it)"
    }
    $AppParameters = "serve --vault `"$VaultPath`" --cache `"$CacheDir`""
} else {
    Write-Warning "No -CacheDir passed. Service will use LocalSystem's %APPDATA% (cold rebuild on first start). Pass -CacheDir to share cache with your user account."
    $AppParameters = "serve --vault `"$VaultPath`""
}
$StdoutLog = Join-Path $LogDir "$ServiceName-stdout.log"
$StderrLog = Join-Path $LogDir "$ServiceName-stderr.log"

# AppEnvironmentExtra: NSSM accepts a single string of KEY=VALUE pairs
# separated by CR-LF (or just LF). The engine reads these via
# vault_engine.config.load_config — same precedence as the macOS
# LaunchAgent (env-var > function-arg > dataclass-default). The cache
# dir is intentionally left to the EngineConfig default unless the
# operator passed -CacheDir, in which case it's already wired via the
# CLI flag and we don't want env to override the explicit arg.
$envLines = @(
    "VAULT_ENGINE_BIND_ADDR=$BindAddr"
    "VAULT_ENGINE_HTTP_PORT=$HttpPort"
)
if ($HttpToken) {
    $envLines += "VAULT_ENGINE_HTTP_TOKEN=$HttpToken"
}
$AppEnvironmentExtra = $envLines -join "`r`n"

# Run-list: each item is a label + the nssm argv after "nssm". We collect
# them all up front so -DryRun can print the full plan before any state
# changes, and so a failure mid-install names the exact step that broke.
$Steps = @(
    @{ Label = "install service '$ServiceName'";   Args = @('install', $ServiceName, $VaultEngineExe) },
    @{ Label = "set AppParameters";                 Args = @('set', $ServiceName, 'AppParameters', $AppParameters) },
    @{ Label = "set AppDirectory";                  Args = @('set', $ServiceName, 'AppDirectory', $VaultPath) },
    @{ Label = "set AppEnvironmentExtra";           Args = @('set', $ServiceName, 'AppEnvironmentExtra', $AppEnvironmentExtra) },
    @{ Label = "set Start to AUTO";                 Args = @('set', $ServiceName, 'Start', 'SERVICE_AUTO_START') },
    @{ Label = "set Description";                   Args = @('set', $ServiceName, 'Description', 'Vault retrieval engine HTTP/MCP service (P2 service wrapper).') },
    @{ Label = "set DisplayName";                   Args = @('set', $ServiceName, 'DisplayName', 'Vault Retrieval Engine') },
    @{ Label = "set ObjectName to LocalSystem";     Args = @('set', $ServiceName, 'ObjectName', 'LocalSystem') },
    @{ Label = "set AppStdout";                     Args = @('set', $ServiceName, 'AppStdout', $StdoutLog) },
    @{ Label = "set AppStderr";                     Args = @('set', $ServiceName, 'AppStderr', $StderrLog) },
    @{ Label = "set AppRotateFiles=1";              Args = @('set', $ServiceName, 'AppRotateFiles', '1') },
    @{ Label = "set AppRotateOnline=1";             Args = @('set', $ServiceName, 'AppRotateOnline', '1') },
    @{ Label = "set AppRotateBytes=10MB";           Args = @('set', $ServiceName, 'AppRotateBytes', '10485760') },
    @{ Label = "set AppExit Default Restart";       Args = @('set', $ServiceName, 'AppExit', 'Default', 'Restart') },
    @{ Label = "set AppRestartDelay=30s";           Args = @('set', $ServiceName, 'AppRestartDelay', '30000') },
    @{ Label = "set AppThrottle=10s";               Args = @('set', $ServiceName, 'AppThrottle', '10000') }
)

$tokenSummary = if ($HttpToken) { "<set, $($HttpToken.Length) chars>" } else { "<unset; loopback-only>" }
Write-Host "Plan:"
Write-Host "  service:   $ServiceName"
Write-Host "  exe:       $VaultEngineExe"
Write-Host "  arguments: $AppParameters"
Write-Host "  cwd:       $VaultPath"
Write-Host "  logs:      $LogDir"
Write-Host "  nssm:      $Nssm"
Write-Host "  env vars:  VAULT_ENGINE_BIND_ADDR=$BindAddr"
Write-Host "             VAULT_ENGINE_HTTP_PORT=$HttpPort"
Write-Host "             VAULT_ENGINE_HTTP_TOKEN=$tokenSummary"
Write-Host ''

foreach ($step in $Steps) {
    $cmd = "$Nssm " + ($step.Args -join ' ')
    if ($DryRun) {
        Write-Host "  [dry-run] $($step.Label)"
        Write-Host "             $cmd"
        continue
    }
    Write-Host "  -> $($step.Label)"
    & $Nssm @($step.Args) | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "nssm step failed: $($step.Label) (exit $LASTEXITCODE)"
    }
}

if ($Start -and -not $DryRun) {
    Write-Host "  -> start service"
    & $Nssm start $ServiceName | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "nssm start failed (exit $LASTEXITCODE). Check $StderrLog for details."
    }
    Start-Sleep -Seconds 2
    & $Nssm status $ServiceName
    Write-Host ''
    Write-Host "Service '$ServiceName' is running."
    Write-Host "Health check:  curl http://127.0.0.1:7842/health"
    Write-Host "Logs:          $StdoutLog"
    Write-Host "                $StderrLog"
    Write-Host "Manage via:    services.msc, or '$Nssm <start|stop|restart|status> $ServiceName'"
} elseif ($DryRun) {
    Write-Host ''
    Write-Host "Dry run complete. Re-run without -DryRun (from elevated PowerShell) to apply."
}
