param(
    [ValidateRange(1, 65535)]
    [int]$ApiPort = 8000,
    [ValidateRange(1, 65535)]
    [int]$WebPort = 5173
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$projectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$taskName = 'MoneyGun Desktop Live'

$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($null -ne $task) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
}
Start-Sleep -Milliseconds 500

$processTable = @{}
foreach ($process in Get-CimInstance Win32_Process) {
    $processTable[[int]$process.ProcessId] = $process
}

function Test-MoneyGunProcess([object]$Process, [string]$Kind) {
    if ($null -eq $Process) { return $false }
    $command = [string]$Process.CommandLine
    $executable = [string]$Process.ExecutablePath
    $fromProject = $command.IndexOf($projectRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0 -or
        $executable.StartsWith($projectRoot, [StringComparison]::OrdinalIgnoreCase)
    if ($Kind -eq 'api') {
        return $fromProject -and $command -match 'uvicorn\s+moneygun_api\.main:app'
    }
    if ($Kind -eq 'worker') {
        return $fromProject -and $command -match (
            'moneygun_api\.(close_auction_scheduler|l0_mode_scheduler|news_scheduler|auto_execution_scheduler|daily_market_refresh_scheduler)'
        )
    }
    return $fromProject -and $command -match 'vite(?:\.js)?["'']?\s+preview'
}

$targets = [Collections.Generic.List[int]]::new()
$seen = [Collections.Generic.HashSet[int]]::new()
foreach ($definition in @(
    @{ Port = $ApiPort; Kind = 'api' },
    @{ Port = $WebPort; Kind = 'web' }
)) {
    $listeners = @(Get-NetTCPConnection -LocalPort $definition.Port -State Listen -ErrorAction SilentlyContinue)
    foreach ($listener in $listeners) {
        if ($listener.LocalAddress -notin @('127.0.0.1', '::1')) {
            throw "Refusing to stop a non-loopback listener on port $($definition.Port)."
        }
        $process = $processTable[[int]$listener.OwningProcess]
        if (-not (Test-MoneyGunProcess $process $definition.Kind)) {
            throw "Refusing to stop an unverified process on port $($definition.Port)."
        }
        if ($seen.Add([int]$process.ProcessId)) {
            $targets.Add([int]$process.ProcessId)
        }
    }
}

foreach ($process in $processTable.Values) {
    if ((Test-MoneyGunProcess $process 'worker') -and $seen.Add([int]$process.ProcessId)) {
        $targets.Add([int]$process.ProcessId)
    }
}

foreach ($processId in $targets) {
    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
}

$deadline = (Get-Date).AddSeconds(10)
do {
    Start-Sleep -Milliseconds 250
    $remaining = @(
        Get-NetTCPConnection -LocalPort $ApiPort, $WebPort -State Listen -ErrorAction SilentlyContinue |
            Where-Object { $_.LocalAddress -in @('127.0.0.1', '::1') }
    )
} while ($remaining.Count -gt 0 -and (Get-Date) -lt $deadline)

if ($remaining.Count -gt 0) {
    throw 'Signal Guild loopback listeners did not stop cleanly.'
}

Write-Host "Signal Guild Desktop Live stopped. Verified listener processes stopped: $($targets.Count)."
