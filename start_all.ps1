# Restart all bot processes: stop old ones, VERIFY they are gone, start new ones.
# A second live bot instance polling Telegram causes duplicated/conflicting
# replies - this script guarantees exactly one instance of each process.

$venv = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$proj = $PSScriptRoot

function Get-BotProcs {
    Get-WmiObject Win32_Process -Filter "Name='python.exe'" | Where-Object {
        $_.CommandLine -match 'bot\.main|bot\.worker\.tasks'
    }
}

Write-Host "=== Stopping old processes ===" -ForegroundColor Yellow
Get-BotProcs | ForEach-Object {
    Write-Host "  Stopping PID $($_.ProcessId)"
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}

# Wait until they are REALLY gone (up to 15s) - starting on top of a
# surviving instance means two pollers and duplicated messages.
$deadline = (Get-Date).AddSeconds(15)
while ((Get-Date) -lt $deadline) {
    $left = @(Get-BotProcs)
    if ($left.Count -eq 0) { break }
    Start-Sleep -Milliseconds 500
}
$left = @(Get-BotProcs)
if ($left.Count -gt 0) {
    Write-Host "ERROR: $($left.Count) old process(es) still alive - NOT starting new ones." -ForegroundColor Red
    $left | ForEach-Object { Write-Host "  PID $($_.ProcessId): $($_.CommandLine)" }
    exit 1
}

if (-not (Test-Path "$proj\logs")) { New-Item -ItemType Directory "$proj\logs" | Out-Null }

Write-Host "=== Starting bot + 3 workers ===" -ForegroundColor Green
Start-Process $venv -ArgumentList "-m","bot.main" -WorkingDirectory $proj -WindowStyle Hidden `
    -RedirectStandardOutput "$proj\logs\bot.out.log" -RedirectStandardError "$proj\logs\bot.err.log"
Start-Process $venv -ArgumentList "-m","arq","bot.worker.tasks.WorkerSettings" -WorkingDirectory $proj -WindowStyle Hidden `
    -RedirectStandardOutput "$proj\logs\worker.out.log" -RedirectStandardError "$proj\logs\worker.err.log"
Start-Process $venv -ArgumentList "-m","arq","bot.worker.tasks.RecognitionWorkerSettings" -WorkingDirectory $proj -WindowStyle Hidden `
    -RedirectStandardOutput "$proj\logs\worker_recog.out.log" -RedirectStandardError "$proj\logs\worker_recog.err.log"
Start-Process $venv -ArgumentList "-m","arq","bot.worker.tasks.MusicWorkerSettings" -WorkingDirectory $proj -WindowStyle Hidden `
    -RedirectStandardOutput "$proj\logs\worker_music.out.log" -RedirectStandardError "$proj\logs\worker_music.err.log"

Start-Sleep -Seconds 8

# Verify: 4 logical processes (each may show a venv-launcher parent + child pair)
$procs = @(Get-BotProcs)
$mains = @($procs | Where-Object { $_.CommandLine -match 'bot\.main' -and $_.ParentProcessId -notin $procs.ProcessId })
Write-Host "Running bot processes: $($procs.Count) total, $($mains.Count) bot.main root instance(s)" -ForegroundColor Cyan
if ($mains.Count -ne 1) {
    Write-Host "WARNING: expected exactly 1 bot.main root instance!" -ForegroundColor Red
}
Write-Host "Logs: logs\bot.out.log, logs\worker*.log" -ForegroundColor Cyan
