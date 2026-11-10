$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Split-Path -Parent $MyInvocation.MyCommand.Path)).Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Run .\install.ps1 first"
}

$env:PYTHONPATH = "$Root\src" + $(if ($env:PYTHONPATH) { ";$env:PYTHONPATH" } else { "" })
$env:WMIBENCH_DATA_DIR = $Root
& $Python -m wmibench @args
exit $LASTEXITCODE
