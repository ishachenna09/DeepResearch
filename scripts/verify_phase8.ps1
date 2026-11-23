# Phase 8 verification: report library, MD/PDF export, admin overview.
#
# Proves, in order:
#   1. the library lists only the caller's reports, with audit fields and totals
#   2. search / status / sort filters work, and bad input is a 422
#   3. Markdown export is a real attachment with sources and the stored audit
#   4. PDF export is a real PDF (magic + pages + extractable text), not a stub
#   5. report ids are owner-scoped (another account gets 404, not the file)
#   6. /admin/overview is 403 for an agent, then works for an admin and shows
#      every user's aggregates (the same role gate as /usage?scope=all)
#
# This script owns its API: it starts one on :8004 and stops it in `finally`.
# It writes to the shared dev database (registers admin@test.com/other@test.com
# if missing; promotes admin@test.com). No provider calls are made.
#
# Run:
#   powershell -ExecutionPolicy Bypass -File C:\projects\DeepResearch\scripts\verify_phase8.ps1
#   ... -KeepRunning      -> leave the API up

param(
    [string]$Email    = "you@test.com",
    [string]$Password = "password123",
    [int]$ApiPort     = 8004,
    [switch]$KeepRunning
)

$ErrorActionPreference = "Stop"
$root   = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$apiDir = Join-Path $root "api"
$Base   = "http://localhost:$ApiPort/api/v1"
$apiErr = Join-Path $env:TEMP "deepresearch_phase8_api.err"
$apiOut = Join-Path $env:TEMP "deepresearch_phase8_api.log"

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

$api = $null
try {
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

    $login = Login $Email $Password
    $h = @{ Authorization = "Bearer $($login.access_token)" }

    # ------------------------------------------------------------- library
    Head "1. library: owner-scoped list with audit fields"
    $lib = Invoke-RestMethod "$Base/reports?limit=10" -Headers $h -TimeoutSec 60
    if ($lib.total -lt 1) { Fail "no reports for $Email - generate one first" }
    if ($lib.items.Count -lt 1) { Fail "library returned total=$($lib.total) but no items" }
    $first = $lib.items[0]
    foreach ($field in @("report_id", "job_id", "question", "status", "updated_at")) {
        if (-not $first.$field) { Fail "library row is missing $field" }
    }
    if ($first.status -eq "ready" -and $first.words -le 0) { Fail "ready report has no word count" }
    Ok ("total={0}, showing {1}; newest = {2}" -f $lib.total, $lib.items.Count, $first.question.Substring(0, [Math]::Min(60, $first.question.Length)))
    Note ("words={0}, sources={1}, precision={2}, coverage={3}, tokens={4}, model={5}" -f `
        $first.words, $first.citations, $first.precision, $first.coverage, $first.tokens_used, $first.model)

    Head "2. filters: search, status, sort, and a 422 on bad input"
    $search = Invoke-RestMethod "$Base/reports?q=pgvector&limit=10" -Headers $h -TimeoutSec 60
    if ($search.total -lt 1) { Fail "search 'pgvector' found nothing" }
    foreach ($item in $search.items) {
        if ($item.question -notmatch "pgvector") { Fail "search returned a non-matching question: $($item.question)" }
    }
    Ok "q=pgvector -> $($search.total) of $($lib.total)"

    $ready = Invoke-RestMethod "$Base/reports?status=ready&limit=100" -Headers $h -TimeoutSec 60
    foreach ($item in $ready.items) { if ($item.status -ne "ready") { Fail "status=ready returned $($item.status)" } }
    Ok "status=ready -> $($ready.total)"

    $byTokens = Invoke-RestMethod "$Base/reports?sort=tokens&limit=100" -Headers $h -TimeoutSec 60
    $sorted = $true
    for ($i = 1; $i -lt $byTokens.items.Count; $i++) {
        if ($byTokens.items[$i].tokens_used -gt $byTokens.items[$i - 1].tokens_used) { $sorted = $false }
    }
    if (-not $sorted) { Fail "sort=tokens is not descending" }
    Ok "sort=tokens descending (top: $($byTokens.items[0].tokens_used) tokens)"

    try {
        Invoke-RestMethod "$Base/reports?status=bogus" -Headers $h -TimeoutSec 30 | Out-Null
        Fail "status=bogus was accepted"
    } catch {
        $code = [int]$_.Exception.Response.StatusCode
        if ($code -ne 422) { Fail "status=bogus -> $code, expected 422" }
    }
    Ok "status=bogus -> 422"

    # ------------------------------------------------------------- markdown
    Head "3. Markdown export: attachment, sources, stored audit"
    $rid = $byTokens.items[0].report_id
    if ($byTokens.items[0].status -ne "ready") { Fail "the top report is not ready; cannot export" }
    $md = Invoke-WebRequest -UseBasicParsing "$Base/reports/$rid/export.md" -Headers $h -TimeoutSec 60
    $disposition = $md.Headers["Content-Disposition"]
    if ($disposition -notmatch 'attachment; filename="deepresearch-.*\.md"') {
        Fail "unexpected Content-Disposition: $disposition"
    }
    if ($md.Headers["Content-Type"] -notmatch "text/markdown") { Fail "unexpected Content-Type: $($md.Headers['Content-Type'])" }
    if ($md.Content -notmatch "## Sources") { Fail "markdown export has no Sources section" }
    if ($md.Content -notmatch "precision") { Fail "markdown export has no audit line" }
    if ($md.Content -notmatch "\[1\]") { Fail "markdown export has no numbered citations" }
    Ok ("{0} bytes, filename {1}" -f $md.RawContentLength, $disposition.Split('"')[1])
    Note "contains: title, audit line, ## Sources, [n] markers"

    # ------------------------------------------------------------- pdf
    Head "4. PDF export: real bytes, real pages, real text"
    $pdfPath = Join-Path $env:TEMP "deepresearch_phase8_export.pdf"
    $pdf = Invoke-WebRequest -UseBasicParsing "$Base/reports/$rid/export.pdf" -Headers $h -OutFile $pdfPath -PassThru -TimeoutSec 120
    if (-not (Test-Path $pdfPath)) { Fail "no PDF written" }
    $bytes = [IO.File]::ReadAllBytes($pdfPath)
    if ($bytes.Length -lt 5000) { Fail "PDF is only $($bytes.Length) bytes" }
    $magic = [Text.Encoding]::ASCII.GetString($bytes[0..4])
    if ($magic -ne "%PDF-") { Fail "PDF magic is '$magic'" }
    $pdfDisposition = $pdf.Headers["Content-Disposition"]
    if ($pdfDisposition -notmatch 'filename="deepresearch-.*\.pdf"') { Fail "unexpected PDF filename: $pdfDisposition" }

    $env:PYTHONIOENCODING = "utf-8"
    $py = "from pypdf import PdfReader; r=PdfReader(r'$pdfPath'); t=chr(10).join((p.extract_text() or '') for p in r.pages); print(len(r.pages)); print('Sources' in t); print('Page 1 of' in t)"
    $pdfCheck = & $python -c $py
    if ($LASTEXITCODE -ne 0) { Fail "pypdf could not read the export" }
    $pageCount = [int]$pdfCheck[0]
    if ($pageCount -lt 1) { Fail "PDF has no pages" }
    if ($pdfCheck[1] -ne "True") { Fail "PDF text has no Sources section" }
    if ($pdfCheck[2] -ne "True") { Fail "PDF has no page footer" }
    Ok ("{0:N0} bytes, {1} page(s), extractable text, Sources present, 'Page 1 of N' footer" -f $bytes.Length, $pageCount)

    # ------------------------------------------------------------- ownership
    Head "5. ownership: another account gets 404, not the file"
    Ensure-User "other@test.com" "Other Account"
    $other = Login "other@test.com" $Password
    $hOther = @{ Authorization = "Bearer $($other.access_token)" }
    try {
        Invoke-WebRequest -UseBasicParsing "$Base/reports/$rid/export.pdf" -Headers $hOther -TimeoutSec 30 | Out-Null
        Fail "another user downloaded someone else's report"
    } catch {
        $code = [int]$_.Exception.Response.StatusCode
        if ($code -ne 404) { Fail "cross-user export -> $code, expected 404" }
    }
    try {
        Invoke-RestMethod "$Base/reports/00000000-0000-0000-0000-000000000000/export.md" -Headers $h -TimeoutSec 30 | Out-Null
        Fail "a random report id returned a file"
    } catch {
        $code = [int]$_.Exception.Response.StatusCode
        if ($code -ne 404) { Fail "random report id -> $code, expected 404" }
    }
    Ok "cross-user and random ids -> 404"

    # ------------------------------------------------------------- admin
    Head "6. admin overview: 403 for an agent, aggregates for an admin"
    try {
        Invoke-RestMethod "$Base/admin/overview" -Headers $h -TimeoutSec 30 | Out-Null
        Fail "admin overview was accepted for a non-admin"
    } catch {
        $code = [int]$_.Exception.Response.StatusCode
        if ($code -ne 403) { Fail "agent /admin/overview -> $code, expected 403" }
    }
    Ok "agent /admin/overview -> 403"

    Ensure-User "admin@test.com" "Admin Account"
    & $python (Join-Path $root "scripts\set_role.py") --email "admin@test.com" --role admin 2>&1 | ForEach-Object { Write-Host $_ }
    if ($LASTEXITCODE -ne 0) { Fail "could not promote admin@test.com" }
    $adminLogin = Login "admin@test.com" $Password
    $ha = @{ Authorization = "Bearer $($adminLogin.access_token)" }

    $ov = Invoke-RestMethod "$Base/admin/overview?days=30" -Headers $ha -TimeoutSec 60
    if ($ov.users.total -lt 3) { Fail "overview sees $($ov.users.total) users, expected at least 3" }
    if ($ov.reports.total -lt 2) { Fail "overview sees $($ov.reports.total) reports, expected at least 2" }
    if ($ov.tokens.total -le 0) { Fail "overview reports zero tokens" }
    if ($ov.by_day.Count -lt 1) { Fail "overview has no daily buckets" }
    if ($ov.by_model.Count -lt 1) { Fail "overview has no per-model rows" }
    if ($ov.top_users.Count -lt 1) { Fail "overview has no top users" }
    if (-not $ov.cost.pricing.as_of) { Fail "overview has no pricing as_of" }
    Ok ("users={0} ({1} active), jobs={2}, reports={3} ({4} ready/{5} failed)" -f `
        $ov.users.total, $ov.users.active, $ov.jobs.total, $ov.reports.total, $ov.reports.ready, $ov.reports.failed)
    Ok ("tokens={0:N0} (research {1:N0} + report {2:N0}), shadow cost = `${3}" -f `
        $ov.tokens.total, $ov.tokens.research, $ov.tokens.report, $ov.cost.estimated_usd)
    Note ("success_rate={0}, failures in window={1}, days in chart={2}" -f `
        $ov.reports.success_rate, $ov.recent_failures.Count, $ov.by_day.Count)
    foreach ($row in $ov.top_users) {
        Note ("{0,-28} jobs={1,3} reports={2,3} tokens={3,9:N0}" -f $row.email, $row.jobs, $row.reports, $row.tokens)
    }

    $week = Invoke-RestMethod "$Base/admin/overview?days=7" -Headers $ha -TimeoutSec 60
    if ($week.window.days -ne 7) { Fail "days=7 echoed $($week.window.days)" }
    Ok "days=7 -> window $($week.window.from.Substring(0,10)) .. $($week.window.to.Substring(0,10))"

    try {
        Invoke-RestMethod "$Base/admin/overview?days=0" -Headers $ha -TimeoutSec 30 | Out-Null
        Fail "days=0 was accepted"
    } catch {
        $code = [int]$_.Exception.Response.StatusCode
        if ($code -ne 422) { Fail "days=0 -> $code, expected 422" }
    }
    Ok "days=0 -> 422"

    Write-Host "`nAll Phase 8 checks passed." -ForegroundColor Green
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
        Write-Host "`nStopped the Phase 8 API." -ForegroundColor DarkGray
    }
}
