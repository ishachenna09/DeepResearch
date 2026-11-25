# Phase 5 verification: writer streams a cited report + deterministic citation audit + SSE.
#
# Proves, in order:
#   1. a report can only be generated from finished research (409 guard)
#   2. POST is idempotent while a generation is running (no duplicate second run)
#   3. the SSE endpoint streams real tokens (`text/event-stream`, id: lines, status -> token -> done)
#   4. the stream is replayable: reconnecting with Last-Event-ID resumes instead of restarting
#      (this is the difference between a Redis Stream and pub/sub)
#   5. the audit is exact: precision 1.0 (no invented source numbers), coverage reported,
#      and every citation maps back to a stored note (url + note_id) - traceability by construction
#   6. regenerating overwrites the same report row (one current report per job)
#
# Prereqs: LiteLLM :4000, API :8000, migrations at head (0005_reports), and at least one
#          job in `notes_ready` with notes (run verify_phase3/4 first to create one).
# Run:
#   powershell -ExecutionPolicy Bypass -File C:\projects\DeepResearch\scripts\verify_phase5.ps1
#   ... -JobId <guid>   -> verify a specific job instead of the newest usable one
#   ... -SkipRegen      -> skips section 7 (saves ~40 s)

param(
    [string]$Base     = "http://localhost:8000/api/v1",
    [string]$Email    = "you@test.com",
    [string]$Password = "password123",
    [string]$JobId    = "",
    [int]$TimeoutSec  = 400,
    [switch]$SkipRegen
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Net.Http

function Ok($m)   { Write-Host "[ OK ] $m" -ForegroundColor Green }
function Fail($m) { Write-Host "[FAIL] $m" -ForegroundColor Red; exit 1 }
function Head($m) { Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Note($m) { Write-Host "       $m" -ForegroundColor DarkGray }
function Body($e) { try { return $e.ErrorDetails.Message } catch { return "" } }

# Reads an SSE response. Returns counts, the final event, and the id of the Nth token
# (so we can prove resume semantics later).
function Read-SSE {
    param(
        [string]$Url,
        [string]$Token,
        [string]$LastEventId = "",
        [int]$CaptureTokenIdAt = 0
    )
    $client = New-Object System.Net.Http.HttpClient
    $client.Timeout = [TimeSpan]::FromSeconds($TimeoutSec)
    $req = New-Object System.Net.Http.HttpRequestMessage([System.Net.Http.HttpMethod]::Get, $Url)
    $req.Headers.Authorization = New-Object System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", $Token)
    $req.Headers.Accept.ParseAdd("text/event-stream")
    if ($LastEventId) { $req.Headers.Add("Last-Event-ID", $LastEventId) }

    $sw = [Diagnostics.Stopwatch]::StartNew()
    try { $resp = $client.SendAsync($req, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).Result }
    catch { Fail "SSE request failed: $($_.Exception.Message)" }
    if (-not $resp.IsSuccessStatusCode) { Fail "SSE returned HTTP $([int]$resp.StatusCode)" }

    $reader = New-Object System.IO.StreamReader($resp.Content.ReadAsStreamAsync().Result)
    $event = ""
    $data = @()
    $id = ""
    $tokens = 0
    $chars = 0
    $ids = 0
    $firstTokenAt = $null
    $capturedId = ""
    $final = ""
    $finalData = ""
    $statusEvent = ""

    while (-not $reader.EndOfStream) {
        $line = $reader.ReadLine()
        if ($null -eq $line) { break }

        if ($line.StartsWith("id:")) { $id = $line.Substring(3).Trim(); $ids++; continue }
        if ($line.StartsWith("event:")) { $event = $line.Substring(6).Trim(); continue }
        if ($line.StartsWith("data:")) { $data += $line.Substring(5).Trim(); continue }
        if ($line.Trim() -ne "") { continue }              # ": heartbeat" comment

        $payload = $data -join ""
        if ($event -eq "token") {
            $tokens++
            if ($null -eq $firstTokenAt) { $firstTokenAt = [int]$sw.Elapsed.TotalSeconds }
            $chars += (($payload | ConvertFrom-Json).text).Length
            if ($CaptureTokenIdAt -gt 0 -and $tokens -eq $CaptureTokenIdAt) { $capturedId = $id }
        } elseif ($event -eq "status") {
            $statusEvent = $payload
        } elseif ($event -eq "done" -or $event -eq "error") {
            $final = $event
            $finalData = $payload
            break
        }
        $event = ""; $data = @()
    }
    $reader.Dispose(); $resp.Dispose(); $client.Dispose()

    return @{
        tokens       = $tokens
        chars        = $chars
        ids          = $ids
        firstTokenAt = $firstTokenAt
        capturedId   = $capturedId
        final        = $final
        finalData    = $finalData
        statusEvent  = $statusEvent
        seconds      = [int]$sw.Elapsed.TotalSeconds
        contentType  = "$($resp.Content.Headers.ContentType)"
    }
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

# ------------------------------------------------------------------ pre-flight
Head "0. API + dependencies"
try { $hz = Invoke-RestMethod "$Base/healthz" -TimeoutSec 20 }
catch { Fail "API not reachable. Start it: powershell -File .\scripts\start_api.ps1" }
if ($hz.overall -ne "healthy") { Fail "healthz reports '$($hz.overall)'" }
Ok "healthz: $($hz | ConvertTo-Json -Compress)"

# Phase 9 adds a 10/hour per-user limit on report generation. This script starts
# (and restarts) reports, so repeated runs inside the hour would be refused with
# 429. Clearing the limiter counters is test setup (the limit itself is proven by
# verify_phase9.ps1), and only keys under `dr:rl:` are touched.
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (Test-Path $python) {
    & $python (Join-Path $root "scripts\phase9_reset_limits.py") 2>&1 | ForEach-Object { Note $_ }
}

Head "1. login"
try {
    $login = Invoke-RestMethod -Method Post "$Base/auth/login" -ContentType "application/json" `
        -Body (@{ email = $Email; password = $Password } | ConvertTo-Json) -TimeoutSec 30
} catch { Fail "login failed: $($_.Exception.Message) $(Body $_)" }
$h = @{ Authorization = "Bearer $($login.access_token)" }
Ok "logged in as $Email"

Head "2. pick a job that has notes"
$jobs = Invoke-RestMethod "$Base/research?limit=50" -Headers $h -TimeoutSec 30
if ($JobId) {
    $job = $jobs | Where-Object { $_.job_id -eq $JobId } | Select-Object -First 1
    if (-not $job) { Fail "job $JobId not found for this user" }
} else {
    $job = $jobs | Where-Object { $_.status -eq "notes_ready" -and $_.notes_count -gt 0 } | Select-Object -First 1
}
if (-not $job) { Fail "no job in 'notes_ready' with notes. Run verify_phase3.ps1 or verify_phase4.ps1 first." }
if ($job.status -ne "notes_ready" -or $job.notes_count -le 0) {
    Fail "job $($job.job_id) is '$($job.status)' with $($job.notes_count) notes - needs notes_ready"
}
Ok "job $($job.job_id)"
Note "question : $($job.question)"
Note "notes    : $($job.notes_count) from $($job.rounds) round(s)"

Head "3. guard: a report needs finished research"
$pending = $jobs | Where-Object { $_.status -eq "awaiting_approval" } | Select-Object -First 1
if ($pending) {
    $code = 0
    try { Invoke-RestMethod -Method Post "$Base/research/$($pending.job_id)/report" -Headers $h -TimeoutSec 20 | Out-Null }
    catch { $code = $_.Exception.Response.StatusCode.value__; $detail = (Body $_ | ConvertFrom-Json).detail }
    if ($code -ne 409) { Fail "expected 409 for a non-notes_ready job, got $code" }
    Ok "409 on '$($pending.status)' job: $detail"
} else {
    Note "skipped - no awaiting_approval job in the last 50"
}

Head "4. generate: POST returns immediately, second POST attaches"
$sw = [Diagnostics.Stopwatch]::StartNew()
try { $post = Invoke-RestMethod -Method Post "$Base/research/$($job.job_id)/report" -Headers $h -TimeoutSec 30 }
catch { Fail "POST report failed: $($_.Exception.Message) $(Body $_)" }
$postSeconds = [int]$sw.Elapsed.TotalSeconds
if ($post.status -ne "generating") { Fail "expected 'generating', got '$($post.status)'" }
Ok "POST -> generating in ${postSeconds}s (report $($post.report_id))"
$again = Invoke-RestMethod -Method Post "$Base/research/$($job.job_id)/report" -Headers $h -TimeoutSec 30
if ($again.report_id -ne $post.report_id) { Fail "second POST created a different report ($($again.report_id))" }
Ok "second POST attached to the same report (idempotent)"

Head "5. SSE stream (live tokens)"
$sse = Read-SSE -Url "$Base/research/$($job.job_id)/report/stream" -Token $login.access_token -CaptureTokenIdAt 100
Ok "content-type: $($sse.contentType)"
Ok "status event: $($sse.statusEvent)"
if ($sse.tokens -lt 50) { Fail "only $($sse.tokens) token events - the stream is not streaming" }
if ($sse.ids -lt $sse.tokens) { Fail "missing id: lines ($($sse.ids) ids for $($sse.tokens) tokens) - replay impossible" }
if ($sse.final -ne "done") { Fail "stream ended with '$($sse.final)': $($sse.finalData)" }
Ok "tokens: $($sse.tokens) events, $($sse.chars) chars, first token at $($sse.firstTokenAt)s, total $($sse.seconds)s"
if ($sse.firstTokenAt -gt 120) { Note "first token was slow ($($sse.firstTokenAt)s) - provider latency, not the stream" }
$done = $sse.finalData | ConvertFrom-Json
Ok "done: $($done.citations) citations, precision=$($done.validation.precision), coverage=$($done.validation.coverage), $($done.tokens_used) tokens"

Head "6. replay: reconnect with Last-Event-ID resumes (Streams, not pub/sub)"
if (-not $sse.capturedId) { Fail "could not capture a mid-stream id" }
$resumed = Read-SSE -Url "$Base/research/$($job.job_id)/report/stream" -Token $login.access_token -LastEventId $sse.capturedId
$expected = $sse.tokens - 100
if ($resumed.tokens -ne $expected) {
    Fail "resume replayed $($resumed.tokens) tokens, expected $expected (100 were already consumed)"
}
if ($resumed.final -ne "done") { Fail "resumed stream did not reach done (got '$($resumed.final)')" }
Ok "resumed from $($sse.capturedId): $($resumed.tokens) tokens (not $($sse.tokens)) and still reached done"

Head "7. the report + audit as stored"
$rep = Invoke-RestMethod "$Base/research/$($job.job_id)/report" -Headers $h -TimeoutSec 30
if ($rep.status -ne "ready") { Fail "report status is '$($rep.status)': $($rep.error)" }
$v = $rep.validation
Note "words     : $($v.words)"
Note "markers   : $($v.markers) (valid $($v.valid_markers), unknown $($v.unknown_markers -join ','))"
Note "precision : $($v.precision)"
Note "coverage  : $($v.coverage) ($($v.cited_sentences)/$($v.sentences) sentences cite a source)"
Note "unused    : $($v.unused_sources) of the registry never cited"

if ($v.precision -ne 1.0) { Fail "citation precision $($v.precision) - the model cited numbers that do not exist: $($v.unknown_markers -join ',')" }
if ($v.markers -lt 10) { Fail "only $($v.markers) citation markers - the report is not properly cited" }
if ($v.coverage -lt 0.6) { Fail "coverage $($v.coverage) - most sentences are unsupported" }
if ($v.words -lt 250) { Fail "report is only $($v.words) words" }
if ($rep.citations.Count -ne $v.valid_markers) { Fail "citations array ($($rep.citations.Count)) != valid markers ($($v.valid_markers))" }
if (($rep.markdown -split "\s+").Count -lt 250) { Fail "markdown looks truncated" }
if ($rep.markdown.Contains([string][char]0xFFFD)) { Fail "report contains U+FFFD (encoding damage)" }
Ok "precision 1.0, coverage >= 0.6, $($rep.citations.Count) citations, no encoding damage"

Head "8. traceability: every citation maps to a stored note"
$notes = @()
$offset = 0
while ($true) {
    $page = Invoke-RestMethod "$Base/research/$($job.job_id)/notes?limit=200&offset=$offset" -Headers $h -TimeoutSec 30
    $notes += $page.notes
    if ($notes.Count -ge $page.count -or $page.notes.Count -eq 0) { break }
    $offset += 200
}
if ($notes.Count -lt $job.notes_count) { Note "fetched $($notes.Count) notes (job reports $($job.notes_count))" }
$noteUrls = @{}
$noteIds = @{}
foreach ($n in $notes) { $noteUrls[$n.url] = 1; $noteIds["$($n.id)"] = 1 }
$bad = @()
foreach ($c in $rep.citations) {
    if (-not $noteUrls.ContainsKey($c.url)) { $bad += "url not in notes: $($c.url)" }
    if (-not $noteIds.ContainsKey("$($c.note_id)")) { $bad += "note_id not in notes: $($c.note_id)" }
}
if ($bad.Count -gt 0) { Fail ("untraceable citations:`n  " + ($bad -join "`n  ")) }
Ok "all $($rep.citations.Count) citations resolve to a stored note (url + note_id)"
Note "distinct sites cited: $((($rep.citations | ForEach-Object { $_.url }) | Sort-Object -Unique).Count)"

Head "9. the markdown itself"
$lines = $rep.markdown -split "`n"
Note (($lines | Select-Object -First 6) -join "`n       ")
$headings = ($lines | Where-Object { $_.StartsWith("## ") }).Count
if ($headings -lt 2) { Fail "expected markdown sections, found $headings headings" }
Ok "$headings sections, $($lines.Count) lines"

if (-not $SkipRegen) {
    Head "10. regenerate overwrites the same row"
    $first = $rep.markdown
    $re = Invoke-RestMethod -Method Post "$Base/research/$($job.job_id)/report" -Headers $h -TimeoutSec 30
    if ($re.report_id -ne $rep.report_id) { Fail "regeneration created a new row" }
    $waited = Wait-ForReport $job.job_id $h "regeneration"
    if ($waited.report.status -ne "ready") { Fail "regeneration failed: $($waited.report.error)" }
    if ($waited.report.markdown.Length -lt 500) { Fail "regenerated report is suspiciously short" }
    Ok "regenerated in $($waited.seconds)s, same report id, $($waited.report.validation.markers) markers"
    if ($waited.report.markdown -eq $first) { Note "text is identical - same prompt and sources, expected sometimes" }
}

Write-Host "`nAll Phase 5 checks passed." -ForegroundColor Green
