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
$secretDirectory = Split-Path -Parent $secretPath
New-Item -ItemType Directory -Path $secretDirectory -Force | Out-Null

function Read-RequiredSecret([string]$Prompt, [int]$MinimumLength = 1) {
    $secret = Read-Host $Prompt -AsSecureString
    if ($secret.Length -lt $MinimumLength) {
        throw "$Prompt must contain at least $MinimumLength characters."
    }
    return $secret
}

$secrets = [ordered]@{
    KIWOOM_APP_KEY = Read-RequiredSecret 'Kiwoom production read App Key'
    KIWOOM_SECRET_KEY = Read-RequiredSecret 'Kiwoom production read Secret Key'
    KIWOOM_ORDER_APP_KEY = Read-RequiredSecret 'Kiwoom production order App Key'
    KIWOOM_ORDER_SECRET_KEY = Read-RequiredSecret 'Kiwoom production order Secret Key'
    MONEYGUN_OWNER_API_TOKEN = Read-RequiredSecret 'Signal Guild owner token (24+ chars)' 24
}
$secrets | Export-Clixml -LiteralPath $secretPath -Depth 3 -Force

$acl = Get-Acl -LiteralPath $secretPath
$acl.SetAccessRuleProtection($true, $false)
$rule = [Security.AccessControl.FileSystemAccessRule]::new(
    [Security.Principal.WindowsIdentity]::GetCurrent().Name,
    [Security.AccessControl.FileSystemRights]::FullControl,
    [Security.AccessControl.AccessControlType]::Allow
)
$acl.SetAccessRule($rule)
Set-Acl -LiteralPath $secretPath -AclObject $acl
Write-Host 'DESKTOP_LIVE secrets were encrypted for the current Windows user.'
Write-Host "Secret file: $secretPath"
Write-Host 'Secret values were not printed.'
