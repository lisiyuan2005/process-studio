#!/usr/bin/env bash
# Build the Tauri desktop shell with its packaged Python worker (macOS/Linux).
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

python3 -m pip install -e ".[render]"
python3 -m pip install 'pyinstaller>=6.10'

# The shell spawns this binary for every RPC call, so it ships inside the bundle.
python3 -m PyInstaller \
  --noconfirm \
  --clean \
  --distpath desktop/src-tauri/resources/worker \
  --workpath work/pyinstaller-worker \
  packaging/ProcessStudioWorker.spec

WORKER="desktop/src-tauri/resources/worker/process-studio-worker"
test -x "$WORKER" || { echo "The packaged worker is missing at $WORKER"; exit 1; }

# Smoke-test the worker on its own before it is wrapped in an installer.
echo '{"kind":"request","id":1,"method":"describe"}' | "$WORKER" | grep -q '"protocolVersion"'

cd desktop
npm ci
npm run test
if [ "$(uname)" = "Darwin" ]; then
  npm run tauri build -- --bundles app
else
  npm run tauri build -- --no-bundle
fi

echo "Built desktop/src-tauri/target/release"
