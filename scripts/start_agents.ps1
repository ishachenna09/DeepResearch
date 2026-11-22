# Starts (or stops) the two A2A agent services: writer :8010, critic :8011.
#
# Why two processes and not `--workers 2` of one app?
# They are different agents with different cards, skills and failure modes. A
# deploy scales them independently (the writer is the expensive one) and a crash
# in one must not take the other down.
#
# Why uvicorn and not a bare `python app/a2a/writer_app.py`?
# The apps are module-level `app` objects built for ASGI; uvicorn gives us the
# same lifecycle, logging and graceful shutdown the API has. Nothing in the app
# knows which server runs it.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\scripts\start_agents.ps1
#   powershell -ExecutionPolicy Bypass -File .\scripts\start_agents.ps1 -Stop
#
# Logs: %TEMP%\deepresearch_writer.log / .err  and  ..._critic.log / .err

param(
    [int]$WriterPort = 8010,
    [int]$CriticPort = 8011,
    [int]$WaitSec = 90,
    [switch]$Stop
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$apiDir = Join-Path $root "api"
$writerPidFile = Join-Path $env:TEMP "deepresearch_writer.pid"
$criticPidFile = Join-Path $env:TEMP "deepresearch_critic.pid"

function Ok($m)   { Write-Host "[ OK ] $m" -ForegroundColor Green }
function Fail($m) { Write-Host "[FAIL] $m" -ForegroundColor Red; exit 1 }
function Note($m) { Write-Host "       $m" -ForegroundColor DarkGray }

function Stop-One($pidFile, $name) {
    if (-not (Test-Path $pidFile)) { Note "$name : no pid file"; return }
    $procId = (Get-Content $pidFile -Raw).Trim()
    if ($procId -match '^\d+$') {
        try { Stop-Process -Id ([int]$procId) -Force -ErrorAction SilentlyContinue } catch { }
        # uvicorn's reloader/child (if any) shares the console; kill the tree too.
        try { taskkill /PID $procId /T /F 2>$null | Out-Null } catch { }
        Note "$name : stopped pid $procId"
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}

if ($Stop) {
    Stop-One $writerPidFile "writer"
    Stop-One $criticPidFile "critic"
    Ok "agent services stopped"
    exit 0
}

if (-not (Test-Path $python)) { Fail "missing $python - create .venv first" }
$token = (Select-String -Path (Join-Path $root ".env") -Pattern '^A2A_TOKEN=(.+)$' | Select-Object -First 1)
if (-not $token -or $token.Matches[0].Groups[1].Value.Trim() -eq "") {
    Fail "A2A_TOKEN is empty in .env. Generate one: python -c `"import secrets; print(secrets.token_urlsafe(32))`""
}

foreach ($port in @($WriterPort, $CriticPort)) {
    $busy = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($busy) { Fail "port $port is already in use (pid $($busy.OwningProcess -join ',')). Run -Stop or free it." }
}

# The card advertises the URL it was built with, so the child must agree with the
# port we are about to bind. Save/restore so the caller's session is untouched.
$oldWriterUrl = $env:A2A_WRITER_URL
$oldCriticUrl = $env:A2A_CRITIC_URL
$env:A2A_WRITER_URL = "http://127.0.0.1:$WriterPort"
$env:A2A_CRITIC_URL = "http://127.0.0.1:$CriticPort"

try {
    $writer = Start-Process -FilePath $python `
        -ArgumentList "-m", "uvicorn", "app.a2a.writer_app:app", "--host", "127.0.0.1", "--port", "$WriterPort", "--log-level", "info" `
        -WorkingDirectory $apiDir -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $env:TEMP "deepresearch_writer.log") `
        -RedirectStandardError  (Join-Path $env:TEMP "deepresearch_writer.err")
    $writer.Id | Out-File $writerPidFile -Encoding ascii

    $critic = Start-Process -FilePath $python `
        -ArgumentList "-m", "uvicorn", "app.a2a.critic_app:app", "--host", "127.0.0.1", "--port", "$CriticPort", "--log-level", "info" `
        -WorkingDirectory $apiDir -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $env:TEMP "deepresearch_critic.log") `
        -RedirectStandardError  (Join-Path $env:TEMP "deepresearch_critic.err")
    $critic.Id | Out-File $criticPidFile -Encoding ascii
} finally {
    $env:A2A_WRITER_URL = $oldWriterUrl
    $env:A2A_CRITIC_URL = $oldCriticUrl
}

"writer pid=$($writer.Id)  critic pid=$($critic.Id)"

$sw = [Diagnostics.Stopwatch]::StartNew()
$up = @{}
while ($sw.Elapsed.TotalSeconds -lt $WaitSec) {
    Start-Sleep -Seconds 2
    foreach ($pair in @(@("writer", $WriterPort), @("critic", $CriticPort))) {
        if ($up[$pair[0]]) { continue }
        try {
            Invoke-RestMethod "http://127.0.0.1:$($pair[1])/healthz" -TimeoutSec 2 | Out-Null
            $up[$pair[0]] = $true
        } catch { }
    }
    if ($up.Count -eq 2) { break }
}

foreach ($name in @("writer", "critic")) {
    if (-not $up[$name]) {
        $errFile = Join-Path $env:TEMP "deepresearch_$name.err"
        if (Test-Path $errFile) { Get-Content $errFile -Tail 15 }
        Fail "$name did not come up within $WaitSec s"
    }
}

Ok "writer  : http://127.0.0.1:$WriterPort  card /.well-known/agent-card.json  skill write-cited-report"
Ok "critic  : http://127.0.0.1:$CriticPort  card /.well-known/agent-card.json  skill critique-coverage"
Note "logs: $env:TEMP\deepresearch_writer.log | $env:TEMP\deepresearch_critic.log"
Note "stop: powershell -File .\scripts\start_agents.ps1 -Stop"
