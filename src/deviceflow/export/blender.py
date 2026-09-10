"""Blender is a renderer only: it receives a validated GLB and produces a
PNG and/or a .blend. The work is done by ``_blender_render.py`` inside a
headless Blender subprocess; this module prepares the job and checks the
report."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..exceptions import ProcessError

VIEWS = ("iso", "top", "front", "side")
SCRIPT = Path(__file__).with_name("_blender_render.py")


# Where Blender usually lives when it is not on PATH (IDE launched from the Dock,
# Windows installer, Linux tarball unpacked somewhere).
_CANDIDATES = [
    "/Applications/Blender.app/Contents/MacOS/Blender",
    "/Applications/Blender/Blender.app/Contents/MacOS/Blender",
    "~/Applications/Blender.app/Contents/MacOS/Blender",
    "/usr/local/bin/blender",
    "/opt/blender/blender",
    "/snap/bin/blender",
    "~/bin/blender",
    "~/.local/bin/blender",
    "C:/Program Files/Blender Foundation/Blender/blender.exe",
]


def find_blender(explicit=None) -> str:
    """Blender executable: explicit argument > $BLENDER > PATH > well-known locations."""
    candidates = []
    if explicit:
        candidates.append(str(explicit))
    if os.environ.get("BLENDER"):
        candidates.append(os.environ["BLENDER"])
    on_path = shutil.which("blender")
    if on_path:
        candidates.append(on_path)
    candidates.extend(_CANDIDATES)
    candidates.extend(str(p) for p in sorted(Path("C:/Program Files/Blender Foundation").glob("Blender*/blender.exe"), reverse=True))
    for c in candidates:
        p = Path(c).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    raise ProcessError(
        "blender executable not found; install Blender, put it on PATH, set BLENDER=/path/to/blender, "
        "or pass blender=... to export_blender()"
    )


def blender_available() -> bool:
    try:
        find_blender()
        return True
    except ProcessError:
        return False


def render_glb(
    glb: Path,
    *,
    blend=None,
    png=None,
    view: str = "iso",
    samples: int = 32,
    resolution=(1200, 900),
    hide=(),
    transparent=None,
    background=(0.85, 0.85, 0.85),
    film_transparent: bool = False,
    hdri=None,
    outline: bool = False,
    debug_normals: bool = False,
    view_transform: str = "Khronos PBR Neutral",
    exposure: float = 0.0,
    lights: dict | None = None,
    blender=None,
    timeout: float = 600,
) -> dict:
    if blend is None and png is None:
        raise ProcessError("export_blender needs blend= and/or png=")
    if view not in VIEWS:
        raise ProcessError(f"unknown view {view!r}; expected one of {VIEWS}")
    if samples < 1:
        raise ProcessError("samples must be >= 1")
    exe = find_blender(blender)
    for p in (blend, png):
        if p is not None:
            Path(p).parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="deviceflow_blender_") as tmp:
        job_path = Path(tmp) / "job.json"
        report_path = Path(tmp) / "report.json"
        job = {
            "glb": str(Path(glb).resolve()),
            "blend": str(Path(blend).resolve()) if blend else None,
            "png": str(Path(png).resolve()) if png else None,
            "report": str(report_path),
            "view": view,
            "samples": int(samples),
            "resolution": [int(resolution[0]), int(resolution[1])],
            "hide": list(hide),
            "transparent": {k: float(v) for k, v in (transparent or {}).items()},
            "background": list(background),
            "film_transparent": bool(film_transparent),
            "hdri": str(Path(hdri).resolve()) if hdri else None,
            "outline": bool(outline),
            "debug_normals": bool(debug_normals),
            "view_transform": view_transform,
            "exposure": float(exposure),
            **{k: float(v) for k, v in (lights or {}).items() if k in ("key", "fill", "headlight", "ambient", "hdri_strength", "outline_strength")},
        }
        if hdri and not Path(hdri).is_file():
            raise ProcessError(f"HDRI file not found: {hdri}")
        job_path.write_text(json.dumps(job))
        proc = subprocess.run(
            [exe, "-b", "--python", str(SCRIPT), "--", str(job_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if proc.returncode != 0 or not report_path.exists():
            out_lines = (proc.stdout or "").splitlines()
            err_lines = (proc.stderr or "").splitlines()
            tail = "\n".join(out_lines[-20:] + err_lines[-20:])
            raise ProcessError(f"Blender render failed (exit {proc.returncode}):\n{tail}")
        report = json.loads(report_path.read_text())
    for name, info in report["objects"].items():
        if info["modifiers"]:
            raise ProcessError(f"Blender object {name} has modifiers {info['modifiers']}; renderer must not modify geometry")
    report["blend"] = str(blend) if blend else None
    report["png"] = str(png) if png else None
    return report
