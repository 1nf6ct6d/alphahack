param([switch]$Demo, [int]$Port = 8080)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$pythonPath = Join-Path $projectRoot '.venv_backend\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    py -3.14 -m venv .venv_backend
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.14 environment creation failed' }
}
& $pythonPath -m pip install -r requirements-backend.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
if ($Demo) { $env:ALFA_DEMO_SEED = '1' }
& $pythonPath -m backend.cli serve --host 127.0.0.1 --port $Port
exit $LASTEXITCODE
