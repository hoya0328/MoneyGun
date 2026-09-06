param(
    [string]$SecretFile = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $SecretFile) {
    $SecretFile = Join-Path $projectRoot 'data/secrets/desktop-live-secrets.clixml'
}
$secretPath = [IO.Path]::GetFullPath($SecretFile)
$allowedRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot 'data/secrets'))
if (-not $secretPath.StartsWith($allowedRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'DESKTOP_LIVE secret file must stay under data/secrets.'
}
if (-not (Test-Path -LiteralPath $secretPath -PathType Leaf)) {
    throw 'DESKTOP_LIVE secret file is missing. Run scripts/Set-DesktopLiveSecrets.ps1.'
}
$allowedNames = @(
    'KIWOOM_APP_KEY',
    'KIWOOM_SECRET_KEY',
    'KIWOOM_ORDER_APP_KEY',
    'KIWOOM_ORDER_SECRET_KEY',
    'MONEYGUN_OWNER_API_TOKEN'
)
$secrets = Import-Clixml -LiteralPath $secretPath
foreach ($name in $allowedNames) {
    $secureValue = $secrets[$name]
    if ($null -eq $secureValue -or $secureValue.Length -lt 1) {
        throw "DESKTOP_LIVE secret is missing: $name"
    }
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureValue)
    try {
        $plainValue = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        [Environment]::SetEnvironmentVariable($name, $plainValue, 'Process')
    }
    finally {
        if ($pointer -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        }
        $plainValue = $null
    }
}
$env:MONEYGUN_WINDOWS_SECRETS_LOADED = 'true'
$env:MONEYGUN_SECRET_BACKEND = 'WINDOWS_DPAPI'
