"""Write one version into every file that carries it.

    python scripts/set_version.py 0.1.3            # a stable release
    python scripts/set_version.py 0.1.4-beta.2     # a beta
    python scripts/set_version.py 0.1.3.1          # a fix release of 0.1.3

The release workflow runs this before building, from the tag it was given,
so a beta and the stable release it is promoted to are built from the same
commit and differ only in the version they carry.

The application's own version -- what the update check compares, what the
home page shows -- is the Python one and keeps every part. The Rust
crate and the Tauri bundle get the bare "0.1.4": the Windows installer
wants three plain numbers, so a fourth ("0.1.3.1") is left out there, as it
is from package.json, which wants semver. The Python package gets the PEP
440 spelling ("0.1.4b2", "0.1.3.1").
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?(?:-beta\.(\d+))?$")


def _replace(path: Path, pattern: str, value: str, count: int = 1) -> None:
    text = path.read_text(encoding="utf-8")
    new, found = re.subn(pattern, lambda match: match.group(1) + value + match.group(2), text, count=count)
    if found != count:
        raise SystemExit(f"{path.relative_to(ROOT)}: expected {count} version(s), found {found}")
    path.write_text(new, encoding="utf-8")


def set_version(version: str) -> None:
    match = VERSION.match(version)
    if not match:
        raise SystemExit(f"not a version: {version!r} (want 0.1.3, 0.1.3.1 or 0.1.3-beta.2)")
    base = ".".join(match.group(i) for i in (1, 2, 3))
    full = base + (f".{match.group(4)}" if match.group(4) else "")
    beta = match.group(5)
    python = full + (f"b{beta}" if beta else "")
    semver = base + (f"-beta.{beta}" if beta else "")
    any_version = r'[0-9][0-9A-Za-z.\-]*'
    _replace(ROOT / "pyproject.toml", rf'(\nversion = "){any_version}(")', python)
    for module in ("src/process_studio/__init__.py", "src/process_studio/worker/__init__.py"):
        _replace(ROOT / module, rf'(__version__ = "){any_version}(")', version)
    _replace(ROOT / "desktop/package.json", rf'(\n  "version": "){any_version}(")', semver)
    # the lock file names the package twice: at the top and as packages[""]
    _replace(
        ROOT / "desktop/package-lock.json",
        rf'("name": "process-studio-desktop",\s+"version": "){any_version}(")',
        semver,
        count=2,
    )
    _replace(ROOT / "desktop/src-tauri/tauri.conf.json", rf'(\n  "version": "){any_version}(")', base)
    _replace(ROOT / "desktop/src-tauri/Cargo.toml", rf'(name = "process-studio-desktop"\nversion = "){any_version}(")', base)
    _replace(ROOT / "desktop/src-tauri/Cargo.lock", rf'(name = "process-studio-desktop"\nversion = "){any_version}(")', base)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    set_version(sys.argv[1].removeprefix("v"))
    print(f"version {sys.argv[1].removeprefix('v')}")
