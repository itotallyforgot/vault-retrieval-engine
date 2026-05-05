# Running vault-retrieval-engine as a Windows service (P3 #2)

`vault-engine serve` runs a long-lived FastAPI HTTP server (default
`127.0.0.1:7842`) plus an MCP stdio surface. Wrapping it in a Windows
service via [NSSM](https://nssm.cc) makes the process survive logout,
sleep, and reboot — which is the difference between "engine is up
whenever I'm logged in" and "engine is up because the box is on."

## What gets configured

The install script wires up a service with:

| Aspect | Value |
|---|---|
| Service name | `vault-engine` (override with `-ServiceName`) |
| Display name | "Vault Retrieval Engine" |
| Start mode | `SERVICE_AUTO_START` (boots with the OS) |
| Account | `LocalSystem` |
| Restart on failure | `Default Restart` with 30 s back-off |
| Throttle (don't restart-loop) | 10 s |
| Stdout/Stderr | rotating logs in `%APPDATA%\vault-retrieval\service-logs\` (10 MB / file) |

The service runs `<vault-engine.exe path> --vault <vault path> serve`.
The HTTP port and bind address come from `EngineConfig` (default
`127.0.0.1:7842`).

## Prerequisites

1. **NSSM installed.** `winget install --id NSSM.NSSM` (then restart
   shell so the alias picks up). The install script auto-detects NSSM at
   `%LOCALAPPDATA%\Microsoft\WinGet\Packages\NSSM.NSSM_*\nssm-*\win64\nssm.exe`,
   so you don't strictly need it on PATH.
2. **`vault-engine` on PATH.** Verify with `vault-engine --help`. The
   install script auto-detects it from PATH.
3. **Admin rights.** Service registration touches the SCM. Run the
   install/uninstall scripts from an elevated PowerShell.

## Install

```powershell
# Preview without elevation:
.\scripts\install-windows-service.ps1 -VaultPath /path/to/vault -DryRun

# Real install (elevated PowerShell):
.\scripts\install-windows-service.ps1 -VaultPath /path/to/vault
```

The script will:

1. Resolve `vault-engine.exe` and `nssm.exe`.
2. Print the install plan (service name, exe, args, log paths).
3. Run the NSSM commands one at a time (each labelled, so a mid-install
   failure names the exact step that broke).
4. Start the service and print a status check.

After install, verify:

```powershell
nssm status vault-engine            # should print SERVICE_RUNNING
curl http://127.0.0.1:7842/health   # should return 200
```

Logs:

```
%APPDATA%\vault-retrieval\service-logs\vault-engine-stdout.log
%APPDATA%\vault-retrieval\service-logs\vault-engine-stderr.log
```

## Manage

```powershell
nssm start vault-engine
nssm stop vault-engine
nssm restart vault-engine
nssm status vault-engine
nssm edit vault-engine               # GUI to tweak config
```

`services.msc` works too — search for "Vault Retrieval Engine".

## Uninstall

```powershell
.\scripts\uninstall-windows-service.ps1
```

Idempotent — safe to run if the service doesn't exist. Pass
`-KeepLogs:$false` to also delete the log directory.

## Troubleshooting

**Service starts then stops immediately.** Check
`%APPDATA%\vault-retrieval\service-logs\vault-engine-stderr.log` — the
most common cause is a config issue (vault path doesn't exist, port
already in use, mismatched embedding model fingerprint in the cache).

**`vault-engine reindex --force` to clear a model fingerprint mismatch.**
Run this *before* the service starts; the service won't accept the
mismatch on its own.

**Port 7842 already in use.** Either stop whatever's on it, or set a
different port via `EngineConfig.http_port` in the engine repo and
reinstall the service so NSSM picks up the new entry-point.

**"This script must run from an elevated PowerShell" error.** Right-click
PowerShell → "Run as administrator", then re-run. Or use `-DryRun` to
preview.

**Hook + service double-reindex.** The `post-commit` git hook (P3 #3)
runs `vault-engine reindex` on every vault commit. If the service is
also running and watching for file changes, both can try to rebuild at
once. The hook's atomic-mkdir mutex serialises across processes, so
this is safe — one will skip with "lock held" in the log. The
serialisation cost is the only overhead.
