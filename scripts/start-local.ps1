param(
    [ValidateRange(1, 65535)]
    [int]$PreferredApiPort = 8000,
    [ValidateRange(1, 65535)]
    [int]$WebPort = 5173
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$OutputEncoding = [Console]::OutputEncoding
$projectRoot = Split-Path -Parent $PSScriptRoot
$apiScript = Join-Path $PSScriptRoot 'start-api.ps1'
$webOrigin = "http://127.0.0.1:$WebPort"

function Test-MoneyGunApi([int]$Port) {
    try {
        $activation = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/v1/execution/activation-readiness" -TimeoutSec 2
        $opening = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/v1/strategies/opening-range/spec" -TimeoutSec 2
        return $null -ne $activation.missions -and $opening.validation_protocol_version -eq 'POINT_IN_TIME_INTRADAY-v2'
    }
    catch {
        return $false
    }
}

$apiPort = $null
$apiNeedsStart = $false
foreach ($candidate in $PreferredApiPort..($PreferredApiPort + 10)) {
    if (Test-MoneyGunApi $candidate) {
        $apiPort = $candidate
        break
    }
    $listener = Get-NetTCPConnection -LocalPort $candidate -State Listen -ErrorAction SilentlyContinue
    if (-not $listener) {
        $apiPort = $candidate
        $apiNeedsStart = $true
        break
    }
}
if ($null -eq $apiPort) {
    throw 'No MoneyGun API port is available in the 8000-8010 range.'
}

if ($apiNeedsStart) {
    Start-Process -FilePath 'powershell.exe' -ArgumentList @(
        '-NoProfile',
        '-ExecutionPolicy', 'Bypass',
        '-File', $apiScript,
        '-Port', $apiPort,
        '-PublicOrigin', $webOrigin
    ) -WorkingDirectory $projectRoot -WindowStyle Hidden
    $ready = $false
    foreach ($attempt in 1..40) {
        if (Test-MoneyGunApi $apiPort) {
            $ready = $true
            break
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) {
        throw "MoneyGun API did not start within 20 seconds. Port: $apiPort"
    }
}

$env:VITE_API_BASE_URL = "http://127.0.0.1:$apiPort"
$webListener = Get-NetTCPConnection -LocalPort $WebPort -State Listen -ErrorAction SilentlyContinue
Write-Host "MoneyGun API: http://127.0.0.1:$apiPort"
Write-Host "MoneyGun Web: $webOrigin"
if ($webListener) {
    Write-Host 'The web server is already running. Refresh the browser.'
    exit 0
}

Set-Location -LiteralPath $projectRoot
& npm.cmd run dev --workspace '@moneygun/web' -- --host 127.0.0.1 --port $WebPort --strictPort
