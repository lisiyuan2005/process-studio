# Build the Tauri desktop shell with its packaged Python worker (Windows).
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

python -m pip install -e ".[render]"
python -m pip install "pyinstaller>=6.10"

# The shell spawns this binary for every RPC call, so it ships inside the bundle.
python -m PyInstaller `
  --noconfirm `
  --clean `
  --distpath desktop/src-tauri/resources/worker `
  --workpath work/pyinstaller-worker `
  packaging/ProcessStudioWorker.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed to build the process worker." }

$Worker = "desktop/src-tauri/resources/worker/process-studio-worker.exe"
if (-not (Test-Path $Worker)) { throw "The packaged worker is missing at $Worker." }

# Smoke-test the worker on its own before it is wrapped in an installer.
$Response = '{"kind":"request","id":1,"method":"describe"}' | & $Worker
if ($LASTEXITCODE -ne 0 -or -not ($Response -match '"protocolVersion"')) {
  throw "The packaged worker failed its describe smoke test."
}

Set-Location (Join-Path $ProjectRoot "desktop")
npm ci
npm run test
npm run tauri build -- --no-bundle
if ($LASTEXITCODE -ne 0) { throw "Tauri failed to build the desktop shell." }

Write-Host "Built desktop/src-tauri/target/release/process-studio-desktop.exe"
