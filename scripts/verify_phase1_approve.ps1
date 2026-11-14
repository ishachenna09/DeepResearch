# Phase 1 verification - part 2 of 3.
#
# RUN THIS ONLY AFTER killing (Ctrl+C) and restarting the API.
# Proves: the graph state survived the process death in Redis and resumes
# from inside approval_gate -> status "approved".
#
# Run: powershell -ExecutionPolicy Bypass -File .\scripts\verify_phase1_approve.ps1
#
# Use -Action reject to exercise the reject path on the SAME job instead.

param(
    [string]$Base     = "http://localhost:8000/api/v1",
    [string]$Email    = "you@test.com",
    [string]$Password = "password123",
    [ValidateSet("approve", "reject")]
    [string]$Action   = "approve"
)

$ErrorActionPreference = "Stop"
$JobFile = Join-Path $env:TEMP "deepresearch_job_id.txt"

function Ok($m)   { Write-Host "[ OK ] $m" -ForegroundColor Green }
function Fail($m) { Write-Host "[FAIL] $m" -ForegroundColor Red; exit 1 }
function Head($m) { Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Body($e) {
    try { return $e.ErrorDetails.Message } catch { return "" }
}

if (-not (Test-Path $JobFile)) { Fail "no saved job id at $JobFile - run verify_phase1.ps1 first." }
$jobId = (Get-Content $JobFile -Raw).Trim()

# --- fresh process? confirm the API really restarted (uptime resets) ---
Head "0. API reachable (must be a FRESH process)"
try { $hz = Invoke-RestMethod "$Base/healthz" -TimeoutSec 15 }
catch { Fail "API not reachable - restart it: powershell -File .\scripts\start_api.ps1" }
Ok "healthz: $($hz | ConvertTo-Json -Compress)"

Head "1. login (new token; old one may have expired)"
try {
    $login = Invoke-RestMethod -Method Post "$Base/auth/login" -ContentType "application/json" `
        -Body (@{ email = $Email; password = $Password } | ConvertTo-Json) -TimeoutSec 20
} catch { Fail "login failed: $($_.Exception.Message) $(Body $_)" }
$h = @{ Authorization = "Bearer $($login.access_token)" }
Ok "authenticated as $Email"

Head "2. GET job BEFORE resume (Postgres still says awaiting_approval)"
$before = Invoke-RestMethod "$Base/research/$jobId" -Headers $h -TimeoutSec 20
if ($before.status -ne "awaiting_approval") {
    Fail "status is '$($before.status)', expected 'awaiting_approval' - job already decided (run verify_phase1.ps1 for a new one)."
}
Ok "status=$($before.status) ($(@($before.plan.sub_questions).Count) sub-questions still there)"

Head "3. resumes from Redis checkpoint -> $Action"
$sw = [Diagnostics.Stopwatch]::StartNew()
try {
    $res = Invoke-RestMethod -Method Post "$Base/research/$jobId/plan/approve" -Headers $h `
        -ContentType "application/json" -Body (@{ action = $Action } | ConvertTo-Json) -TimeoutSec 60
} catch {
    Fail "resume failed: $($_.Exception.Message) $(Body $_)`n       -> 502 'Resume failed' means the Redis checkpoint was lost (wrong REDIS_URL?)."
}
$sw.Stop()
$res | ConvertTo-Json -Depth 8
Write-Host "`n--- assertions ---"

$expected = if ($Action -eq "approve") { "approved" } else { "rejected" }
if ($res.status -ne $expected) { Fail "expected status '$expected', got '$($res.status)'" }
Ok "status=$($res.status) after a FULL API restart, in $([math]::Round($sw.Elapsed.TotalSeconds,1))s"

Head "4. GET again (final state persisted)"
$after = Invoke-RestMethod "$Base/research/$jobId" -Headers $h -TimeoutSec 20
if ($after.status -ne $res.status) { Fail "DB says '$($after.status)' but resume returned '$($res.status)'" }
Ok "Postgres agrees: status=$($after.status)"

Remove-Item $JobFile -Force
Write-Host @"

==================================================================
PART 2 PASSED - Redis checkpoint survived a process restart.
That is the Phase 1 demo. Optional extras:

  powershell -ExecutionPolicy Bypass -File .\scripts\verify_phase1_negative.ps1

==================================================================
"@ -ForegroundColor Yellow
