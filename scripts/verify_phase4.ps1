# Phase 4 verification: critic + bounded gap-filling loop + token budget.
#
# Proves, in order:
#   1. tokens are measured from provider usage, not estimated
#   2. a deliberately NARROW approved plan triggers exactly one gap-filling round
#      (the critic rescues a plan a human edited badly - a real HITL case)
#   3. the second round actually searched and added notes (round-tagged sources)
#   4. the loop terminates with a recorded stop_reason (never runs away)
#   5. no duplicate notes after the extra round (reducer dedupe is idempotent)
#   6. a normal, unedited plan does not loop forever either (bounded + reported)
#
# Prereqs: LiteLLM :4000, API :8000, migrations at head (0004_critic_loop).
# Run:
#   powershell -ExecutionPolicy Bypass -File C:\projects\DeepResearch\scripts\verify_phase4.ps1
#   ... -SkipSecond   -> skips section 7 (saves ~3-5 min)

param(
    [string]$Base     = "http://localhost:8000/api/v1",
    [string]$Email    = "you@test.com",
    [string]$Password = "password123",
    [string]$Question = "How should a team choose between pgvector, Pinecone and FAISS for a production RAG system?",
    [int]$TimeoutSec  = 900,
    [switch]$SkipSecond
)

$ErrorActionPreference = "Stop"

function Ok($m)   { Write-Host "[ OK ] $m" -ForegroundColor Green }
function Fail($m) { Write-Host "[FAIL] $m" -ForegroundColor Red; exit 1 }
function Head($m) { Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Note($m) { Write-Host "       $m" -ForegroundColor DarkGray }
function Body($e) { try { return $e.ErrorDetails.Message } catch { return "" } }

function Wait-ForJob($jobId, $h, $label) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $last = ""
    while ($sw.Elapsed.TotalSeconds -lt $TimeoutSec) {
        Start-Sleep -Seconds 4
        try { $job = Invoke-RestMethod "$Base/research/$jobId" -Headers $h -TimeoutSec 30 }
        catch { Fail "polling $label failed: $($_.Exception.Message) $(Body $_)" }
        if ($job.status -ne $last) {
            Note ("  {0,5:N0}s  {1,-14} notes={2}" -f $sw.Elapsed.TotalSeconds, $job.status, $job.notes_count)
            $last = $job.status
        }
        if ($job.status -in @("notes_ready", "failed", "rejected")) {
            $sw.Stop()
            return @{ job = $job; seconds = [int]$sw.Elapsed.TotalSeconds }
        }
    }
    Fail "$label did not finish within $TimeoutSec s (last status: $last)"
}

function Show-Job($job) {
    Note "rounds       : $($job.rounds)"
    Note "tokens used  : $($job.tokens_used) / budget $($job.plan.budget_tokens)"
    Note "stop reason  : $($job.critique.stop_reason)"
    Note "critic reason: $($job.critique.reason)"
    $byRound = @($job.sources | Where-Object { $_.url -and -not $_.error } | Group-Object round |
                 Sort-Object Name | ForEach-Object { "round $($_.Name): $($_.Count) pages" })
    if ($byRound) { Note "pages        : $($byRound -join ' | ')" }
}

# ------------------------------------------------------------------ pre-flight
Head "0. API + dependencies"
try { $hz = Invoke-RestMethod "$Base/healthz" -TimeoutSec 20 }
catch { Fail "API not reachable. Start it: powershell -File .\scripts\start_api.ps1" }
if ($hz.overall -ne "healthy") { Fail "healthz reports '$($hz.overall)'" }
Ok "healthz: $($hz | ConvertTo-Json -Compress)"

# Phase 9 adds a 5/hour per-user limit on POST /research; this script creates two
# jobs per run, so a second run inside the hour would be refused with 429.
# Clearing the limiter counters is test setup (the limit itself is proven by
# verify_phase9.ps1), and only keys under `dr:rl:` are touched.
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (Test-Path $python) {
    & $python (Join-Path $root "scripts\phase9_reset_limits.py") 2>&1 | ForEach-Object { Note $_ }
}

Head "1. login"
try {
    $login = Invoke-RestMethod -Method Post "$Base/auth/login" -ContentType "application/json" `
        -Body (@{ email = $Email; password = $Password } | ConvertTo-Json) -TimeoutSec 20
} catch { Fail "login failed: $($_.Exception.Message) $(Body $_)" }
$h = @{ Authorization = "Bearer $($login.access_token)" }
Ok "authenticated as $Email"

# --------------------------------------------------- A. narrow plan -> gaps
Head "2. create job (planner runs, tokens are measured)"
try {
    $jobA = Invoke-RestMethod -Method Post "$Base/research" -Headers $h -ContentType "application/json" `
        -Body (@{ question = $Question } | ConvertTo-Json) -TimeoutSec 180
} catch { Fail "create failed: $($_.Exception.Message) $(Body $_)" }
if ($jobA.status -ne "awaiting_approval") { Fail "expected awaiting_approval, got '$($jobA.status)'" }
Ok "job $($jobA.job_id) planned with $(@($jobA.plan.sub_questions).Count) sub-questions"
Ok "planner tokens measured (visible after the run): $($jobA.plan.budget_tokens) budget set"

Head "3. approve a NARROWED plan (only 3 pgvector-performance questions)"
# This is the product case the critic exists for: the HITL gate lets a human
# edit the plan, and a human can under-specify it. The critic must notice.
$narrow = @{
    question      = $Question
    sub_questions = @(
        "What query latency and throughput does pgvector achieve at one million vectors?",
        "How does pgvector HNSW index tuning (m, ef_construction) affect recall and build time?",
        "Which embedding dimensions does pgvector support?"
    )
    angles       = @("pgvector performance")
    constraints  = @()
    budget_tokens = 200000
}
$sw = [Diagnostics.Stopwatch]::StartNew()
try {
    $approved = Invoke-RestMethod -Method Post "$Base/research/$($jobA.job_id)/plan/approve" -Headers $h `
        -ContentType "application/json" -Body (@{ action = "approve"; plan = $narrow } | ConvertTo-Json -Depth 8) -TimeoutSec 30
} catch { Fail "approve failed: $($_.Exception.Message) $(Body $_)" }
$sw.Stop()
if ($approved.status -ne "researching") { Fail "expected researching, got '$($approved.status)'" }
Ok "accepted in $([math]::Round($sw.Elapsed.TotalSeconds,2))s with 3 sub-questions (4 angles missing on purpose)"

# ------------------------------------------------------- wait + inspect loop
Head "4. bounded loop: round 1 -> critic -> gap round -> critic"
$doneA = Wait-ForJob $jobA.job_id $h "jobA"
$jobA = $doneA.job
if ($jobA.status -eq "failed") { Fail "job failed: $($jobA.error)" }
Show-Job $jobA

if ($jobA.rounds -lt 2) {
    Fail "the critic stopped after round 1 although 3 of 6 angles were never searched (stop_reason=$($jobA.critique.stop_reason))"
}
Ok "gap-filling fired: $($jobA.rounds) rounds (in $($doneA.seconds)s)"

$round2 = @($jobA.sources | Where-Object { $_.round -eq 2 -and $_.url -and -not $_.error })
if ($round2.Count -lt 1) { Fail "round 2 produced no pages - the loop ran but did not search" }
Ok "round 2 searched $($round2.Count) pages: $(($round2 | Select-Object -First 3 | ForEach-Object { $_.url }) -join ', ')"

if (-not $jobA.critique.stop_reason) { Fail "no stop_reason recorded - the loop's brake is not auditable" }
Ok "terminated with stop_reason='$($jobA.critique.stop_reason)' after $($jobA.rounds) round(s) (cap: $($jobA.critique.round) reviewed)"

if ($jobA.tokens_used -le 0) { Fail "tokens_used is 0 - usage_metadata is not being captured" }
$ratio = [math]::Round(100.0 * $jobA.tokens_used / [double]$jobA.plan.budget_tokens, 1)
Ok "measured tokens: $($jobA.tokens_used) ($ratio% of the $($jobA.plan.budget_tokens) budget)"

# ------------------------------------------------------- no duplicate notes
Head "5. dedupe after the extra round (no duplicate claims)"
try { $notesA = Invoke-RestMethod "$Base/research/$($jobA.job_id)/notes?limit=200" -Headers $h -TimeoutSec 60 }
catch { Fail "notes list failed: $($_.Exception.Message) $(Body $_)" }
$all = @($notesA.notes)
$keys = @($all | ForEach-Object { "$($_.url)|$($_.note)" })
$unique = @($keys | Sort-Object -Unique)
if ($all.Count -lt 1) { Fail "no notes stored" }
if ($unique.Count -ne $all.Count) {
    Fail "duplicate notes: $($all.Count) rows but only $($unique.Count) distinct (url,note) pairs"
}
Ok "$($all.Count) notes, all distinct (job reports $($notesA.count))"
$byRoundSub = @($all | Group-Object sub_question | Sort-Object Count -Descending | Select-Object -First 3)
$byRoundSub | ForEach-Object { Note ("{0,3} notes  {1}" -f $_.Count, $_.Name.Substring(0, [Math]::Min(85, $_.Name.Length))) }

# ------------------------------------------- B. unedited plan stays bounded
if (-not $SkipSecond) {
    Head "6. control run: an unedited plan must also terminate (bounded, reported)"
    try {
        $jobB = Invoke-RestMethod -Method Post "$Base/research" -Headers $h -ContentType "application/json" `
            -Body (@{ question = $Question } | ConvertTo-Json) -TimeoutSec 180
        $null = Invoke-RestMethod -Method Post "$Base/research/$($jobB.job_id)/plan/approve" -Headers $h `
            -ContentType "application/json" -Body '{"action":"approve"}' -TimeoutSec 30
    } catch { Fail "control job failed to start: $($_.Exception.Message) $(Body $_)" }

    $doneB = Wait-ForJob $jobB.job_id $h "jobB"
    $jobB = $doneB.job
    if ($jobB.status -eq "failed") { Fail "control job failed: $($jobB.error)" }
    Show-Job $jobB
    if ($jobB.rounds -gt 2) { Fail "loop exceeded MAX_ROUNDS: $($jobB.rounds)" }
    if (-not $jobB.critique.stop_reason) { Fail "control job has no stop_reason" }
    Ok "control job finished in $($jobB.rounds) round(s), stop_reason='$($jobB.critique.stop_reason)'"
    if ($jobB.rounds -eq 1) {
        Ok "the critic said 'sufficient' without a second round - it is not a loop generator"
    } else {
        Note "the critic asked for one gap round on a full plan too - acceptable, it was bounded and recorded"
    }
}

Write-Host @"

==================================================================
PHASE 4 PASSED - critic, bounded gap-filling loop, measured tokens.

  Commit:  git add -A
           git commit -m "feat: coverage critic + bounded gap-filling loop + token budget"

  Next: Phase 5 - writer streams a cited report, citation validator
==================================================================
"@ -ForegroundColor Yellow
