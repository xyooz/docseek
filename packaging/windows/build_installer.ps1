param(
    [string]$OutputRoot = "dist"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $repoRoot

# Build the exact portable runtime that the installer will deploy. Keeping one
# runtime payload avoids a separate installer-only dependency graph.
& (Join-Path $PSScriptRoot "build_portable.ps1") -OutputRoot $OutputRoot
if ($LASTEXITCODE -ne 0) {
    throw "Portable package build failed with exit code $LASTEXITCODE"
}

$version = (python -c "import docseek; print(docseek.__version__)").Trim()
if (-not $version) {
    throw "Unable to resolve DocSeek version"
}

$outputRootPath = Join-Path $repoRoot $OutputRoot
$sourceDir = Join-Path $outputRootPath "DocSeek-$version-Windows-x64"
$issPath = Join-Path $PSScriptRoot "DocSeek.iss"
$setupPath = Join-Path $outputRootPath "DocSeek-$version-Setup-x64.exe"

if (-not (Test-Path $sourceDir)) {
    throw "Portable source directory not found: $sourceDir"
}
if (Test-Path $setupPath) {
    Remove-Item -Force $setupPath
}

$command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
$iscc = if ($command) { $command.Source } else { $null }
if (-not $iscc) {
    foreach ($candidate in @(
        "$env:ProgramFiles(x86)\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 7\ISCC.exe",
        "$env:ProgramFiles(x86)\Inno Setup 7\ISCC.exe"
    )) {
        if ($candidate -and (Test-Path $candidate)) {
            $iscc = $candidate
            break
        }
    }
}
if (-not $iscc) {
    throw "Inno Setup compiler (ISCC.exe) was not found"
}

Write-Host "Building DocSeek $version per-user installer with $iscc ..."
$isccArgs = @(
    "/Qp",
    "/DAppVersion=$version",
    "/DSourceDir=$sourceDir",
    "/DOutputDir=$outputRootPath",
    $issPath
)
& $iscc @isccArgs
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup build failed with exit code $LASTEXITCODE"
}
if (-not (Test-Path $setupPath)) {
    throw "Expected installer was not created: $setupPath"
}

$setup = Get-Item $setupPath
$sizeMiB = [Math]::Round($setup.Length / 1MB, 1)
Write-Host "Installer: $($setup.FullName) ($sizeMiB MiB)"
Write-Output "DOCSEEK_VERSION=$version"
Write-Output "DOCSEEK_INSTALLER=$($setup.FullName)"
