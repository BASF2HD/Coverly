$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
& '.\.venv\Scripts\python.exe' -m pip install -e '.[ai]'
if ($LASTEXITCODE -ne 0) { throw 'AI dependency installation failed; basic CPU restoration remains available.' }
& '.\.venv\Scripts\python.exe' tools/setup_ai.py
if ($LASTEXITCODE -ne 0) { throw 'Model download or checksum verification failed.' }
