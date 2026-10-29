$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$buildEnv = Join-Path $PSScriptRoot 'output/desktop-build/venv-windows'
py -3.12 -m venv $buildEnv
if ($LASTEXITCODE -ne 0) { throw 'Install 64-bit Python 3.12 from python.org first.' }
$buildPython = Join-Path $buildEnv 'Scripts/python.exe'
& $buildPython -m pip install -e '.[desktop,build,browser]'
if ($LASTEXITCODE -ne 0) { throw 'Desktop dependency installation failed.' }
& $buildPython scripts/build_desktop.py @args
if ($LASTEXITCODE -ne 0) { throw 'Desktop build failed.' }
