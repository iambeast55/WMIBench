param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "WMIBench")
)

$ErrorActionPreference = "Stop"
$Source = (Resolve-Path (Split-Path -Parent $MyInvocation.MyCommand.Path)).Path
$Target = [IO.Path]::GetFullPath($InstallDir)

if ($Source.TrimEnd('\') -ieq $Target.TrimEnd('\')) {
    Write-Host "WMIBench is already in $Target"
    exit 0
}

New-Item -ItemType Directory -Force -Path $Target | Out-Null
$preserve = @("remoteops.sqlite3", "commands.json", "commands.json.v1.bak")
Get-ChildItem -LiteralPath $Source -Force |
    Where-Object { $_.Name -notin $preserve -and $_.Name -ne "plugins" } |
    Copy-Item -Destination $Target -Recurse -Force

New-Item -ItemType Directory -Force -Path (Join-Path $Target "plugins") | Out-Null
if (Test-Path (Join-Path $Source "plugins")) {
    Copy-Item -Path (Join-Path $Source "plugins\*") -Destination (Join-Path $Target "plugins") -Recurse -Force
}

Write-Host "WMIBench installed to $Target"
Write-Host "Run: $Target\run.cmd"
