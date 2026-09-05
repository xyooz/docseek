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
$frozenExe = Join-Path $packageDir "DocSeek.exe"

# A successful PyInstaller command is not enough: execute the frozen binary and
# require it to import the actual desktop/indexing modules before packaging.
$env:DOCSEEK_FROZEN_SMOKE = "1"
try {
    $process = Start-Process `
        -FilePath $frozenExe `
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

# Index Pipeline V2 launches compatibility parsers through the same frozen EXE.
# Prove that hidden worker mode is actually packaged, runnable and able to emit
# a chunk stream; source-level imports alone cannot catch PyInstaller misses.
$workerSource = Join-Path $outputRootPath "worker-smoke.txt"
$workerOutput = Join-Path $outputRootPath "worker-smoke.bin"
try {
    Set-Content -Path $workerSource -Value "DocSeek frozen extraction worker smoke" -Encoding UTF8
    $worker = Start-Process `
        -FilePath $frozenExe `
        -ArgumentList @("--docseek-extract-worker", $workerSource, $workerOutput) `
        -Wait `
        -PassThru `
        -NoNewWindow
    if ($worker.ExitCode -ne 0) {
        throw "Frozen extraction worker failed with exit code $($worker.ExitCode)"
    }
    if (-not (Test-Path $workerOutput)) {
        throw "Frozen extraction worker did not create its chunk output"
    }
    if ((Get-Item $workerOutput).Length -le 0) {
        throw "Frozen extraction worker produced an empty chunk output"
    }
}
finally {
    Remove-Item $workerSource -Force -ErrorAction SilentlyContinue
    Remove-Item $workerOutput -Force -ErrorAction SilentlyContinue
    Remove-Item ($workerOutput + ".error.txt") -Force -ErrorAction SilentlyContinue
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
