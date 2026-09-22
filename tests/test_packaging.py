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
import sys

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
