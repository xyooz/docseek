param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $repoRoot

if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
    throw "Rust/Cargo was not found. Install the stable Rust toolchain before building the portable package."
}

$maturin = Get-Command maturin -ErrorAction SilentlyContinue
if (-not $maturin) {
    Write-Host "Installing maturin for the Rust extension build..."
    & python -m pip install "maturin>=1.7,<2"
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to install maturin (exit code $LASTEXITCODE)"
    }
    $maturin = Get-Command maturin -ErrorAction SilentlyContinue
}
if (-not $maturin) {
    throw "maturin was not found after installation"
}

$wheelDir = Join-Path $repoRoot "target\wheels"
if (Test-Path $wheelDir) {
    Get-ChildItem -Path $wheelDir -Filter "docseek_rust-*.whl" -File |
        Remove-Item -Force
}
else {
    New-Item -ItemType Directory -Force -Path $wheelDir | Out-Null
}

$manifestPath = Join-Path $repoRoot "crates\docseek-python\Cargo.toml"
Write-Host "Building the release PyO3 wheel from $manifestPath ..."
& $maturin.Source build `
    --release `
    --interpreter python `
    --manifest-path $manifestPath
if ($LASTEXITCODE -ne 0) {
    throw "maturin build failed with exit code $LASTEXITCODE"
}

$wheel = Get-ChildItem -Path $wheelDir -Filter "docseek_rust-*.whl" -File |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if (-not $wheel) {
    throw "maturin did not produce a docseek_rust wheel under $wheelDir"
}

Write-Host "Installing $($wheel.Name) into the packaging Python environment..."
& python -m pip install --force-reinstall --no-deps $wheel.FullName
if ($LASTEXITCODE -ne 0) {
    throw "PyO3 wheel installation failed with exit code $LASTEXITCODE"
}

$modulePath = (& python -c "import docseek_rust; print(docseek_rust.__file__)").Trim()
if (-not $modulePath) {
    throw "docseek_rust could not be imported after wheel installation"
}

Write-Host "Rust extension ready: $modulePath"
Write-Output "DOCSEEK_RUST_WHEEL=$($wheel.FullName)"
Write-Output "DOCSEEK_RUST_MODULE=$modulePath"
