#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

python3 -m pip install -e .
python3 -m pip install 'pyinstaller>=6.10'
python3 -m PyInstaller \
  --noconfirm \
  --clean \
  --distpath release/macos \
  --workpath work/pyinstaller-macos \
  packaging/ProcessStudio.macos.spec

release/macos/ProcessStudio.app/Contents/MacOS/ProcessStudio \
  --smoke-test \
  --workspace work/app-smoke

ditto -c -k --sequesterRsrc --keepParent \
  release/macos/ProcessStudio.app \
  release/macos/ProcessStudio-macOS.zip

echo "Built release/macos/ProcessStudio-macOS.zip"
