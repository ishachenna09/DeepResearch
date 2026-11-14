# Phase 1 verification - part 3 of 3 (optional).
#
# Proves the two negative paths:
#   A. reject  -> status "rejected"
#   B. 404 on someone else's job (IDOR / ownership check)
#
# Run: powershell -ExecutionPolicy Bypass -File .\scripts\verify_phase1_negative.ps1

param(
    [string]$Base     = "http://localhost:8000/api/v1",
    [string]$Email    = "you@test.com",
    [string]$Password = "password123",
    [string]$Question = "What are the tradeoffs of Postgres vs SQLite for a small SaaS?"
)

$ErrorActionPreference = "Stop"
$OtherEmail = "other$(Get-Random -Maximum 9999)@test.com"
$OtherPass  = "password123"

function Ok($m)   { Write-Host "[ OK ] $m" -ForegroundColor Green }
function Fail($m) { Write-Host "[FAIL] $m" -ForegroundColor Red; exit 1 }
function Head($m) { Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Body($e) {
    try { return $e.ErrorDetails.Message } catch { return "" }
}
function StatusCode($e) {
    try { return [int]$e.Exception.Response.StatusCode } catch { return 0 }
}

# ------------------------------------------------------------------- login
Head "1. login as $Email"
$login = Invoke-RestMethod -Method Post "$Base/auth/login" -ContentType "application/json" `
    -Body (@{ email = $Email; password = $Password } | ConvertTo-Json) -TimeoutSec 20
$h = @{ Authorization = "Bearer $($login.access_token)" }
Ok "authenticated"

# ------------------------------------------------------------ A. reject path
Head "2. new job -> reject (needs a fresh job; decided jobs 409)"
$job = Invoke-RestMethod -Method Post "$Base/research" -Headers $h -ContentType "application/json" `
    -Body (@{ question = $Question } | ConvertTo-Json) -TimeoutSec 120
Ok "job $($job.job_id) status=$($job.status)"

$rej = Invoke-RestMethod -Method Post "$Base/research/$($job.job_id)/plan/approve" -Headers $h `
    -ContentType "application/json" -Body '{"action":"reject"}' -TimeoutSec 60
if ($rej.status -ne "rejected") { Fail "expected 'rejected', got '$($rej.status)'" }
Ok "reject path works: status=$($rej.status)"

# ---------------------------------------------- double-decide must be 409
Head "3. decide the same job twice -> expect 409"
try {
    Invoke-RestMethod -Method Post "$Base/research/$($job.job_id)/plan/approve" -Headers $h `
        -ContentType "application/json" -Body '{"action":"approve"}' -TimeoutSec 30
    Fail "double-decide was ALLOWED - state guard is broken"
} catch {
    $sc = StatusCode $_
    if ($sc -eq 409) { Ok "409 Conflict (job already 'rejected') - guard works" }
    else { Fail "expected 409, got HTTP $sc $(Body $_)" }
}

# --------------------------------------------------- B. IDOR -> expect 404
Head "4. another user reads my job -> expect 404"
try {
    Invoke-RestMethod -Method Post "$Base/auth/register" -ContentType "application/json" `
        -Body (@{ email = $OtherEmail; full_name = "Other User"; password = $OtherPass } | ConvertTo-Json) | Out-Null
} catch { }
$l2 = Invoke-RestMethod -Method Post "$Base/auth/login" -ContentType "application/json" `
    -Body (@{ email = $OtherEmail; password = $OtherPass } | ConvertTo-Json) -TimeoutSec 20
$h2 = @{ Authorization = "Bearer $($l2.access_token)" }
Ok "second account ready: $OtherEmail"

try {
    Invoke-RestMethod "$Base/research/$($job.job_id)" -Headers $h2 -TimeoutSec 20 | Out-Null
    Fail "SECURITY BUG: another user read the job (expected 404)"
} catch {
    $sc = StatusCode $_
    if ($sc -eq 404) { Ok "404 Not Found - ownership is enforced in the query, not the prompt" }
    else { Fail "expected 404, got HTTP $sc $(Body $_)" }
}

Write-Host @"

==================================================================
PART 3 PASSED - reject path + 409 guard + IDOR 404 all correct.
Phase 1 checklist is complete. Commit it.

==================================================================
"@ -ForegroundColor Yellow
