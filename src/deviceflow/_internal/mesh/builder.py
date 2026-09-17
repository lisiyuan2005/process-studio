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
from dataclasses import dataclass

import numpy as np
import shapely
import trimesh
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.polygon import orient

from ...exceptions import MeshError
from ...material import Material
from ..geometry import polygons as P
from ..geometry.state import ProcessState
from .manifold import split_nonmanifold
from .triangulate import triangulate

XYZ = tuple[float, float, float]

#: How many arrangement edges the wall pass compares at a time. The arrays
#: are (slabs x chunk), so this is what keeps them megabytes rather than the
#: hundreds of megabytes the whole edge list would need.
WALL_CHUNK = 4096


class _MeshAccumulator:
    def __init__(self) -> None:
        self._index: dict[XYZ, int] = {}
        self.vertices: list[XYZ] = []
        self.faces: list[tuple[int, int, int]] = []
        #: per face: index of the material it lies against, -1 for none
        self.neighbour: list[int] = []

    def vertex(self, x: float, y: float, z: float) -> int:
        key = (x, y, z)
        i = self._index.get(key)
        if i is None:
            i = len(self.vertices)
            self._index[key] = i
            self.vertices.append(key)
        return i

    def triangle(self, a: XYZ, b: XYZ, c: XYZ, neighbour: int = -1) -> None:
        self.faces.append((self.vertex(*a), self.vertex(*b), self.vertex(*c)))
        self.neighbour.append(int(neighbour))

    def to_trimesh(self) -> trimesh.Trimesh:
        return trimesh.Trimesh(
            vertices=np.asarray(self.vertices, dtype=np.float64),
            faces=np.asarray(self.faces, dtype=np.int64),
            process=False,
        )


@dataclass(frozen=True)
class Arrangement:
    """The planar figure every material's mesh is cut out of.

    Nothing in here depends on which material is being built: the linework
    is every material's rings noded together, so each atomic face is
    uniformly inside or outside every material in every slab. That is what
    makes a face's neighbour exact without probing -- and it is also why
    building it once per material was most of the cost. On a 253-slab, 7
    material stack it takes 6.6 s and the whole mesh took 88 s; 46 s of
    that was this figure, built seven times over.
    """

    #: the atomic faces, each oriented counter-clockwise
    faces: list[Polygon]
    #: per face, its ring edges with the face on their left
    edges_of_face: list[list[tuple[tuple, tuple]]]
    #: undirected edge -> the faces on its two sides, with which way round
    adjacent: dict[tuple, list[tuple[int, bool]]]
    #: per slab, per face: which material holds it, -1 for none. A slab's
    #: regions are disjoint, so one number says everything -- a face is
    #: this material's where it names this material, and lies against
    #: whatever else it names.
    owner: np.ndarray
    materials: list[Material]
    index_of: dict[Material, int]

    def member(self, index: int) -> np.ndarray:
        """Per slab, per face: is this material there?"""
        return self.owner == np.int16(index)

    def against(self, index: int) -> np.ndarray:
        """Per slab, per face: which *other* material is there, -1 for none."""
        return np.where(self.owner == np.int16(index), np.int16(-1), self.owner)


def arrange(state: ProcessState, materials: list[Material] | None = None) -> Arrangement:
    """Node every material's rings together and label the faces that fall out.

    A slab's regions are disjoint (``ProcessState.validate`` says so), so
    an overlap here means the state was never valid; that is an error
    rather than a mesh with two materials in one place.
    """
    if materials is None:
        materials = materials_in_order(state)
    index_of = {m: i for i, m in enumerate(materials)}
    slabs = state.slabs
    segments = P.unique_segments(g for s in slabs for g in s.regions.values())
    master = shapely.unary_union(segments) if segments is not None else P.EMPTY
    faces = [
        orient(f, sign=1.0)
        for f in shapely.get_parts(shapely.polygonize(shapely.get_parts(master)))
        if f.area > 0
    ]
    if not faces:
        raise MeshError("no faces to mesh")
    reps = shapely.points(
        np.array([[p.x, p.y] for p in (f.representative_point() for f in faces)])
    )

    owner = np.full((len(slabs), len(faces)), -1, dtype=np.int16)
    for k, slab in enumerate(slabs):
        for material, region in slab.regions.items():
            if region.is_empty:
                continue
            shapely.prepare(region)
            held = shapely.contains(region, reps)
            if np.any(held & (owner[k] >= 0)):
                raise MeshError(
                    f"{material.name} overlaps another material in the slab at z={slab.z0}"
                )
            owner[k][held] = index_of[material]

    edges_of_face = [_directed_edges(f) for f in faces]
    adjacent: dict[tuple, list[tuple[int, bool]]] = defaultdict(list)
    for fi, edges in enumerate(edges_of_face):
        for a, b in edges:
            if a < b:
                adjacent[(a, b)].append((fi, True))
            else:
                adjacent[(b, a)].append((fi, False))
    return Arrangement(faces, edges_of_face, dict(adjacent), owner, list(materials), index_of)


def build_material_meshes(
    state: ProcessState, *, manifold: bool = True, buried: bool = True
) -> "OrderedDict[Material, trimesh.Trimesh]":
    """One mesh per material that owns any volume, in first-appearance order.

    ``manifold=False`` skips the vertex splitting that makes every vertex a
    single fan. A renderer that shades each face on its own never shares
    vertices anyway, and the split is a third of the build time.

    ``buried=False`` leaves out every face that lies against another
    material, keeping only the free surface -- what the solid shows to the
    outside. The meshes are then not closed, so it is for looking at, not
    for exporting or measuring: a stack is nearly all buried interfaces,
    and leaving them out is most of the work. A material with no free
    surface at all comes back with an empty mesh rather than an error.

    Each mesh records, per face, which of the materials (by position in this
    order) the face lies against, in ``metadata["neighbour_faces"]``.
    """
    materials = materials_in_order(state)
    figure = arrange(state, materials)
    out: "OrderedDict[Material, trimesh.Trimesh]" = OrderedDict()
    for m in materials:
        out[m] = build_one_material(
            state, m, manifold=manifold, materials=materials, buried=buried, arrangement=figure
        )
    return out


def materials_in_order(state: ProcessState) -> list[Material]:
    """The materials that own volume, in first-appearance order.

    Every mesh a build hands out is keyed by this order, and a face records
    the material it lies against by its place in it, so anything that builds
    one material on its own has to agree with it.
    """
    materials: list[Material] = []
    for slab in state.slabs:
        for m in slab.regions:
            if m not in materials:
                materials.append(m)
    return materials


def build_one_material(
    state: ProcessState,
    material: Material,
    *,
    manifold: bool = True,
    materials: list[Material] | None = None,
    buried: bool = True,
    arrangement: Arrangement | None = None,
) -> trimesh.Trimesh:
    """Caps and walls of one material from a single planar arrangement.

    The state is harmonized, so the rings of this material in every slab are
    made of edges of one shared linework. Polygonizing that linework gives
    atomic faces; each face is either inside or outside the material's region
    in every slab (``member``) and inside or outside the other materials'
    regions (``neighbour``). Then, with no tolerance anywhere:

    * a face is an up-cap at plane k if member changes from True (below) to
      False (above), a down-cap for the opposite change;
    * an arrangement edge is a wall in slab k if member differs between the
      faces on its two sides (the solid is on the ``True`` side).

    Around any 3D edge the number of incident mesh faces is then the number
    of changes around the four (below/above x left/right) cells: always even.

    Every face carries the index (in ``materials``, first-appearance order
    when not given) of the material on its other side, -1 for none, in
    ``metadata["neighbour_faces"]``; ``metadata["interface_faces"]`` is the
    same as a boolean.
    """
    slabs = state.slabs
    n = len(slabs)
    regions: list[MultiPolygon] = [s.regions.get(material, P.EMPTY) for s in slabs]
    z = [s.z0 for s in slabs] + [slabs[-1].z1]

    if all(r.is_empty for r in regions):
        raise MeshError(f"{material.name}: no geometry")
    # The figure the faces come from says nothing about this material in
    # particular, so a caller building every material hands over one for
    # all of them (see ``arrange``).
    if arrangement is None:
        arrangement = arrange(state, materials)
    faces2d = arrangement.faces
    edges_of_face = arrangement.edges_of_face
    adjacent = arrangement.adjacent
    mine = arrangement.index_of[material]
    member = arrangement.member(mine)
    neighbour = arrangement.against(mine)

    acc = _MeshAccumulator()
    empty = np.zeros(len(faces2d), dtype=bool)
    nobody = np.full(len(faces2d), -1, dtype=np.int16)

    # caps: merge faces with equal (direction, neighbour) labels per plane, then triangulate
    for k in range(n + 1):
        below = member[k - 1] if k > 0 else empty
        above = member[k] if k < n else empty
        neighbour_below = neighbour[k - 1] if k > 0 else nobody
        neighbour_above = neighbour[k] if k < n else nobody
        zk = z[k]
        for up in (True, False):
            sel_dir = (below & ~above) if up else (above & ~below)
            if not sel_dir.any():
                continue
            other = neighbour_above if up else neighbour_below
            for who in np.unique(other[sel_dir]):
                if not buried and who >= 0:
                    continue  # this cap is an interface; nothing sees it
                sel = np.nonzero(sel_dir & (other == who))[0]
                for poly in _merge_faces(faces2d, edges_of_face, sel).geoms:
                    for a, b, c in triangulate(poly):
                        if up:
                            acc.triangle((*a, zk), (*b, zk), (*c, zk), int(who))
                        else:
                            acc.triangle((*a, zk), (*c, zk), (*b, zk), int(who))

    # walls: an arrangement edge bounds the solid in slab k where membership
    # differs across it. Asking that edge by edge and slab by slab was
    # 95,586 x 253 = 24 million Python steps on the stack this was measured
    # on -- and the same 24 million whether the material ended up with
    # 277,000 triangles or 68. So the question is asked of numpy, in chunks
    # of edges to keep the arrays small, and only the strips that exist are
    # walked. The chunks go in edge order and each chunk in slab order,
    # which is the order the columns used to be met in.
    edges = list(adjacent.items())
    nowhere = len(faces2d)  # a column standing for "no face on that side"
    left_of = np.full(len(edges), nowhere, dtype=np.int32)
    right_of = np.full(len(edges), nowhere, dtype=np.int32)
    for ei, (_ends, adj) in enumerate(edges):
        for fi, forward in adj:
            side = left_of if forward else right_of
            if side[ei] == nowhere:
                side[ei] = fi
    member_at = np.zeros((n, nowhere + 1), dtype=bool)  # not a member of nowhere
    member_at[:, :nowhere] = member
    against_at = np.full((n, nowhere + 1), -1, dtype=np.int16)  # nothing against nowhere
    against_at[:, :nowhere] = neighbour

    strips: dict[tuple, list[tuple[float, float]]] = defaultdict(list)
    for start in range(0, len(edges), WALL_CHUNK):
        stop = min(start + WALL_CHUNK, len(edges))
        left, right = left_of[start:stop], right_of[start:stop]
        in_left = member_at[:, left]
        in_right = member_at[:, right]
        who = np.where(in_left, against_at[:, right], against_at[:, left])
        wall = in_left != in_right
        if not buried:
            wall &= who < 0  # an interface wall; nothing sees it
        here, slab = np.nonzero(wall.T)
        for ei, k, mine_left, against in zip(
            here.tolist(),
            slab.tolist(),
            in_left.T[here, slab].tolist(),
            who.T[here, slab].tolist(),
        ):
            a, b = edges[start + ei][0]
            p, q = (a, b) if mine_left else (b, a)
            strips[(p[0], p[1], q[0], q[1], against)].append((z[k], z[k + 1]))
    _emit_walls(acc, strips)

    mesh = acc.to_trimesh()
    if len(mesh.faces) == 0 and buried:
        # Without the buried faces an enclosed material really has nothing
        # to show, which is an answer rather than a failure.
        raise MeshError(f"{material.name}: empty mesh")
    if manifold:
        mesh, n_split = split_nonmanifold(mesh)  # keeps face order
    else:
        n_split = 0
    mesh.metadata["pinch_vertices_split"] = n_split
    neighbours = np.asarray(acc.neighbour, dtype=np.int16)
    mesh.metadata["neighbour_faces"] = neighbours
    mesh.metadata["interface_faces"] = neighbours >= 0
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
    edges = [ends for ends, c in count.items() if c == 1]
    if not edges:
        return P.EMPTY
    ends = np.asarray(edges, dtype=float)  # (edges, 2 endpoints, xy)
    boundary = shapely.linestrings(
        ends.reshape(-1, 2), indices=np.repeat(np.arange(len(edges)), 2)
    )
    rings = shapely.get_parts(shapely.polygonize(boundary))
    pieces = []
    for g in shapely.make_valid(rings):
        pieces.extend(q for q in P._iter_polygons(g) if q.area > 0)
    if not pieces:
        return P.EMPTY
    # polygonize also returns the holes as pieces: keep the ones that are (mostly) selected area
    selected_union = shapely.unary_union([faces[i] for i in selected])
    parts = np.asarray(pieces, dtype=object)
    overlap = shapely.area(shapely.intersection(selected_union, parts))
    keep = [
        orient(piece, sign=1.0)
        for piece, over, area in zip(pieces, overlap, shapely.area(parts))
        if over > 0.5 * area
    ]
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
        px, py, qx, qy, who = key
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
                acc.triangle(p0, q0, q1, who)
                acc.triangle(p0, q1, p1, who)
                continue
            # polygon in (u, z): u=0 is the p side, u=1 the q side; CCW = outward
            ring = [(0.0, za), (1.0, za)] + [(1.0, zz) for zz in right] + [(1.0, zb), (0.0, zb)]
            ring += [(0.0, zz) for zz in reversed(left)]
            for a, b, c in triangulate(Polygon(ring)):
                acc.triangle(*(_wall_xyz(key, uv) for uv in (a, b, c)), who)


def _wall_xyz(key, uv) -> XYZ:
    px, py, qx, qy, _ = key
    u, zz = uv
    return (px, py, zz) if u == 0.0 else (qx, qy, zz)
