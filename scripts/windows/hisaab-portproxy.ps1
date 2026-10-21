# Hisaab phone access, FALLBACK path (OPERATIONS.md, "Phone access"): only if mirrored networking doesn't work.
# Forwards Windows port 8765 to WSL's current address and opens the port on private networks.
# WSL's address changes on every reboot, so run this at logon, elevated (OPERATIONS.md has the schtasks line).
#Requires -RunAsAdministrator

$port = 8765
$distro = "Ubuntu"
$wslIp = ((wsl.exe -d $distro hostname -I) -split "\s+" | Where-Object { $_ -match '^\d+\.\d+\.\d+\.\d+$' } | Select-Object -First 1)
if (-not $wslIp) { Write-Error "Couldn't read WSL's address (is the $distro distro installed?)"; exit 1 }

netsh interface portproxy delete v4tov4 listenport=$port listenaddress=0.0.0.0 2>$null | Out-Null
netsh interface portproxy add v4tov4 listenport=$port listenaddress=0.0.0.0 connectport=$port connectaddress=$wslIp

if (-not (Get-NetFirewallRule -DisplayName "Hisaab 8765" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName "Hisaab 8765" -Direction Inbound -Action Allow -Protocol TCP -LocalPort $port -Profile Private | Out-Null
}
Write-Output "Port $port now forwards to WSL at $wslIp"
