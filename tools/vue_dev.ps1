param(
    [int]$ApiPort = 8000,
    [int]$FrontendPort = 5173
)

$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

Start-Process -WindowStyle Hidden -FilePath $python -ArgumentList "-m", "uvicorn", "backend.app:app", "--host", "127.0.0.1", "--port", $ApiPort -WorkingDirectory $repoRoot
Set-Location (Join-Path $repoRoot "frontend")
npm run dev -- --port $FrontendPort
