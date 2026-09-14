# Build the Tauri desktop shell with its packaged Python worker (Windows).
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

# Which kernels this build ships. PROCESS_STUDIO_KERNELS is what the worker
# reads (a comma-separated list of ids, unset for both); the variant names the
# product so two builds can sit side by side on one machine.
$Kernels = if ($env:PROCESS_STUDIO_KERNELS) { $env:PROCESS_STUDIO_KERNELS } else { "levelset,slab" }
$env:PROCESS_STUDIO_KERNELS = $Kernels
switch ($Kernels) {
  "slab"     { $Product = "Process Studio Slab";      $Identifier = "com.processstudio.desktop.slab" }
  "levelset" { $Product = "Process Studio Level Set"; $Identifier = "com.processstudio.desktop.levelset" }
  default    { $Product = "Process Studio";           $Identifier = "com.processstudio.desktop" }
}
New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot "work") | Out-Null
$VariantConfig = Join-Path $ProjectRoot "work/tauri-variant.json"
@{
  productName = $Product
  identifier = $Identifier
  app = @{ windows = @(@{ title = $Product; width = 1440; height = 900; minWidth = 960; minHeight = 640; resizable = $true; fullscreen = $false; center = $true }) }
} | ConvertTo-Json -Depth 5 | Set-Content -Path $VariantConfig -Encoding UTF8
Write-Host "Building $Product with kernels: $Kernels"

# The build's Python lives in its own virtual environment unless one is
# already active (see build_desktop.sh); PROCESS_STUDIO_VENV overrides where.
if (-not $env:VIRTUAL_ENV) {
    $venv = if ($env:PROCESS_STUDIO_VENV) { $env:PROCESS_STUDIO_VENV } else { Join-Path $ProjectRoot "work\venv" }
    if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
        Write-Host "Creating the build's virtual environment at $venv"
        python -m venv $venv
    }
    . (Join-Path $venv "Scripts\Activate.ps1")
}
python -m pip install --upgrade pip | Out-Null
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

# Smoke-test the worker on its own before it is wrapped in an installer. The
# kernels are checked by name: a worker that lost one of them still answers
# describe, and the shell would simply stop offering that kernel.
$Response = '{"kind":"request","id":1,"method":"describe"}' | & $Worker
if ($LASTEXITCODE -ne 0 -or -not ($Response -match '"protocolVersion"')) {
  throw "The packaged worker failed its describe smoke test."
}
foreach ($Kernel in $Kernels.Split(",")) {
  if (-not ($Response -replace '\s', '' -match [regex]::Escape("`"id`":`"$Kernel`""))) {
    throw "The packaged worker does not offer the $Kernel kernel."
  }
}

Set-Location (Join-Path $ProjectRoot "desktop")
npm ci
npm run test
npm run tauri build -- --no-bundle --config $VariantConfig
if ($LASTEXITCODE -ne 0) { throw "Tauri failed to build the desktop shell." }

Write-Host "Built desktop/src-tauri/target/release/process-studio-desktop.exe"
