param([string]$PackageName = "wmibench-task-registry-plugins")

$ErrorActionPreference = "Stop"
if ($PackageName -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$') {
    throw "PackageName must be a simple directory name"
}
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$dist = Join-Path $repo "dist"
New-Item -ItemType Directory -Force -Path $dist | Out-Null
$stage = Join-Path $dist $PackageName
$archive = Join-Path $dist "$PackageName.zip"
if ((Test-Path -LiteralPath $stage) -or (Test-Path -LiteralPath $archive)) {
    throw "Package already exists. Choose a new PackageName to preserve the prior bundle."
}
New-Item -ItemType Directory -Path $stage | Out-Null
foreach ($alias in @("scheduled-task", "registry")) {
    $destination = Join-Path $stage $alias
    New-Item -ItemType Directory -Path $destination | Out-Null
    foreach ($file in @("command.json", "implementation.py", "test_implementation.py")) {
        Copy-Item -LiteralPath (Join-Path $repo "examples\plugins\$alias\$file") -Destination $destination
    }
}
foreach ($file in @("ADMIN_PLUGINS.md", "WINDOWS_SETUP.md", "STANDALONE.md", "PYTHON_COMMANDS.md", "PLUGIN_API.md")) {
    Copy-Item -LiteralPath (Join-Path $repo "docs\$file") -Destination $stage
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "README.md") -Destination $stage
Compress-Archive -LiteralPath $stage -DestinationPath $archive
Write-Host "Created $archive"
