param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:WMIBENCH_DATA_DIR = $Root
$Executable = Join-Path $Root "WMIBench.exe"

& $Executable @Arguments
exit $LASTEXITCODE
