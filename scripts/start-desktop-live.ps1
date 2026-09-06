param(
    [ValidateRange(1, 65535)]
    [int]$ApiPort = 8000,
    [ValidateRange(1, 65535)]
    [int]$WebPort = 5173
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$projectRoot = Split-Path -Parent $PSScriptRoot
$configurationPath = Join-Path $projectRoot 'data/desktop-live/config.json'
$webIndex = Join-Path $projectRoot 'apps/web/dist/index.html'
$logRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot 'data/logs/desktop-live'))
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null

$createdNew = $false
$mutex = [Threading.Mutex]::new($true, 'Local\MoneyGunDesktopLiveSupervisor', [ref]$createdNew)
if (-not $createdNew) {
    throw 'Signal Guild DESKTOP_LIVE supervisor is already running.'
}

function Test-Truthy([object]$Value) {
    return [string]$Value -match '^(?i:1|true|yes|enabled)$'
}

function Test-SleepDisabled {
    try {
        $output = & powercfg.exe /query scheme_current 238C9FA8-0AAD-41ED-83F4-97BE242C8F20 29f6c1db-86da-48c5-9fdb-f2b67b1f44da 2>$null
        $indexes = [regex]::Matches(($output -join "`n"), '0x[0-9a-fA-F]{8}') | ForEach-Object { $_.Value }
        # powercfg also prints the minimum, maximum, and increment before the
        # current AC/DC values. The final two indexes are the active values on
        # both English and localized Windows installations.
        if ($indexes.Count -lt 2) { return $false }
        $currentIndexes = @($indexes[($indexes.Count - 2)..($indexes.Count - 1)])
        return @($currentIndexes | Where-Object { $_ -ne '0x00000000' }).Count -eq 0
    }
    catch { return $false }
}

function Test-ClockSynchronized {
    try {
        $service = Get-Service -Name W32Time -ErrorAction Stop
        if ($service.Status -ne 'Running') { return $false }
        & w32tm.exe /query /status 1>$null 2>$null
        return $LASTEXITCODE -eq 0
    }
    catch { return $false }
}

function Get-ObservedPublicIp {
    try {
        $value = (Invoke-RestMethod -Uri 'https://checkip.amazonaws.com' -TimeoutSec 5).Trim()
        if ($value -match '^(?:\d{1,3}\.){3}\d{1,3}$') { return $value }
    }
    catch { }
    return ''
}

function Get-ProfileState {
    if (-not (Test-Path -LiteralPath $configurationPath -PathType Leaf)) {
        throw 'DESKTOP_LIVE configuration is missing. Run scripts/install-desktop-live.ps1.'
    }
    $configuration = Get-Content -Raw -LiteralPath $configurationPath | ConvertFrom-Json
    $taskReady = $null -ne (Get-ScheduledTask -TaskName 'MoneyGun Desktop Live' -ErrorAction SilentlyContinue)
    return [ordered]@{
        MONEYGUN_ENV = 'desktop-live'
        MONEYGUN_DATABASE_BACKEND = 'sqlite'
        MONEYGUN_DATABASE_PATH = 'data/moneygun.sqlite3'
        MONEYGUN_PUBLIC_ORIGIN = "http://127.0.0.1:$WebPort"
        MONEYGUN_SECRET_BACKEND = 'WINDOWS_DPAPI'
        MONEYGUN_DESKTOP_AUTOSTART_CONFIGURED = $taskReady.ToString().ToLowerInvariant()
        MONEYGUN_SLEEP_DISABLED = (Test-SleepDisabled).ToString().ToLowerInvariant()
        MONEYGUN_CLOCK_SYNCHRONIZED = (Test-ClockSynchronized).ToString().ToLowerInvariant()
        MONEYGUN_KIWOOM_REGISTERED_IP = [string]$configuration.registered_public_ip
        MONEYGUN_OBSERVED_PUBLIC_IP = Get-ObservedPublicIp
        MONEYGUN_DESKTOP_LOGGING_CONFIGURED = 'true'
        MONEYGUN_BACKUP_ENCRYPTION_CONFIRMED = (Test-Truthy $configuration.backup_encryption_confirmed).ToString().ToLowerInvariant()
        MONEYGUN_BACKUP_ENCRYPTION_MODE = [string]$configuration.backup_encryption_mode
        KIWOOM_ENVIRONMENT = 'production'
        KIWOOM_ORDER_ENVIRONMENT = 'production'
        MONEYGUN_CLOSE_AUCTION_SCHEDULER_ENABLED = (
            Test-Truthy $configuration.close_auction_scheduler_enabled
        ).ToString().ToLowerInvariant()
        MONEYGUN_MULTI_MODE_SCHEDULER_ENABLED = (
            Test-Truthy $configuration.multi_mode_scheduler_enabled
        ).ToString().ToLowerInvariant()
        MONEYGUN_NEWS_SCHEDULER_ENABLED = (
            Test-Truthy $configuration.news_scheduler_enabled
        ).ToString().ToLowerInvariant()
        MONEYGUN_DAILY_MARKET_REFRESH_ENABLED = (
            Test-Truthy $configuration.daily_market_refresh_enabled
        ).ToString().ToLowerInvariant()
        MONEYGUN_AUTO_EXECUTION_SCHEDULER_ENABLED = (
            Test-Truthy $configuration.auto_execution_scheduler_enabled
        ).ToString().ToLowerInvariant()
        MONEYGUN_L0_AUTO_EXECUTION_RELEASE_ENABLED = (
            Test-Truthy $configuration.auto_execution_release_enabled
        ).ToString().ToLowerInvariant()
    }
}

function Set-ProfileEnvironment([System.Collections.IDictionary]$Profile) {
    foreach ($entry in $Profile.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key, [string]$entry.Value, 'Process')
    }
}

function Start-ApiProcess {
    $stamp = Get-Date -Format 'yyyyMMdd'
    return Start-Process -FilePath 'powershell.exe' -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
        (Join-Path $PSScriptRoot 'start-api.ps1'), '-Port', $ApiPort,
        '-PublicOrigin', "http://127.0.0.1:$WebPort"
    ) -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
      -RedirectStandardOutput (Join-Path $logRoot "api-$stamp.log") `
      -RedirectStandardError (Join-Path $logRoot "api-$stamp.err.log")
}

function Start-WebProcess {
    $stamp = Get-Date -Format 'yyyyMMdd'
    return Start-Process -FilePath 'npm.cmd' -ArgumentList @(
        'run', 'preview', '--workspace', '@moneygun/web', '--',
        '--host', '127.0.0.1', '--port', $WebPort, '--strictPort'
    ) -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
      -RedirectStandardOutput (Join-Path $logRoot "web-$stamp.log") `
      -RedirectStandardError (Join-Path $logRoot "web-$stamp.err.log")
}

function Start-CloseAuctionScheduler {
    $stamp = Get-Date -Format 'yyyyMMdd'
    [Environment]::SetEnvironmentVariable(
        'PYTHONPATH', (Join-Path $projectRoot 'apps/api/src'), 'Process'
    )
    return Start-Process -FilePath (Join-Path $projectRoot '.venv/Scripts/python.exe') -ArgumentList @(
        '-m', 'moneygun_api.close_auction_scheduler', '--api-url', "http://127.0.0.1:$ApiPort"
    ) -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
      -RedirectStandardOutput (Join-Path $logRoot "close-auction-$stamp.log") `
      -RedirectStandardError (Join-Path $logRoot "close-auction-$stamp.err.log")
}

function Start-MultiModeScheduler {
    $stamp = Get-Date -Format 'yyyyMMdd'
    [Environment]::SetEnvironmentVariable(
        'PYTHONPATH', (Join-Path $projectRoot 'apps/api/src'), 'Process'
    )
    return Start-Process -FilePath (Join-Path $projectRoot '.venv/Scripts/python.exe') -ArgumentList @(
        '-m', 'moneygun_api.l0_mode_scheduler', '--api-url', "http://127.0.0.1:$ApiPort"
    ) -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
      -RedirectStandardOutput (Join-Path $logRoot "multi-mode-$stamp.log") `
      -RedirectStandardError (Join-Path $logRoot "multi-mode-$stamp.err.log")
}

function Start-NewsScheduler {
    $stamp = Get-Date -Format 'yyyyMMdd'
    [Environment]::SetEnvironmentVariable(
        'PYTHONPATH', (Join-Path $projectRoot 'apps/api/src'), 'Process'
    )
    return Start-Process -FilePath (Join-Path $projectRoot '.venv/Scripts/python.exe') -ArgumentList @(
        '-m', 'moneygun_api.news_scheduler', '--api-url', "http://127.0.0.1:$ApiPort"
    ) -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
      -RedirectStandardOutput (Join-Path $logRoot "news-$stamp.log") `
      -RedirectStandardError (Join-Path $logRoot "news-$stamp.err.log")
}

function Start-AutoExecutionScheduler {
    $stamp = Get-Date -Format 'yyyyMMdd'
    [Environment]::SetEnvironmentVariable(
        'PYTHONPATH', (Join-Path $projectRoot 'apps/api/src'), 'Process'
    )
    return Start-Process -FilePath (Join-Path $projectRoot '.venv/Scripts/python.exe') -ArgumentList @(
        '-m', 'moneygun_api.auto_execution_scheduler', '--api-url', "http://127.0.0.1:$ApiPort"
    ) -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
      -RedirectStandardOutput (Join-Path $logRoot "auto-execution-$stamp.log") `
      -RedirectStandardError (Join-Path $logRoot "auto-execution-$stamp.err.log")
}

function Start-DailyMarketRefreshScheduler {
    $stamp = Get-Date -Format 'yyyyMMdd'
    [Environment]::SetEnvironmentVariable(
        'PYTHONPATH', (Join-Path $projectRoot 'apps/api/src'), 'Process'
    )
    return Start-Process -FilePath (Join-Path $projectRoot '.venv/Scripts/python.exe') -ArgumentList @(
        '-m', 'moneygun_api.daily_market_refresh_scheduler', '--api-url', "http://127.0.0.1:$ApiPort"
    ) -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
      -RedirectStandardOutput (Join-Path $logRoot "daily-market-refresh-$stamp.log") `
      -RedirectStandardError (Join-Path $logRoot "daily-market-refresh-$stamp.err.log")
}

function Stop-OwnedProcess([Diagnostics.Process]$Process) {
    if ($null -ne $Process -and -not $Process.HasExited) {
        Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
        $Process.WaitForExit(5000) | Out-Null
    }
}

if (-not (Test-Path -LiteralPath $webIndex -PathType Leaf)) {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
    throw 'Production web build is missing. Run npm run build before DESKTOP_LIVE.'
}

$apiProcess = $null
$webProcess = $null
$closeAuctionProcess = $null
$multiModeProcess = $null
$newsProcess = $null
$autoExecutionProcess = $null
$dailyMarketRefreshProcess = $null
$profileSignature = ''
try {
    while ($true) {
        $profile = Get-ProfileState
        $newSignature = ($profile | ConvertTo-Json -Compress)
        if ($newSignature -ne $profileSignature) {
            Set-ProfileEnvironment $profile
            $profileSignature = $newSignature
            Stop-OwnedProcess $apiProcess
            $apiProcess = $null
        }
        if ($null -eq $apiProcess -or $apiProcess.HasExited) {
            $apiProcess = Start-ApiProcess
        }
        if ($null -eq $webProcess -or $webProcess.HasExited) {
            $webProcess = Start-WebProcess
        }
        if (Test-Truthy $profile.MONEYGUN_CLOSE_AUCTION_SCHEDULER_ENABLED) {
            if ($null -eq $closeAuctionProcess -or $closeAuctionProcess.HasExited) {
                $closeAuctionProcess = Start-CloseAuctionScheduler
            }
        }
        else {
            Stop-OwnedProcess $closeAuctionProcess
            $closeAuctionProcess = $null
        }
        if (Test-Truthy $profile.MONEYGUN_MULTI_MODE_SCHEDULER_ENABLED) {
            if ($null -eq $multiModeProcess -or $multiModeProcess.HasExited) {
                $multiModeProcess = Start-MultiModeScheduler
            }
        }
        else {
            Stop-OwnedProcess $multiModeProcess
            $multiModeProcess = $null
        }
        if (Test-Truthy $profile.MONEYGUN_NEWS_SCHEDULER_ENABLED) {
            if ($null -eq $newsProcess -or $newsProcess.HasExited) {
                $newsProcess = Start-NewsScheduler
            }
        }
        else {
            Stop-OwnedProcess $newsProcess
            $newsProcess = $null
        }
        if (Test-Truthy $profile.MONEYGUN_AUTO_EXECUTION_SCHEDULER_ENABLED) {
            if ($null -eq $autoExecutionProcess -or $autoExecutionProcess.HasExited) {
                $autoExecutionProcess = Start-AutoExecutionScheduler
            }
        }
        else {
            Stop-OwnedProcess $autoExecutionProcess
            $autoExecutionProcess = $null
        }
        if (Test-Truthy $profile.MONEYGUN_DAILY_MARKET_REFRESH_ENABLED) {
            if ($null -eq $dailyMarketRefreshProcess -or $dailyMarketRefreshProcess.HasExited) {
                $dailyMarketRefreshProcess = Start-DailyMarketRefreshScheduler
            }
        }
        else {
            Stop-OwnedProcess $dailyMarketRefreshProcess
            $dailyMarketRefreshProcess = $null
        }
        Start-Sleep -Seconds 30
    }
}
finally {
    Stop-OwnedProcess $apiProcess
    Stop-OwnedProcess $webProcess
    Stop-OwnedProcess $closeAuctionProcess
    Stop-OwnedProcess $multiModeProcess
    Stop-OwnedProcess $newsProcess
    Stop-OwnedProcess $autoExecutionProcess
    Stop-OwnedProcess $dailyMarketRefreshProcess
    if ($createdNew) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
