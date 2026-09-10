$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

python -m pip install -e .
python -m pip install "pyinstaller>=6.10"
python -m PyInstaller `
  --noconfirm `
  --clean `
  --distpath release/windows `
  --workpath work/pyinstaller-windows `
  packaging/ProcessStudio.windows.spec
if ($LASTEXITCODE -ne 0) {
  throw "PyInstaller failed to build ProcessStudio.exe."
}

& release/windows/ProcessStudio.exe `
  --smoke-test `
  --workspace work/exe-smoke

if ($LASTEXITCODE -ne 0) {
  throw "Packaged ProcessStudio.exe failed its smoke test."
}

Write-Host "Built release/windows/ProcessStudio.exe"
