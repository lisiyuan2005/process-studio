# Build the Tauri desktop shell with its packaged Python worker (Windows).
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

# Which kernels this build ships. PROCESS_STUDIO_KERNELS is what the worker
# reads (a comma-separated list of ids, unset for both); the variant names the
# product so two builds can sit side by side on one machine.
$Kernels = if ($env:PROCESS_STUDIO_KERNELS) { $env:PROCESS_STUDIO_KERNELS } else { "levelset,slab" }
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

# The worker ships as an embeddable Python distribution with the package
# installed into it as ordinary PyPI wheels, not a PyInstaller executable.
# PyInstaller's single-file bundle is exactly the shape antivirus and
# endpoint-protection tools flag on sight (an unsigned executable holding an
# interpreter and bytecode); python.exe here is Python's own signed build,
# and everything installed into it is a normal wheel, so there is nothing
# unusual for a scanner to catch. This needs no system Python at all:
# PowerShell downloads and unpacks everything itself, then bootstraps pip
# with the embeddable interpreter.
try {
  # Windows PowerShell 5.1 (unlike PowerShell 7, which CI uses) does not
  # always default to TLS 1.2, and both download hosts require it.
  [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch {}

$PythonVersion = if ($env:PROCESS_STUDIO_EMBED_PYTHON) { $env:PROCESS_STUDIO_EMBED_PYTHON } else { "3.12.7" }
$PythonDir = Join-Path $ProjectRoot "desktop/src-tauri/resources/python"
if (Test-Path $PythonDir) { Remove-Item -Recurse -Force $PythonDir }
New-Item -ItemType Directory -Force -Path $PythonDir | Out-Null

$EmbedZip = Join-Path $ProjectRoot "work/python-embed.zip"
Write-Host "Downloading the embeddable Python $PythonVersion runtime"
Invoke-WebRequest -UseBasicParsing `
  -Uri "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip" `
  -OutFile $EmbedZip
Expand-Archive -Path $EmbedZip -DestinationPath $PythonDir -Force
Remove-Item $EmbedZip

$PythonExe = Join-Path $PythonDir "python.exe"
$ShortVersion = ($PythonVersion.Split(".")[0..1] -join "")  # "3.12.7" -> "312"
$PthFile = Join-Path $PythonDir "python$ShortVersion._pth"
if (-not (Test-Path $PthFile)) {
  throw "Expected $PthFile in the embeddable distribution; its layout may have changed."
}
# An embeddable distribution runs isolated (only its bundled stdlib zip on
# sys.path) until site.py runs: uncomment the import it ships commented out,
# and add the site-packages directory pip installs into once site.py does.
(Get-Content $PthFile) -replace '^#\s*import site', 'import site' | Set-Content $PthFile
Add-Content -Path $PthFile -Value "Lib\site-packages"

Write-Host "Bootstrapping pip into the embeddable Python"
$GetPip = Join-Path $ProjectRoot "work/get-pip.py"
Invoke-WebRequest -UseBasicParsing -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $GetPip
& $PythonExe $GetPip --no-warn-script-location
if ($LASTEXITCODE -ne 0) { throw "Bootstrapping pip into the embeddable Python failed." }
Remove-Item $GetPip

# A "._pth" file (present the moment site.py is even optionally enabled)
# makes CPython ignore PYTHONPATH outright, isolated mode or not. pip's
# normal build isolation depends on exactly that variable to hand its
# isolated setuptools install to the subprocess it spawns for the local
# package's build hooks, so with a ._pth interpreter that subprocess can
# never see it ("Cannot import 'setuptools.build_meta'") no matter how
# cleanly the isolated install itself succeeded. Installing the build
# backend into the interpreter's own site-packages and skipping build
# isolation altogether sidesteps this: every hook call then runs against
# sys.path as this interpreter already sees it.
Write-Host "Installing the build backend into the embeddable Python"
& $PythonExe -m pip install --no-warn-script-location setuptools wheel
if ($LASTEXITCODE -ne 0) { throw "Installing setuptools into the embeddable Python failed." }

Write-Host "Installing process-studio into the embeddable Python"
& $PythonExe -m pip install --no-warn-script-location --no-build-isolation ".[render]"
if ($LASTEXITCODE -ne 0) { throw "Installing process-studio into the embeddable Python failed." }

# Which kernels this worker offers travels as a file, not just the
# PROCESS_STUDIO_KERNELS this script set for itself: the end user's machine
# never has that environment variable, so the registry falls back to reading
# this beside it (see process_studio/kernels/__init__.py). shapely and
# trimesh are core dependencies either way (the slab kernel needs them
# unconditionally), so a level-set-only build still carries them; only the
# kernels the registry offers depends on this file.
$KernelsDir = Join-Path $PythonDir "Lib/site-packages/process_studio/kernels"
if (-not (Test-Path $KernelsDir)) { throw "process_studio was not installed where expected: $KernelsDir" }
Set-Content -Path (Join-Path $KernelsDir "enabled.txt") -Value $Kernels -NoNewline

# Smoke-test the worker on its own before it is wrapped in the app. The
# kernels are checked by name: a worker that lost one of them still answers
# describe, and the shell would simply stop offering that kernel.
$Response = '{"kind":"request","id":1,"method":"describe"}' | & $PythonExe -m process_studio.worker
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
