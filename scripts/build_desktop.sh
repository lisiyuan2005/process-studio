#!/usr/bin/env bash
# Build the Tauri desktop shell with its packaged Python worker (macOS/Linux).
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

# Which kernels this build ships. PROCESS_STUDIO_KERNELS is what the worker
# reads (a comma-separated list of ids, unset for both); the variant names the
# product so two builds can sit side by side on one machine.
KERNELS="${PROCESS_STUDIO_KERNELS:-levelset,slab}"
export PROCESS_STUDIO_KERNELS="$KERNELS"
case "$KERNELS" in
  slab)     PRODUCT="Process Studio Slab";      IDENTIFIER="com.processstudio.desktop.slab" ;;
  levelset) PRODUCT="Process Studio Level Set"; IDENTIFIER="com.processstudio.desktop.levelset" ;;
  *)        PRODUCT="Process Studio";           IDENTIFIER="com.processstudio.desktop" ;;
esac
VARIANT_CONFIG="$PROJECT_ROOT/work/tauri-variant.json"
mkdir -p "$PROJECT_ROOT/work"
cat > "$VARIANT_CONFIG" <<JSON
{
  "productName": "$PRODUCT",
  "identifier": "$IDENTIFIER",
  "app": { "windows": [ { "title": "$PRODUCT", "width": 1440, "height": 900, "minWidth": 960, "minHeight": 640, "resizable": true, "fullscreen": false, "center": true } ] }
}
JSON
echo "Building $PRODUCT with kernels: $KERNELS"

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

# Smoke-test the worker on its own before it is wrapped in an installer. The
# kernels are checked by name: a worker that lost one of them still answers
# describe, and the shell would simply stop offering that kernel.
DESCRIBED="$(echo '{"kind":"request","id":1,"method":"describe"}' | "$WORKER")"
echo "$DESCRIBED" | grep -q '"protocolVersion"'
for kernel in $(echo "$KERNELS" | tr ',' ' '); do
  echo "$DESCRIBED" | tr -d ' ' | grep -q "\"id\":\"$kernel\"" || {
    echo "The packaged worker does not offer the $kernel kernel"; exit 1; }
done

cd desktop
npm ci
npm run test
if [ "$(uname)" = "Darwin" ]; then
  # The disk image copies the bundle byte for byte, so the signature survives
  # the trip to another machine. A .app that travels as a plain archive can
  # lose symlinks or permissions, and any such change makes macOS call it
  # damaged instead of merely unidentified.
  npm run tauri build -- --bundles app,dmg --config "$VARIANT_CONFIG"
else
  npm run tauri build -- --no-bundle --config "$VARIANT_CONFIG"
fi

echo "Built desktop/src-tauri/target/release"
