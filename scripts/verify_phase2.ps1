# Phase 2 verification: MCP tools + guardrails.
#
# Prereqs: API on :8000 with an MCP server spawned at startup (see the API log
# line "MCP toolbox ready: ['fetch_page', 'web_search']").
#
# Section 6 registers/promotes admin@test.com (shared dev database) because
# Phase 9 made /tools/selftest admin-only. Sections 2-5 use the caller's token.
#
# Run: powershell -ExecutionPolicy Bypass -File C:\projects\DeepResearch\scripts\verify_phase2.ps1

param(
    [string]$Base     = "http://localhost:8000/api/v1",
    [string]$Email    = "you@test.com",
    [string]$Password = "password123"
)

$ErrorActionPreference = "Stop"

function Ok($m)   { Write-Host "[ OK ] $m" -ForegroundColor Green }
function Fail($m) { Write-Host "[FAIL] $m" -ForegroundColor Red; exit 1 }
function Head($m) { Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Body($e) { try { return $e.ErrorDetails.Message } catch { return "" } }

# ------------------------------------------------------------------ pre-flight
Head "0. API + MCP toolbox"
try { $hz = Invoke-RestMethod "$Base/healthz" -TimeoutSec 15 }
catch { Fail "API not reachable. Start it: powershell -File .\scripts\start_api.ps1" }
if ($hz.overall -ne "healthy") { Fail "healthz reports '$($hz.overall)'" }
Ok "healthz: $($hz | ConvertTo-Json -Compress)"

Head "1. login"
try {
    $login = Invoke-RestMethod -Method Post "$Base/auth/login" -ContentType "application/json" `
        -Body (@{ email = $Email; password = $Password } | ConvertTo-Json) -TimeoutSec 20
} catch { Fail "login failed: $($_.Exception.Message) $(Body $_)" }
$h = @{ Authorization = "Bearer $($login.access_token)" }
Ok "authenticated as $Email"

# ------------------------------------------------- what does the MCP server say
Head "2. GET /tools  (MCP handshake + tool schema)"
try { $tools = Invoke-RestMethod "$Base/tools" -Headers $h -TimeoutSec 30 }
catch { Fail "tools list failed: $($_.Exception.Message) $(Body $_) - is the MCP child process alive? See API log." }
$names = @($tools.tools | ForEach-Object { $_.name })
$tools.tools | Format-Table name, description -AutoSize | Out-String | Write-Host
if (-not ($names -contains "web_search" -and $names -contains "fetch_page")) {
    Fail "expected web_search + fetch_page, got: $($names -join ', ')"
}
Ok "tools advertised over $($tools.transport): $($names -join ', ')"

# --------------------------------------------------------------- web_search
Head "3. POST /tools/search  (Tavily through MCP)"
$sw = [Diagnostics.Stopwatch]::StartNew()
try {
    $search = Invoke-RestMethod -Method Post "$Base/tools/search" -Headers $h -ContentType "application/json" `
        -Body (@{ query = "Model Context Protocol python sdk"; max_results = 3 } | ConvertTo-Json) -TimeoutSec 60
} catch { Fail "search failed: $($_.Exception.Message) $(Body $_)" }
$sw.Stop()
@($search.results) | Select-Object -First 3 title, url | Format-Table -AutoSize | Out-String | Write-Host
if (@($search.results).Count -lt 1) { Fail "search returned no results" }
Ok "$(@($search.results).Count) hits in $($search.ms)ms"

# --------------------------------------------------------------- fetch_page
Head "4. POST /tools/fetch  (extraction + injection scan)"
try {
    $page = Invoke-RestMethod -Method Post "$Base/tools/fetch" -Headers $h -ContentType "application/json" `
        -Body (@{ url = "https://example.com" } | ConvertTo-Json) -TimeoutSec 60
} catch { Fail "fetch failed: $($_.Exception.Message) $(Body $_)" }
$page | Select-Object url, title, http_status, chars, injected, ms | Format-List | Out-String | Write-Host
if ($page.chars -lt 1) { Fail "no text extracted" }
Ok "extracted $($page.chars) chars, injected=$($page.injected), $($page.ms)ms"

# ------------------------------------------------------------- SSRF negative
Head "5. SSRF guard  (cloud-metadata address must be refused)"
$code = 0; $detail = ""
try {
    $null = Invoke-RestMethod -Method Post "$Base/tools/fetch" -Headers $h -ContentType "application/json" `
        -Body '{"url":"http://169.254.169.254/latest/meta-data/"}' -TimeoutSec 30
} catch {
    $code = [int]$_.Exception.Response.StatusCode
    $detail = Body $_
}
if ($code -ne 400) { Fail "expected HTTP 400 for the metadata address, got '$code' $detail" }
Ok "HTTP 400 :: $detail"

# --------------------------------------------------------------- one-shot probe
Head "6. GET /tools/selftest  (admin-only since Phase 9)"
# The probe spends Tavily quota and hits two external services, so Phase 9 made
# it admin-only. Ensure the admin account exists, promote it (idempotent), then
# call the probe with that token - the same setup verify_phase8.ps1 uses.
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
try {
    Invoke-RestMethod -Method Post "$Base/auth/register" -ContentType "application/json" `
        -Body (@{ email = "admin@test.com"; full_name = "Admin Account"; password = $Password } | ConvertTo-Json) `
        -TimeoutSec 30 | Out-Null
} catch {
    if ([int]$_.Exception.Response.StatusCode -ne 409) { Fail "could not register admin@test.com: $($_.Exception.Message) $(Body $_)" }
}
& $python (Join-Path $root "scripts\set_role.py") --email "admin@test.com" --role admin 2>&1 | ForEach-Object { Write-Host $_ }
if ($LASTEXITCODE -ne 0) { Fail "could not promote admin@test.com" }
try {
    $adminLogin = Invoke-RestMethod -Method Post "$Base/auth/login" -ContentType "application/json" `
        -Body (@{ email = "admin@test.com"; password = $Password } | ConvertTo-Json) -TimeoutSec 20
} catch { Fail "admin login failed: $($_.Exception.Message) $(Body $_)" }
$adminH = @{ Authorization = "Bearer $($adminLogin.access_token)" }
try { $self = Invoke-RestMethod "$Base/tools/selftest" -Headers $adminH -TimeoutSec 90 }
catch { Fail "selftest failed: $($_.Exception.Message) $(Body $_)" }
$self.checks | Format-Table check, ok, detail, ms -AutoSize | Out-String | Write-Host
if (-not $self.ok) { Fail "selftest reported a failing check" }
Ok "all checks passed"

Write-Host @"

==================================================================
PHASE 2 PASSED - MCP tools, extraction, injection scan, SSRF guard.

  Commit:  git add -A
           git commit -m "feat: MCP tool servers (web_search, fetch_page) + guardrails"

  Next: Phase 3 - fan-out search, page cache, notes -> pgvector
==================================================================
"@ -ForegroundColor Yellow
