param(
    [string]$RegisteredPublicIp = ''
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
if ($env:OS -ne 'Windows_NT') {
    throw 'DESKTOP_LIVE installer supports Windows only.'
}
$projectRoot = Split-Path -Parent $PSScriptRoot
$secretPath = Join-Path $projectRoot 'data/secrets/desktop-live-secrets.clixml'
$configurationRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot 'data/desktop-live'))
$configurationPath = Join-Path $configurationRoot 'config.json'
if (-not (Test-Path -LiteralPath $secretPath -PathType Leaf)) {
    throw 'Run scripts/Set-DesktopLiveSecrets.ps1 before installing auto-start.'
}
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot '.venv/Scripts/python.exe'))) {
    throw 'Python virtual environment is missing.'
}
if (-not $RegisteredPublicIp) {
    $RegisteredPublicIp = (Read-Host 'Kiwoom portal registered public IPv4').Trim()
}
if ($RegisteredPublicIp -notmatch '^(?:\d{1,3}\.){3}\d{1,3}$') {
    throw 'RegisteredPublicIp must be an IPv4 address.'
}

Set-Location -LiteralPath $projectRoot
& npm.cmd run build
if ($LASTEXITCODE -ne 0) { throw 'MoneyGun web build failed.' }

New-Item -ItemType Directory -Path $configurationRoot -Force | Out-Null
$configuration = [ordered]@{
    schema_version = 1
    registered_public_ip = $RegisteredPublicIp
    backup_encryption_confirmed = $true
    backup_encryption_mode = 'AES256_GCM'
    close_auction_scheduler_enabled = $false
    multi_mode_scheduler_enabled = $false
    news_scheduler_enabled = $false
    daily_market_refresh_enabled = $true
    auto_execution_scheduler_enabled = $false
    auto_execution_release_enabled = $false
    installed_at = [DateTimeOffset]::UtcNow.ToString('o')
}
$configuration | ConvertTo-Json | Set-Content -LiteralPath $configurationPath -Encoding UTF8

$taskName = 'MoneyGun Desktop Live'
$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$supervisorPath = Join-Path $PSScriptRoot 'start-desktop-live.ps1'
$actionArguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$supervisorPath`""
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $actionArguments `
    -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Description 'MoneyGun loopback-only DESKTOP_LIVE supervisor' `
    -Force | Out-Null
Start-ScheduledTask -TaskName $taskName

Write-Host 'MoneyGun DESKTOP_LIVE auto-start task is installed.'
Write-Host 'The task starts after this Windows user signs in.'
Write-Host 'Live trading remains blocked until backup, restore drill, recovery reconciliation, and release review pass.'
