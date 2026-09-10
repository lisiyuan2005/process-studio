# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all
from pathlib import Path

project_root = Path(SPECPATH).parent

datas = []
binaries = []
hiddenimports = []
for package in ("matplotlib", "scipy", "skfmm", "gdstk", "openpyxl", "PIL"):
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

analysis = Analysis(
    [str(project_root / "src" / "process_studio" / "__main__.py")],
    pathex=[str(project_root / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest"],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="ProcessStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
collection = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="ProcessStudio",
)
app = BUNDLE(
    collection,
    name="ProcessStudio.app",
    icon=None,
    bundle_identifier="org.processstudio.app",
    info_plist={
        "CFBundleDisplayName": "Process Studio",
        "CFBundleShortVersionString": "0.5.0",
        "NSHighResolutionCapable": True,
    },
)
