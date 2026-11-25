# Phase 6 verification: the writer and critic as A2A services.
#
# Proves, in order:
#   1. discovery: both agents publish a v1.0 AgentCard (JSONRPC + streaming + a skill)
#      at /.well-known/agent-card.json, and the card is readable WITHOUT a token
#   2. the RPC endpoint is protected: no token / wrong token -> 401 + WWW-Authenticate
#   3. protocol errors are the spec's codes, not a 500: unknown method -> -32601,
#      unknown task -> -32001
#   4. the critic really is a network agent: SendMessage returns a Task with a
#      JSON verdict artifact + token/model metadata
#   5. the API in AGENTS_MODE=a2a reports both agents healthy (card discovery)
#   6. a full report is generated THROUGH the writer agent and passes the same
#      citation audit as Phase 5 (precision 1.0, >= 10 markers, traceable citations)
#   7. failure is contained: with the writer stopped, the API's report task fails
#      with AgentUnavailable - proving the remote path is the one being used -
#      and a restart + regenerate recovers
#   8. the agent's own log recorded the task it wrote
#
# This script owns its processes: it starts writer :8010, critic :8011 and a
# SEPARATE API on :8002 with AGENTS_MODE=a2a, then stops all three at the end.
# It never touches the API you have on :8001. (Note: the second API shares Redis
# and Postgres, and its startup reconciles orphaned `generating` reports - do not
# run it while a generation is in flight on :8001.)
#
# Prereqs: LiteLLM :4000 running, migrations at head, and at least one job in
#          `notes_ready` with notes.
# Run:
#   powershell -ExecutionPolicy Bypass -File C:\projects\DeepResearch\scripts\verify_phase6.ps1
#   ... -JobId <guid>     -> verify a specific job
#   ... -SkipE2E          -> protocol checks only (no report generation)
#   ... -KeepRunning      -> leave the services up for manual poking

param(
    [string]$Email    = "you@test.com",
    [string]$Password = "password123",
    [string]$JobId    = "",
    [int]$ApiPort     = 8002,
    [int]$WriterPort  = 8010,
    [int]$CriticPort  = 8011,
    [int]$TimeoutSec  = 400,
    [switch]$SkipE2E,
    [switch]$KeepRunning
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Net.Http

$root    = Split-Path -Parent $PSScriptRoot
$python  = Join-Path $root ".venv\Scripts\python.exe"
$apiDir  = Join-Path $root "api"
$Base    = "http://localhost:$ApiPort/api/v1"
$writerLog = Join-Path $env:TEMP "deepresearch_writer.err"
$criticLog = Join-Path $env:TEMP "deepresearch_critic.err"
$apiLog    = Join-Path $env:TEMP "deepresearch_phase6_api.err"

function Ok($m)   { Write-Host "[ OK ] $m" -ForegroundColor Green }
function Fail($m) { throw "FAIL: $m" }
function Head($m) { Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Note($m) { Write-Host "       $m" -ForegroundColor DarkGray }
function Body($e) { try { return $e.ErrorDetails.Message } catch { return "" } }

$startedWriter = $null
$startedCritic = $null
$startedApi    = $null

function Stop-Proc($proc) {
    if ($null -eq $proc) { return }
    try { taskkill /PID $proc.Id /T /F 2>$null | Out-Null } catch { }
    try { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue } catch { }
}

function Start-Agent($module, $port, $outFile, $errFile) {
    return Start-Process -FilePath $python `
        -ArgumentList "-m", "uvicorn", "$module", "--host", "127.0.0.1", "--port", "$port", "--log-level", "info" `
        -WorkingDirectory $apiDir -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput $outFile -RedirectStandardError $errFile
}

function Wait-Http($url, $seconds, $label) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $seconds) {
        Start-Sleep -Seconds 2
        try { Invoke-RestMethod $url -TimeoutSec 3 | Out-Null; return } catch { }
    }
    Fail "$label did not answer at $url within $seconds s"
}

function Test-Http($url, $seconds) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $seconds) {
        Start-Sleep -Seconds 2
        try { Invoke-RestMethod $url -TimeoutSec 3 | Out-Null; return $true } catch { }
    }
    return $false
}

# Starting three CPython processes (each importing langchain/protobuf) at once can
# hit a transient MemoryError while bytecode is compiled - observed on this
# machine, and it looks exactly like a broken service. A retry with a fresh
# process is honest here: the failure is environmental, not configuration, and
# the last stderr line is printed either way so a real error is still visible.
function Start-WithRetry($module, $port, $outFile, $errFile, $label, $attempts = 3) {
    for ($i = 1; $i -le $attempts; $i++) {
        $proc = Start-Agent $module $port $outFile $errFile
        if (Test-Http "http://127.0.0.1:$port/healthz" 90) { return $proc }
        $tail = ""
        if (Test-Path $errFile) { $tail = (Get-Content $errFile -Tail 1) }
        Note "$label attempt $i/$attempts failed - $tail"
        Stop-Proc $proc
        Start-Sleep -Seconds 4
    }
    Fail "$label did not start after $attempts attempts (see $errFile)"
}

function Start-ApiWithRetry($attempts = 3) {
    $oldMode = $env:AGENTS_MODE
    $oldWriterUrl = $env:A2A_WRITER_URL
    $oldCriticUrl = $env:A2A_CRITIC_URL
    $oldRate = $env:RATE_LIMIT_ENABLED
    $env:AGENTS_MODE = "a2a"
    $env:A2A_WRITER_URL = "http://127.0.0.1:$WriterPort"
    $env:A2A_CRITIC_URL = "http://127.0.0.1:$CriticPort"
    # This private instance exists to prove the A2A boundary, not the limiter;
    # per-user research limits would make a second run inside the hour fail.
    $env:RATE_LIMIT_ENABLED = "false"
    try {
        for ($i = 1; $i -le $attempts; $i++) {
            $proc = Start-Process -FilePath $python `
                -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$ApiPort", "--log-level", "info" `
                -WorkingDirectory $apiDir -PassThru -WindowStyle Hidden `
                -RedirectStandardOutput (Join-Path $env:TEMP "deepresearch_phase6_api.log") `
                -RedirectStandardError $apiLog
            if (Test-Http "$Base/healthz" 150) { return $proc }
            $tail = ""
            if (Test-Path $apiLog) { $tail = (Get-Content $apiLog -Tail 1) }
            Note "API attempt $i/$attempts failed - $tail"
            Stop-Proc $proc
            Start-Sleep -Seconds 4
        }
    } finally {
        $env:AGENTS_MODE = $oldMode
        $env:A2A_WRITER_URL = $oldWriterUrl
        $env:A2A_CRITIC_URL = $oldCriticUrl
        $env:RATE_LIMIT_ENABLED = $oldRate
    }
    Fail "API did not start after $attempts attempts (see $apiLog)"
}

function Read-ReportStream($url, $token) {
    $client = New-Object System.Net.Http.HttpClient
    $client.Timeout = [TimeSpan]::FromSeconds($TimeoutSec)
    $req = New-Object System.Net.Http.HttpRequestMessage([System.Net.Http.HttpMethod]::Get, $url)
    $req.Headers.Authorization = New-Object System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", $token)
    $req.Headers.Accept.ParseAdd("text/event-stream")

    $resp = $client.SendAsync($req, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).Result
    if (-not $resp.IsSuccessStatusCode) { Fail "report stream returned HTTP $([int]$resp.StatusCode)" }

    $reader = New-Object System.IO.StreamReader($resp.Content.ReadAsStreamAsync().Result)
    $event = ""; $data = @(); $tokens = 0; $chars = 0; $final = ""; $finalData = ""
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while (-not $reader.EndOfStream) {
        $line = $reader.ReadLine()
        if ($null -eq $line) { break }
        if ($line.StartsWith("event:")) { $event = $line.Substring(6).Trim(); continue }
        if ($line.StartsWith("data:")) { $data += $line.Substring(5).Trim(); continue }
        if ($line.Trim() -ne "") { continue }
        $payload = $data -join ""
        if ($event -eq "token") { $tokens++; $chars += (($payload | ConvertFrom-Json).text).Length }
        elseif ($event -eq "done" -or $event -eq "error") { $final = $event; $finalData = $payload; break }
        $event = ""; $data = @()
    }
    $reader.Dispose(); $resp.Dispose(); $client.Dispose()
    return @{ tokens = $tokens; chars = $chars; final = $final; finalData = $finalData; seconds = [int]$sw.Elapsed.TotalSeconds }
}

function Wait-ForReport($jobId, $h, $label) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $last = ""
    while ($sw.Elapsed.TotalSeconds -lt $TimeoutSec) {
        Start-Sleep -Seconds 4
        try { $rep = Invoke-RestMethod "$Base/research/$jobId/report" -Headers $h -TimeoutSec 30 }
        catch { Fail "polling report failed: $($_.Exception.Message) $(Body $_)" }
        if ($rep.status -ne $last) {
            Note ("  {0,5:N0}s  {1,-10} chars={2}" -f $sw.Elapsed.TotalSeconds, $rep.status, $rep.markdown.Length)
            $last = $rep.status
        }
        if ($rep.status -in @("ready", "failed")) { return @{ report = $rep; seconds = [int]$sw.Elapsed.TotalSeconds } }
    }
    Fail "$label did not finish within $TimeoutSec s (last: $last)"
}

try {
    # ------------------------------------------------------------ pre-flight
    Head "0. pre-flight + start the agent stack"
    if (-not (Test-Path $python)) { Fail "missing $python - create .venv first" }
    try { Invoke-RestMethod "http://127.0.0.1:4000/health/liveliness" -TimeoutSec 5 | Out-Null }
    catch { Fail "LiteLLM proxy is not on :4000. Run scripts\start_litellm.ps1 first." }

    $token = (Select-String -Path (Join-Path $root ".env") -Pattern '^A2A_TOKEN=(.+)$' | Select-Object -First 1)
    if (-not $token) { Fail "A2A_TOKEN missing from .env" }
    $token = $token.Matches[0].Groups[1].Value.Trim()
    if ($token -eq "") { Fail "A2A_TOKEN is empty in .env" }
    Ok "LiteLLM up, A2A token loaded ($($token.Length) chars)"

    foreach ($port in @($ApiPort, $WriterPort, $CriticPort)) {
        $busy = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        if ($busy) { Fail "port $port is already in use (pid $($busy.OwningProcess -join ',')). Free it or pass other -ApiPort/-WriterPort/-CriticPort." }
    }

    $startedWriter = Start-WithRetry "app.a2a.writer_app:app" $WriterPort `
        (Join-Path $env:TEMP "deepresearch_writer.log") $writerLog "writer"
    Ok "writer up on :$WriterPort (pid $($startedWriter.Id))"

    Start-Sleep -Seconds 2
    $startedCritic = Start-WithRetry "app.a2a.critic_app:app" $CriticPort `
        (Join-Path $env:TEMP "deepresearch_critic.log") $criticLog "critic"
    Ok "critic up on :$CriticPort (pid $($startedCritic.Id))"

    # A second API, in a2a mode, so the user's :8001 keeps working untouched.
    $startedApi = Start-ApiWithRetry
    Ok "API up on :$ApiPort in a2a mode (pid $($startedApi.Id))"

    # ------------------------------------------------------------ discovery
    Head "1. discovery: AgentCards (public, v1.0, JSONRPC, streaming)"
    $wCard = Invoke-RestMethod "http://127.0.0.1:$WriterPort/.well-known/agent-card.json" -TimeoutSec 15
    $cCard = Invoke-RestMethod "http://127.0.0.1:$CriticPort/.well-known/agent-card.json" -TimeoutSec 15
    foreach ($pair in @(@("writer", $wCard, "write-cited-report"), @("critic", $cCard, "critique-coverage"))) {
        $name = $pair[0]; $card = $pair[1]; $skill = $pair[2]
        if ($card.supportedInterfaces[0].protocolBinding -ne "JSONRPC") { Fail "$name card is not JSONRPC" }
        if ($card.supportedInterfaces[0].protocolVersion -ne "1.0") { Fail "$name card is not protocol 1.0" }
        if (-not $card.capabilities.streaming) { Fail "$name card does not advertise streaming" }
        if (-not ($card.skills | Where-Object { $_.id -eq $skill })) { Fail "$name card is missing skill $skill" }
        if (-not $card.securitySchemes.bearer) { Fail "$name card does not advertise the bearer scheme" }
        Ok "$name card: $($card.name) v$($card.version), skill=$skill, auth=bearer, streaming=true"
    }
    Note "card path: /.well-known/agent-card.json (readable without a token by design)"

    # ------------------------------------------------------- auth + protocol
    Head "2. auth + JSON-RPC error codes"
    $probe = @{ jsonrpc = "2.0"; id = "p1"; method = "GetTask"; params = @{ id = "00000000-0000-0000-0000-000000000000" } } | ConvertTo-Json -Depth 6

    try {
        Invoke-WebRequest -Method Post "http://127.0.0.1:$WriterPort/" -ContentType "application/json" `
            -Headers @{ "A2A-Version" = "1.0" } -Body $probe -TimeoutSec 15 | Out-Null
        Fail "RPC without a token was accepted"
    } catch {
        $code = [int]$_.Exception.Response.StatusCode
        $www = $_.Exception.Response.Headers["WWW-Authenticate"]
        if ($code -ne 401) { Fail "no token -> $code, expected 401" }
        Ok "no token -> 401 (WWW-Authenticate: $www)"
    }

    try {
        Invoke-WebRequest -Method Post "http://127.0.0.1:$CriticPort/" -ContentType "application/json" `
            -Headers @{ "A2A-Version" = "1.0"; Authorization = "Bearer definitely-wrong" } -Body $probe -TimeoutSec 15 | Out-Null
        Fail "RPC with a wrong token was accepted"
    } catch {
        if ([int]$_.Exception.Response.StatusCode -ne 401) { Fail "wrong token -> $([int]$_.Exception.Response.StatusCode), expected 401" }
        Ok "wrong token -> 401"
    }

    $unknown = @{ jsonrpc = "2.0"; id = "p2"; method = "NotAMethod"; params = @{} } | ConvertTo-Json -Depth 6
    $r = Invoke-RestMethod -Method Post "http://127.0.0.1:$WriterPort/" -ContentType "application/json" `
        -Headers @{ "A2A-Version" = "1.0"; Authorization = "Bearer $token" } -Body $unknown -TimeoutSec 15
    if ($r.error.code -ne -32601) { Fail "unknown method -> $($r.error.code), expected -32601" }
    Ok "unknown method -> -32601 Method not found"

    $r = Invoke-RestMethod -Method Post "http://127.0.0.1:$WriterPort/" -ContentType "application/json" `
        -Headers @{ "A2A-Version" = "1.0"; Authorization = "Bearer $token" } -Body $probe -TimeoutSec 15
    if ($r.error.code -ne -32001) { Fail "unknown task -> $($r.error.code), expected -32001" }
    Ok "unknown task -> -32001 Task not found"

    # ------------------------------------------------------- critic over A2A
    Head "3. critic agent: SendMessage -> verdict artifact + metadata"
    $criticBody = @{
        jsonrpc = "2.0"; id = "c1"; method = "SendMessage"
        params = @{
            message = @{
                messageId = [guid]::NewGuid().ToString(); role = "ROLE_USER"
                parts = @(@{ data = @{
                    question = "What is HTTP/2 multiplexing?"
                    sub_questions = @("How does multiplexing work?")
                    digest = "1. HTTP/2 multiplexes streams over one TCP connection. [high]"
                    max_gaps = 2
                } })
            }
        }
    } | ConvertTo-Json -Depth 12
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $r = Invoke-RestMethod -Method Post "http://127.0.0.1:$CriticPort/" -ContentType "application/json" `
        -Headers @{ "A2A-Version" = "1.0"; Authorization = "Bearer $token" } -Body $criticBody -TimeoutSec $TimeoutSec
    $task = $r.result.task
    if (-not $task) { Fail "critic returned no task: $($r | ConvertTo-Json -Depth 5 -Compress)" }
    if ($task.status.state -ne "TASK_STATE_COMPLETED") { Fail "critic task state '$($task.status.state)'" }
    $verdict = $task.artifacts[0].parts[0].text | ConvertFrom-Json
    if ($null -eq $verdict.sufficient) { Fail "critic artifact is not a verdict: $($task.artifacts[0].parts[0].text)" }
    if ($verdict.gaps.Count -gt 2) { Fail "critic returned $($verdict.gaps.Count) gaps, max_gaps=2" }
    Ok "task $($task.id) completed in $([int]$sw.Elapsed.TotalSeconds)s"
    Note "verdict: sufficient=$($verdict.sufficient) gaps=$($verdict.gaps.Count) reason=$($verdict.reason.Substring(0, [Math]::Min(90, $verdict.reason.Length)))"
    Note "artifact metadata: tokens=$($task.artifacts[0].metadata.tokens) model=$($task.artifacts[0].metadata.model)"
    if ([double]$task.artifacts[0].metadata.tokens -le 0) { Fail "critic artifact carries no token usage" }

    # ------------------------------------------------- API sees the agents
    Head "4. the API in a2a mode sees both agents"
    $hz = Invoke-RestMethod "$Base/healthz" -TimeoutSec 30
    if ($hz.overall -ne "healthy") { Fail "API healthz is '$($hz.overall)': $($hz | ConvertTo-Json -Compress)" }
    if (-not ("$($hz.writer)" -match '^ok')) { Fail "healthz writer=$($hz.writer)" }
    if (-not ("$($hz.critic)" -match '^ok')) { Fail "healthz critic=$($hz.critic)" }
    Ok "healthz: writer=$($hz.writer) critic=$($hz.critic)"

    $agents = Invoke-RestMethod "$Base/health/agents" -TimeoutSec 30
    if ($agents.mode -ne "a2a") { Fail "/health/agents mode is '$($agents.mode)'" }
    if ($agents.status -ne "ok") { Fail "/health/agents status is '$($agents.status)'" }
    Ok "/health/agents: mode=a2a status=ok ($($agents.writer), $($agents.critic))"

    if (-not $SkipE2E) {
        # ---------------------------------------------------- end-to-end report
        Head "5. login + pick a job with notes"
        try {
            $login = Invoke-RestMethod -Method Post "$Base/auth/login" -ContentType "application/json" `
                -Body (@{ email = $Email; password = $Password } | ConvertTo-Json) -TimeoutSec 30
        } catch { Fail "login failed: $($_.Exception.Message) $(Body $_)" }
        $h = @{ Authorization = "Bearer $($login.access_token)" }
        Ok "logged in as $Email"

        $jobs = Invoke-RestMethod "$Base/research?limit=50" -Headers $h -TimeoutSec 30
        if ($JobId) {
            $job = $jobs | Where-Object { $_.job_id -eq $JobId } | Select-Object -First 1
            if (-not $job) { Fail "job $JobId not found for this user" }
        } else {
            $job = $jobs | Where-Object { $_.status -eq "notes_ready" -and $_.notes_count -gt 0 } | Select-Object -First 1
        }
        if (-not $job) { Fail "no job in 'notes_ready' with notes - run verify_phase4 first" }
        Ok "job $($job.job_id): $($job.notes_count) notes"
        Note "$($job.question)"

        Head "6. generate the report THROUGH the writer agent"
        $before = 0
        if (Test-Path $writerLog) { $before = (Select-String -Path $writerLog -Pattern "writer agent: task" -ErrorAction SilentlyContinue).Count }
        $post = Invoke-RestMethod -Method Post "$Base/research/$($job.job_id)/report" -Headers $h -TimeoutSec 30
        if ($post.status -ne "generating") { Fail "POST report -> '$($post.status)'" }
        Ok "POST -> generating (report $($post.report_id))"

        $sw = [Diagnostics.Stopwatch]::StartNew()
        $sse = Read-ReportStream "$Base/research/$($job.job_id)/report/stream" $login.access_token
        if ($sse.final -ne "done") { Fail "stream ended with '$($sse.final)': $($sse.finalData)" }
        if ($sse.tokens -lt 10) { Fail "only $($sse.tokens) token events reached the browser" }
        Ok "streamed $($sse.tokens) chunks, $($sse.chars) chars in $([int]$sw.Elapsed.TotalSeconds)s"

        $waited = Wait-ForReport $job.job_id $h "report via A2A"
        $rep = $waited.report
        if ($rep.status -ne "ready") { Fail "report failed: $($rep.error)" }
        $v = $rep.validation
        Note "words=$($v.words) markers=$($v.markers) precision=$($v.precision) coverage=$($v.coverage) model=$($rep.model)"
        if ($v.precision -ne 1.0) { Fail "citation precision $($v.precision) - invented markers: $($v.unknown_markers -join ',')" }
        if ($v.markers -lt 10) { Fail "only $($v.markers) markers" }
        if ($v.coverage -lt 0.6) { Fail "coverage $($v.coverage)" }
        if ($rep.citations.Count -lt 1) { Fail "no citations stored" }
        if (-not $rep.model) { Fail "report.model is empty - the served model was not recorded" }
        Ok "ready: $($rep.citations.Count) citations, model '$($rep.model)', $($waited.seconds)s"

        $after = 0
        if (Test-Path $writerLog) { $after = (Select-String -Path $writerLog -Pattern "writer agent: task" -ErrorAction SilentlyContinue).Count }
        if ($after -le $before) {
            Fail "the writer service logged no task - the report did not go through the agent"
        }
        Ok "writer agent logged $($after - $before) new task(s)"

        # ------------------------------------------------------ failure path
        Head "7. failure is contained: writer down -> report fails with AgentUnavailable"
        Stop-Proc $startedWriter
        $startedWriter = $null
        Start-Sleep -Seconds 2
        $post = Invoke-RestMethod -Method Post "$Base/research/$($job.job_id)/report" -Headers $h -TimeoutSec 30
        if ($post.status -ne "generating") { Fail "POST with writer down -> '$($post.status)'" }
        $failed = Wait-ForReport $job.job_id $h "report with writer down"
        if ($failed.report.status -ne "failed") { Fail "expected failed, got '$($failed.report.status)'" }
        if ($failed.report.error -notmatch "AgentUnavailable|AgentError") {
            Fail "error does not name the agent failure: $($failed.report.error)"
        }
        Ok "failed in $($failed.seconds)s: $($failed.report.error.Substring(0, [Math]::Min(120, $failed.report.error.Length)))"
        Note "this is the proof the API used the remote path: a local run cannot raise AgentUnavailable"

        Head "8. restart the writer and regenerate (recovery)"
        $startedWriter = Start-WithRetry "app.a2a.writer_app:app" $WriterPort `
            (Join-Path $env:TEMP "deepresearch_writer.log") $writerLog "writer (restart)"
        Ok "writer back on :$WriterPort (pid $($startedWriter.Id))"

        $post = Invoke-RestMethod -Method Post "$Base/research/$($job.job_id)/report" -Headers $h -TimeoutSec 30
        $recovered = Wait-ForReport $job.job_id $h "recovery report"
        if ($recovered.report.status -ne "ready") { Fail "recovery failed: $($recovered.report.error)" }
        Ok "recovered in $($recovered.seconds)s: $($recovered.report.validation.markers) markers, model '$($recovered.report.model)'"
    } else {
        Note "skipping the end-to-end report (-SkipE2E)"
    }

    Write-Host "`nAll Phase 6 checks passed." -ForegroundColor Green
} catch {
    Write-Host "`n$($_.Exception.Message)" -ForegroundColor Red
    exit 1
} finally {
    if ($KeepRunning) {
        Write-Host "`nLeaving services up: API :$ApiPort (pid $($startedApi.Id)), writer :$WriterPort, critic :$CriticPort" -ForegroundColor Yellow
        Write-Host "Stop them: Stop-Process -Id $($startedApi.Id),$($startedWriter.Id),$($startedCritic.Id) -Force"
    } else {
        Stop-Proc $startedApi
        Stop-Proc $startedWriter
        Stop-Proc $startedCritic
        Write-Host "`nStopped the Phase 6 services." -ForegroundColor DarkGray
    }
}
