param(
    [ValidateRange(1, 65535)]
    [int]$ApiPort = 8000
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$projectRoot = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $projectRoot '.env'

$settings = @{}
if (Test-Path -LiteralPath $envFile) {
    foreach ($line in Get-Content -LiteralPath $envFile -Encoding UTF8) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#') -or -not $trimmed.Contains('=')) {
            continue
        }
        $name, $value = $trimmed.Split('=', 2)
        $settings[$name.Trim()] = $value.Trim()
    }
}
$profile = if (-not [string]::IsNullOrWhiteSpace($env:MONEYGUN_ENV)) {
    [string]$env:MONEYGUN_ENV
}
elseif ($settings.ContainsKey('MONEYGUN_ENV')) { [string]$settings['MONEYGUN_ENV'] }
else { 'local' }
$protectedNames = @(
    'MONEYGUN_DATABASE_URL', 'KIWOOM_APP_KEY', 'KIWOOM_SECRET_KEY',
    'KIWOOM_ORDER_APP_KEY', 'KIWOOM_ORDER_SECRET_KEY', 'MONEYGUN_OWNER_API_TOKEN'
)
foreach ($entry in $settings.GetEnumerator()) {
    if ($profile -eq 'desktop-live' -and $entry.Key -in $protectedNames) { continue }
    [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Process')
}
if ($profile -eq 'desktop-live') {
    $env:MONEYGUN_ENV = 'desktop-live'
    $env:MONEYGUN_DATABASE_BACKEND = 'sqlite'
    $env:MONEYGUN_DATABASE_PATH = 'data/moneygun.sqlite3'
    $env:KIWOOM_ENVIRONMENT = 'production'
    $env:KIWOOM_ORDER_ENVIRONMENT = 'production'
    & (Join-Path $PSScriptRoot 'Import-DesktopLiveSecrets.ps1')
}

function Test-Configured([string]$Name) {
    $value = [Environment]::GetEnvironmentVariable($Name, 'Process')
    return -not [string]::IsNullOrWhiteSpace($value)
}

function Test-Enabled([string]$Name) {
    $value = [Environment]::GetEnvironmentVariable($Name, 'Process')
    return $value -and $value.Trim().ToLowerInvariant() -in @('1', 'true', 'yes', 'enabled')
}

$checks = [ordered]@{
    'Kiwoom production read credentials' = (Test-Configured 'KIWOOM_APP_KEY') -and (Test-Configured 'KIWOOM_SECRET_KEY') -and $env:KIWOOM_ENVIRONMENT -eq 'production'
    'Kiwoom production order credentials' = (Test-Configured 'KIWOOM_ORDER_APP_KEY') -and (Test-Configured 'KIWOOM_ORDER_SECRET_KEY') -and $env:KIWOOM_ORDER_ENVIRONMENT -eq 'production'
    'Owner token (24+ chars)' = (Test-Configured 'MONEYGUN_OWNER_API_TOKEN') -and $env:MONEYGUN_OWNER_API_TOKEN.Length -ge 24
    'Durable local database' = ($env:MONEYGUN_DATABASE_BACKEND -eq 'sqlite' -and (Test-Configured 'MONEYGUN_DATABASE_PATH')) -or ($env:MONEYGUN_DATABASE_BACKEND -eq 'postgresql' -and $env:MONEYGUN_DATABASE_URL -match '^postgres(ql)?://' -and $env:MONEYGUN_DATABASE_URL -notmatch 'change-me')
    'Release approval' = Test-Enabled 'MONEYGUN_RELEASE_APPROVED'
    'Live trading switch' = Test-Enabled 'KIWOOM_TRADING_ENABLED'
}
if ($profile -eq 'desktop-live') {
    $configurationPath = Join-Path $projectRoot 'data/desktop-live/config.json'
    $configuration = if (Test-Path -LiteralPath $configurationPath) {
        Get-Content -Raw -LiteralPath $configurationPath | ConvertFrom-Json
    } else { $null }
    $checks['Windows DPAPI secret store'] = (Test-Enabled 'MONEYGUN_WINDOWS_SECRETS_LOADED') -and $env:MONEYGUN_SECRET_BACKEND -eq 'WINDOWS_DPAPI'
    $checks['Desktop auto-start task'] = $null -ne (Get-ScheduledTask -TaskName 'MoneyGun Desktop Live' -ErrorAction SilentlyContinue)
    $checks['Registered public IP configuration'] = $null -ne $configuration -and [string]$configuration.registered_public_ip -match '^(?:\d{1,3}\.){3}\d{1,3}$'
    $checks['Backup encryption confirmation'] = $null -ne $configuration -and [bool]$configuration.backup_encryption_confirmed
}
else {
    $checks['HTTPS OIDC'] = $env:MONEYGUN_OIDC_ISSUER -match '^https://' -and (Test-Configured 'MONEYGUN_OIDC_CLIENT_ID')
    $checks['External secret manager'] = $env:MONEYGUN_SECRET_BACKEND -in @('AWS_SECRETS_MANAGER', 'GCP_SECRET_MANAGER', 'AZURE_KEY_VAULT', 'VAULT')
    $checks['Error monitoring'] = Test-Configured 'MONEYGUN_ERROR_MONITOR_DSN'
    $checks['HTTPS public origin'] = $env:MONEYGUN_PUBLIC_ORIGIN -match '^https://'
    $checks['Managed backup'] = Test-Enabled 'MONEYGUN_MANAGED_BACKUP_CONFIGURED'
}

$gitDirectory = Join-Path $projectRoot '.git'
$headFile = Join-Path $gitDirectory 'HEAD'
$hasGitBaseline = $false
if (Test-Path -LiteralPath $headFile) {
    $headValue = (Get-Content -LiteralPath $headFile -Raw).Trim()
    if ($headValue.StartsWith('ref: ')) {
        $refRelativePath = $headValue.Substring(5).Replace('/', [IO.Path]::DirectorySeparatorChar)
        $hasGitBaseline = Test-Path -LiteralPath (Join-Path $gitDirectory $refRelativePath)
    }
    else {
        $hasGitBaseline = $headValue -match '^[0-9a-f]{40}$'
    }
}
$checks['Git release baseline'] = $hasGitBaseline

Write-Host 'MoneyGun L0 external-lock check (secret values are never printed)'
foreach ($item in $checks.GetEnumerator()) {
    $mark = if ($item.Value) { '[PASS]' } else { '[BLOCK]' }
    Write-Host "$mark $($item.Key)"
}

$apiReady = $false
try {
    $activation = Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/v1/execution/activation-readiness" -TimeoutSec 30
    $pilot = $activation.missions | Where-Object { $_.mode_code -eq 'L0_PILOT' } | Select-Object -First 1
    $apiReady = $pilot.can_submit_live_order -eq $true
    $apiMark = if ($apiReady) { '[PASS]' } else { '[BLOCK]' }
    Write-Host "$apiMark API L0 aggregate verdict"
    if (-not $apiReady -and $pilot.next_step) {
        Write-Host "       Next action: $($pilot.next_step.label)"
    }
}
catch {
    $launcher = if ($profile -eq 'desktop-live') { 'start-desktop-live.ps1' } else { 'start-local.ps1' }
    Write-Host "[BLOCK] API connection (run .\scripts\$launcher first)"
}

if (($checks.Values -contains $false) -or -not $apiReady) {
    Write-Host 'VERDICT: LIVE ORDER BLOCKED. Use read and shadow features only.'
    exit 1
}

Write-Host 'VERDICT: L0 external locks passed. Quote, reconciliation, window, and approval are still checked per order.'
exit 0
