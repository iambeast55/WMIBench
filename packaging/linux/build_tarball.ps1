param(
    [string]$OutputDir = "dist",
    [string]$PackageName = "wmibench-preview"
)

$ErrorActionPreference = "Stop"

if ($PackageName -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$') {
    throw "PackageName must be a simple directory name"
}

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$distDir = Join-Path $repoRoot $OutputDir
$stageDir = Join-Path $distDir $PackageName
$archive = Join-Path $distDir "$PackageName.tar.gz"

New-Item -ItemType Directory -Force -Path $distDir | Out-Null
$resolvedDist = (Resolve-Path $distDir).Path
$resolvedRepo = (Resolve-Path $repoRoot).Path
if (-not $resolvedDist.StartsWith($resolvedRepo + [IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to package outside repository: $resolvedDist"
}

if (Test-Path $stageDir) {
    $resolvedStage = (Resolve-Path $stageDir).Path
    if (-not $resolvedStage.StartsWith($resolvedDist + [IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove staging directory outside dist: $resolvedStage"
    }
    Remove-Item -LiteralPath $stageDir -Recurse -Force
}
if (Test-Path $archive) {
    $resolvedArchive = (Resolve-Path $archive).Path
    if (-not $resolvedArchive.StartsWith($resolvedDist + [IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove archive outside dist: $resolvedArchive"
    }
    Remove-Item -LiteralPath $archive -Force
}

New-Item -ItemType Directory -Force -Path $stageDir | Out-Null

$items = @(
    "pyproject.toml",
    "README.md",
    "BACKLOG.md",
    "WMI_PLAN.md",
    "WMI_COMMANDS_PLAN.md",
    "docs",
    "examples",
    "src",
    "tests",
    "packaging"
)

foreach ($item in $items) {
    $source = Join-Path $repoRoot $item
    $destination = Join-Path $stageDir $item
    if (Test-Path $source -PathType Container) {
        Copy-Item -LiteralPath $source -Destination $destination -Recurse
    } else {
        Copy-Item -LiteralPath $source -Destination $destination
    }
}

Copy-Item -LiteralPath (Join-Path $repoRoot "packaging\linux\README-linux.md") `
    -Destination (Join-Path $stageDir "README-linux.md")
Copy-Item -LiteralPath (Join-Path $repoRoot "packaging\linux\TESTING-WMI.md") `
    -Destination (Join-Path $stageDir "TESTING-WMI.md")
Copy-Item -LiteralPath (Join-Path $repoRoot "docs\ADDING_WMI_COMMANDS.md") `
    -Destination (Join-Path $stageDir "ADDING_WMI_COMMANDS.md")

$stagePrefix = (Resolve-Path -LiteralPath $stageDir).Path + [IO.Path]::DirectorySeparatorChar
foreach ($cache in Get-ChildItem -LiteralPath $stageDir -Recurse -Directory -Filter "__pycache__") {
    $cachePath = (Resolve-Path -LiteralPath $cache.FullName).Path
    if (-not $cachePath.StartsWith($stagePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove cache outside staging directory: $cachePath"
    }
    Remove-Item -LiteralPath $cachePath -Recurse -Force
}
Get-ChildItem -LiteralPath $stageDir -Recurse -File |
    Where-Object { $_.Extension -in @(".pyc", ".pyo") } |
    Remove-Item -Force

# Windows checkouts can contain CRLF; Bash launchers must use Unix line endings.
foreach ($script in Get-ChildItem -LiteralPath $stageDir -Recurse -File |
         Where-Object { $_.Extension -eq ".sh" -or $_.Name -eq "remoteops-dcom" }) {
    $contents = [IO.File]::ReadAllText($script.FullName).Replace("`r`n", "`n")
    [IO.File]::WriteAllText($script.FullName, $contents, [Text.UTF8Encoding]::new($false))
}

tar -czf $archive -C $distDir $PackageName
if ($LASTEXITCODE -ne 0) {
    throw "tar failed with exit code $LASTEXITCODE"
}

Write-Host "Created $archive"
