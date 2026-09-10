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
hiddenimports = [
    "process_studio.worker",
    "process_studio.worker.protocol",
    # The slab kernel reaches its GDS reader through deviceflow.__getattr__.
    "deviceflow.layout",
]
for package in ("scipy", "skfmm", "gdstk", "openpyxl", "PIL", "skimage", "shapely", "trimesh"):
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
    excludes=["pytest", "tkinter", "matplotlib"],
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
