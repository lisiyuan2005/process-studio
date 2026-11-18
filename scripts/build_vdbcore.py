"""Build the OpenVDB kernel's C++ core into the package.

    python scripts/build_vdbcore.py

Needs CMake, a C++17 compiler, OpenVDB and TBB (apt: libopenvdb-dev
libtbb-dev; Homebrew: openvdb; conda-forge: openvdb) and nanobind (pip).
The module lands in src/process_studio/kernels/openvdb/, where the kernel
imports it; without it the kernel reports itself unavailable and the slab
kernel works as before.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "cpp" / "vdbcore"
TARGET = ROOT / "src" / "process_studio" / "kernels" / "openvdb"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="vdbcore-build-") as build:
        configure = [
            "cmake", "-S", str(SOURCE), "-B", build,
            f"-DPython_EXECUTABLE={sys.executable}", "-DCMAKE_BUILD_TYPE=Release",
        ]
        subprocess.run(configure, check=True)
        subprocess.run(["cmake", "--build", build, "--config", "Release", "--parallel"], check=True)
        built = [
            path for path in Path(build).rglob("_vdbcore*")
            if path.suffix in {".so", ".pyd", ".dylib"} and path.is_file()
        ]
        if not built:
            print("the build produced no _vdbcore module", file=sys.stderr)
            return 1
        for old in TARGET.glob("_vdbcore*"):
            if old.suffix in {".so", ".pyd", ".dylib"}:
                old.unlink()
        shutil.copy2(built[0], TARGET / built[0].name)
        print(f"built {TARGET / built[0].name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
