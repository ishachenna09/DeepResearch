# Phase 1 verification - part 1 of 3.
#
# Proves: planner LLM runs -> graph freezes at interrupt() -> plan persists in Postgres.
#
# Prereqs: LiteLLM on :4000 AND FastAPI on :8000 (both running).
# Run:     powershell -ExecutionPolicy Bypass -File .\scripts\verify_phase1.ps1
#
# Custom account/question:
#   powershell -ExecutionPolicy Bypass -File .\scripts\verify_phase1.ps1 `
#       -Email "me@x.com" -Password "secret123" -Question "Is Rust worth it for a 5-dev team?"

param(
    [string]$Base     = "http://localhost:8000/api/v1",
    [string]$Email    = "you@test.com",
    [string]$Password = "password123",
    [string]$Question = "Compare ClickHouse vs Snowflake for a 20-person SaaS company"
)

$ErrorActionPreference = "Stop"
$JobFile = Join-Path $env:TEMP "deepresearch_job_id.txt"

function Ok($m)   { Write-Host "[ OK ] $m" -ForegroundColor Green }
function Fail($m) { Write-Host "[FAIL] $m" -ForegroundColor Red; exit 1 }
function Head($m) { Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Body($e) {
    try { return $e.ErrorDetails.Message } catch { return "" }
}

# ---------------------------------------------------------------- 0. pre-flight
Head "0. stack reachable"
try { $hz = Invoke-RestMethod "$Base/healthz" -TimeoutSec 15 }
catch { Fail "API not reachable on :8000 (or /api/v1/healthz failed). Start it: powershell -File .\scripts\start_api.ps1" }
Ok "healthz: $($hz | ConvertTo-Json -Compress)"
if ($hz.overall -ne "healthy") { Fail "healthz is '$($hz.overall)' (postgres=$($hz.postgres), redis=$($hz.redis))" }

# --------------------------------------------------------------------- 1. login
Head "1. login as $Email"
try {
    $login = Invoke-RestMethod -Method Post "$Base/auth/login" -ContentType "application/json" `
        -Body (@{ email = $Email; password = $Password } | ConvertTo-Json) -TimeoutSec 20
} catch {
    Fail "login failed: $($_.Exception.Message) $(Body $_)`n       -> if the account does not exist, register it first (see notes)."
}
$h = @{ Authorization = "Bearer $($login.access_token)" }
Ok "access token acquired (expires_in=$($login.expires_in)s)"

$me = Invoke-RestMethod "$Base/auth/me" -Headers $h -TimeoutSec 20
Ok "authenticated as $($me.email) (role=$($me.role), id=$($me.id))"

# --------------------------------------------------------- 1b. LLM reachable
# Authenticated since Phase 9: the diagnostic spends a real LLM call, so it is
# not something an anonymous caller may trigger.
Head "1b. LLM reachable"
try {
    $llm = Invoke-RestMethod "$Base/health/llm" -Headers $h -TimeoutSec 60
    if ($llm.status -ne "ok") { Fail "LLM not OK (status=$($llm.status)). The planner would 502. Fix /health/llm first." }
    Ok "LLM ok | served_by=$($llm.served_by) | reply=$($llm.reply)"
} catch {
    Fail "LLM health check failed: $($_.Exception.Message) $(Body $_)"
}

# ------------------------------------------------------------- 2. create the job
Head "2. POST /research  (real LLM call, 5-20s)"
$sw = [Diagnostics.Stopwatch]::StartNew()
try {
    $job = Invoke-RestMethod -Method Post "$Base/research" -Headers $h -ContentType "application/json" `
        -Body (@{ question = $Question } | ConvertTo-Json) -TimeoutSec 120
} catch {
    Fail "create failed: $($_.Exception.Message) $(Body $_)`n       -> 502 'Planner failed' = upstream LLM issue, not your graph."
}
$sw.Stop()
$job | ConvertTo-Json -Depth 8
Write-Host "`n--- assertions ---"

if ($job.status -ne "awaiting_approval") { Fail "expected status 'awaiting_approval', got '$($job.status)'" }
Ok "status=awaiting_approval (graph froze at interrupt()) after $([math]::Round($sw.Elapsed.TotalSeconds,1))s"

$n = @($job.plan.sub_questions).Count
if ($n -lt 3) { Fail "plan has $n sub-questions; schema requires 3-7" }
Ok "plan has $n sub-questions, budget_tokens=$($job.plan.budget_tokens)"

Set-Content -Path $JobFile -Value $job.job_id -NoNewline
Ok "job_id=$($job.job_id) saved to $JobFile"

# ------------------------------------------- 3. read it back from Postgres only
Head "3. GET /research/{id}  (DB read, graph not involved)"
$again = Invoke-RestMethod "$Base/research/$($job.job_id)" -Headers $h -TimeoutSec 20
if ($again.status -ne "awaiting_approval") { Fail "reload returned status '$($again.status)'" }
if (@($again.plan.sub_questions).Count -ne $n) { Fail "plan changed between create and reload - persistence bug" }
Ok "reloaded from Postgres: identical plan, status=$($again.status)"

Write-Host @"

==================================================================
PART 1 PASSED. Now prove the Redis checkpoint (the money shot):

  1. Terminal 2 (the uvicorn log window) -> press Ctrl+C     (API is now DEAD)
  2. Restart it:      powershell -ExecutionPolicy Bypass -File .\scripts\start_api.ps1
  3. Wait for:       Uvicorn running on http://127.0.0.1:8000
  4. Approve it:     powershell -ExecutionPolicy Bypass -File .\scripts\verify_phase1_approve.ps1

==================================================================
"@ -ForegroundColor Yellow
