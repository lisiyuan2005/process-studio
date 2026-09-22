# -*- mode: python ; coding: utf-8 -*-
"""Console worker bundled beside the Tauri shell.

The shell spawns this binary once per RPC call and talks JSON over stdio, so it
must stay a console executable on every platform.
"""

from PyInstaller.utils.hooks import collect_all
from pathlib import Path

project_root = Path(SPECPATH).parent

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
packages = ["gdstk", "openpyxl", "PIL", "certifi", "truststore", "shapely", "trimesh"]
# The registry imports the kernel module by name at start-up, which static
# analysis cannot see; the slab kernel reaches its GDS reader through
# deviceflow.__getattr__, which it also cannot see.
hiddenimports += ["process_studio.kernels.slab", "deviceflow", "deviceflow.layout"]
# The level-set kernel's solver and meshing went with it.
excludes += ["scipy", "skfmm", "skimage"]
for package in packages:
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

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
