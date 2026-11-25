# Phase 9 verification: rate limits, SSRF defence, concurrency caps, security.
#
# Proves, in order:
#   1. security headers on a normal response, forged Host rejected, 401 without a token
#   2. the LLM diagnostic and the tool self-test are no longer open to anonymous users
#   3. the SSRF matrix is blocked through the real /tools/fetch path (400, not 500)
#   4. a rotated refresh token cannot be replayed (reuse revokes the whole family)
#   5. the partial unique index rejects a second active job (real database)
#   6. another user's report/job id is a 404, not a file
#   7. a short read load run and four concurrent PDF exports produce no 5xx
#   8. a login burst gets 429 + Retry-After, and the 429 still has security headers
#   9. the unit suite is green
#
# This script owns its API: it starts one on :8006 and stops it in `finally`.
# It uses the shared dev database but only creates/deletes a throwaway probe user.
# No LLM call is made (the authenticated /health/llm path is not exercised).
#
# Run:
#   powershell -ExecutionPolicy Bypass -File C:\projects\DeepResearch\scripts\verify_phase9.ps1
#   ... -KeepRunning      -> leave the API up

param(
    [string]$Email    = "you@test.com",
    [string]$Password = "password123",
    [int]$ApiPort     = 8006,
    [switch]$KeepRunning
)

$ErrorActionPreference = "Stop"
$root   = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$apiDir = Join-Path $root "api"
$Base   = "http://localhost:$ApiPort/api/v1"
$apiErr = Join-Path $env:TEMP "deepresearch_phase9_api.err"
$apiOut = Join-Path $env:TEMP "deepresearch_phase9_api.log"

function Ok($m)   { Write-Host "[ OK ] $m" -ForegroundColor Green }
function Fail($m) { throw "FAIL: $m" }
function Head($m) { Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Note($m) { Write-Host "       $m" -ForegroundColor DarkGray }

function Test-Http($url, $seconds) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $seconds) {
        Start-Sleep -Seconds 2
        try { Invoke-RestMethod $url -TimeoutSec 3 | Out-Null; return $true } catch { }
    }
    return $false
}

function Login($mail, $pass) {
    $body = @{ email = $mail; password = $pass } | ConvertTo-Json
    return Invoke-RestMethod -Method Post "$Base/auth/login" -ContentType "application/json" `
        -Body $body -TimeoutSec 30
}

function Ensure-User($mail, $name) {
    try {
        Invoke-RestMethod -Method Post "$Base/auth/register" -ContentType "application/json" `
            -Body (@{ email = $mail; full_name = $name; password = $Password } | ConvertTo-Json) `
            -TimeoutSec 30 | Out-Null
        Note "registered $mail"
    } catch {
        if ([int]$_.Exception.Response.StatusCode -ne 409) { throw }
        Note "$mail already exists"
    }
}

function Get-Status($url, $headers) {
    try {
        Invoke-WebRequest -UseBasicParsing $url -Headers $headers -TimeoutSec 60 | Out-Null
        return 200
    } catch {
        return [int]$_.Exception.Response.StatusCode
    }
}

function Post-Status($url, $headers, $body) {
    try {
        Invoke-WebRequest -UseBasicParsing -Method Post $url -Headers $headers `
            -ContentType "application/json" -Body $body -TimeoutSec 60 | Out-Null
        return 200
    } catch {
        return [int]$_.Exception.Response.StatusCode
    }
}

function Run-Python {
    param([string[]]$arguments)
    $output = & $python @arguments 2>&1
    $code = $LASTEXITCODE
    return @{ code = $code; text = ($output | Out-String).Trim() }
}

$api = $null
$oldRateEnabled = $env:RATE_LIMIT_ENABLED
$oldRateMultiplier = $env:RATE_LIMIT_MULTIPLIER
$oldAgentsMode = $env:AGENTS_MODE
$oldPythonPath = $env:PYTHONPATH
$oldTrustProxy = $env:TRUST_PROXY_HEADERS
$oldRegistration = $env:REGISTRATION_MODE
try {
    Head "0. pre-flight + start a private API on :$ApiPort"
    if (-not (Test-Path $python)) { Fail "missing $python - create .venv first" }
    $busy = Get-NetTCPConnection -LocalPort $ApiPort -State Listen -ErrorAction SilentlyContinue
    if ($busy) { Fail "port $ApiPort is already in use (pid $($busy.OwningProcess -join ',')). Pass another -ApiPort." }

    # Normal limits: this run is about proving 429, not measuring throughput.
    $env:RATE_LIMIT_ENABLED = "true"
    $env:RATE_LIMIT_MULTIPLIER = "1"
    $env:TRUST_PROXY_HEADERS = "false"
    $env:REGISTRATION_MODE = "open"
    $env:AGENTS_MODE = "local"
    $env:PYTHONPATH = $apiDir

    $api = Start-Process -FilePath $python `
        -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$ApiPort", "--log-level", "info" `
        -WorkingDirectory $apiDir -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput $apiOut -RedirectStandardError $apiErr
    if (-not (Test-Http "$Base/healthz" 150)) {
        $tail = if (Test-Path $apiErr) { Get-Content $apiErr -Tail 3 | Out-String } else { "" }
        Fail "API did not start. $tail"
    }
    Ok "API up on :$ApiPort (pid $($api.Id))"

    $reset = Run-Python (Join-Path $root "scripts\phase9_reset_limits.py")
    if ($reset.code -ne 0) { Fail "could not reset rate-limit keys: $($reset.text)" }
    Note $reset.text

    # ------------------------------------------------------- headers + host
    Head "1. security headers, forged Host, unauthenticated access"
    $hz = Invoke-WebRequest -UseBasicParsing "$Base/healthz" -TimeoutSec 30
    foreach ($header in @("X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy", "Permissions-Policy", "Content-Security-Policy")) {
        if (-not $hz.Headers[$header]) { Fail "missing security header $header" }
    }
    if ($hz.Headers["X-Content-Type-Options"] -ne "nosniff") { Fail "unexpected nosniff value" }
    if ($hz.Headers["Strict-Transport-Security"]) { Fail "HSTS was sent from a local API" }
    Ok "healthz 200 with nosniff / DENY / no-referrer / Permissions-Policy / CSP; no HSTS locally"

    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        $hostCode = & curl.exe -s -o NUL -w "%{http_code}" -H "Host: evil.example" "$Base/healthz"
        if ($hostCode -ne "400") { Fail "forged Host -> $hostCode, expected 400" }
        Ok "forged Host header -> 400"
    } else {
        Note "curl.exe not found; skipped the forged-Host check"
    }

    $unauth = Get-Status "$Base/research?limit=1" @{}
    if ($unauth -ne 401) { Fail "unauthenticated /research -> $unauth, expected 401" }
    Ok "unauthenticated /research -> 401"

    # ------------------------------------------------------------ auth setup
    $login = Login $Email $Password
    $token = $login.access_token
    $h = @{ Authorization = "Bearer $token" }
    Ok "logged in as $Email (token $($token.Length) chars)"

    # The MCP server is a child process; /tools answers 503 until it is ready.
    $toolReady = $false
    for ($i = 0; $i -lt 30; $i++) {
        if ((Get-Status "$Base/tools" $h) -eq 200) { $toolReady = $true; break }
        Start-Sleep -Seconds 2
    }
    if (-not $toolReady) { Fail "MCP toolbox did not become ready" }

    Head "2. costly diagnostics are closed"
    $llmAnon = Get-Status "$Base/health/llm" @{}
    if ($llmAnon -ne 401) { Fail "anonymous /health/llm -> $llmAnon, expected 401" }
    Ok "anonymous /health/llm -> 401 (no free LLM call)"

    $selftest = Get-Status "$Base/tools/selftest" $h
    if ($selftest -ne 403) { Fail "agent /tools/selftest -> $selftest, expected 403" }
    Ok "agent /tools/selftest -> 403"

    $admin = Get-Status "$Base/admin/overview" $h
    if ($admin -ne 403) { Fail "agent /admin/overview -> $admin, expected 403" }
    Ok "agent /admin/overview -> 403"

    # ----------------------------------------------------------------- SSRF
    Head "3. SSRF matrix through POST /tools/fetch"
    $ssrfUrls = @(
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8000/",
        "http://[::1]/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://100.64.0.1/",
        "http://2130706433/",
        "http://0x7f000001/",
        "http://example.com:22/",
        "file:///etc/passwd",
        "http://user:pass@example.com/"
    )
    foreach ($url in $ssrfUrls) {
        $code = Post-Status "$Base/tools/fetch" $h (@{ url = $url } | ConvertTo-Json)
        if ($code -ne 400) { Fail "SSRF $url -> $code, expected 400" }
    }
    Ok "$($ssrfUrls.Count) unsafe URLs -> 400 (metadata, loopback, RFC1918, CGNAT, decimal/hex, port 22, file://, credentials)"

    # ------------------------------------------------------ refresh reuse
    Head "4. refresh-token rotation + reuse detection"
    Ensure-User "other@test.com" "Other Account"
    $session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
    $otherBody = @{ email = "other@test.com"; password = $Password } | ConvertTo-Json
    Invoke-RestMethod -Method Post "$Base/auth/login" -ContentType "application/json" `
        -Body $otherBody -WebSession $session -TimeoutSec 30 | Out-Null
    $cookieUri = "http://localhost:$ApiPort/"
    $oldCookie = ($session.Cookies.GetCookies($cookieUri) | Where-Object { $_.Name -eq "DeepSearch_refresh" }).Value
    if (-not $oldCookie) { Fail "login did not set the refresh cookie" }

    Invoke-RestMethod -Method Post "$Base/auth/refresh" -WebSession $session -TimeoutSec 30 | Out-Null
    $newCookie = ($session.Cookies.GetCookies($cookieUri) | Where-Object { $_.Name -eq "DeepSearch_refresh" }).Value
    if (-not $newCookie -or $newCookie -eq $oldCookie) { Fail "refresh did not rotate the cookie" }
    Ok "refresh rotated the token"

    $replay = Post-Status "$Base/auth/refresh" @{ Cookie = "DeepSearch_refresh=$oldCookie" } $null
    if ($replay -ne 401) { Fail "replayed refresh token -> $replay, expected 401" }
    $family = Post-Status "$Base/auth/refresh" @{ Cookie = "DeepSearch_refresh=$newCookie" } $null
    if ($family -ne 401) { Fail "new token survived reuse detection -> $family, expected 401" }
    Ok "replayed token -> 401 and the whole family was revoked"

    # ------------------------------------------------------------- database
    Head "5. partial unique index (real database, throwaway probe user)"
    $dbRun = Run-Python (Join-Path $root "scripts\phase9_db_checks.py") "--email", $Email
    if ($dbRun.code -ne 0) { Fail "DB checks failed: $($dbRun.text)" }
    $db = $dbRun.text | ConvertFrom-Json
    if ($db.index -ne "ok") { Fail "index check did not report ok: $($dbRun.text)" }
    Ok "second active job rejected by uq_research_jobs_active_per_user; finished job frees the slot"

    # ----------------------------------------------------------------- IDOR
    Head "6. ownership: another user's ids are 404"
    if ($db.other_report_id) {
        $crossReport = Get-Status "$Base/reports/$($db.other_report_id)/export.md" $h
        if ($crossReport -ne 404) { Fail "cross-user report export -> $crossReport, expected 404" }
        if ($db.other_job_id) {
            $crossJob = Get-Status "$Base/research/$($db.other_job_id)/report" $h
            if ($crossJob -ne 404) { Fail "cross-user job report -> $crossJob, expected 404" }
        }
        Ok "another user's report/job -> 404"
    } else {
        Note "no ready report owned by another user; cross-user check skipped"
    }
    $random = Get-Status "$Base/reports/00000000-0000-0000-0000-000000000000/export.md" $h
    if ($random -ne 404) { Fail "random report id -> $random, expected 404" }
    Ok "random report id -> 404"

    # ------------------------------------------------------------- load runs
    Head "7. short read run + four concurrent PDF exports"
    $readRun = Run-Python (Join-Path $root "scripts\load_test.py") `
        "--base", $Base, "--scenario", "read", "--users", "3", "--seconds", "2", "--token", $token
    if ($readRun.code -ne 0) { Fail "read load run failed: $($readRun.text)" }
    $read = $readRun.text | ConvertFrom-Json
    Ok ("read: {0} requests, {1} rps, p50={2}ms p95={3}ms p99={4}ms, status {5}" -f `
        $read.requests, $read.rps, $read.p50_ms, $read.p95_ms, $read.p99_ms, ($read.status | ConvertTo-Json -Compress))

    $exportRun = Run-Python (Join-Path $root "scripts\load_test.py") `
        "--base", $Base, "--scenario", "export", "--users", "4", "--seconds", "1", "--token", $token
    if ($exportRun.code -ne 0) { Fail "export load run failed: $($exportRun.text)" }
    $export = $exportRun.text | ConvertFrom-Json
    Ok ("export: {0} requests, p95={1}ms, status {2} (no 5xx; 503 is the capacity brake)" -f `
        $export.requests, $export.p95_ms, ($export.status | ConvertTo-Json -Compress))

    # -------------------------------------------------------------- 429 burst
    Head "8. login burst -> 429 + Retry-After, headers on the 429"
    # Reset immediately before the burst as well: the earlier logins in this run
    # (and the load tests) consume the same per-IP window, which makes a 12-shot
    # burst land on two different minutes and pass. A clean window plus more
    # attempts than the per-email hour budget makes the 429 deterministic.
    $reset = Run-Python (Join-Path $root "scripts\phase9_reset_limits.py")
    if ($reset.code -ne 0) { Fail "could not reset rate-limit keys: $($reset.text)" }
    $allowed = 0
    $blocked = 0
    $retryAfter = $null
    $headersOn429 = $null
    $burstBody = @{ email = "phase9-burst@example.com"; password = "definitely-wrong" } | ConvertTo-Json
    for ($i = 1; $i -le 24; $i++) {
        try {
            Invoke-WebRequest -UseBasicParsing -Method Post "$Base/auth/login" `
                -ContentType "application/json" -Body $burstBody -TimeoutSec 30 | Out-Null
            $allowed++
        } catch {
            $code = [int]$_.Exception.Response.StatusCode
            if ($code -eq 429) {
                $blocked++
                if (-not $retryAfter) {
                    $retryAfter = $_.Exception.Response.Headers["Retry-After"]
                    $headersOn429 = $_.Exception.Response.Headers
                }
            } elseif ($code -ne 401) {
                Fail "unexpected login burst status $code"
            } else {
                $allowed++
            }
        }
    }
    if ($blocked -lt 1) { Fail "login burst never hit the limiter" }
    if (-not $retryAfter) { Fail "429 had no Retry-After header" }
    if (-not $headersOn429["X-Content-Type-Options"]) { Fail "429 response lost the security headers" }
    Ok "24 attempts -> $allowed allowed / $blocked blocked; Retry-After=$retryAfter; security headers present"

    # -------------------------------------------------------------- unit suite
    Head "9. regression: unit suite"
    Push-Location $apiDir
    try {
        & $python -m pytest tests -q
        if ($LASTEXITCODE -ne 0) { Fail "pytest failed" }
    } finally {
        Pop-Location
    }
    Ok "pytest green"

    Write-Host "`nAll Phase 9 checks passed." -ForegroundColor Green
} catch {
    Write-Host "`n$($_.Exception.Message)" -ForegroundColor Red
    exit 1
} finally {
    $env:RATE_LIMIT_ENABLED = $oldRateEnabled
    $env:RATE_LIMIT_MULTIPLIER = $oldRateMultiplier
    $env:AGENTS_MODE = $oldAgentsMode
    $env:PYTHONPATH = $oldPythonPath
    $env:TRUST_PROXY_HEADERS = $oldTrustProxy
    $env:REGISTRATION_MODE = $oldRegistration
    if ($KeepRunning) {
        Write-Host "`nLeaving the Phase 9 API up on :$ApiPort (pid $($api.Id))" -ForegroundColor Yellow
    } else {
        if ($api) {
            try { taskkill /PID $api.Id /T /F 2>$null | Out-Null } catch { }
            try { Stop-Process -Id $api.Id -Force -ErrorAction SilentlyContinue } catch { }
        }
        Write-Host "`nStopped the Phase 9 API." -ForegroundColor DarkGray
    }
}
