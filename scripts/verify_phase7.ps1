# Phase 7 verification: eval gate + cost dashboard + Langfuse tracing.
#
# Proves, in order:
#   1. the eval gate runs offline and passes on recorded artifacts, and its
#      negative controls are rejected (a gate that cannot say no is decoration)
#   2. the gate is backed by tests, and those tests fail when the metrics break
#   3. /usage returns measured tokens and a shadow cost with an as-of date
#   4. scope=all is 403 for a non-admin (cost is still user data)
#   5. Langfuse is actually reachable (ok) or deliberately off (disabled) -
#      "configured" and "working" are different states
#   6. optional -Live: regenerate one report through the local writer and show
#      that its tokens land in /usage and that tracing stayed connected
#
# This script owns its API: it starts one on :8003 and stops it in `finally`,
# so the API you have on :8001 is untouched. It shares Postgres with it, and
# startup marks orphaned `generating` reports as failed - do not run this while
# a report is in flight on :8001. LiteLLM is only needed for -Live.
#
# Prereqs: migrations at head, at least one note-bearing job, .env present.
# Run:
#   powershell -ExecutionPolicy Bypass -File C:\projects\DeepResearch\scripts\verify_phase7.ps1
#   ... -Live             -> also regenerate a report (needs LiteLLM on :4000)
#   ... -KeepRunning      -> leave the API up

param(
    [string]$Email    = "you@test.com",
    [string]$Password = "password123",
    [int]$ApiPort     = 8003,
    [int]$TimeoutSec  = 240,
    [switch]$Live,
    [switch]$KeepRunning
)

$ErrorActionPreference = "Stop"
$root   = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$apiDir = Join-Path $root "api"
$Base   = "http://localhost:$ApiPort/api/v1"
$apiErr = Join-Path $env:TEMP "deepresearch_phase7_api.err"
$apiOut = Join-Path $env:TEMP "deepresearch_phase7_api.log"

function Ok($m)   { Write-Host "[ OK ] $m" -ForegroundColor Green }
function Fail($m) { throw "FAIL: $m" }
function Head($m) { Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Note($m) { Write-Host "       $m" -ForegroundColor DarkGray }
function Body($e) { try { return $e.ErrorDetails.Message } catch { return "" } }

function Test-Http($url, $seconds) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $seconds) {
        Start-Sleep -Seconds 2
        try { Invoke-RestMethod $url -TimeoutSec 3 | Out-Null; return $true } catch { }
    }
    return $false
}

$api = $null
try {
    # ------------------------------------------------------------ pre-flight
    Head "0. pre-flight + start a private API on :$ApiPort"
    if (-not (Test-Path $python)) { Fail "missing $python - create .venv first" }

    $busy = Get-NetTCPConnection -LocalPort $ApiPort -State Listen -ErrorAction SilentlyContinue
    if ($busy) { Fail "port $ApiPort is already in use (pid $($busy.OwningProcess -join ',')). Pass another -ApiPort." }

    $oldMode = $env:AGENTS_MODE
    $env:AGENTS_MODE = "local"
    try {
        $attempt = 0
        while ($attempt -lt 3) {
            $attempt++
            $api = Start-Process -FilePath $python `
                -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$ApiPort", "--log-level", "info" `
                -WorkingDirectory $apiDir -PassThru -WindowStyle Hidden `
                -RedirectStandardOutput $apiOut -RedirectStandardError $apiErr
            if (Test-Http "$Base/healthz" 150) { break }
            $tail = if (Test-Path $apiErr) { Get-Content $apiErr -Tail 1 } else { "" }
            Note "API attempt $attempt/3 failed - $tail"
            if ($api) { try { taskkill /PID $api.Id /T /F 2>$null | Out-Null } catch { } }
            Start-Sleep -Seconds 4
        }
    } finally {
        $env:AGENTS_MODE = $oldMode
    }
    if (-not (Test-Http "$Base/healthz" 5)) { Fail "API did not start (see $apiErr)" }
    Ok "API up on :$ApiPort (pid $($api.Id))"

    $login = Invoke-RestMethod -Method Post "$Base/auth/login" -ContentType "application/json" `
        -Body (@{ email = $Email; password = $Password } | ConvertTo-Json) -TimeoutSec 30
    $h = @{ Authorization = "Bearer $($login.access_token)" }
    $me = Invoke-RestMethod "$Base/auth/me" -Headers $h -TimeoutSec 30
    Ok "logged in as $Email (role $($me.role))"

    # ------------------------------------------------------------ usage API
    Head "1. /usage: measured tokens + shadow cost"
    $usage = Invoke-RestMethod "$Base/usage" -Headers $h -TimeoutSec 60
    if ($usage.totals.tokens -le 0) { Fail "usage reports 0 tokens; run a job first" }
    if (-not $usage.pricing.as_of) { Fail "usage has no pricing as_of date" }
    if ($usage.pricing.output_share -le 0) { Fail "usage has no input/output assumption" }
    if (-not $usage.by_model) { Fail "usage has no per-model breakdown" }
    Ok ("tokens={0:N0} (research {1:N0} + report {2:N0}), jobs={3}, notes={4}" -f `
        $usage.totals.tokens, $usage.totals.research_tokens, $usage.totals.report_tokens, `
        $usage.totals.jobs, $usage.totals.notes)
    Ok ("shadow cost = `${0} at list prices, as of {1}; free tier billed `${2}" -f `
        $usage.totals.estimated_usd, $usage.pricing.as_of, "0.00")
    foreach ($row in $usage.by_model) {
        Note ("{0,-24} {1,3} report(s) {2,10:N0} tokens  `${3}`n" -f $row.model, $row.reports, $row.tokens, $row.estimated_usd).TrimEnd()
    }
    if ($usage.by_day.Count -gt 0) { Note "daily buckets: $($usage.by_day.Count)" }

    Head "2. RBAC: scope=all is admin-only"
    try {
        Invoke-RestMethod "$Base/usage?scope=all" -Headers $h -TimeoutSec 30 | Out-Null
        Fail "scope=all was accepted for role '$($me.role)'"
    } catch {
        $code = [int]$_.Exception.Response.StatusCode
        if ($code -ne 403) { Fail "scope=all -> $code, expected 403" }
    }
    Ok "scope=all -> 403 for role '$($me.role)'"

    # ------------------------------------------------------------ langfuse
    Head "3. Langfuse: configured is not the same as working"
    $lf = Invoke-RestMethod "$Base/health/langfuse" -TimeoutSec 60
    if ($lf.status -eq "error") { Fail "Langfuse is configured but failing: $($lf.detail)" }
    if ($lf.status -eq "ok") {
        Ok "Langfuse reachable: $($lf.host) ($($lf.detail))"
        Note "each report runs inside tags=[report,mode:*], session_id=<job id>"
    } else {
        Ok "Langfuse disabled by configuration (no keys); tracing degrades to no-op"
    }

    # ------------------------------------------------------------ eval gate
    Head "4. eval gate: fixtures + negative controls"
    $env:PYTHONIOENCODING = "utf-8"
    & $python (Join-Path $root "evals\run_evals.py") 2>&1 | ForEach-Object { Write-Host $_ }
    $evalExit = $LASTEXITCODE
    $latest = Join-Path $root "evals\results\latest.json"
    if (-not (Test-Path $latest)) { Fail "eval gate wrote no $latest" }
    $summary = Get-Content $latest -Raw | ConvertFrom-Json
    if ($evalExit -ne 0 -or -not $summary.gate.passed) {
        $bad = ($summary.cases | Where-Object { -not $_.passed } | ForEach-Object { $_.case_id }) -join ", "
        Fail "eval gate failed (graded $($summary.gate.graded), failing: $bad)"
    }
    if ($summary.gate.graded -lt $summary.gate.min_cases) {
        Fail "only $($summary.gate.graded) case(s) graded, minimum $($summary.gate.min_cases)"
    }
    $accepted = $summary.negative_controls | Where-Object { -not $_.rejected }
    if ($accepted) { Fail "negative control(s) passed the gate: $(($accepted | ForEach-Object { $_.control }) -join ', ')" }
    Ok "gate passed: $($summary.gate.graded) cases, $($summary.negative_controls.Count) controls rejected"
    Note ("evaluated tokens {0:N0}, shadow `${1}" -f $summary.totals.tokens, $summary.totals.estimated_usd)

    Head "5. the gate itself is tested"
    Push-Location $apiDir
    try {
        & $python -m pytest tests\test_evals.py tests\test_pricing.py -q 2>&1 | Select-Object -Last 3 | ForEach-Object { Write-Host $_ }
        if ($LASTEXITCODE -ne 0) { Fail "eval/pricing tests failed" }
    } finally {
        Pop-Location
    }
    Ok "grader + pricing tests pass (the gate rejects stripped and invented citations)"

    # ------------------------------------------------------------ optional live
    if ($Live) {
        Head "6. live: regenerate a report and watch its tokens land"
        try { Invoke-RestMethod "http://127.0.0.1:4000/health/liveliness" -TimeoutSec 5 | Out-Null }
        catch { Fail "LiteLLM is not on :4000. Run scripts\start_litellm.ps1 or drop -Live." }

        $before = (Invoke-RestMethod "$Base/usage" -Headers $h -TimeoutSec 60).totals
        $jobs = Invoke-RestMethod "$Base/research?limit=50" -Headers $h -TimeoutSec 30
        $job = $jobs | Where-Object { $_.status -eq "notes_ready" -and $_.notes_count -gt 0 } | Select-Object -First 1
        if (-not $job) { Fail "no job in notes_ready with notes" }
        Note "job $($job.job_id): $($job.notes_count) notes - $($job.question.Substring(0, [Math]::Min(70, $job.question.Length)))"

        $post = Invoke-RestMethod -Method Post "$Base/research/$($job.job_id)/report" -Headers $h -TimeoutSec 30
        if ($post.status -ne "generating") { Fail "POST report -> '$($post.status)'" }

        $sw = [Diagnostics.Stopwatch]::StartNew()
        do {
            Start-Sleep -Seconds 3
            $rep = Invoke-RestMethod "$Base/research/$($job.job_id)/report" -Headers $h -TimeoutSec 30
        } while ($rep.status -eq "generating" -and $sw.Elapsed.TotalSeconds -lt $TimeoutSec)
        if ($rep.status -ne "ready") { Fail "report -> '$($rep.status)': $($rep.error)" }
        Ok "report ready in $([int]$sw.Elapsed.TotalSeconds)s: $($rep.validation.words) words, $($rep.citations.Count) citations, model '$($rep.model)'"
        if ($rep.tokens_used -le 0) { Fail "report has no token accounting" }

        Start-Sleep -Seconds 3   # Langfuse exports in the background
        $after = (Invoke-RestMethod "$Base/usage" -Headers $h -TimeoutSec 60).totals
        if ($after.report_tokens -le 0) { Fail "usage still reports 0 report tokens" }
        Ok ("usage after: report_tokens={0:N0} (was {1:N0}), total={2:N0}" -f `
            $after.report_tokens, $before.report_tokens, $after.tokens)

        $lf2 = Invoke-RestMethod "$Base/health/langfuse" -TimeoutSec 60
        if ($lf2.status -eq "error") { Fail "Langfuse broke during generation: $($lf2.detail)" }
        Ok "Langfuse still $($lf2.status) after the report"

        if ($lf2.status -eq "ok") {
            & $python (Join-Path $root "scripts\check_traces.py") $job.job_id --wait 45 2>&1 | ForEach-Object { Write-Host $_ }
            if ($LASTEXITCODE -ne 0) {
                Fail "no Langfuse spans found for session $($job.job_id) - session/tag propagation did not take effect"
            }
            Ok "Langfuse spans found for session $($job.job_id) (tag report, session = job id)"
        } else {
            Note "tracing disabled, so there is no span to check"
        }
    } else {
        Note "skipping the live report (-Live)"
    }

    Write-Host "`nAll Phase 7 checks passed." -ForegroundColor Green
} catch {
    Write-Host "`n$($_.Exception.Message)" -ForegroundColor Red
    exit 1
} finally {
    if ($KeepRunning) {
        Write-Host "`nLeaving the API up on :$ApiPort (pid $($api.Id))" -ForegroundColor Yellow
    } else {
        if ($api) {
            try { taskkill /PID $api.Id /T /F 2>$null | Out-Null } catch { }
            try { Stop-Process -Id $api.Id -Force -ErrorAction SilentlyContinue } catch { }
        }
        Write-Host "`nStopped the Phase 7 API." -ForegroundColor DarkGray
    }
}
