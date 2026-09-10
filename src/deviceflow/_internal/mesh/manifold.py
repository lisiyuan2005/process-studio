"""Split non-manifold contacts so that every material mesh is a 2-manifold.

Two features that touch at a single XY point (e.g. two etched squares
sharing one corner) leave a solid that is pinched along a vertical line:
an edge shared by four faces. No manifold mesh represents that solid with
shared vertices, so the pinch is resolved the standard way — the vertices
on the pinch are duplicated, one copy per solid sector — which keeps the
geometry bit-identical and gives trimesh/Blender a watertight, consistently
wound mesh whose volume is unchanged.

The pairing of faces across a non-manifold edge uses the face winding: the
mesh is oriented outward, so walking around the edge the faces alternate
"solid starts here" / "solid ends here", and consecutive faces of one solid
sector are paired. Vertices are then split by connectivity of their face fan
through manifold edges and through those pairings.
"""

from __future__ import annotations

import math
from collections import defaultdict

import numpy as np
import trimesh

from ...exceptions import MeshError


def split_nonmanifold(mesh: trimesh.Trimesh) -> tuple[trimesh.Trimesh, int]:
    """Return (mesh, number of vertices added)."""
    V = np.asarray(mesh.vertices, dtype=np.float64)
    F = np.asarray(mesh.faces, dtype=np.int64).copy()

    edge_faces: dict[tuple[int, int], list[int]] = defaultdict(list)
    for fi, (a, b, c) in enumerate(F):
        for u, v in ((a, b), (b, c), (c, a)):
            edge_faces[(u, v) if u < v else (v, u)].append(fi)

    nonmanifold = {e: fs for e, fs in edge_faces.items() if len(fs) > 2}
    if not nonmanifold and _all_fans_connected(F, edge_faces):
        return mesh, 0

    # pairs[(edge)] = list of frozensets of faces that bound one solid sector
    pairs: dict[tuple[int, int], list[frozenset]] = {}
    for e, fs in nonmanifold.items():
        pairs[e] = _pair_faces_around_edge(V, F, e, fs)

    vertex_faces: dict[int, list[int]] = defaultdict(list)
    for fi, f in enumerate(F):
        for v in f:
            vertex_faces[int(v)].append(fi)

    # Decide every split on the original topology first, then renumber.
    splits: list[tuple[int, list[int]]] = []
    for p, fs in vertex_faces.items():
        comps = _fan_components(p, fs, F, edge_faces, pairs)
        for comp in comps[1:]:
            splits.append((p, comp))
    new_vertices = [V]
    next_index = len(V)
    for p, comp in splits:
        new_vertices.append(V[p][None, :])
        for fi in comp:
            F[fi][F[fi] == p] = next_index
        next_index += 1
    out = trimesh.Trimesh(np.vstack(new_vertices), F, process=False)
    # every non-manifold edge must now be resolved; a surviving one is a seam
    # the geometry layer should have opened (ProcessState._remove_seams)
    remaining = defaultdict(int)
    for a, b, c in F:
        for u, v in ((a, b), (b, c), (c, a)):
            remaining[(u, v) if u < v else (v, u)] += 1
    bad = [e for e, n in remaining.items() if n > 2]
    if bad:
        u, v = bad[0]
        raise MeshError(
            f"{len(bad)} non-manifold edge(s) survive vertex splitting, e.g. "
            f"{np.round(out.vertices[u], 6)} -> {np.round(out.vertices[v], 6)}: a cap-to-cap seam; "
            "the process state should have opened it (ProcessState._remove_seams)"
        )
    return out, len(splits)


def _all_fans_connected(F, edge_faces) -> bool:
    vertex_faces: dict[int, list[int]] = defaultdict(list)
    for fi, f in enumerate(F):
        for v in f:
            vertex_faces[int(v)].append(fi)
    return all(len(_fan_components(p, fs, F, edge_faces, {})) == 1 for p, fs in vertex_faces.items())


def _fan_components(p: int, fs: list[int], F, edge_faces, pairs) -> list[list[int]]:
    """Connected components of the faces around vertex p."""
    parent = {fi: fi for fi in fs}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    seen_edges = set()
    for fi in fs:
        for q in F[fi]:
            q = int(q)
            if q == p:
                continue
            e = (p, q) if p < q else (q, p)
            if e in seen_edges:
                continue
            seen_edges.add(e)
            faces_on_e = edge_faces[e]
            if e in pairs:
                for pair in pairs[e]:
                    a, b = tuple(pair)
                    union(a, b)
            elif len(faces_on_e) == 2:
                union(faces_on_e[0], faces_on_e[1])
    groups: dict[int, list[int]] = defaultdict(list)
    for fi in fs:
        groups[find(fi)].append(fi)
    return sorted(groups.values(), key=lambda g: min(g))


def _pair_faces_around_edge(V, F, e, fs) -> list[frozenset]:
    u, v = e
    d = V[v] - V[u]
    d = d / np.linalg.norm(d)
    b1 = np.cross(d, [1.0, 0.0, 0.0])
    if np.linalg.norm(b1) < 1e-9:
        b1 = np.cross(d, [0.0, 1.0, 0.0])
    b1 /= np.linalg.norm(b1)
    b2 = np.cross(d, b1)

    entries = []  # (angle, solid_above, face)
    for fi in fs:
        f = [int(x) for x in F[fi]]
        w = next(x for x in f if x != u and x != v)
        t = V[w] - V[u]
        t = t - (t @ d) * d
        angle = math.atan2(t @ b2, t @ b1)
        # does the face traverse u -> v (in its cyclic order)?
        i = f.index(u)
        forward = f[(i + 1) % 3] == v
        # forward: normal ~ d x t (angle + 90deg) -> solid is at angles below the face
        solid_above = not forward
        entries.append((angle, solid_above, fi))
    entries.sort()
    n = len(entries)
    if n % 2:
        raise MeshError(f"edge {e} has an odd number of faces ({n})")
    out = []
    for i in range(n):
        angle, solid_above, fi = entries[i]
        if solid_above:
            angle2, solid_above2, fj = entries[(i + 1) % n]
            if solid_above2:
                raise MeshError(f"inconsistent orientation around non-manifold edge {e}")
            out.append(frozenset((fi, fj)))
    if len(out) != n // 2:
        raise MeshError(f"cannot pair faces around non-manifold edge {e}")
    return out
