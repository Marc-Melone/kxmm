# kxmm control for Windows: .\kxmm.ps1 start|stop|status|log|test|run
$PY = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $PY)) {
    Write-Host "no venv at .venv\Scripts\python.exe; see README.md (Windows setup)" -ForegroundColor Red
    exit 1
}
& $PY (Join-Path $PSScriptRoot "kxmm.py") @args
exit $LASTEXITCODE
