$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (Test-Path '.venv\bin\python') {
    Write-Host 'A macOS/Linux virtual environment was found. Remove only .venv, then rerun setup.ps1. Archive data is separate.'
    exit 1
}
if ($env:COVERLY_PYTHON) {
    $PythonExe = $env:COVERLY_PYTHON
    $PythonArgs = @()
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $PythonExe = 'py'
    $PythonArgs = @('-3')
} else {
    $PythonExe = 'python'
    $PythonArgs = @()
}
& $PythonExe @PythonArgs -c 'import sys; sys.exit(sys.version_info < (3,12))'
if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.12 or newer from python.org, then rerun setup.ps1.' }
& $PythonExe @PythonArgs -m venv .venv
if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
& '.\.venv\Scripts\python.exe' -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'pip upgrade failed.' }
& '.\.venv\Scripts\python.exe' -m pip install -e '.[test]'
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& '.\.venv\Scripts\python.exe' -m coverly migrate
if ($LASTEXITCODE -ne 0) { throw 'Database migration failed.' }
Write-Host 'Coverly is ready. Run .\start.ps1, then open http://localhost:8000.'
Write-Host 'Tamil OCR: install Tesseract with tam+eng data and add tesseract.exe to PATH. See README.md.'
