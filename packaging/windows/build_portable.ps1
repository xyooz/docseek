param(
    [string]$OutputRoot = "dist"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $repoRoot

$version = (python -c "import docseek; print(docseek.__version__)").Trim()
if (-not $version) {
    throw "Unable to resolve DocSeek version"
}

$outputRootPath = Join-Path $repoRoot $OutputRoot
$stageRoot = Join-Path $outputRootPath "pyinstaller-stage"
$workRoot = Join-Path $repoRoot "build\pyinstaller"
$packageDir = Join-Path $outputRootPath "DocSeek-$version-Windows-x64"
$zipPath = Join-Path $outputRootPath "DocSeek-$version-Windows-x64.zip"

foreach ($path in @($stageRoot, $workRoot, $packageDir, $zipPath)) {
    if (Test-Path $path) {
        Remove-Item -Recurse -Force $path
    }
}
New-Item -ItemType Directory -Force -Path $outputRootPath | Out-Null

$pyInstallerArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--windowed",
    "--onedir",
    "--name", "DocSeek",
    "--distpath", $stageRoot,
    "--workpath", $workRoot,
    "--specpath", $workRoot,
    "--paths", (Join-Path $repoRoot "src"),
    "--collect-all", "python_calamine",
    "--collect-all", "iscc_tika",
    "--hidden-import", "pythoncom",
    "--hidden-import", "win32com.client",
    (Join-Path $repoRoot "packaging\windows\docseek_launcher.py")
)

Write-Host "Building DocSeek $version portable Windows package..."
& python @pyInstallerArgs
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE"
}

$frozenDir = Join-Path $stageRoot "DocSeek"
$frozenExe = Join-Path $frozenDir "DocSeek.exe"
if (-not (Test-Path $frozenExe)) {
    throw "Expected frozen executable was not created: $frozenExe"
}

Move-Item $frozenDir $packageDir
Copy-Item (Join-Path $repoRoot "packaging\windows\PORTABLE_README.txt") (Join-Path $packageDir "README.txt")

# A successful PyInstaller command is not enough: execute the frozen binary and
# require it to import the actual desktop/indexing modules before packaging.
$env:DOCSEEK_FROZEN_SMOKE = "1"
try {
    $process = Start-Process `
        -FilePath (Join-Path $packageDir "DocSeek.exe") `
        -Wait `
        -PassThru `
        -NoNewWindow
    if ($process.ExitCode -ne 0) {
        throw "Frozen DocSeek smoke failed with exit code $($process.ExitCode)"
    }
}
finally {
    Remove-Item Env:DOCSEEK_FROZEN_SMOKE -ErrorAction SilentlyContinue
}

Compress-Archive -Path (Join-Path $packageDir "*") -DestinationPath $zipPath -CompressionLevel Optimal
if (-not (Test-Path $zipPath)) {
    throw "Portable ZIP was not created"
}

$zip = Get-Item $zipPath
$sizeMiB = [Math]::Round($zip.Length / 1MB, 1)
Write-Host "Portable package: $($zip.FullName) ($sizeMiB MiB)"
Write-Output "DOCSEEK_VERSION=$version"
Write-Output "DOCSEEK_PACKAGE=$($zip.FullName)"
