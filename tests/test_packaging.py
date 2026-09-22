"""The packaged worker installs numpy, pillow, gdstk, openpyxl, pyyaml,
shapely and trimesh -- and never scipy, scikit-image or scikit-fmm, which
were the level-set kernel's own solver and meshing and went with it. The
worker's module-level imports must not quietly grow a dependency on any of
them, or the package stops merely importing.

This does not literally uninstall anything (that would need a whole
separate environment); it makes further imports of those packages fail the
way an absence would, then re-imports every module the worker loads on
start-up fresh, from scratch, so any module-level ``import scipy`` surfaces
here instead of only on a real machine.
"""

from __future__ import annotations

import builtins
import importlib
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

#: Every module process_studio.worker.protocol pulls in transitively at
#: import time, in registry order. If any of these gained a hard
#: (module-level, unguarded) scipy or scikit-image import, reimporting it
#: fresh below raises before this list even finishes.
_STARTUP_MODULES = [
    "process_studio.grid",
    "process_studio.storage",
    "process_studio.worker.render",
    "process_studio.worker.export",
    "process_studio.worker.runner",
    "process_studio.worker.files",
    "process_studio.worker.protocol",
    "process_studio.kernels",
    "process_studio.kernels.slab",
    "process_studio.cli",
]


@pytest.fixture()
def forbid_the_retired_packages(monkeypatch):
    """Make `import scipy` / `import skimage` fail like they are not installed."""
    blocked = {"scipy", "skimage", "scikit_fmm", "skfmm"}
    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        root = name.split(".")[0]
        if root in blocked:
            raise ImportError(f"No module named {root!r} (blocked for this test)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    # Every already-imported module that reached scipy/skimage stays cached
    # in sys.modules from earlier tests; drop process_studio's own modules
    # so the imports below actually re-run their top level, not a cache hit.
    for name in list(sys.modules):
        if name == "process_studio" or name.startswith("process_studio."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    yield
    for name in list(sys.modules):
        if name == "process_studio" or name.startswith("process_studio."):
            monkeypatch.delitem(sys.modules, name, raising=False)


def test_the_startup_path_never_needs_scipy_or_scikit_image(
    forbid_the_retired_packages,
):
    for name in _STARTUP_MODULES:
        importlib.import_module(name)

    from process_studio.kernels import configure

    configure({"slab"})
    try:
        from process_studio.worker.protocol import dispatch

        response = dispatch({"kind": "request", "id": 1, "method": "describe"}, None)
    finally:
        configure(None)
    kernels = {kernel["id"] for kernel in response["kernels"]}
    assert kernels == {"slab"}
    assert response["kernels"][0]["surfaces"] is True


def test_writing_a_mesh_file_does_not_need_scipy(tmp_path):
    """The export path, in a process that never had scipy to begin with.

    trimesh decides at import time whether scipy is there; a process that
    already imported it holds the real module, so blocking the import later
    proves nothing. This runs a real export in a fresh interpreter with a
    scipy that refuses to import -- what the packaged worker is -- because
    the mesh export is the one place trimesh reached for it (colouring the
    faces made it build a sparse face-to-vertex matrix).
    """
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "scipy.py").write_text('raise ImportError("no scipy in this build")\n')
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(stub)!r})
        import trimesh
        assert not trimesh.exceptions or True
        from process_studio.defaults import default_grid, default_materials
        from process_studio.kernels import get_kernel
        from process_studio.models import ProjectDefinition
        from process_studio.worker.export import write_mesh
        from process_studio.worker.serialize import grid_dict
        from pathlib import Path

        kernel = get_kernel("slab")
        project = ProjectDefinition("p", grid_dict(default_grid()), kernel="slab", resolution_um=0.02)
        materials = default_materials()
        state = kernel.initial_state(project, materials=materials)
        written = write_mesh(
            kernel, state, project, {{"Si": "#8a6f4a"}}, Path({str(tmp_path / "wafer.glb")!r})
        )
        print(written["triangles"])
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert (tmp_path / "wafer.glb").stat().st_size > 0
