"""MeshBuilder: ProcessState -> one triangle mesh per material.

No process Boolean happens here; the builder only converts known material
boundaries into surfaces.

For one material M the algorithm is:

1. *Master linework*: node the boundaries of M's regions from every slab
   together (``unary_union`` of the rings). Every vertex that any cap or wall
   of M will ever use is a node of this linework, so shared edges between
   caps, walls and neighbouring slabs are split identically everywhere —
   this is what makes the result watertight without any welding tolerance.
2. *Atomic faces*: ``polygonize`` the master linework. Each atomic face is
   entirely inside or outside M's region in every slab.
3. *Caps*: at every Z plane, an atomic face becomes an up-facing cap where
   M is present below but not above, a down-facing cap where M is present
   above but not below, and nothing where M continues through the plane.
4. *Walls*: each ring edge of each slab's region is split at the master
   nodes lying on it and extruded z0 -> z1. Rings are oriented (exterior
   CCW, holes CW) so the solid is always on the left of an edge and the
   outward normal follows from the vertex order.

Vertices are welded by exact (x, y, z) key; no tolerance is involved.
"""

from __future__ import annotations

from collections import Counter, OrderedDict, defaultdict

import numpy as np
import shapely
import trimesh
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.polygon import orient

from ...exceptions import MeshError
from ...material import Material
from ..geometry import polygons as P
from ..geometry.state import ProcessState
from .loft import chain_rings, match_rings, zipper
from .manifold import split_nonmanifold
from .triangulate import triangulate

XYZ = tuple[float, float, float]


class _MeshAccumulator:
    def __init__(self) -> None:
        self._index: dict[XYZ, int] = {}
        self.vertices: list[XYZ] = []
        self.faces: list[tuple[int, int, int]] = []
        self.interface: list[bool] = []  # True: face touches another material

    def vertex(self, x: float, y: float, z: float) -> int:
        key = (x, y, z)
        i = self._index.get(key)
        if i is None:
            i = len(self.vertices)
            self._index[key] = i
            self.vertices.append(key)
        return i

    def triangle(self, a: XYZ, b: XYZ, c: XYZ, interface: bool = False) -> None:
        self.faces.append((self.vertex(*a), self.vertex(*b), self.vertex(*c)))
        self.interface.append(bool(interface))

    def to_trimesh(self) -> trimesh.Trimesh:
        return trimesh.Trimesh(
            vertices=np.asarray(self.vertices, dtype=np.float64),
            faces=np.asarray(self.faces, dtype=np.int64),
            process=False,
        )


def build_material_meshes(
    state: ProcessState,
    *,
    manifold: bool = True,
    loft: float | None = None,
    loft_reach: float | None = None,
) -> "OrderedDict[Material, trimesh.Trimesh]":
    """One mesh per material that owns any volume, in first-appearance order.

    ``manifold=False`` skips the vertex splitting that makes every vertex a
    single fan. A renderer that shades each face on its own never shares
    vertices anyway, and the split is a third of the build time.

    ``loft`` joins the outlines of consecutive slabs no thicker than it with
    slanted faces instead of a step, for a display mesh of a film sampled
    in z steps (see :mod:`.loft`). ``loft_reach`` is how far an outline may
    move between two joined slabs; four times ``loft`` when not given.
    """
    materials: list[Material] = []
    for slab in state.slabs:
        for m in slab.regions:
            if m not in materials:
                materials.append(m)
    out: "OrderedDict[Material, trimesh.Trimesh]" = OrderedDict()
    for m in materials:
        out[m] = build_one_material(state, m, manifold=manifold, loft=loft, loft_reach=loft_reach)
    return out


def build_one_material(
    state: ProcessState,
    material: Material,
    *,
    manifold: bool = True,
    loft: float | None = None,
    loft_reach: float | None = None,
) -> trimesh.Trimesh:
    """Caps and walls of one material from a single planar arrangement.

    The state is harmonized, so the rings of this material in every slab are
    made of edges of one shared linework. Polygonizing that linework gives
    atomic faces; each face is either inside or outside the material's region
    in every slab (``member``) and inside or outside the other materials'
    regions (``touch``). Then, with no tolerance anywhere:

    * a face is an up-cap at plane k if member changes from True (below) to
      False (above), a down-cap for the opposite change;
    * an arrangement edge is a wall in slab k if member differs between the
      faces on its two sides (the solid is on the ``True`` side).

    Around any 3D edge the number of incident mesh faces is then the number
    of changes around the four (below/above x left/right) cells: always even.

    With ``loft`` set, a plane between two thin slabs whose rings continue
    each other gets no caps: each slab's wall stops at its mid height and
    the two outlines are joined there by slanted triangles (:mod:`.loft`).
    The outlines are the arrangement edges, so they share the walls' nodes.
    """
    slabs = state.slabs
    n = len(slabs)
    regions: list[MultiPolygon] = [s.regions.get(material, P.EMPTY) for s in slabs]
    z = [s.z0 for s in slabs] + [slabs[-1].z1]

    if all(r.is_empty for r in regions):
        raise MeshError(f"{material.name}: no geometry")
    # the arrangement of *all* materials' rings: every face is then uniformly
    # inside/outside every material in every slab, so interface classification
    # (touch) is exact and needs no probing
    rings = [g.boundary for s in slabs for g in s.regions.values()]
    master = shapely.unary_union(rings)
    faces2d = [
        orient(f, sign=1.0)
        for f in shapely.get_parts(shapely.polygonize(shapely.get_parts(master)))
        if f.area > 0
    ]
    if not faces2d:
        raise MeshError(f"{material.name}: no faces to mesh")
    reps = shapely.points(np.array([[p.x, p.y] for p in (f.representative_point() for f in faces2d)]))

    member = np.zeros((n, len(faces2d)), dtype=bool)
    touch = np.zeros((n, len(faces2d)), dtype=bool)
    for k, slab in enumerate(slabs):
        r = regions[k]
        if not r.is_empty:
            shapely.prepare(r)
            member[k] = shapely.contains(r, reps)
        parts = [g for m, g in slab.regions.items() if m is not material]
        if parts:
            u = shapely.unary_union(parts)
            shapely.prepare(u)
            touch[k] = shapely.contains(u, reps)

    # directed edges of every face (face on the left) and edge -> adjacent faces
    edges_of_face = [_directed_edges(f) for f in faces2d]
    adjacent: dict[tuple, list[tuple[int, bool]]] = defaultdict(list)  # (a<b) -> [(face, forward)]
    for fi, edges in enumerate(edges_of_face):
        for a, b in edges:
            if a < b:
                adjacent[(a, b)].append((fi, True))
            else:
                adjacent[(b, a)].append((fi, False))

    # walls: an arrangement edge bounds the solid in slab k where membership differs across it
    wall_edges: list[list[tuple[tuple, tuple, bool]]] = [[] for _ in range(n)]
    for (a, b), adj in adjacent.items():
        left = next((fi for fi, fwd in adj if fwd), None)
        right = next((fi for fi, fwd in adj if not fwd), None)
        for k in range(n):
            in_left = bool(member[k][left]) if left is not None else False
            in_right = bool(member[k][right]) if right is not None else False
            if in_left == in_right:
                continue
            if in_left:
                p, q, other = a, b, right
            else:
                p, q, other = b, a, left
            iface = bool(touch[k][other]) if other is not None else False
            wall_edges[k].append((p, q, iface))

    # lofted planes: a ring of slab k joined to the ring of slab k+1 it
    # continues, at their mid heights, instead of a cap between them
    mids = [0.5 * (z[k] + z[k + 1]) for k in range(n)]
    rings: list[list | None] = [None] * n  # the chained rings of each thin slab
    ring_of_edge: list[dict[tuple, int]] = [{} for _ in range(n)]
    pairs: list[list] = [[] for _ in range(n)]  # pairs[k]: (ring of k, ring of k+1, region between)
    covered: list[np.ndarray] = [np.zeros(len(faces2d), dtype=bool)] * (n + 1)
    joined_up: list[set[int]] = [set() for _ in range(n)]
    joined_down: list[set[int]] = [set() for _ in range(n)]
    if loft is not None:
        reach = 4.0 * loft if loft_reach is None else loft_reach
        thin = [slabs[k].thickness <= loft + 1e-9 and bool(wall_edges[k]) for k in range(n)]
        for k in range(n):
            if thin[k] and (k + 1 < n and thin[k + 1] or k > 0 and thin[k - 1]):
                rings[k] = chain_rings(wall_edges[k])
                if rings[k] is not None:
                    for index, (loop, _flags) in enumerate(rings[k]):
                        for a, b in zip(loop, loop[1:] + loop[:1]):
                            ring_of_edge[k][(a, b)] = index
        for k in range(n - 1):
            if rings[k] is None or rings[k + 1] is None:
                continue
            pairs[k] = match_rings(rings[k], rings[k + 1], reach)
            if not pairs[k]:
                continue
            mask = np.zeros(len(faces2d), dtype=bool)
            for i, j, between in pairs[k]:
                joined_up[k].add(i)
                joined_down[k + 1].add(j)
                shapely.prepare(between)
                mask |= shapely.contains(between, reps)
            covered[k + 1] = mask

    acc = _MeshAccumulator()
    empty = np.zeros(len(faces2d), dtype=bool)

    # caps: merge faces with equal (direction, interface) labels per plane, then triangulate
    for k in range(n + 1):
        below = member[k - 1] if k > 0 else empty
        above = member[k] if k < n else empty
        touch_below = touch[k - 1] if k > 0 else empty
        touch_above = touch[k] if k < n else empty
        zk = z[k]
        for up in (True, False):
            sel_dir = ((below & ~above) if up else (above & ~below)) & ~covered[k]
            if not sel_dir.any():
                continue
            iface_flag = touch_above if up else touch_below
            for iface in (False, True):
                sel = np.nonzero(sel_dir & (iface_flag == iface))[0]
                if len(sel) == 0:
                    continue
                for poly in _merge_faces(faces2d, edges_of_face, sel).geoms:
                    for a, b, c in triangulate(poly):
                        if up:
                            acc.triangle((*a, zk), (*b, zk), (*c, zk), iface)
                        else:
                            acc.triangle((*a, zk), (*c, zk), (*b, zk), iface)

    # lofts, before the walls so their vertices count as used at the mid planes
    for k in range(n - 1):
        for i, j, _between in pairs[k]:
            zipper(acc.triangle, rings[k][i], mids[k], rings[k + 1][j], mids[k + 1])  # type: ignore[index]

    # walls: the full slab, or only the half of it a loft does not replace
    strips: dict[tuple, list[tuple[float, float]]] = defaultdict(list)
    for k in range(n):
        for p, q, iface in wall_edges[k]:
            ring = ring_of_edge[k].get((p, q))
            lo = mids[k] if ring in joined_down[k] else z[k]
            hi = mids[k] if ring in joined_up[k] else z[k + 1]
            if lo < hi:
                strips[(p[0], p[1], q[0], q[1], iface)].append((lo, hi))
    _emit_walls(acc, strips)

    mesh = acc.to_trimesh()
    if len(mesh.faces) == 0:
        raise MeshError(f"{material.name}: empty mesh")
    if manifold:
        mesh, n_split = split_nonmanifold(mesh)  # keeps face order
    else:
        n_split = 0
    mesh.metadata["pinch_vertices_split"] = n_split
    mesh.metadata["interface_faces"] = np.asarray(acc.interface, dtype=bool)
    return mesh


def _directed_edges(face: Polygon) -> list[tuple[tuple, tuple]]:
    out = []
    for ring in (face.exterior, *face.interiors):
        c = ring.coords
        out.extend((tuple(c[i]), tuple(c[i + 1])) for i in range(len(c) - 1))
    return out


def _merge_faces(faces, edges_of_face, selected) -> MultiPolygon:
    """Union of arrangement faces that keeps every boundary node: boundary
    edges are the ones used by exactly one selected face."""
    count: Counter = Counter()
    for fi in selected:
        for a, b in edges_of_face[fi]:
            count[(a, b) if a < b else (b, a)] += 1
    boundary = [shapely.linestrings([a, b]) for (a, b), c in count.items() if c == 1]
    pieces = []
    for g in shapely.get_parts(shapely.polygonize(boundary)):
        pieces.extend(q for q in P._iter_polygons(shapely.make_valid(g)) if q.area > 0)
    # polygonize also returns the holes as pieces: keep the ones that are (mostly) selected area
    selected_union = shapely.unary_union([faces[i] for i in selected])
    shapely.prepare(selected_union)
    keep = []
    for piece in pieces:
        if selected_union.intersection(piece).area > 0.5 * piece.area:
            keep.append(orient(piece, sign=1.0))
    return MultiPolygon(keep) if keep else P.EMPTY


def _emit_walls(acc: _MeshAccumulator, strips: dict) -> None:
    """Merge the strips of each wall column into one planar polygon per
    contiguous z-run and triangulate it.

    A plane vertex (p, z) is kept on the polygon boundary if a cap uses it
    or if any column through it is not contiguous across z (a strip starts
    or ends there). Every face that needs the vertex therefore has it as a
    vertex, and no T-junction can arise. The common case (nothing touches
    the column) collapses to a single quad.
    """
    cap_used = set(acc.vertices)  # caps were emitted first
    columns = {key: sorted(ivs) for key, ivs in strips.items()}

    # For every plane vertex: is every column through it contiguous there?
    contiguous_ok: dict[XYZ, bool] = {}
    for key, ivs in columns.items():
        px, py, qx, qy, _ = key
        starts = {z0 for z0, _ in ivs}
        ends = {z1 for _, z1 in ivs}
        for zz in starts | ends:
            ok = zz in starts and zz in ends
            for v in ((px, py, zz), (qx, qy, zz)):
                contiguous_ok[v] = contiguous_ok.get(v, True) and ok

    def keep(v: XYZ) -> bool:
        return v in cap_used or not contiguous_ok.get(v, False)

    for key, ivs in columns.items():
        px, py, qx, qy, iface = key
        # maximal contiguous runs
        runs: list[list[float]] = []
        for z0, z1 in ivs:
            if runs and runs[-1][-1] == z0:
                runs[-1].append(z1)
            else:
                runs.append([z0, z1])
        for run in runs:
            za, zb, inner = run[0], run[-1], run[1:-1]
            left = [zz for zz in inner if keep((px, py, zz))]
            right = [zz for zz in inner if keep((qx, qy, zz))]
            if not left and not right:
                p0, q0, q1, p1 = (px, py, za), (qx, qy, za), (qx, qy, zb), (px, py, zb)
                acc.triangle(p0, q0, q1, iface)
                acc.triangle(p0, q1, p1, iface)
                continue
            # polygon in (u, z): u=0 is the p side, u=1 the q side; CCW = outward
            ring = [(0.0, za), (1.0, za)] + [(1.0, zz) for zz in right] + [(1.0, zb), (0.0, zb)]
            ring += [(0.0, zz) for zz in reversed(left)]
            for a, b, c in triangulate(Polygon(ring)):
                acc.triangle(*(_wall_xyz(key, uv) for uv in (a, b, c)), iface)


def _wall_xyz(key, uv) -> XYZ:
    px, py, qx, qy, _ = key
    u, zz = uv
    return (px, py, zz) if u == 0.0 else (qx, qy, zz)
