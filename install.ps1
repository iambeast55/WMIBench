$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Split-Path -Parent $MyInvocation.MyCommand.Path)).Path
$PyLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($null -ne $PyLauncher) {
    & $PyLauncher.Source -3.12 -m venv (Join-Path $Root ".venv")
} else {
    $PyLauncher = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $PyLauncher) { throw "Python 3 is required" }
    & $PyLauncher.Source -m venv (Join-Path $Root ".venv")
}
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install --editable $Root
New-Item -ItemType Directory -Force -Path (Join-Path $Root "plugins") | Out-Null

Write-Host "WMIBench installed in $Root\.venv"
Write-Host "Run: .\run.ps1"
