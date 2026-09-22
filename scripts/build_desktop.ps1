# Build the Tauri desktop shell with its packaged Python worker (Windows).
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$Product = "Process Studio"
$Identifier = "com.processstudio.desktop"
New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot "work") | Out-Null
$VariantConfig = Join-Path $ProjectRoot "work/tauri-variant.json"
@{
  productName = $Product
  identifier = $Identifier
  app = @{ windows = @(@{ title = $Product; width = 1440; height = 900; minWidth = 720; minHeight = 600; resizable = $true; fullscreen = $false; center = $true }) }
} | ConvertTo-Json -Depth 5 | Set-Content -Path $VariantConfig -Encoding UTF8
Write-Host "Building $Product"

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
# The worker's own dependencies, installed explicitly so the package itself
# can go in with --no-deps. scipy is not here either: the one place that
# reached for it was trimesh turning face colours into vertex colours when
# writing a mesh file, and the exporter colours the vertices itself now.
# Keep this list in sync with pyproject.toml's [project] dependencies.
& $PythonExe -m pip install --no-warn-script-location --no-build-isolation `
  numpy pillow gdstk openpyxl pyyaml shapely trimesh mapbox-earcut truststore certifi
if ($LASTEXITCODE -ne 0) { throw "Installing process-studio's dependencies into the embeddable Python failed." }
& $PythonExe -m pip install --no-warn-script-location --no-build-isolation --no-deps .
if ($LASTEXITCODE -ne 0) { throw "Installing process-studio into the embeddable Python failed." }

# Smoke-test the worker on its own before it is wrapped in the app.
$Response = '{"kind":"request","id":1,"method":"describe"}' | & $PythonExe -m process_studio.worker
if ($LASTEXITCODE -ne 0 -or -not ($Response -match '"protocolVersion"')) {
  throw "The packaged worker failed its describe smoke test."
}

# The update check is the one thing that needs certificate authorities; a
# build that cannot verify one has lost certifi and truststore. Both halves
# are needed: a successful check carries the release notes, which carry
# commit subjects, one of which is about certificates.
$Update = '{"kind":"request","id":2,"method":"check_update"}' | & $PythonExe -m process_studio.worker
if ($Update -match '"ok":false' -and $Update -match 'certificate') {
  throw "The packaged worker cannot verify certificates: $Update"
}
# The kernel is checked by name: a worker that lost it still answers
# describe, and the shell would simply have nothing to run a project on.
if (-not ($Response -replace '\s', '' -match '"id":"slab"')) {
  throw "The packaged worker does not offer the slab kernel."
}

# The 3D view's mesh is built on several cores, which means the worker has to
# be able to start a second copy of this interpreter -- an embeddable Python
# restricts its own import path, so that is worth finding out here rather than
# on a user's machine. It only costs speed, so a one-core builder is not a
# failure; a machine with cores that cannot use them is.
$Cores = & $PythonExe -m process_studio.worker --json cores
Write-Host "Mesh pool: $Cores"
$CoreReport = ($Cores -join "") -replace '\s', ''
if ($CoreReport -notmatch '"workers":1,' -and $CoreReport -match '"pool":false') {
  throw "The packaged worker cannot build meshes on more than one core: $Cores"
}
# And it must be able to *build* a mesh in one of those children, which is
# the part that can be right in the source tree and lost in the packaging.
if ($CoreReport -match '"warmed":false') {
  throw "The packaged worker's children cannot build a mesh: $Cores"
}

# Trim the bundle now that everything that runs on it has already run:
# pip, setuptools and wheel exist only to have installed the rest and are
# never imported by the worker itself; every package's own test suite
# ships inside the wheel but nothing outside that package ever imports
# it; __pycache__ is a cache the interpreter rebuilds on first import (or
# runs fine without, just marginally slower) and only bloats the archive.
# None of this touches process_studio, deviceflow or any package's actual
# runtime code, only build tooling and dead weight beside it.
Write-Host "Trimming build tooling, test suites and bytecode caches from the bundle"
$SitePackages = Join-Path $PythonDir "Lib/site-packages"
foreach ($Name in @("pip", "setuptools", "wheel", "pkg_resources", "_distutils_hack")) {
  Get-ChildItem -Path $SitePackages -Directory -Filter "$Name*" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force
}
foreach ($Name in @("pip*.exe", "wheel*.exe")) {
  Get-ChildItem -Path (Join-Path $PythonDir "Scripts") -Filter $Name -ErrorAction SilentlyContinue |
    Remove-Item -Force
}
Get-ChildItem -Path $SitePackages -Directory | ForEach-Object {
  Get-ChildItem -Path $_.FullName -Recurse -Directory -Include "test", "tests" -ErrorAction SilentlyContinue
} | Sort-Object FullName -Descending | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem -Path $PythonDir -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
  Remove-Item -Recurse -Force

Set-Location (Join-Path $ProjectRoot "desktop")
npm ci
npm run test
npm run tauri build -- --no-bundle --config $VariantConfig
if ($LASTEXITCODE -ne 0) { throw "Tauri failed to build the desktop shell." }

Write-Host "Built desktop/src-tauri/target/release/process-studio-desktop.exe"
