"""The voxel film model: grid tools, each process, the views, and the kernel wiring."""

from __future__ import annotations

import base64
import io
import itertools
import json

import numpy as np
import pytest
from PIL import Image

from process_studio.kernels import get_kernel, voxel
from process_studio.kernels.slab import _rgb
from process_studio.worker.protocol import dispatch

CELL = 0.01  # 10 nm cells on a 1 um window: 100 x 100


def make_state(layers: list[tuple[float, float, np.ndarray | str]], size: int = 100) -> voxel.VoxelState:
    """A state from (z0, z1, grid-or-material-name) layers on a 1 um window."""
    materials: list[str] = []
    grids = []
    z = [layers[0][0]]
    for z0, z1, content in layers:
        assert abs(z0 - z[-1]) < 1e-12
        z.append(z1)
        if isinstance(content, str):
            if content not in materials:
                materials.append(content)
            grid = np.full((size, size), materials.index(content) + 1, dtype=np.uint8)
        else:
            grid = content.astype(np.uint8)
        grids.append(grid)
    for name in ("Si", "SiO2", "SiN", "TiN"):
        if name not in materials:
            materials.append(name)
    return voxel.VoxelState((0.0, 0.0, 1.0, 1.0), size, size, np.array(z), np.stack(grids), materials, -0.2)


def label(state: voxel.VoxelState, name: str) -> int:
    return state.material_id(name)


def brute_components(mask: np.ndarray) -> list[set]:
    """Reference 6-connected components by flood fill."""
    seen = np.zeros_like(mask)
    pieces = []
    for start in zip(*np.nonzero(mask)):
        if seen[start]:
            continue
        stack, piece = [start], set()
        seen[start] = True
        while stack:
            k, y, x = stack.pop()
            piece.add((k, y, x))
            for dk, dy, dx in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
                n = (k + dk, y + dy, x + dx)
                if all(0 <= n[i] < mask.shape[i] for i in range(3)) and mask[n] and not seen[n]:
                    seen[n] = True
                    stack.append(n)
        pieces.append(piece)
    return pieces


# -- grid tools -------------------------------------------------------------


@pytest.mark.parametrize("seed", range(6))
def test_components_match_a_flood_fill(seed):
    rng = np.random.default_rng(seed)
    mask = rng.random((4, 17, 23)) < 0.45
    labels = voxel.components(mask)
    assert (labels > 0).tolist() == mask.tolist()
    for piece in brute_components(mask):
        values = {int(labels[cell]) for cell in piece}
        assert len(values) == 1
    assert len(np.unique(labels[labels > 0])) == len(brute_components(mask))


def test_diagonal_contact_does_not_connect():
    mask = np.zeros((1, 3, 3), dtype=bool)
    mask[0, 0, 0] = mask[0, 1, 1] = True
    labels = voxel.components(mask)
    assert labels[0, 0, 0] != labels[0, 1, 1]


@pytest.mark.parametrize("radius", [0.0, 0.009, 0.01, 0.0141, 0.0142, 0.05, 0.123])
def test_within_is_exact_euclidean_distance(radius):
    rng = np.random.default_rng(1)
    seed = rng.random((2, 30, 40)) < 0.02
    got = voxel.within(seed, radius, 0.01, 0.011)
    ys, xs = np.mgrid[0:30, 0:40]
    for layer in range(2):
        sy, sx = np.nonzero(seed[layer])
        if sy.size == 0:
            assert not got[layer].any()
            continue
        d = np.sqrt(
            ((xs[..., None] - sx) * 0.01) ** 2 + ((ys[..., None] - sy) * 0.011) ** 2
        ).min(axis=-1)
        assert got[layer].tolist() == (d <= radius + 1e-7).tolist()


# -- processes --------------------------------------------------------------


def test_a_blanket_film_is_exactly_as_thick_as_asked():
    state = make_state([(0.0, 0.2, "Si")])
    voxel.deposit(state, "SiO2", 0.005, planar=False)
    assert state.top == pytest.approx(0.205)
    assert state.volume("SiO2") == pytest.approx(1.0 * 0.005)


def trench_state() -> voxel.VoxelState:
    """Si substrate with a 0.2 um oxide, cut by a trench 0.3 um wide."""
    grid = np.full((100, 100), 2, dtype=np.uint8)  # SiO2
    grid[:, 35:65] = voxel.VOID
    return make_state([(0.0, 0.2, "Si"), (0.2, 0.4, grid)])


def test_a_conformal_film_lines_the_walls_floor_and_top():
    state = trench_state()
    voxel.deposit(state, "TiN", 0.02, planar=False)
    tin = label(state, "TiN")
    # On the floor: 20 nm above the Si, the trench's middle columns.
    floor = [k for k in range(state.n) if state.z[k] >= 0.2 - 1e-9 and state.z[k + 1] <= 0.22 + 1e-9]
    assert floor and all((state.labels[k][:, 40:60] == tin).all() for k in floor)
    # On the wall halfway up: two cells (20 nm) inside each wall, not three.
    k = int(np.searchsorted(state.z, 0.3)) - 1
    row = state.labels[k][50]
    assert (row[35:37] == tin).all() and row[37] == voxel.VOID
    assert (row[63:65] == tin).all() and row[62] == voxel.VOID
    # On top: 20 nm over the oxide.
    assert state.top == pytest.approx(0.42)


def test_a_sidewall_film_thinner_than_a_cell_is_kept_one_cell_thick():
    state = trench_state()
    voxel.deposit(state, "TiN", 0.002, planar=False)
    k = int(np.searchsorted(state.z, 0.3)) - 1
    row = state.labels[k][50]
    tin = label(state, "TiN")
    assert row[35] == tin and row[36] == voxel.VOID


def test_a_planar_film_leaves_the_space_under_an_overhang_empty():
    # An oxide ledge at 0.3-0.4 over the left half of an empty 0.2-0.3 gap.
    ledge = np.zeros((100, 100), dtype=np.uint8)
    ledge[:, :50] = 2
    state = make_state([(0.0, 0.2, "Si"), (0.2, 0.3, np.zeros((100, 100))), (0.3, 0.4, ledge)])
    voxel.deposit(state, "TiN", 0.02, planar=True)
    tin = label(state, "TiN")
    k = int(np.searchsorted(state.z, 0.21)) - 1
    under, open_ = state.labels[k][:, :45], state.labels[k][:, 60:]
    assert not (under == tin).any()
    assert (open_ == tin).all()


def test_a_vertical_etch_stops_on_a_stop_layer_and_splits_a_partial_slab():
    state = make_state([(0.0, 0.2, "Si"), (0.2, 0.25, "SiN"), (0.25, 0.45, "SiO2")])
    opening = np.zeros((100, 100), dtype=bool)
    opening[40:60, 40:60] = True
    voxel.etch_vertical(state, {"SiO2": 1.0}, 0.5, opening)  # SiN has rate 0
    assert state.volume("SiO2") == pytest.approx(0.2 - 0.2 * 0.04)
    assert state.volume("SiN") == pytest.approx(0.05)
    state = make_state([(0.0, 0.2, "Si"), (0.2, 0.45, "SiO2")])
    voxel.etch_vertical(state, {"SiO2": 1.0}, 0.1, opening)
    assert 0.35 in [round(z, 9) for z in state.z]
    assert state.volume("SiO2") == pytest.approx(0.25 - 0.1 * 0.04)


def sandwich_state(open_both: bool = True):
    """SiO2 / SiN / SiO2 on Si, a trench through all three at x = 0.45-0.55."""
    def cut(name_id):
        grid = np.full((100, 100), name_id, dtype=np.uint8)
        grid[:, 45:55] = voxel.VOID
        return grid

    return make_state(
        [(0.0, 0.2, "Si"), (0.2, 0.25, cut(2)), (0.25, 0.3, cut(3)), (0.3, 0.35, cut(2))]
    )


def test_a_wet_etch_pulls_a_layer_back_from_the_trench_by_its_depth():
    state = sandwich_state()
    oxide_before = state.volume("SiO2")
    voxel.etch_isotropic(state, {"SiN": 1.0}, 0.1, None)
    sin = label(state, "SiN")
    k = int(np.searchsorted(state.z, 0.27)) - 1
    row = state.labels[k][50]
    # 10 cells pulled back on each side of the trench, the 11th kept.
    assert (row[35:45] == voxel.VOID).all() and row[34] == sin
    assert (row[55:65] == voxel.VOID).all() and row[65] == sin
    assert state.volume("SiO2") == pytest.approx(oxide_before)


def test_a_wet_etch_does_not_reach_a_layer_through_the_oxide_above_it():
    # A nitride sheet sealed on every side, 50 nm below the open trench's
    # nitride: within reach in a straight line, not along a path.
    sealed = np.full((100, 100), 3, dtype=np.uint8)
    open_ = np.full((100, 100), 3, dtype=np.uint8)
    open_[:, 45:55] = voxel.VOID
    oxide = np.full((100, 100), 2, dtype=np.uint8)
    oxide_cut = oxide.copy()
    oxide_cut[:, 45:55] = voxel.VOID
    state = make_state(
        [(0.0, 0.2, "Si"), (0.2, 0.25, sealed), (0.25, 0.3, oxide), (0.3, 0.35, open_), (0.35, 0.4, oxide_cut)]
    )
    voxel.etch_isotropic(state, {"SiN": 1.0}, 0.1, None)
    k = int(np.searchsorted(state.z, 0.22)) - 1
    assert (state.labels[k] == label(state, "SiN")).all()


def test_a_wet_etch_under_a_mask_starts_only_in_the_opening_and_creeps_under_it():
    oxide = np.full((100, 100), 2, dtype=np.uint8)
    state = make_state([(0.0, 0.2, "Si"), (0.2, 0.3, oxide)])
    opening = np.zeros((100, 100), dtype=bool)
    opening[:, 45:55] = True
    voxel.etch_isotropic(state, {"SiO2": 1.0}, 0.05, opening)
    top = state.labels[-1][50]
    assert (top[40:60] == voxel.VOID).all()  # 5 cells of undercut each side
    assert top[39] != voxel.VOID and top[60] != voxel.VOID
    assert state.labels[-1][50, 0] == label(state, "SiO2")


def test_the_etchant_goes_round_a_barrier_not_through_it():
    # A nitride U around an oxide block, opened over its left arm only. The
    # right arm is 0.1 away through the oxide but 0.9 along the nitride.
    base = np.full((100, 100), 2, dtype=np.uint8)
    base[:, 20:50] = 3
    arms = np.full((100, 100), 2, dtype=np.uint8)
    arms[:, 20:30] = 3
    arms[:, 40:50] = 3
    cap = np.full((100, 100), 2, dtype=np.uint8)
    cap[:, 20:30] = voxel.VOID
    state = make_state([(0.0, 0.2, "Si"), (0.2, 0.25, base), (0.25, 0.65, arms), (0.65, 0.7, cap)])
    voxel.etch_isotropic(state, {"SiN": 1.0}, 0.35, None)
    k = int(np.searchsorted(state.z, 0.6)) - 1
    assert (state.labels[k][50, 20:30] == voxel.VOID).all()  # the open arm goes
    assert (state.labels[k][50, 40:50] == label(state, "SiN")).all()  # the far one stays


def test_a_slow_material_is_etched_only_once_the_fast_one_has_exposed_it():
    # 50 nm of oxide at rate 1 over nitride at rate 0.1, for a time that
    # takes 150 nm of oxide: the nitride sees the etchant for 100 of it.
    state = make_state([(0.0, 0.2, "Si"), (0.2, 0.25, "SiN"), (0.25, 0.3, "SiO2")])
    voxel.etch_isotropic(state, {"SiO2": 1.0, "SiN": 0.1}, 0.15, None)
    assert state.volume("SiO2") == 0.0
    assert 0.05 - state.volume("SiN") == pytest.approx(0.01, abs=1e-9)


def test_a_cavity_the_etch_breaks_into_etches_from_inside():
    sheet = np.full((100, 100), 3, dtype=np.uint8)
    sheet[:, 40:60] = voxel.VOID  # sealed, in the middle of a nitride sheet
    upper = np.full((100, 100), 3, dtype=np.uint8)
    upper[:, 0:5] = voxel.VOID
    cap = np.full((100, 100), 2, dtype=np.uint8)
    cap[:, 0:5] = voxel.VOID  # the only way in, far to the left
    state = make_state([(0.0, 0.2, "Si"), (0.2, 0.25, sheet), (0.25, 0.3, upper), (0.3, 0.35, cap)])
    voxel.etch_isotropic(state, {"SiN": 1.0}, 0.5, None)
    k = int(np.searchsorted(state.z, 0.225)) - 1
    # Reached at x = 0.4 after 0.35; the remaining 0.15 goes on from there.
    left = int((state.labels[k][50, 60:] == label(state, "SiN")).sum())
    assert left == pytest.approx(25, abs=1)


def test_an_isotropic_front_is_round_in_z():
    hole = np.zeros((200, 200), dtype=bool)
    hole[99:101, 99:101] = True
    state = make_state([(0.0, 0.2, "Si"), (0.2, 0.6, "SiO2")], size=200)
    voxel.etch_isotropic(state, {"SiO2": 1.0}, 0.2, hole)
    k = int(np.searchsorted(state.z, 0.5)) - 1
    radius = int((state.labels[k][100] == voxel.VOID).sum()) * 0.005 / 2
    # A ball reaches sqrt(0.2^2 - 0.1^2) = 0.173 sideways 0.1 down; a box 0.2.
    assert radius == pytest.approx(0.173, abs=0.012)


def test_open_holes_outside_the_mask_are_under_resist():
    grid = np.full((100, 100), 3, dtype=np.uint8)
    grid[:, 10:20] = voxel.VOID  # an open hole, outside the mask
    grid[:, 70:80] = voxel.VOID  # an open hole, inside it
    state = make_state([(0.0, 0.2, "Si"), (0.2, 0.3, grid)])
    opening = np.zeros((100, 100), dtype=bool)
    opening[:, 60:90] = True
    voxel.etch_isotropic(state, {"SiN": 1.0}, 0.03, opening)
    k = state.n - 1
    assert state.labels[k][50, 9] == label(state, "SiN")  # beside the covered hole
    assert state.labels[k][50, 20] == label(state, "SiN")
    assert state.labels[k][50, 68] == voxel.VOID  # beside the open one


def test_a_sealed_cavity_is_not_an_etchant_source():
    grid = np.full((100, 100), 2, dtype=np.uint8)
    grid[40:60, 40:60] = voxel.VOID  # a buried cavity
    state = make_state([(0.0, 0.2, "Si"), (0.2, 0.3, grid), (0.3, 0.4, "SiO2")])
    before = state.volume("SiO2")
    voxel.etch_isotropic(state, {"SiO2": 1.0}, 0.02, None)
    # Only the top 20 nm goes; the cavity walls stay.
    assert state.volume("SiO2") == pytest.approx(before - 0.02)
    k = int(np.searchsorted(state.z, 0.25)) - 1
    assert state.labels[k][50, 39] == label(state, "SiO2")


def test_oxidation_relabels_the_skin_without_changing_the_volume():
    state = sandwich_state()
    total = state.volume("SiN") + state.volume("SiO2")
    voxel.etch_isotropic(state, {"SiN": 1.0}, 0.03, None, product="SiO2")
    assert state.volume("SiN") + state.volume("SiO2") == pytest.approx(total)
    assert state.volume("SiN") == pytest.approx(0.05 * (0.9 - 2 * 0.03))


def test_cmp_and_flip():
    state = make_state([(0.0, 0.2, "Si"), (0.2, 0.25, "SiN"), (0.25, 0.45, "SiO2")])
    voxel.cmp(state, 0.3)
    assert state.top == pytest.approx(0.3)
    assert state.volume("SiO2") == pytest.approx(0.05)
    marked = state.copy()
    marked.labels[-1][:, :10] = voxel.VOID
    before = {name: marked.volume(name) for name in ("Si", "SiN", "SiO2")}
    voxel.flip(marked, "y")
    assert {name: marked.volume(name) for name in before} == pytest.approx(before)
    assert marked.labels[0][0, -1] == voxel.VOID  # mirrored in x, now at the bottom
    assert marked.labels[-1][0, 0] == label(marked, "Si")


# -- storage and views ------------------------------------------------------


def test_a_state_round_trips_and_the_kernel_tells_it_from_a_polygon_state(tmp_path):
    state = sandwich_state()
    path = tmp_path / "state.dfz"
    state.save(path)
    loaded = get_kernel("slab").load_state(path)
    assert isinstance(loaded, voxel.VoxelState)
    assert loaded.labels.tolist() == state.labels.tolist()
    assert loaded.z.tolist() == state.z.tolist()
    assert loaded.materials == state.materials


@pytest.mark.parametrize("buried", [False, True])
def test_meshes_are_closed_and_face_outward(buried):
    state = sandwich_state()
    voxel.etch_isotropic(state, {"SiN": 1.0}, 0.1, None)
    voxel.deposit(state, "TiN", 0.02, planar=False)
    meshes = voxel.build_meshes(state, buried=True)
    for name, (vertices, faces, interface, neighbour) in meshes.items():
        a, b, c = (vertices[faces[:, i]].astype(np.float64) for i in range(3))
        signed = np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0
        assert signed == pytest.approx(state.volume(name), rel=1e-6), name
        assert len(interface) == len(faces) == len(neighbour)
    free = voxel.build_meshes(state, buried=False)
    for name in free:
        assert not free[name][2].any()


def test_views_draw():
    state = sandwich_state()
    colors = {"Si": "#888888", "SiO2": "#99ccff", "SiN": "#ffcc44"}
    top = voxel.top_view(state, colors, _rgb)
    image = Image.open(io.BytesIO(base64.b64decode(top["image"])))
    assert image.size == (top["width"], top["height"])
    section = voxel.section(state, colors, _rgb, z_max=0.3, axis="y", position=0.5)
    assert base64.b64decode(section["image"])[:4] == b"\x89PNG"
    along = voxel.section(state, colors, _rgb, z_max=0.3, line=((0.0, 0.0), (1.0, 1.0)))
    assert along["axis"] == "line"


# -- through the worker -----------------------------------------------------


def call(method, **params):
    return dispatch({"method": method, "params": params}, io.StringIO())


def test_a_voxel_project_runs_beside_the_other_film_models(tmp_path):
    root = str(tmp_path / "voxel")
    document = call("create_workspace", root=root, name="Voxel", kernel="slab")
    branch = document["branches"][0]
    first = branch["steps"][0]["id"]
    call("run_flow", root=root, branchId=branch["id"])
    document["project"]["fidelity"] = "voxel"
    switched = call("save_document", root=root, document=document)
    assert switched["stepStatuses"][branch["id"]][first] == "dirty"
    ran = call("run_flow", root=root, branchId=branch["id"])
    assert len(ran["executedStepIds"]) == len(branch["steps"])
    assert ran["materials"] == ["Si", "Al2O3"]
    last = branch["steps"][-1]["id"]
    surfaces = call("get_surfaces", root=root, branchId=branch["id"], stepId=last)
    assert [surface["material"] for surface in surfaces["surfaces"]] == ["Si", "Al2O3"]
    for kind, extra in (("get_section", {"axis": "y"}), ("get_top_view", {})):
        payload = call(kind, root=root, branchId=branch["id"], stepId=last, **extra)
        assert base64.b64decode(payload["image"])[:4] == b"\x89PNG"
    # The simplified results are still there.
    document["project"]["fidelity"] = "simplified"
    back = call("save_document", root=root, document=document)
    assert back["stepStatuses"][branch["id"]][last] == "clean"


@pytest.mark.parametrize("radius", [0.3, 0.4, 0.61])
def test_a_long_reach_takes_the_envelope_and_stays_exact(radius, monkeypatch):
    monkeypatch.setattr(voxel, "ENVELOPE_ROWS", 24)
    assert radius / 0.011 > voxel.ENVELOPE_ROWS
    rng = np.random.default_rng(2)
    seed = rng.random((2, 60, 50)) < 0.002
    seed[1] = False
    got = voxel.within(seed, radius, 0.01, 0.011)
    ys, xs = np.mgrid[0:60, 0:50]
    sy, sx = np.nonzero(seed[0])
    d = np.sqrt(((xs[..., None] - sx) * 0.01) ** 2 + ((ys[..., None] - sy) * 0.011) ** 2).min(axis=-1)
    assert got[0].tolist() == (d <= radius + 1e-7).tolist()
    assert not got[1].any()


def _unmatched_edges(vertices, faces):
    """Directed edges with no edge running the other way: 0 for a closed,
    conforming surface -- a T-junction leaves the long edge unmatched."""
    q = np.round(vertices.astype(np.float64) / 1e-7).astype(np.int64)
    pairs = ((0, 1), (1, 2), (2, 0))
    forward = np.concatenate([np.concatenate([q[faces[:, a]], q[faces[:, b]]], 1) for a, b in pairs])
    backward = np.concatenate([np.concatenate([q[faces[:, b]], q[faces[:, a]]], 1) for a, b in pairs])
    as_rows = lambda array: array.view([("", array.dtype)] * 6).ravel()
    return int((~np.isin(as_rows(backward), as_rows(forward))).sum())


def test_the_display_mesh_has_no_t_junctions():
    # Round holes and films: rows of cells starting at different places,
    # which is what used to leave corners part-way along long edges.
    grid = np.full((100, 100), 2, dtype=np.uint8)
    yy, xx = np.mgrid[0:100, 0:100]
    grid[(xx - 50) ** 2 + (yy - 50) ** 2 < 23**2] = voxel.VOID
    state = make_state([(0.0, 0.2, "Si"), (0.2, 0.3, grid)])
    voxel.deposit(state, "TiN", 0.02, planar=False)
    voxel.etch_isotropic(state, {"SiO2": 1.0}, 0.03, None)
    for name, (vertices, faces, _interface, _neighbour) in voxel.build_meshes(state, buried=True).items():
        assert _unmatched_edges(vertices, faces) == 0, name
    # And across materials: everything drawn while all are shown is one
    # closed surface, the outside of the whole stack.
    free = voxel.build_meshes(state, buried=False)
    offsets = np.cumsum([0] + [len(m[0]) for m in free.values()])[:-1]
    vertices = np.concatenate([m[0] for m in free.values()])
    faces = np.concatenate([m[1].astype(np.int64) + o for m, o in zip(free.values(), offsets)])
    assert _unmatched_edges(vertices, faces) == 0
