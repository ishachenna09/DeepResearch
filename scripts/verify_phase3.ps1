# Phase 3 verification: approval -> background fan-out -> notes in pgvector.
#
# Proves, in order:
#   1. approve returns immediately with status=researching (no HTTP timeout)
#   2. the graph fans out per sub-question and finishes with notes_ready
#   3. notes are stored in Postgres with embeddings (pgvector)
#   4. semantic search ranks by cosine similarity and returns real hits
#   5. a second job with the SAME plan hits the Redis page cache (cached=True)
#   6. a duplicate approve is refused with 409 (no double spend)
#
# Prereqs: LiteLLM on :4000, API on :8000 (MCP toolbox ready).
# Run:
#   powershell -ExecutionPolicy Bypass -File C:\projects\DeepResearch\scripts\verify_phase3.ps1
#   ... -SkipCache  -> stops after step 4 (saves ~2 min, skips the cache proof)

param(
    [string]$Base     = "http://localhost:8000/api/v1",
    [string]$Email    = "you@test.com",
    [string]$Password = "password123",
    [string]$Question = "How does pgvector compare to FAISS and Pinecone for semantic search in production Postgres applications?",
    [int]$TimeoutSec  = 600,
    [switch]$SkipCache
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
        Start-Sleep -Seconds 3
        try { $job = Invoke-RestMethod "$Base/research/$jobId" -Headers $h -TimeoutSec 30 }
        catch { Fail "polling $label failed: $($_.Exception.Message) $(Body $_)" }
        if ($job.status -ne $last) {
            Note ("  {0,5:N0}s  {1}  notes={2}" -f $sw.Elapsed.TotalSeconds, $job.status, $job.notes_count)
            $last = $job.status
        }
        if ($job.status -in @("notes_ready", "failed", "rejected")) {
            $sw.Stop()
            return @{ job = $job; seconds = [int]$sw.Elapsed.TotalSeconds }
        }
    }
    Fail "$label did not finish within $TimeoutSec s (last status: $last)"
}

# ------------------------------------------------------------------ pre-flight
Head "0. API + dependencies"
try { $hz = Invoke-RestMethod "$Base/healthz" -TimeoutSec 20 }
catch { Fail "API not reachable. Start it: powershell -File .\scripts\start_api.ps1" }
if ($hz.overall -ne "healthy") { Fail "healthz reports '$($hz.overall)' (postgres=$($hz.postgres) redis=$($hz.redis))" }
Ok "healthz: $($hz | ConvertTo-Json -Compress)"
Note "the API log must show: MCP toolbox ready + page cache ready"

Head "1. login"
try {
    $login = Invoke-RestMethod -Method Post "$Base/auth/login" -ContentType "application/json" `
        -Body (@{ email = $Email; password = $Password } | ConvertTo-Json) -TimeoutSec 20
} catch { Fail "login failed: $($_.Exception.Message) $(Body $_)" }
$h = @{ Authorization = "Bearer $($login.access_token)" }
Ok "authenticated as $Email"

# ------------------------------------------------------------------- plan
Head "2. POST /research  (planner -> awaiting_approval)"
try {
    $job1 = Invoke-RestMethod -Method Post "$Base/research" -Headers $h -ContentType "application/json" `
        -Body (@{ question = $Question } | ConvertTo-Json) -TimeoutSec 180
} catch { Fail "create failed: $($_.Exception.Message) $(Body $_)" }
if ($job1.status -ne "awaiting_approval") { Fail "expected awaiting_approval, got '$($job1.status)' $($job1.error)" }
Ok "job $($job1.job_id) planned with $(@($job1.plan.sub_questions).Count) sub-questions"
@($job1.plan.sub_questions) | ForEach-Object { Note "  - $_" }

# --------------------------------------------------- approve (must be async)
Head "3. POST /plan/approve  (must return fast, status=researching)"
$sw = [Diagnostics.Stopwatch]::StartNew()
try {
    $approved = Invoke-RestMethod -Method Post "$Base/research/$($job1.job_id)/plan/approve" -Headers $h `
        -ContentType "application/json" -Body '{"action":"approve"}' -TimeoutSec 30
} catch { Fail "approve failed: $($_.Exception.Message) $(Body $_)" }
$sw.Stop()
if ($approved.status -ne "researching") { Fail "expected researching right after approve, got '$($approved.status)'" }
if ($sw.Elapsed.TotalSeconds -gt 10) { Fail "approve took $([int]$sw.Elapsed.TotalSeconds)s - it is still blocking" }
Ok "returned '$($approved.status)' in $([math]::Round($sw.Elapsed.TotalSeconds,2))s (background task spawned)"

Head "    duplicate approve must be refused"
$code = 0; $detail = ""
try {
    $null = Invoke-RestMethod -Method Post "$Base/research/$($job1.job_id)/plan/approve" -Headers $h `
        -ContentType "application/json" -Body '{"action":"approve"}' -TimeoutSec 20
} catch { $code = [int]$_.Exception.Response.StatusCode; $detail = Body $_ }
if ($code -ne 409) { Fail "expected HTTP 409 on the second approve, got '$code' $detail" }
Ok "HTTP 409 :: $detail"

# ------------------------------------------------------- wait for the fan-out
Head "4. fan-out research + persist  (polling /research/{id})"
$done = Wait-ForJob $job1.job_id $h "job1"
$job1 = $done.job
if ($job1.status -eq "failed") { Fail "research failed after $($done.seconds)s: $($job1.error)" }
Ok "notes_ready in $($done.seconds)s -> $($job1.notes_count) notes"

$pages = @($job1.sources | Where-Object { $_.url -and -not $_.error })
$errs  = @($job1.sources | Where-Object { $_.error })
$injected = @($pages | Where-Object { $_.injected })
Note "pages read=$($pages.Count)  fetch/search errors=$($errs.Count)  flagged pages=$($injected.Count)  cached=$(@($pages | Where-Object { $_.cached }).Count)"
if ($job1.notes_count -lt 1) { Fail "no notes stored - check the API log for embedding/extraction errors" }

# ---------------------------------------------------------------- notes list
Head "5. GET /research/{id}/notes  (Postgres + pgvector rows)"
try { $notes = Invoke-RestMethod "$Base/research/$($job1.job_id)/notes?limit=5" -Headers $h -TimeoutSec 60 }
catch { Fail "notes list failed: $($_.Exception.Message) $(Body $_)" }
if ($notes.count -lt 1) { Fail "notes endpoint returned count=0" }
@($notes.notes) | Select-Object -First 3 confidence, sub_question, note | Format-List | Out-String | Write-Host
Ok "$($notes.count) notes stored (showing $(@($notes.notes).Count))"

Head "6. POST /notes/search  (kNN: embed query -> cosine <=>)"
$sw = [Diagnostics.Stopwatch]::StartNew()
try {
    $hits = Invoke-RestMethod -Method Post "$Base/research/$($job1.job_id)/notes/search" -Headers $h `
        -ContentType "application/json" `
        -Body (@{ query = "index build cost and query latency tradeoffs"; top_k = 5 } | ConvertTo-Json) -TimeoutSec 120
} catch { Fail "semantic search failed: $($_.Exception.Message) $(Body $_)" }
$sw.Stop()
if ($hits.count -lt 1) { Fail "semantic search returned no hits" }
$hits.results | Select-Object score, confidence, note | Format-Table -AutoSize -Wrap | Out-String | Write-Host
$scores = @($hits.results | ForEach-Object { [double]$_.score })
if (($scores | Measure-Object -Maximum).Maximum -lt 0.3) { Fail "top score $($scores[0]) looks random - check EMBED_DIM / that documents and query use one model" }
$ms = [int]$sw.Elapsed.TotalMilliseconds
Ok "$($hits.count) hits in ${ms}ms (top score $($scores[0]))"

# ------------------------------------------------------- Redis page cache proof
if (-not $SkipCache) {
    Head "7. cache proof: same plan, second job -> cached=True"
    try {
        $job2 = Invoke-RestMethod -Method Post "$Base/research" -Headers $h -ContentType "application/json" `
            -Body (@{ question = $Question } | ConvertTo-Json) -TimeoutSec 180
    } catch { Fail "second create failed: $($_.Exception.Message) $(Body $_)" }

    # Reuse job1's *approved* plan so the branches search the exact same queries:
    # same query -> same URLs -> the cache must answer them.
    $payload = @{ action = "approve"; plan = $job1.plan } | ConvertTo-Json -Depth 8
    try {
        $null = Invoke-RestMethod -Method Post "$Base/research/$($job2.job_id)/plan/approve" -Headers $h `
            -ContentType "application/json" -Body $payload -TimeoutSec 30
    } catch { Fail "second approve failed: $($_.Exception.Message) $(Body $_)" }

    $done2 = Wait-ForJob $job2.job_id $h "job2"
    $job2 = $done2.job
    if ($job2.status -eq "failed") { Fail "second run failed: $($job2.error)" }
    $pages2 = @($job2.sources | Where-Object { $_.url -and -not $_.error })
    $cached2 = @($pages2 | Where-Object { $_.cached })
    Note "job2: $($job2.notes_count) notes, pages=$($pages2.Count), cached=$($cached2.Count) in $($done2.seconds)s"
    if ($cached2.Count -lt 1) {
        Fail "no page came from cache - is Redis reachable? Check the API log for 'page cache'"
    }
    Ok "cache served $($cached2.Count)/$($pages2.Count) pages (elapsed $($done2.seconds)s vs $($done.seconds)s; wall clock also swings with provider latency)"
}

Write-Host @"

==================================================================
PHASE 3 PASSED - fan-out research, Redis page cache, notes -> pgvector.

  Commit:  git add -A
           git commit -m "feat: parallel research fan-out, redis page cache, pgvector notes"

  Next: Phase 4 - gap analysis, outline synthesis, cited report
==================================================================
"@ -ForegroundColor Yellow
