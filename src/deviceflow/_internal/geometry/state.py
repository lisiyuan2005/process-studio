"""ProcessState: the internal geometry model.

The device is a contiguous stack of :class:`Slab` objects. Each slab is a Z
interval ``[z0, z1)`` holding, per material, a clean XY ``MultiPolygon``.
Within one slab the material regions are pairwise disjoint (at most one
solid material at any point). Empty space is simply absence of a region.

Z planes are stored normalised (rounded to :data:`Z_DECIMALS`) so that two
planes computed by different arithmetic routes compare equal exactly; plane
identity is never an epsilon comparison.
"""

from __future__ import annotations

import math
from typing import Iterator

import numpy as np
import shapely
from shapely.geometry import MultiPolygon, Point, Polygon, box

from ...cancellation import check_cancelled
from ...exceptions import GeometryError
from ...material import Material
from . import polygons as P

Z_DECIMALS = 9


def znorm(z) -> float:
    z = round(float(z), Z_DECIMALS)
    return 0.0 if z == 0 else z  # no -0.0


class Slab:
    __slots__ = ("z0", "z1", "regions")

    def __init__(self, z0: float, z1: float, regions: dict[Material, MultiPolygon]):
        self.z0 = z0
        self.z1 = z1
        self.regions = regions

    @property
    def thickness(self) -> float:
        return znorm(self.z1 - self.z0)  # 1.3 - 1.0 is 0.30000000000000004 in floats

    @property
    def is_empty(self) -> bool:
        return not self.regions

    def occupied(self) -> MultiPolygon:
        if not self.regions:
            return P.EMPTY
        return P.as_multipolygon(shapely.unary_union(list(self.regions.values())))

    def material_at(self, x: float, y: float) -> Material | None:
        pt = Point(x, y)
        for material, region in self.regions.items():
            if region.covers(pt):
                return material
        return None

    def same_regions(self, other: "Slab") -> bool:
        if self.regions.keys() != other.regions.keys():
            return False
        return all(P.equals(self.regions[m], other.regions[m]) for m in self.regions)

    def copy(self) -> "Slab":
        return Slab(self.z0, self.z1, dict(self.regions))

    def __repr__(self) -> str:
        mats = ", ".join(m.name for m in self.regions) or "void"
        return f"Slab({self.z0}..{self.z1}: {mats})"


class ProcessState:
    def __init__(self, bounds, grid: float):
        x0, y0, x1, y1 = (float(v) for v in bounds)
        if not (x1 > x0 and y1 > y0):
            raise GeometryError(f"bounds must be (x0, y0, x1, y1) with x1 > x0, y1 > y0; got {bounds!r}")
        self.bounds = (x0, y0, x1, y1)
        self.grid = float(grid)
        self._box = box(x0, y0, x1, y1)
        self._slabs: list[Slab] = []

    # -- inspection -------------------------------------------------------

    @property
    def bounds_area(self) -> float:
        x0, y0, x1, y1 = self.bounds
        return (x1 - x0) * (y1 - y0)

    @property
    def slabs(self) -> list[Slab]:
        return self._slabs

    @property
    def z_planes(self) -> list[float]:
        if not self._slabs:
            return []
        return [s.z0 for s in self._slabs] + [self._slabs[-1].z1]

    @property
    def top(self) -> float | None:
        """Highest Z with any solid material, or None."""
        for s in reversed(self._slabs):
            if not s.is_empty:
                return s.z1
        return None

    @property
    def floor(self) -> float | None:
        for s in self._slabs:
            if not s.is_empty:
                return s.z0
        return None

    def slab_at(self, z: float) -> Slab | None:
        """Slab containing z, half-open: z0 <= z < z1."""
        z = znorm(z)
        for s in self._slabs:
            if s.z0 <= z < s.z1:
                return s
        return None

    def slabs_between(self, z0: float, z1: float) -> Iterator[Slab]:
        """Slabs overlapping the open interval (z0, z1)."""
        z0, z1 = znorm(z0), znorm(z1)
        for s in self._slabs:
            if s.z1 > z0 and s.z0 < z1:
                yield s

    def occupied_at(self, z: float) -> MultiPolygon:
        s = self.slab_at(z)
        return P.EMPTY if s is None else s.occupied()

    def material_at(self, x: float, y: float, z: float) -> Material | None:
        s = self.slab_at(z)
        return None if s is None else s.material_at(x, y)

    def volume(self, material: Material) -> float:
        return sum(
            s.regions[material].area * s.thickness for s in self._slabs if material in s.regions
        )

    # -- mutation ---------------------------------------------------------

    def clean(self, geom) -> MultiPolygon:
        """Clip to the device bounds and normalise."""
        if geom is None or geom.is_empty:
            return P.EMPTY
        if not geom.is_valid:  # e.g. after shapely.snap; an overlay on invalid input can throw
            geom = shapely.make_valid(geom)
        # Most of what comes here was cut out of something already inside
        # the window, and clipping such a thing is an overlay that returns
        # its input. The bounds settle it in arithmetic.
        x0, y0, x1, y1 = self.bounds
        bx0, by0, bx1, by1 = geom.bounds
        if not (x0 <= bx0 and y0 <= by0 and bx1 <= x1 and by1 <= y1):
            geom = shapely.intersection(geom, self._box)
        return P.clean(geom, self.grid)

    def _clean_regions(self, regions) -> dict[Material, MultiPolygon]:
        out: dict[Material, MultiPolygon] = {}
        for material, geom in regions.items():
            if not isinstance(material, Material):
                raise GeometryError(f"region key must be a Material, got {material!r}")
            g = self.clean(geom)
            if not g.is_empty:
                out[material] = g
        self._check_disjoint(out)
        return out

    def _check_disjoint(self, regions: dict[Material, MultiPolygon]) -> None:
        items = list(regions.items())
        eps = self.grid * self.grid
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                (ma, ga), (mb, gb) = items[i], items[j]
                # Neighbouring materials meet along their boundary; the
                # predicates settle that without building the intersection.
                if not ga.intersects(gb) or ga.touches(gb):
                    continue
                overlap = ga.intersection(gb).area
                if overlap > eps:
                    raise GeometryError(
                        f"materials {ma.name} and {mb.name} overlap by {overlap:.3g} um^2"
                    )

    def add_slab(self, z0: float, z1: float, regions) -> Slab:
        """Append a slab above (or below) the current stack; gaps become void."""
        z0, z1 = znorm(z0), znorm(z1)
        if not (z1 > z0):
            raise GeometryError(f"slab needs z1 > z0, got {z0}..{z1}")
        slab = Slab(z0, z1, self._clean_regions(regions))
        if not self._slabs:
            self._slabs.append(slab)
            return slab
        zmin, zmax = self._slabs[0].z0, self._slabs[-1].z1
        if z0 >= zmax:
            if z0 > zmax:
                self._slabs.append(Slab(zmax, z0, {}))
            self._slabs.append(slab)
        elif z1 <= zmin:
            if z1 < zmin:
                self._slabs.insert(0, Slab(z1, zmin, {}))
            self._slabs.insert(0, slab)
        else:
            raise GeometryError(
                f"slab {z0}..{z1} overlaps existing stack {zmin}..{zmax}; use split_at + region edits"
            )
        return slab

    def split_at(self, z: float) -> None:
        """Ensure a Z plane exists at z (no-op if outside the stack)."""
        z = znorm(z)
        for i, s in enumerate(self._slabs):
            if s.z0 < z < s.z1:
                lower = Slab(s.z0, z, dict(s.regions))
                upper = Slab(z, s.z1, dict(s.regions))
                self._slabs[i : i + 1] = [lower, upper]
                return

    def harmonize(self, changed=None, depth: int = 0) -> None:
        """Rebuild every region from one snap-rounded planar arrangement.

        Boolean results are snapped to the grid slab by slab, so rings of
        different slabs can run within a grid step of each other without
        sharing vertices, and noding them together (as the mesh builder must)
        would create near-duplicate nodes: zero-length edges, sliver caps and
        walls that disagree with caps. Instead all rings of all slabs are
        noded together here, rounded to the grid, re-noded until stable, and
        each region is re-assembled from the faces of that arrangement. Every
        edge of every region is then an edge of the shared linework, so the
        mesh builder never has to create a node. Geometry moves by at most a
        grid step (1e-6 um by default); volumes stay exactly consistent.

        ``changed`` names the slabs a step touched. The arrangement and the
        reassembly cover every slab regardless (they are cheap, and the new
        rings may put nodes on old edges), but the passes that open pinches
        and seams only look at those slabs and their neighbours: the rest
        were left pinch- and seam-free by the previous call. Regions coming
        out of one arrangement are disjoint by construction, so nothing is
        checked for overlap here; ``validate`` still does.
        """
        # Rebuilding the arrangement is the other half of a step's cost, so
        # a caller asking to stop is heard here too, before the work starts.
        check_cancelled()
        regions = [(s, m, r) for s in self._slabs for m, r in s.regions.items()]
        if not regions:
            return
        changed_ids = None if changed is None else {id(s) for s in changed}
        # After repeated deposition most slabs carry a region that some other
        # slab carries too, so the same boundary would enter the linework many
        # times over. Feeding each distinct one once builds the same union
        # from a fraction of the input; the key is the exact coordinates, so
        # only genuinely identical boundaries are folded together.
        seen: set[bytes] = set()
        distinct = []
        shape_of_region: list[bytes] = []
        for _, _, region in regions:
            shape = shapely.to_wkb(region)
            shape_of_region.append(shape)
            if shape not in seen:
                seen.add(shape)
                distinct.append(region)
        # Distinct regions still share most of their edges (a boundary
        # between two materials is a ring of both); node each edge once.
        segments = P.unique_segments(distinct)
        if segments is None:
            return
        master = shapely.unary_union(segments)
        for _ in range(6):
            check_cancelled()
            rounded = shapely.set_precision(master, self.grid, mode="valid_output")
            noded = shapely.unary_union(rounded)
            if noded.equals_exact(master, 0.0):
                break
            master = noded
        faces = [f for f in shapely.get_parts(shapely.polygonize(shapely.get_parts(master))) if f.area > 0]
        if not faces:
            return
        reps = shapely.points(np.array([[p.x, p.y] for p in (f.representative_point() for f in faces)]))
        boundaries = _Boundaries([_directed_edges(f) for f in faces])
        # Identical regions classify identically against one arrangement, so
        # each distinct one is classified and reassembled once. The cache
        # lives only for this call, and the shared result is immutable, as
        # shapely geometry always is.
        rebuilt_by_shape: dict[bytes, MultiPolygon | None] = {}
        inside_by_shape: dict[bytes, np.ndarray] = {}
        # A face two regions of one slab both cover (two boundaries rounded
        # onto each other) goes to the first of them: the regions of a slab
        # must stay disjoint, and such a face is a sliver either way.
        claimed_in_slab: dict[int, np.ndarray] = {}
        for (slab, material, region), shape in zip(regions, shape_of_region):
            inside = inside_by_shape.get(shape)
            if inside is None:
                shapely.prepare(region)
                inside = np.nonzero(shapely.contains(region, reps))[0]
                inside_by_shape[shape] = inside
            claimed = claimed_in_slab.setdefault(id(slab), np.zeros(len(faces), dtype=bool))
            contested = claimed[inside]
            if contested.any():
                inside = inside[~contested]
                claimed[inside] = True
                rebuilt = _assemble(boundaries, inside, region) if len(inside) else None
            else:
                claimed[inside] = True
                if shape in rebuilt_by_shape:
                    rebuilt = rebuilt_by_shape[shape]
                else:
                    rebuilt = _assemble(boundaries, inside, region) if len(inside) else None
                    rebuilt_by_shape[shape] = rebuilt
            if rebuilt is None:
                # thinner than the grid everywhere: it vanishes
                del slab.regions[material]
                continue
            slab.regions[material] = rebuilt
        opened = self._remove_pinches(changed_ids)
        opened += self._remove_seams(changed_ids)
        if opened and depth < 4:
            self.harmonize(opened, depth=depth + 1)

    def regions_mark(self) -> dict[int, tuple]:
        """What every slab holds right now, for ``changed_since``."""
        return {id(s): tuple((m, id(r)) for m, r in s.regions.items()) for s in self._slabs}

    def changed_since(self, mark: dict[int, tuple]) -> list["Slab"]:
        """The slabs whose regions were replaced, or that did not exist, at ``mark``."""
        return [
            s for s in self._slabs
            if mark.get(id(s)) != tuple((m, id(r)) for m, r in s.regions.items())
        ]

    def _remove_pinches(self, changed_ids: set[int] | None = None) -> list["Slab"]:
        """Open point contacts inside a region by a one-grid-step notch.

        Two parts (or two holes, or a hole and the exterior) of one material
        touching at a single point make the mesh non-manifold along the
        vertical line through that point. When the material is continuous
        above and below, no vertex duplication can repair it, so the contact
        is opened at the grid scale instead: a 2-grid square around the point
        is removed from the region. Returns the slabs it changed.
        """
        changed: list[Slab] = []
        g = self.grid
        for slab in self._slabs:
            if changed_ids is not None and id(slab) not in changed_ids:
                continue
            for material, region in list(slab.regions.items()):
                seen: dict[tuple, int] = {}
                pinches = []
                for poly in region.geoms:
                    for ring in (poly.exterior, *poly.interiors):
                        for xy in ring.coords[:-1]:
                            key = (xy[0], xy[1])
                            seen[key] = seen.get(key, 0) + 1
                            if seen[key] == 2:
                                pinches.append(key)
                if not pinches:
                    continue
                notches = shapely.unary_union([box(x - g, y - g, x + g, y + g) for x, y in pinches])
                new = self.clean(region.difference(notches))
                if new.is_empty:
                    del slab.regions[material]
                else:
                    slab.regions[material] = new
                changed.append(slab)
        return changed

    def _remove_seams(self, changed_ids: set[int] | None = None) -> list["Slab"]:
        """Open the cap-to-cap seams that no manifold mesh can represent.

        At a plane where one material's region below and region above touch
        along an edge from opposite sides (below on one side, above on the
        other), the up-facing cap and the down-facing cap would share that
        edge with the two walls: a crack whose ends merge into the solid,
        which vertex duplication cannot fix. Such coincidences come from two
        independent offset edges rounding onto the same grid line, so they
        are resolved at the same scale: the upper region is set back from the
        seam by one grid step. Returns the slabs it changed.
        """
        changed: list[Slab] = []
        for below, above in zip(self._slabs[:-1], self._slabs[1:]):
            if changed_ids is not None and id(below) not in changed_ids and id(above) not in changed_ids:
                continue
            for material in list(above.regions):
                rb = below.regions.get(material)
                ra = above.regions[material]
                if rb is None or rb is ra:
                    continue
                # A seam needs an edge both boundaries run along; the
                # sampling slabs of one film mostly carry the same region,
                # and a wall meets a cap along a point at most. Only pairs
                # whose boundaries share a line pay for the two overlays.
                if shapely.to_wkb(rb) == shapely.to_wkb(ra):
                    continue
                # A polygon's boundary is closed, so every point of it is
                # interior in the DE-9IM sense: the two meeting in a
                # 1-dimensional set is exactly "they run along a shared
                # edge", and the pattern settles it without building the
                # shared linework. That overlay, on every slab pair of
                # every material, was 50 s of a real stack's harmonise.
                if not shapely.relate_pattern(rb.boundary, ra.boundary, "1********"):
                    continue
                up_cap = rb.difference(ra)
                down_cap = ra.difference(rb)
                if up_cap.is_empty or down_cap.is_empty:
                    continue
                seam = up_cap.boundary.intersection(down_cap.boundary)
                lines = [g for g in shapely.get_parts(seam) if g.geom_type == "LineString" and g.length > 0]
                if not lines:
                    continue
                strip = shapely.unary_union(lines).buffer(self.grid, cap_style="flat", join_style="mitre")
                new = self.clean(ra.difference(strip))
                if new.is_empty:
                    del above.regions[material]
                else:
                    above.regions[material] = new
                changed.append(above)
        return changed

    def consolidate(self) -> None:
        """Merge identical adjacent slabs; drop void slabs at the top and bottom."""
        merged: list[Slab] = []
        for s in self._slabs:
            if merged and merged[-1].same_regions(s):
                merged[-1] = Slab(merged[-1].z0, s.z1, merged[-1].regions)
            else:
                merged.append(s)
        while merged and merged[-1].is_empty:
            merged.pop()
        while merged and merged[0].is_empty:
            merged.pop(0)
        self._slabs = merged

    def copy(self) -> "ProcessState":
        other = ProcessState(self.bounds, self.grid)
        other._slabs = [s.copy() for s in self._slabs]
        return other

    # -- validation -------------------------------------------------------

    def validate(self) -> None:
        """Raise GeometryError on any structural problem."""
        prev_z1 = None
        for s in self._slabs:
            if not (math.isfinite(s.z0) and math.isfinite(s.z1)):
                raise GeometryError(f"non-finite Z in {s!r}")
            if not s.z1 > s.z0:
                raise GeometryError(f"non-positive thickness in {s!r}")
            if prev_z1 is not None and s.z0 != prev_z1:
                raise GeometryError(f"stack is not contiguous at z={prev_z1} / {s.z0}")
            prev_z1 = s.z1
            for material, region in s.regions.items():
                if not isinstance(region, MultiPolygon):
                    raise GeometryError(f"{material.name} in {s!r} is not a MultiPolygon")
                if region.is_empty:
                    raise GeometryError(f"{material.name} in {s!r} has an empty region")
                if not region.is_valid:
                    raise GeometryError(
                        f"{material.name} in {s!r} is invalid: {shapely.is_valid_reason(region)}"
                    )
                if not _finite(region):
                    raise GeometryError(f"{material.name} in {s!r} has non-finite coordinates")
                if not self._box.buffer(self.grid).covers(region):
                    raise GeometryError(f"{material.name} in {s!r} leaves the device bounds")
            self._check_disjoint(s.regions)


def _directed_edges(face) -> list[tuple[tuple, tuple]]:
    out = []
    for ring in (face.exterior, *face.interiors):
        c = ring.coords
        out.extend((tuple(c[i]), tuple(c[i + 1])) for i in range(len(c) - 1))
    return out


class _Boundaries:
    """The arrangement's edges, interned once, so a region's outline is arithmetic.

    Which edges bound a set of faces is "the ones used by exactly one of
    them", and counting that with a dictionary of coordinate pairs was 50 s
    of the 94 s harmonise spent reassembling regions on a real 253-slab
    stack -- the same 190,000 tuples hashed again for every region. As
    integers it is a ``bincount``.

    The first-seen order is kept: ``polygonize`` reads this order and hands
    the pieces back in it, so changing it would reorder the parts of every
    region in the state for no reason.
    """

    def __init__(self, edges_of_face: list) -> None:
        interned: dict[tuple, int] = {}
        ends: list[tuple] = []
        self.per_face: list[np.ndarray] = []
        for edges in edges_of_face:
            row = np.empty(len(edges), dtype=np.int64)
            for position, (a, b) in enumerate(edges):
                key = (a, b) if a < b else (b, a)
                index = interned.get(key)
                if index is None:
                    index = len(ends)
                    interned[key] = index
                    ends.append(key)
                row[position] = index
            self.per_face.append(row)
        #: (edges, 2 endpoints, xy), in the canonical a < b order
        self.ends = np.asarray(ends, dtype=float).reshape(-1, 2, 2)

    def around(self, selected) -> np.ndarray:
        """The edges used by exactly one of ``selected``, first-seen first."""
        if len(selected) == 0:
            return np.empty(0, dtype=np.int64)
        used = np.concatenate([self.per_face[fi] for fi in selected])
        times = np.bincount(used, minlength=len(self.ends))
        distinct, first = np.unique(used, return_index=True)
        return used[np.sort(first[times[distinct] == 1])]


def _assemble(boundaries: _Boundaries, selected, original) -> MultiPolygon:
    """Union of arrangement faces, keeping every node: boundary edges are
    those used by exactly one selected face; polygonize them and keep the
    pieces whose interior lies in the original region."""
    edges = boundaries.around(selected)
    if len(edges) == 0:
        return P.EMPTY
    # One call, not one per edge. A real stack's harmonise came through here
    # 1,502 times and built 5.6 million LineStrings one at a time, 20 s of
    # the 50 s this function cost.
    ends = boundaries.ends[edges]
    boundary = shapely.linestrings(
        ends.reshape(-1, 2), indices=np.repeat(np.arange(len(edges)), 2)
    )
    rings = shapely.get_parts(shapely.polygonize(boundary))
    if len(rings) == 0:
        return P.EMPTY
    pieces = []
    # a ring that touches itself at a node (pinch) is split into valid parts
    for g in shapely.make_valid(rings):
        pieces.extend(q for q in P._iter_polygons(g) if q.area > 0)
    if not pieces:
        return P.EMPTY
    shapely.prepare(original)
    parts = np.asarray(pieces, dtype=object)
    keep = [
        g
        for g, inside in zip(pieces, shapely.contains(original, shapely.point_on_surface(parts)))
        if inside
    ]
    from shapely.geometry.polygon import orient

    mp = MultiPolygon([Polygon(g.exterior, g.interiors) for g in keep])
    if not mp.is_valid:  # parts touching along a node chain: let GEOS split/merge them
        mp = P.as_multipolygon(shapely.make_valid(mp))
    return P.as_multipolygon(MultiPolygon([orient(g, sign=1.0) for g in mp.geoms]))


def _finite(geom) -> bool:
    return bool(np.isfinite(shapely.get_coordinates(geom)).all())
