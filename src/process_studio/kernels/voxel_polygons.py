"""The voxel state as polygons, for a 3D view drawn the way the slab kernel's is.

The cell mesh (:func:`voxel.build_meshes`) draws a face for every fine
cell side along a boundary, so its size grows with the fine grid and the
view has to draw a coarser copy of a fine state. Here each slab's
materials are turned into polygons instead: the exact outlines of the fine
cells and their cut lines (:func:`voxel_vector.field_loops`, what the top
view draws), simplified together so that two materials keep one shared
edge -- no cracks, no overlaps -- to within a small fraction of a fine
cell, and handed to the slab kernel's own mesh builder as a stack of
polygon slabs. A curve is then a few hundred points at any resolution, and
a flat face two triangles however large it is.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import numpy as np
import shapely
from shapely.geometry import MultiPolygon, Polygon

if TYPE_CHECKING:
    from .slab import SlabState
    from .voxel import VoxelState

#: How far a simplified outline may stray from the exact one, in fine
#: cells. The cut lines themselves are a quarter of a fine cell from the
#: shape they were fitted to.
TOLERANCE_CELLS = 0.1


def slab_faces(state: "VoxelState", k: int) -> tuple[np.ndarray, np.ndarray]:
    """Slab ``k`` cut into faces of one material each: (polygons, labels).

    Every material's outline (fine cells and cut lines, exact) goes into
    one set of lines, noded together, and the faces of that are read back
    at a point inside each. Faces made this way share their edges point
    for point -- which outlines drawn material by material do not, where
    the corner of one lands part-way along an edge of another with open
    space on both sides -- so they are a coverage the simplifier keeps
    stitched."""
    from . import voxel_vector
    from .voxel import VOID

    cells, refs = state.slab_refs(k)
    B = state.refine
    lab = state.pool[refs] if cells.size else np.zeros((0, B, B), np.uint8)
    cut = state.pool_cut[refs] if cells.size else np.zeros((0, B, B), np.uint16)
    found = voxel_vector.field_loops(
        state.labels[k], cells, lab, cut, state.bounds[0], state.bounds[1],
        0.5 * state.fine_x, 0.5 * state.fine_y,
    )
    rings = []
    for points, starts in found.values():
        ends = np.concatenate([starts[1:], [len(points)]])
        for a, b in zip(starts, ends):
            if b - a >= 3:
                rings.append(np.vstack([points[a:b], points[a : a + 1]]))
    if not rings:
        return np.zeros(0, dtype=object), np.zeros(0, np.int64)
    lines = shapely.linestrings(
        np.concatenate(rings), indices=np.repeat(np.arange(len(rings)), [len(r) for r in rings])
    )
    noded = shapely.get_parts(shapely.union_all(lines))
    faces = np.array(list(shapely.get_parts(shapely.polygonize(noded))), dtype=object)
    if faces.size == 0:
        return faces, np.zeros(0, np.int64)
    probe = shapely.get_coordinates(shapely.point_on_surface(faces))
    label = state.sample(k, probe[:, 0], probe[:, 1]).astype(np.int64)
    keep = label != VOID
    return faces[keep], label[keep]


def slab_regions(state: "VoxelState", k: int, tolerance: float) -> dict[int, MultiPolygon]:
    """Each material's region in slab ``k``, simplified to ``tolerance``
    together with its neighbours (a coverage simplification: an edge two
    materials share stays one edge)."""
    faces, label = slab_faces(state, k)
    if faces.size == 0:
        return {}
    if tolerance > 0.0:
        try:
            faces = shapely.coverage_simplify(faces, tolerance, simplify_boundary=True)
        except Exception:  # an older GEOS: the exact faces
            pass
    out: dict[int, MultiPolygon] = {}
    for m in np.unique(label):
        merged = shapely.union_all(faces[label == m])
        if not merged.is_valid:
            merged = shapely.make_valid(merged)
        parts = [p for p in shapely.get_parts(merged) if isinstance(p, Polygon) and not p.is_empty]
        if parts:
            out[int(m)] = MultiPolygon(parts)
    return out


def _slab_key(state: "VoxelState", k: int) -> bytes:
    """What slab ``k`` holds, to draw two slabs that hold the same once."""
    cells, refs = state.slab_refs(k)
    digest = hashlib.blake2b(digest_size=16)
    digest.update(np.ascontiguousarray(state.labels[k]).tobytes())
    digest.update(cells.tobytes())
    if cells.size:
        used = state.pool[refs]
        digest.update(used.tobytes())
        digest.update(state.pool_cut[refs].tobytes())
    return digest.digest()


def polygon_state(state: "VoxelState", should_cancel=None) -> "SlabState":
    """The state as a stack of polygon slabs the slab kernel can mesh,
    built once per state. Slabs that hold the same are drawn once, and the
    rest side by side on the machine's cores."""
    with state.mesh_lock:
        if state.polygons is not None:
            return state.polygons
    built = _build(state, should_cancel)
    with state.mesh_lock:
        if state.polygons is None:
            state.polygons = built
        return state.polygons


def _build(state: "VoxelState", should_cancel=None) -> "SlabState":
    import os
    from concurrent.futures import ThreadPoolExecutor

    from deviceflow import Device
    from deviceflow._internal.geometry.state import ProcessState, Slab

    from .slab import GEOMETRY_GRID_UM, SlabState

    x_min, y_min, x_max, y_max = state.bounds
    device = Device("voxel", (x_min, y_min, x_max, y_max), grid=GEOMETRY_GRID_UM, verbose=False)
    registry = device._materials
    for name in state.materials:
        if name in registry:
            continue
        try:
            device.material(name)
        except Exception:  # a name the material table does not know
            device.material(name, role="other")
    tolerance = TOLERANCE_CELLS * state.fine
    keys = [_slab_key(state, k) for k in range(state.n)]
    first: dict[bytes, int] = {}
    for k, key in enumerate(keys):
        first.setdefault(key, k)

    def one(k: int) -> dict[int, MultiPolygon]:
        if should_cancel is not None and should_cancel():
            raise RuntimeError("stopped")
        return slab_regions(state, k, tolerance)

    # numpy and GEOS let go of the interpreter for the heavy parts, so
    # threads share the work without copying the state to other processes
    workers = max(1, min(8, os.cpu_count() or 1, len(first)))
    with ThreadPoolExecutor(workers) as pool:
        drawn = dict(zip(first, pool.map(one, first.values())))
    slabs = []
    for k in range(state.n):
        regions = {registry.resolve(state.materials[m - 1]): geom for m, geom in drawn[keys[k]].items()}
        slabs.append(Slab(float(state.z[k]), float(state.z[k + 1]), regions))
    process = ProcessState((x_min, y_min, x_max, y_max), grid=GEOMETRY_GRID_UM)
    process._slabs = slabs
    device._state = process
    return SlabState(device, state.z_offset)
