$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

py -3.12 -m venv .build-env
if ($LASTEXITCODE -ne 0) { throw "Install Python 3.12 x64 first." }
$python = Join-Path (Get-Location) ".build-env\Scripts\python.exe"
& $python -m pip install -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
& $python scripts/build_windows.py
if ($LASTEXITCODE -ne 0) { throw "Build or verification failed." }
