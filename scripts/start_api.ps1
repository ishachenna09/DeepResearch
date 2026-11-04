# Starts the FastAPI app using the venv's uvicorn directly (no activation needed).
$root = Split-Path -Parent $PSScriptRoot
$uvicorn = Join-Path $root '.venv\Scripts\uvicorn.exe'
Set-Location (Join-Path $root 'api')
Write-Host "Starting FastAPI on :8000  (docs: http://localhost:8000/docs)" -ForegroundColor Cyan
& $uvicorn app.main:app --reload --port 8000
