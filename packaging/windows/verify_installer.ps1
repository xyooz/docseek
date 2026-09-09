param(
    [string]$SetupPath = "",
    [string]$InstallDir = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $repoRoot

if (-not $SetupPath) {
    $setup = Get-ChildItem (Join-Path $repoRoot "dist\DocSeek-*-Setup-x64.exe") | Select-Object -First 1
    if (-not $setup) {
        throw "Installer was not found under dist"
    }
    $SetupPath = $setup.FullName
}
else {
    $SetupPath = (Resolve-Path $SetupPath).Path
}

if (-not $InstallDir) {
    $base = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [IO.Path]::GetTempPath() }
    $InstallDir = Join-Path $base "DocSeek-installed"
}

if (Test-Path $InstallDir) {
    Remove-Item -Recurse -Force $InstallDir
}

# User index/settings deliberately live outside the application directory.
# Use a unique sentinel so this verifier never collides with a real user file.
$dataDir = Join-Path $env:USERPROFILE ".docseek"
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
$sentinel = Join-Path $dataDir ("installer-preserve-" + [Guid]::NewGuid().ToString("N") + ".txt")
Set-Content -Path $sentinel -Value "preserve-me" -Encoding utf8

$installed = $false
try {
    $install = Start-Process -FilePath $SetupPath -ArgumentList @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/SP-",
        "/DIR=$InstallDir",
        "/TASKS="
    ) -Wait -PassThru
    if ($install.ExitCode -ne 0) {
        throw "Silent install failed with exit code $($install.ExitCode)"
    }
    $installed = $true

    $exe = Join-Path $InstallDir "DocSeek.exe"
    if (-not (Test-Path $exe)) {
        throw "Installed DocSeek.exe was not found"
    }

    $env:DOCSEEK_FROZEN_SMOKE = "1"
    try {
        $smoke = Start-Process -FilePath $exe -Wait -PassThru
        if ($smoke.ExitCode -ne 0) {
            throw "Installed DocSeek smoke failed with exit code $($smoke.ExitCode)"
        }
    }
    finally {
        Remove-Item Env:DOCSEEK_FROZEN_SMOKE -ErrorAction SilentlyContinue
    }

    $env:DOCSEEK_FROZEN_RUST_SMOKE = "1"
    try {
        $rustSmoke = Start-Process -FilePath $exe -Wait -PassThru
        if ($rustSmoke.ExitCode -ne 0) {
            throw "Installed strict Rust smoke failed with exit code $($rustSmoke.ExitCode)"
        }
    }
    finally {
        Remove-Item Env:DOCSEEK_FROZEN_RUST_SMOKE -ErrorAction SilentlyContinue
    }

    $uninstaller = Join-Path $InstallDir "unins000.exe"
    if (-not (Test-Path $uninstaller)) {
        throw "Uninstaller was not created"
    }
    $uninstall = Start-Process -FilePath $uninstaller -ArgumentList @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART"
    ) -Wait -PassThru
    if ($uninstall.ExitCode -ne 0) {
        throw "Silent uninstall failed with exit code $($uninstall.ExitCode)"
    }
    $installed = $false

    for ($attempt = 0; $attempt -lt 20 -and (Test-Path $InstallDir); $attempt++) {
        Start-Sleep -Milliseconds 250
    }
    if (Test-Path $InstallDir) {
        throw "Application directory remains after uninstall: $InstallDir"
    }
    if (-not (Test-Path $sentinel)) {
        throw "Uninstall removed DocSeek user index/settings data"
    }

    Write-Host "Install -> frozen smoke -> uninstall lifecycle passed; user data was preserved."
}
finally {
    Remove-Item Env:DOCSEEK_FROZEN_SMOKE -ErrorAction SilentlyContinue
    Remove-Item Env:DOCSEEK_FROZEN_RUST_SMOKE -ErrorAction SilentlyContinue
    if ($installed -and (Test-Path (Join-Path $InstallDir "unins000.exe"))) {
        try {
            Start-Process -FilePath (Join-Path $InstallDir "unins000.exe") -ArgumentList @(
                "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"
            ) -Wait | Out-Null
        }
        catch {
            Write-Warning "Cleanup uninstall failed: $($_.Exception.Message)"
        }
    }
    Remove-Item -Force $sentinel -ErrorAction SilentlyContinue
}
