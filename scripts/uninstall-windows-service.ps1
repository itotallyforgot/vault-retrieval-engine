<#
.SYNOPSIS
    Stop and remove the vault-retrieval-engine Windows service (P3 #2).

.DESCRIPTION
    Counterpart to install-windows-service.ps1. Stops the service if
    running, then removes it from the Service Control Manager. Idempotent:
    safe to run when the service doesn't exist.

.PARAMETER ServiceName
    Windows service name. Defaults to `vault-engine`.

.PARAMETER Nssm
    Full path to `nssm.exe`. Auto-detected if omitted.

.PARAMETER KeepLogs
    Preserve the service log directory after removal. Default: $true. Pass
    `-KeepLogs:$false` to delete logs as well.

.EXAMPLE
    # From elevated PowerShell:
    .\uninstall-windows-service.ps1
#>

[CmdletBinding()]
param(
    [string]$ServiceName = 'vault-engine',
    [string]$Nssm,
    [bool]$KeepLogs = $true
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
    throw "Could not locate $Name. Pass -$Name explicitly."
}

if (-not $Nssm) {
    $Nssm = Resolve-Tool -Name 'nssm' `
        -Hint "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\NSSM.NSSM*"
}

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent() `
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Error "Run from elevated PowerShell. Removing a Windows service touches the SCM."
    exit 2
}

# Check existence first so we can be idempotent.
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (-not $svc) {
    Write-Host "Service '$ServiceName' not registered. Nothing to do."
    exit 0
}

if ($svc.Status -ne 'Stopped') {
    Write-Host "  -> stopping service"
    & $Nssm stop $ServiceName confirm | Out-Null
    Start-Sleep -Seconds 2
}

Write-Host "  -> removing service"
& $Nssm remove $ServiceName confirm | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "nssm remove failed (exit $LASTEXITCODE)"
}

if (-not $KeepLogs) {
    $LogDir = Join-Path $env:APPDATA 'vault-retrieval\service-logs'
    if (Test-Path $LogDir) {
        Write-Host "  -> removing logs at $LogDir"
        Remove-Item -Recurse -Force $LogDir
    }
}

Write-Host "Service '$ServiceName' removed."
