param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000,
    [string]$PublicOrigin = ''
)

$ErrorActionPreference = 'Stop'

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
elseif ($settings.ContainsKey('MONEYGUN_ENV')) {
    [string]$settings['MONEYGUN_ENV']
}
else { 'local' }
$protectedNames = @(
    'MONEYGUN_DATABASE_URL',
    'KIWOOM_APP_KEY',
    'KIWOOM_SECRET_KEY',
    'KIWOOM_ORDER_APP_KEY',
    'KIWOOM_ORDER_SECRET_KEY',
    'MONEYGUN_OWNER_API_TOKEN'
)
$runtimeOwnedNames = @(
    'MONEYGUN_ENV',
    'MONEYGUN_DATABASE_BACKEND',
    'MONEYGUN_DATABASE_PATH',
    'MONEYGUN_SECRET_BACKEND',
    'MONEYGUN_PUBLIC_ORIGIN',
    'MONEYGUN_DESKTOP_AUTOSTART_CONFIGURED',
    'MONEYGUN_SLEEP_DISABLED',
    'MONEYGUN_CLOCK_SYNCHRONIZED',
    'MONEYGUN_OBSERVED_PUBLIC_IP',
    'MONEYGUN_DESKTOP_LOGGING_CONFIGURED',
    'MONEYGUN_BACKUP_ENCRYPTION_CONFIRMED'
)
foreach ($entry in $settings.GetEnumerator()) {
    if ($profile -eq 'desktop-live' -and $entry.Key -in $protectedNames) {
        continue
    }
    if (
        $profile -eq 'desktop-live' -and
        $entry.Key -in $runtimeOwnedNames -and
        -not [string]::IsNullOrWhiteSpace(
            [Environment]::GetEnvironmentVariable($entry.Key, 'Process')
        )
    ) {
        continue
    }
    [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Process')
}
if ($profile -eq 'desktop-live') {
    & (Join-Path $PSScriptRoot 'Import-DesktopLiveSecrets.ps1')
}

if ($PublicOrigin) {
    $env:MONEYGUN_PUBLIC_ORIGIN = $PublicOrigin.TrimEnd('/')
}

$env:PYTHONPATH = Join-Path $projectRoot 'apps/api/src'
Set-Location -LiteralPath $projectRoot
& (Join-Path $projectRoot '.venv/Scripts/python.exe') -m uvicorn moneygun_api.main:app --host 127.0.0.1 --port $Port
