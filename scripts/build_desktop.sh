#!/usr/bin/env bash
# Build the Tauri desktop shell with its packaged Python worker (macOS/Linux).
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

PRODUCT="Process Studio"
IDENTIFIER="com.processstudio.desktop"
VARIANT_CONFIG="$PROJECT_ROOT/work/tauri-variant.json"
mkdir -p "$PROJECT_ROOT/work"
cat > "$VARIANT_CONFIG" <<JSON
{
  "productName": "$PRODUCT",
  "identifier": "$IDENTIFIER",
  "app": { "windows": [ { "title": "$PRODUCT", "width": 1440, "height": 900, "minWidth": 720, "minHeight": 600, "resizable": true, "fullscreen": false, "center": true } ] }
}
JSON
echo "Building $PRODUCT"

# The build's Python lives in its own virtual environment: a Homebrew or
# Debian python3 refuses to install packages into itself (PEP 668), and a
# build that pollutes the system interpreter is a bad neighbour anyway. An
# environment that is already active (CI, or one the user made) is used as
# it is; PROCESS_STUDIO_VENV points the script at another directory.
if [ -z "${VIRTUAL_ENV:-}" ]; then
  VENV="${PROCESS_STUDIO_VENV:-$PROJECT_ROOT/work/venv}"
  if [ ! -x "$VENV/bin/python" ]; then
    echo "Creating the build's virtual environment at $VENV"
    python3 -m venv "$VENV"
  fi
  # shellcheck disable=SC1091
  . "$VENV/bin/activate"
fi
echo "Using Python $(python3 -c 'import sys; print(sys.version.split()[0], sys.executable)')"

python3 -m pip install --upgrade pip >/dev/null
python3 -m pip install -e .
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
# kernel is checked by name: a worker that lost it still answers describe,
# and the shell would simply have nothing to run a project on.
DESCRIBED="$(echo '{"kind":"request","id":1,"method":"describe"}' | "$WORKER")"
echo "$DESCRIBED" | grep -q '"protocolVersion"'
echo "$DESCRIBED" | tr -d ' ' | grep -q '"id":"slab"' || {
  echo "The packaged worker does not offer the slab kernel"; exit 1; }

# The update check is the one thing that needs certificate authorities, and a
# frozen build has none of its own unless certifi and truststore were
# collected. It shipped broken once, so a build that cannot verify a
# certificate fails here. Both halves are needed: a successful check carries
# the release notes, which carry commit subjects, one of which is about
# certificates.
UPDATE="$(echo '{"kind":"request","id":2,"method":"check_update"}' | "$WORKER" || true)"
case "$UPDATE" in
  *'"ok":false'*certificate*|*certificate*'"ok":false'*)
    echo "The packaged worker cannot verify certificates: $UPDATE"; exit 1 ;;
esac

# The 3D view's mesh is built on several cores, and a frozen binary makes a
# child process by starting *itself* -- so this is the one thing that can be
# right in the source tree and lost in the packaging. It only costs speed, so
# a one-core machine is not a failure; a machine with cores that cannot use
# them is.
CORES="$("$WORKER" --json cores || true)"
echo "Mesh pool: $CORES"
REPORT="$(echo "$CORES" | tr -d ' \n')"
case "$REPORT" in
  *'"workers":1,'*) : ;;                       # nothing to spread over anyway
  *'"pool":false'*) echo "The packaged worker cannot build meshes on more than one core"; exit 1 ;;
esac
# And it must be able to *build* a mesh in one of those children, which is
# the part that can be right in the source tree and lost in the packaging.
case "$REPORT" in
  *'"warmed":false'*)
    echo "The packaged worker's children cannot build a mesh: $CORES"; exit 1 ;;
esac

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
