$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "The project environment is missing. Follow the Windows setup steps in README.md first."
}

& $python -c "import pydantic_core._pydantic_core"
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "The virtual environment contains packages for a different Python version." -ForegroundColor Yellow
    Write-Host "Recreate .venv with one Python version, then reinstall requirements:" -ForegroundColor Yellow
    Write-Host "  Remove-Item -Recurse -Force .venv"
    Write-Host "  py -3.12 -m venv .venv"
    Write-Host "  .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
    Write-Host "  .\.venv\Scripts\python.exe -m playwright install chromium"
    exit 1
}

# Keep one stable process by default. Set APP_RELOAD=true explicitly only while
# developing; the reload supervisor creates extra child processes on Windows.
if (-not $env:APP_RELOAD) {
    $env:APP_RELOAD = "false"
}

& $python (Join-Path $projectRoot "run.py")
