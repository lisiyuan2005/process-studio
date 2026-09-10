"""Per-material mesh validation (strict).

Checks: finite coordinates, every edge in exactly two faces (watertight),
consistent winding, every vertex has a single face fan (vertex-manifold),
positive volume equal to the process volume, no duplicate/degenerate faces,
no unreferenced vertices, no self-intersections between non-adjacent
triangles, every connected component positively oriented, bounds inside the
device window and z range.
"""

from __future__ import annotations

import math
from collections import defaultdict

import numpy as np
import trimesh

from ...exceptions import MeshError
from ...reports import MaterialMeshReport

VOLUME_ABS_TOL = 1e-9
VOLUME_REL_TOL = 1e-9


def validate_material_mesh(
    name: str,
    mesh: trimesh.Trimesh,
    process_volume: float,
    grid: float = 1e-6,
    bounds=None,
    z_range=None,
) -> MaterialMeshReport:
    errors: list[str] = []
    V = np.asarray(mesh.vertices, dtype=np.float64)
    F = np.asarray(mesh.faces, dtype=np.int64)
    finite = bool(np.isfinite(V).all())
    if not finite:
        errors.append("non-finite vertex coordinates")
    watertight = bool(mesh.is_watertight)
    if not watertight:
        errors.append("not watertight (an edge is not shared by exactly two faces)")
    winding = bool(mesh.is_winding_consistent)
    if not winding:
        errors.append("inconsistent winding")
    vertex_manifold = _vertex_manifold(F) if watertight else False
    if watertight and not vertex_manifold:
        errors.append("not vertex-manifold (a vertex has more than one face fan)")
    volume = float(mesh.volume) if finite else math.nan
    if not volume > 0:
        errors.append(f"non-positive volume {volume}")
    tol = VOLUME_ABS_TOL + VOLUME_REL_TOL * abs(process_volume)
    if not abs(volume - process_volume) <= tol:
        errors.append(f"mesh volume {volume!r} != process volume {process_volume!r}")
    unique = mesh.unique_faces()
    duplicate_faces = int(len(F) - int(np.count_nonzero(unique)))
    if duplicate_faces:
        errors.append(f"{duplicate_faces} duplicate faces")
    # degenerate = thinner than the geometry grid: the smallest legitimate
    # triangle on the grid has area grid^2 / 2 and edges of one grid step
    tri = mesh.triangles
    edge_len = np.linalg.norm(tri - np.roll(tri, -1, axis=1), axis=2)
    degenerate = (mesh.area_faces < 0.25 * grid * grid) | (edge_len.min(axis=1) < 0.5 * grid)
    degenerate_faces = int(np.count_nonzero(degenerate))
    if degenerate_faces:
        errors.append(f"{degenerate_faces} zero-area faces (area below grid^2/4 or an edge shorter than grid/2)")
    unreferenced = int(len(V) - len(np.unique(F)))
    if unreferenced:
        errors.append(f"{unreferenced} unreferenced vertices")
    components = _component_volumes(mesh) if watertight and winding else []
    if any(v <= 0 for v in components):
        errors.append("a connected component has non-positive volume (inverted shell)")
    self_intersections = _self_intersections(V, F) if finite else -1
    if self_intersections:
        errors.append(f"{self_intersections} pairs of non-adjacent triangles intersect")
    if bounds is not None and finite and len(V):
        x0, y0, x1, y1 = bounds
        eps = grid
        if V[:, 0].min() < x0 - eps or V[:, 0].max() > x1 + eps or V[:, 1].min() < y0 - eps or V[:, 1].max() > y1 + eps:
            errors.append("mesh leaves the device bounds")
    if z_range is not None and finite and len(V):
        zlo, zhi = z_range
        if V[:, 2].min() < zlo - 1e-9 or V[:, 2].max() > zhi + 1e-9:
            errors.append("mesh leaves the device z range (bounds)")
    return MaterialMeshReport(
        name=name,
        watertight=watertight,
        winding_consistent=winding,
        vertex_manifold=vertex_manifold,
        volume=volume,
        process_volume=process_volume,
        vertices=int(len(V)),
        faces=int(len(F)),
        euler_number=int(mesh.euler_number),
        duplicate_faces=duplicate_faces,
        degenerate_faces=degenerate_faces,
        self_intersections=int(self_intersections),
        components=len(components),
        errors=tuple(errors),
    )


def _vertex_manifold(F: np.ndarray) -> bool:
    """Every vertex's incident faces form one fan connected through shared edges."""
    edge_faces: dict[tuple[int, int], list[int]] = defaultdict(list)
    vertex_faces: dict[int, list[int]] = defaultdict(list)
    for fi, (a, b, c) in enumerate(F):
        for u, v in ((a, b), (b, c), (c, a)):
            edge_faces[(u, v) if u < v else (v, u)].append(fi)
        for v in (a, b, c):
            vertex_faces[int(v)].append(fi)
    for p, fs in vertex_faces.items():
        parent = {fi: fi for fi in fs}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for fi in fs:
            for q in F[fi]:
                q = int(q)
                if q == p:
                    continue
                e = (p, q) if p < q else (q, p)
                fl = edge_faces[e]
                if len(fl) == 2:
                    ra, rb = find(fl[0]), find(fl[1])
                    if ra != rb:
                        parent[ra] = rb
        if len({find(fi) for fi in fs}) != 1:
            return False
    return True


def _component_volumes(mesh: trimesh.Trimesh) -> list[float]:
    try:
        parts = mesh.split(only_watertight=False)
    except Exception:  # pragma: no cover - split needs scipy/networkx
        return [float(mesh.volume)]
    return [float(p.volume) for p in parts]


#: Candidate pairs tested per batch. Bounds peak memory: a batch allocates a
#: handful of (chunk, 3) float arrays, never a matrix over all triangle pairs.
_NARROW_CHUNK = 200_000


def _self_intersections(V: np.ndarray, F: np.ndarray, limit: int = 10_000_000) -> int:
    """Count pairs of triangles that share no vertex yet intersect.

    Broad phase: axis-aligned bounding boxes, swept on x with the y and z
    overlap tests applied to the whole active set at once. Narrow phase: each
    edge of one triangle against the other triangle, both ways, evaluated in
    batches so that the per-pair cost is array work rather than interpreter
    work. Neither phase allocates anything proportional to the square of the
    triangle count.

    ``limit`` caps the candidate pairs examined. Reaching it means the answer
    is a lower bound rather than a count, which the caller must not read as a
    clean result.
    """
    n = len(F)
    if n < 2:
        return 0
    # Vertices duplicated on purpose (pinch sectors, seams) have equal
    # coordinates: treat coordinate-equal vertices as shared, like indices.
    _, key = np.unique(V, axis=0, return_inverse=True)
    key = key.reshape(-1)
    FK = np.sort(key[F], axis=1)
    tri = V[F]  # (n, 3, 3)
    lo = tri.min(axis=1)
    hi = tri.max(axis=1)
    eps = 1e-12
    order = np.argsort(lo[:, 0], kind="stable")

    left = np.empty(0, dtype=np.intp)
    pending_a: list[np.ndarray] = []
    pending_b: list[np.ndarray] = []
    pending_size = 0
    count = 0
    checked = 0
    truncated = False

    for position, index in enumerate(order):
        x0 = lo[index, 0]
        if left.size:
            # Drop the triangles the sweep has passed, then test the rest as
            # one array instead of one Python iteration per member.
            left = left[hi[left, 0] >= x0 - eps]
        if left.size:
            overlap = (
                (hi[index, 1] >= lo[left, 1] - eps)
                & (lo[index, 1] <= hi[left, 1] + eps)
                & (hi[index, 2] >= lo[left, 2] - eps)
                & (lo[index, 2] <= hi[left, 2] + eps)
            )
            candidates = left[overlap]
            if candidates.size:
                # Triangles that share a vertex are adjacent by construction.
                mine = FK[index]
                theirs = FK[candidates]
                shares = (
                    (theirs == mine[0]) | (theirs == mine[1]) | (theirs == mine[2])
                ).any(axis=1)
                candidates = candidates[~shares]
            if candidates.size:
                checked += int(candidates.size)
                if checked > limit:
                    truncated = True
                    break
                pending_a.append(np.full(candidates.size, index, dtype=np.intp))
                pending_b.append(candidates)
                pending_size += int(candidates.size)
                if pending_size >= _NARROW_CHUNK:
                    count += _count_intersecting(
                        tri[np.concatenate(pending_a)], tri[np.concatenate(pending_b)], eps
                    )
                    pending_a, pending_b, pending_size = [], [], 0
        left = np.append(left, index)

    if pending_size:
        count += _count_intersecting(
            tri[np.concatenate(pending_a)], tri[np.concatenate(pending_b)], eps
        )
    if truncated:
        raise MeshError(
            f"self-intersection check stopped after {limit} candidate pairs; "
            f"{count} intersecting pairs found so far. The result is a lower bound, "
            "not a clean mesh: simplify the structure or raise the limit."
        )
    return count


def _count_intersecting(a: np.ndarray, b: np.ndarray, eps: float) -> int:
    """How many of the given triangle pairs intersect, evaluated as arrays.

    Same predicate as the scalar test, in the same order of alternatives: the
    three edges of each triangle against the other triangle, both ways.
    """
    if not len(a):
        return 0
    hit = np.zeros(len(a), dtype=bool)
    for first, second in ((a, b), (b, a)):
        for corner in range(3):
            remaining = ~hit
            if not remaining.any():
                break
            hit[remaining] |= _segments_hit_triangles(
                first[remaining, corner],
                first[remaining, (corner + 1) % 3],
                second[remaining],
                eps,
            )
    return int(np.count_nonzero(hit))


def _segments_hit_triangles(p: np.ndarray, q: np.ndarray, tri: np.ndarray, eps: float) -> np.ndarray:
    """Moeller-Trumbore for finite segments, one per row.

    Coplanar segments count as a miss, exactly as in the scalar version:
    coplanar overlap between non-adjacent faces is caught by the other
    triangle's edges crossing this one's plane, or is a shared-plane touch.
    """
    direction = q - p
    edge1 = tri[:, 1] - tri[:, 0]
    edge2 = tri[:, 2] - tri[:, 0]
    h = np.cross(direction, edge2)
    det = np.einsum("ij,ij->i", edge1, h)
    alive = np.abs(det) >= eps
    if not alive.any():
        return np.zeros(len(p), dtype=bool)
    inverse = np.zeros(len(p))
    np.divide(1.0, det, out=inverse, where=alive)
    offset = p - tri[:, 0]
    u = inverse * np.einsum("ij,ij->i", offset, h)
    alive &= (u >= -eps) & (u <= 1 + eps)
    if not alive.any():
        return np.zeros(len(p), dtype=bool)
    qv = np.cross(offset, edge1)
    v = inverse * np.einsum("ij,ij->i", direction, qv)
    alive &= (v >= -eps) & (u + v <= 1 + eps)
    if not alive.any():
        return np.zeros(len(p), dtype=bool)
    t = inverse * np.einsum("ij,ij->i", edge2, qv)
    # The triangles share no vertex, so any contact of this edge with the
    # triangle, its border included, is an intersection of non-adjacent faces.
    return alive & (t >= -eps) & (t <= 1 + eps)


def _triangles_intersect(a: np.ndarray, b: np.ndarray, eps: float = 1e-12) -> bool:
    for t1, t2 in ((a, b), (b, a)):
        for i in range(3):
            if _segment_hits_triangle(t1[i], t1[(i + 1) % 3], t2, eps):
                return True
    return False


def _segment_hits_triangle(p, q, tri, eps) -> bool:
    """Moeller-Trumbore for a finite segment; coplanar segments count as miss
    (coplanar overlap between non-adjacent faces is caught via the other
    triangle's edges crossing this one's plane, or is a shared-plane touch)."""
    d = q - p
    e1 = tri[1] - tri[0]
    e2 = tri[2] - tri[0]
    h = np.cross(d, e2)
    det = e1 @ h
    if abs(det) < eps:
        return False
    inv = 1.0 / det
    s = p - tri[0]
    u = inv * (s @ h)
    if u < -eps or u > 1 + eps:
        return False
    qv = np.cross(s, e1)
    v = inv * (d @ qv)
    if v < -eps or u + v > 1 + eps:
        return False
    t = inv * (e2 @ qv)
    # The triangles share no vertex, so any contact of this edge with the
    # triangle (including on its border) is an intersection of non-adjacent faces.
    return (-eps <= t <= 1 + eps) and (u >= -eps) and (v >= -eps) and (u + v <= 1 + eps)
