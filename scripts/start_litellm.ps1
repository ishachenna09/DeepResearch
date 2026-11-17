# Starts the LiteLLM proxy from its OWN virtualenv (.venv-litellm).
#
# WHY a second venv?
#   litellm[proxy] imports litellm.proxy.proxy_server -> needs mcp>=2.3.0
#   langchain-mcp-adapters (used by the API) needs mcp<2.0.0
#   -> the two packages cannot share one site-packages. In production LiteLLM is
#      a separate container, so locally it is a separate venv: same topology.
#
# Setup once:
#   python -m venv .venv-litellm
#   .\.venv-litellm\Scripts\python.exe -m pip install -r scripts\requirements-litellm.txt
$root = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $root '.venv-litellm\Scripts\python.exe'
$litellm = Join-Path $root '.venv-litellm\Scripts\litellm.exe'
$required = @('GEMINI_API_KEY', 'GROQ_API_KEY', 'LITELLM_MASTER_KEY')

if (-not (Test-Path $litellm)) {
    Write-Host "STOP: .venv-litellm is missing (or has no litellm.exe)." -ForegroundColor Red
    Write-Host "Run once, from the repo root:" -ForegroundColor Yellow
    Write-Host "  python -m venv .venv-litellm"
    Write-Host "  .\.venv-litellm\Scripts\python.exe -m pip install -r scripts\requirements-litellm.txt"
    exit 1
}

$loaded = @{}
Get-Content (Join-Path $root '.env') | ForEach-Object {
    if ($_ -match '^\s*([^#=]+)=(.*)$') {
        $k = $matches[1].Trim(); $v = $matches[2].Trim()
        if ($required -contains $k -and $v -ne '') { $loaded[$k] = $v }
    }
}

$missing = $required | Where-Object { -not $loaded.ContainsKey($_) }
if ($missing) {
    Write-Host "STOP: missing in .env -> $($missing -join ', ')" -ForegroundColor Red
    Write-Host "Add them (same value the FastAPI app will use), then re-run." -ForegroundColor Yellow
    exit 1
}

foreach ($k in $loaded.Keys) { [Environment]::SetEnvironmentVariable($k, $loaded[$k], 'Process') }

# LiteLLM's startup banner contains non-ASCII characters. If stdout is a pipe
# (redirected log, Docker log driver) Python defaults to cp1252 on Windows and
# raises UnicodeEncodeError -> startup aborts. Force UTF-8 for the child.
[Environment]::SetEnvironmentVariable('PYTHONIOENCODING', 'utf-8', 'Process')

Write-Host "Loaded keys: $($loaded.Keys -join ', ')" -ForegroundColor Green
try {
    # PowerShell does not process backslash escapes inside double quotes, so the
    # snippet below must not use escaped quotes (f-string with \" made this print
    # nothing at all). Single quotes inside a double-quoted string are fine.
    $versions = & $venvPython -c "import importlib.metadata as m; print('litellm', m.version('litellm'), '| mcp', m.version('mcp'))" 2>$null
    if ($versions) { Write-Host "Versions:    $versions" -ForegroundColor DarkGray }
} catch { }

Set-Location $root
Write-Host "Starting LiteLLM proxy on :4000  (Gemini -> Groq -> Gemini-Lite)..." -ForegroundColor Cyan
& $litellm --config litellm_config.yaml --port 4000
