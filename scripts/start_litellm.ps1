# Starts the LiteLLM proxy using the venv's litellm.exe (no activation needed).
# Exports ONLY the keys the proxy consumes, and fails fast if any is missing.
# Our DB URL is named APP_DATABASE_URL so LiteLLM never mistakes it for its own DB.
$root = Split-Path -Parent $PSScriptRoot
$litellm = Join-Path $root '.venv\Scripts\litellm.exe'
$required = @('GEMINI_API_KEY', 'GROQ_API_KEY', 'LITELLM_MASTER_KEY')

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
Write-Host "Loaded keys: $($loaded.Keys -join ', ')" -ForegroundColor Green

Set-Location $root
Write-Host "Starting LiteLLM proxy on :4000  (Gemini -> Groq fallback)..." -ForegroundColor Cyan
& $litellm --config litellm_config.yaml --port 4000
