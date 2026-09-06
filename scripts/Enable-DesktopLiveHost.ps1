param()

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this host preparation script from an Administrator PowerShell.'
}

# Keep the desktop host awake while connected to AC power. Battery policy is
# intentionally untouched so a laptop can still protect itself when unplugged.
& powercfg.exe /change standby-timeout-ac 0
if ($LASTEXITCODE -ne 0) { throw 'Failed to disable AC sleep.' }
& powercfg.exe /change hibernate-timeout-ac 0
if ($LASTEXITCODE -ne 0) { throw 'Failed to disable AC hibernation.' }

Set-Service -Name W32Time -StartupType Automatic
Start-Service -Name W32Time
& w32tm.exe /resync /force
if ($LASTEXITCODE -ne 0) { throw 'Windows Time resynchronization failed.' }

Write-Host 'Signal Guild DESKTOP_LIVE host prerequisites were applied.'
Write-Host 'AC sleep and hibernation are disabled; Windows Time is running and synchronized.'
