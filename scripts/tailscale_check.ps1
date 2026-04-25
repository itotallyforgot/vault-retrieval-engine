# Verify tailnet IP exists before binding HTTP server. Exits 1 if no 100.x.y.z found.
$tsIp = (Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -match "^100\." } |
    Select-Object -First 1).IPAddress
if (-not $tsIp) {
    Write-Error "No tailnet IP found. Is Tailscale up? Run: tailscale up"
    exit 1
}
Write-Host "Tailnet IP: $tsIp"
