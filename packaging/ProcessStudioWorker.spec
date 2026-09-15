# -*- mode: python ; coding: utf-8 -*-
"""Console worker bundled beside the Tauri shell.

The shell spawns this binary once per RPC call and talks JSON over stdio, so it
must stay a console executable on every platform.
"""

import os
from PyInstaller.utils.hooks import collect_all
from pathlib import Path

project_root = Path(SPECPATH).parent

# PROCESS_STUDIO_KERNELS names the kernels this worker ships (comma-separated
# ids); unset means both. The choice is baked in as a data file the registry
# reads, and the other kernel's own packages are left out of the bundle.
kernels = {item.strip() for item in os.environ.get("PROCESS_STUDIO_KERNELS", "").split(",") if item.strip()}
if not kernels:
    kernels = {"levelset", "slab"}

datas = []
binaries = []
hiddenimports = ["process_studio.worker", "process_studio.worker.protocol", "process_studio.cli", "process_studio.cli.flowfile", "process_studio.cli.session", "certifi", "truststore"]
# The CLI reads YAML flow files when pyyaml is installed; ship it if it is.
try:
    import yaml  # noqa: F401
except ImportError:
    pass
else:
    hiddenimports.append("yaml")
excludes = ["pytest", "tkinter", "matplotlib"]
# certifi and truststore are how the update check knows which certificate
# authorities to trust. Both are reached through a guarded import inside a
# function, and certifi's whole point is a data file (cacert.pem) that a
# frozen build only carries if something collects it -- so they are named
# here rather than left to static analysis. Without them the check dies
# with "unable to get local issuer certificate" on every machine.
packages = ["scipy", "skfmm", "gdstk", "openpyxl", "PIL", "certifi", "truststore"]
# The registry imports kernel modules by name at start-up, which static
# analysis cannot see, so each enabled kernel is named here.
if "levelset" in kernels:
    packages.append("skimage")
    hiddenimports.append("process_studio.kernels.levelset")
else:
    excludes.append("skimage")
if "slab" in kernels:
    packages += ["shapely", "trimesh"]
    # The slab kernel reaches its GDS reader through deviceflow.__getattr__.
    hiddenimports += ["process_studio.kernels.slab", "deviceflow", "deviceflow.layout"]
else:
    excludes += ["deviceflow", "shapely", "trimesh"]
for package in packages:
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

enabled_file = Path(workpath) / "enabled.txt"
enabled_file.parent.mkdir(parents=True, exist_ok=True)
enabled_file.write_text(",".join(sorted(kernels)) + "\n", encoding="utf-8")
datas.append((str(enabled_file), "process_studio/kernels"))

analysis = Analysis(
    [str(project_root / "src" / "process_studio" / "worker" / "run_worker.py")],
    pathex=[str(project_root / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="process-studio-worker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
