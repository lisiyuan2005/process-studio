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
    row = state.to_fine()[k][50 * state.refine]
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


def test_a_channel_under_the_resist_fills_from_a_hole_in_the_opening():
    # An oxide layer with a buried channel along x, under a nitride cap that
    # is open only over the channel's right end, inside the mask's opening.
    # The resist lies on the cap; it does not get into the channel, so the
    # etchant runs along it and etches the oxide round it under the resist.
    oxide = np.full((100, 100), 2, dtype=np.uint8)
    oxide[45:55, 20:80] = voxel.VOID
    cap = np.full((100, 100), 3, dtype=np.uint8)
    cap[45:55, 70:80] = voxel.VOID
    state = make_state([(0.0, 0.2, "Si"), (0.2, 0.25, oxide), (0.25, 0.3, cap)])
    opening = np.zeros((100, 100), dtype=bool)
    opening[:, 60:] = True
    voxel.etch_isotropic(state, {"SiO2": 1.0}, 0.03, opening)
    state.check()
    k = int(np.searchsorted(state.z, 0.225)) - 1
    row = state.labels[k][50]
    assert (state.labels[k][42:45, 30] == voxel.VOID).all()  # beside the channel, under resist
    assert row[17] == voxel.VOID and row[16] == label(state, "SiO2")  # 3 cells past its far end
    assert state.labels[k][40, 30] == label(state, "SiO2")


def test_a_masked_deposition_coats_a_channel_under_the_resist_but_not_a_hole_it_fills():
    oxide = np.full((100, 100), 2, dtype=np.uint8)
    oxide[45:55, 20:80] = voxel.VOID  # a buried channel, reached from the opening
    cap = np.full((100, 100), 3, dtype=np.uint8)
    cap[45:55, 70:80] = voxel.VOID  # its way up, inside the opening
    cap[10:20, 10:20] = voxel.VOID  # a hole open straight up, under the resist
    oxide[10:20, 10:20] = voxel.VOID
    state = make_state([(0.0, 0.2, "Si"), (0.2, 0.25, oxide), (0.25, 0.3, cap)])
    opening = np.zeros((100, 100), dtype=bool)
    opening[:, 60:] = True
    voxel.deposit(state, "TiN", 0.02, planar=False, opening=opening)
    state.check()
    tin = label(state, "TiN")
    k = int(np.searchsorted(state.z, 0.225)) - 1
    assert (state.labels[k][45:47, 30] == tin).all()  # on the channel wall, under resist
    assert state.labels[k][50, 30] == voxel.VOID  # a 50 nm channel, 20 nm each side
    assert (state.labels[k][10:20, 10:20] == voxel.VOID).all()  # the resist was in there


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


# -- the two-level grid -----------------------------------------------------


def fine_state(fine, refine, z, extent=1.0):
    return voxel.VoxelState.from_fine(
        (0.0, 0.0, extent, extent), fine, refine, np.array(z, float), ["Si", "SiO2", "SiN", "TiN"]
    )


def test_bricks_round_trip_and_stay_canonical(tmp_path):
    rng = np.random.default_rng(3)
    fine = np.zeros((3, 32, 32), np.uint8)
    fine[0] = 1
    fine[1, :, :13] = 2  # an edge that cuts through cells
    fine[2] = (rng.random((32, 32)) < 0.1) * 3
    state = fine_state(fine, 4, [0.0, 0.1, 0.2, 0.3])
    state.check()
    assert state.brick_keys.size > 0 and state.labels[0].max() == 1  # slab 0 needs no bricks
    assert np.array_equal(state.to_fine(), fine)
    assert state.volume("SiO2") == pytest.approx(13 / 32 * 0.1)
    path = tmp_path / "s.dfz"
    state.save(path)
    back = voxel.VoxelState.load(path)
    assert back.refine == 4 and np.array_equal(back.to_fine(), fine)


def test_splitting_and_merging_slabs_carries_the_bricks():
    fine = np.zeros((2, 16, 16), np.uint8)
    fine[0] = 1
    fine[1, 3:11, 5:7] = 2
    state = fine_state(fine, 4, [0.0, 0.1, 0.3])
    before = state.to_fine()
    source = state.split([0.15, 0.2, 0.25])
    assert list(source) == [0, 1, 1, 1, 1]
    state.check()
    assert all(np.array_equal(state.to_fine()[k], before[source[k]]) for k in range(state.n))
    state.consolidate()
    state.check()
    assert state.n == 2 and np.array_equal(state.to_fine(), before)


def test_sampling_reads_the_fine_level():
    fine = np.zeros((1, 8, 8), np.uint8)
    fine[0, 2, 5] = 3
    state = fine_state(fine, 4, [0.0, 0.1])
    x = (5 + 0.5) / 8
    y = (2 + 0.5) / 8
    assert state.sample(0, x, y) == 3
    assert state.sample(0, x + 1 / 8, y) == 0


def plain_twin(state):
    """The same grid at the fine resolution, with no bricks: the reference."""
    return voxel.VoxelState.from_fine(
        state.bounds, state.to_fine(), 1, state.z.copy(), state.materials, state.z_offset
    )


def assert_same(two_level, plain):
    two_level.check()
    assert np.allclose(two_level.z, plain.z)
    assert np.array_equal(two_level.to_fine(), plain.to_fine())


def test_a_mask_is_rasterized_finely_along_its_outline():
    from shapely.geometry import Point

    state = fine_state(np.zeros((1, 64, 64), np.uint8), 8, [0.0, 0.1])
    disc = Point(0.43, 0.51).buffer(0.21, quad_segs=32)
    two = voxel.rasterize(state, disc)
    ref = voxel.rasterize(plain_twin(state), disc)
    assert np.array_equal(two.to_fine(8), ref.to_fine(1))
    assert two.cells.size < 64  # only along the outline


def layered_state(refine=4):
    """Oxide over nitride over silicon, with a round hole in the oxide."""
    size = 16 * refine
    fine = np.zeros((3, size, size), np.uint8)
    fine[0] = 1
    fine[1] = 3
    fine[2] = 2
    yy, xx = np.mgrid[0:size, 0:size]
    fine[2][(xx - size * 0.45) ** 2 + (yy - size * 0.55) ** 2 < (size * 0.23) ** 2] = voxel.VOID
    return fine_state(fine, refine, [0.0, 0.2, 0.25, 0.4])


def test_a_vertical_etch_on_two_levels_matches_the_fine_grid():
    from shapely.geometry import box

    state = layered_state()
    plain = plain_twin(state)
    square = box(0.2, 0.3, 0.71, 0.77)
    voxel.etch_vertical(state, {"SiO2": 1.0, "SiN": 0.5}, 0.17, voxel.rasterize(state, square))
    voxel.etch_vertical(plain, {"SiO2": 1.0, "SiN": 0.5}, 0.17, voxel.rasterize(plain, square))
    assert_same(state, plain)


def test_cmp_and_flip_on_two_levels_match_the_fine_grid():
    state, plain = layered_state(), None
    plain = plain_twin(state)
    for st in (state, plain):
        voxel.cmp(st, 0.33)
        voxel.flip(st, "y")
    assert_same(state, plain)
    for st in (state, plain):
        voxel.flip(st, "x")
    assert_same(state, plain)


def _distance_to_cells(solid2d, fx, fy):
    """Brute force: each cell centre's distance to the union of the solid cells'
    squares (from outside, the squares on the solid's edge are enough)."""
    edge = solid2d.copy()
    edge[1:-1, 1:-1] &= ~(solid2d[:-2, 1:-1] & solid2d[2:, 1:-1] & solid2d[1:-1, :-2] & solid2d[1:-1, 2:])
    ys, xs = np.nonzero(edge)
    gy, gx = np.mgrid[0 : solid2d.shape[0], 0 : solid2d.shape[1]]
    px = (gx + 0.5)[..., None] * fx
    py = (gy + 0.5)[..., None] * fy
    ddx = np.maximum(np.maximum(xs * fx - px, px - (xs + 1) * fx), 0.0)
    ddy = np.maximum(np.maximum(ys * fy - py, py - (ys + 1) * fy), 0.0)
    return np.sqrt(ddx**2 + ddy**2).min(axis=-1)


@pytest.mark.parametrize("thickness", [0.013, 0.031, 0.08])
def test_a_film_on_two_levels_is_exact_to_the_fine_cell(thickness):
    # A round pillar through the stack: at mid-height the film is every
    # open fine cell within the thickness of the pillar, exactly.
    B, size = 8, 24
    fine = np.zeros((2, size * B, size * B), np.uint8)
    fine[0] = 1
    yy, xx = np.mgrid[0 : size * B, 0 : size * B]
    pillar = (xx - size * B * 0.47) ** 2 + (yy - size * B * 0.52) ** 2 < (size * B * 0.18) ** 2
    fine[1][pillar] = 2
    state = fine_state(fine, B, [0.0, 0.1, 0.5])
    plain = plain_twin(state)
    voxel.deposit(state, "TiN", thickness, planar=False)
    voxel.deposit(plain, "TiN", thickness, planar=False)
    assert_same(state, plain)
    k = int(np.searchsorted(state.z, 0.3)) - 1
    got = state.to_fine()[k] == state.known_id("TiN")
    f = 1.0 / (size * B)
    ys, xs = np.nonzero(pillar)
    gy, gx = np.mgrid[0 : size * B, 0 : size * B]
    edge = pillar.copy()
    edge[1:-1, 1:-1] &= ~(pillar[:-2, 1:-1] & pillar[2:, 1:-1] & pillar[1:-1, :-2] & pillar[1:-1, 2:])
    ey, ex = np.nonzero(edge)
    between = np.sqrt(((gx[..., None] - ex) * f) ** 2 + ((gy[..., None] - ey) * f) ** 2).min(axis=-1)
    expected = (between <= thickness + 0.5 * f + 1e-12) & ~pillar
    assert np.array_equal(got, expected)
    assert state.brick_keys.size < 0.5 * state.n * state.plane  # refined only along edges


def test_a_planar_film_on_two_levels_matches_the_fine_grid():
    B, size = 4, 20
    fine = np.zeros((3, size * B, size * B), np.uint8)
    fine[0] = 1
    fine[1, :, : size * B // 2 + 3] = 2  # a wall with an edge inside a cell
    fine[2, 10:37, 13:51] = 3  # an overhang
    state = fine_state(fine, B, [0.0, 0.1, 0.2, 0.25])
    plain = plain_twin(state)
    voxel.deposit(state, "TiN", 0.021, planar=True)
    voxel.deposit(plain, "TiN", 0.021, planar=True)
    assert_same(state, plain)


def _same_at_common_heights(two_level, plain):
    """Fine labels equal everywhere, compared slab piece by slab piece (the
    two grids may cut the stack at different heights)."""
    two_level.check()
    a, b = two_level.to_fine(), plain.to_fine()
    zs = np.union1d(two_level.z, plain.z)
    mids = 0.5 * (zs[:-1] + zs[1:])
    ka = np.searchsorted(two_level.z, mids, side="right") - 1
    kb = np.searchsorted(plain.z, mids, side="right") - 1
    assert np.array_equal(a[ka], b[kb])


def wet_both(state, rates, budget, mask=None):
    plain = plain_twin(state)
    for st in (state, plain):
        opening = None if mask is None else voxel.rasterize(st, mask)
        voxel.etch_isotropic(st, rates, budget, opening, dz=0.05)
    return plain


def test_a_wet_etch_on_two_levels_rounds_a_hole_as_the_fine_grid_does():
    # SiN pulled back from a round hole: the rim lies in bulk cells, and each
    # fine row there must still measure to the nearest step of the hole's wall.
    B, size = 8, 24
    S = size * B
    yy, xx = np.mgrid[0:S, 0:S]
    hole = (xx - S * 0.47) ** 2 + (yy - S * 0.52) ** 2 < (S * 0.13) ** 2
    fine = np.zeros((4, S, S), np.uint8)
    fine[0], fine[1], fine[2], fine[3] = 1, 2, 3, 2
    fine[1:, hole] = voxel.VOID
    state = fine_state(fine, B, [0.0, 0.2, 0.25, 0.3, 0.35])
    plain = wet_both(state, {"SiN": 1.0}, 0.1)
    _same_at_common_heights(state, plain)
    k = int(np.searchsorted(state.z, 0.275)) - 1
    f = 1.0 / S
    d = np.hypot((xx + 0.5) * f - 0.47, (yy + 0.5) * f - 0.52)
    got = state.to_fine()[k] == voxel.VOID
    assert got[d <= 0.13 + 0.1 - 0.5 * f].all() and not got[d > 0.13 + 0.1 + 1.5 * f].any()


def test_a_wet_etch_on_two_levels_runs_along_a_wall_thinner_than_a_cell():
    B, size = 8, 24
    S = size * B
    fine = np.zeros((3, S, S), np.uint8)
    fine[0], fine[1], fine[2] = 1, 2, 2
    fine[1, 100:103, 20:180] = 3  # three fine cells wide
    fine[2, 100:103, 20:30] = voxel.VOID  # opened above its left end
    state = fine_state(fine, B, [0.0, 0.2, 0.3, 0.35])
    plain = wet_both(state, {"SiN": 1.0}, 0.25)
    _same_at_common_heights(state, plain)
    assert (state.to_fine()[1][101, 20:60] == voxel.VOID).all()


def test_a_wet_etch_on_two_levels_does_not_leak_through_a_thin_barrier():
    B, size = 8, 24
    S = size * B
    fine = np.zeros((3, S, S), np.uint8)
    fine[0], fine[1], fine[2] = 1, 3, 2
    fine[1, :, 97:99] = 4  # TiN, two fine cells, inside refined cells
    fine[1:, :, 10:20] = voxel.VOID
    state = fine_state(fine, B, [0.0, 0.2, 0.3, 0.35])
    plain = wet_both(state, {"SiN": 1.0}, 0.6)
    _same_at_common_heights(state, plain)
    assert (state.to_fine()[1][:, 99:] == 3).all()


def test_a_masked_wet_etch_on_two_levels_matches_the_fine_grid():
    from shapely.geometry import box

    B, size = 4, 20
    S = size * B
    fine = np.zeros((3, S, S), np.uint8)
    fine[0], fine[1], fine[2] = 1, 3, 2
    state = fine_state(fine, B, [0.0, 0.2, 0.3, 0.4])
    plain = wet_both(state, {"SiO2": 1.0}, 0.07, mask=box(0.31, 0.27, 0.58, 0.64))
    _same_at_common_heights(state, plain)


def test_a_front_that_crosses_into_a_slower_layer_lands_near_the_exact_place():
    # Oxide over nitride under a mask: the oxide is undercut and uncovers
    # the nitride as it goes. The path bends where it crosses; both grids
    # place the crossing at a face between cells, so allow two fine cells.
    from shapely.geometry import box

    B, size = 4, 20
    S = size * B
    fine = np.zeros((3, S, S), np.uint8)
    fine[0], fine[1], fine[2] = 1, 3, 2
    state = fine_state(fine, B, [0.0, 0.2, 0.3, 0.33])
    wet_both(state, {"SiO2": 1.0, "SiN": 0.5}, 0.12, mask=box(0.31, 0.27, 0.58, 0.64))
    k = int(np.searchsorted(state.z, 0.275)) - 1
    row = state.to_fine()[k][S // 2]
    last = int(np.flatnonzero(row == voxel.VOID).max())
    # exact: oxide reaches the nitride d past the mask edge at sqrt(d^2 + 0.03^2)
    d = np.linspace(0.0, 0.2, 20001)
    exact = max(
        c for c in range(S)
        if (np.sqrt(d**2 + 0.03**2) + 2 * np.sqrt(((c + 0.5) / S - 0.58 - d) ** 2 + 0.025**2)).min() <= 0.12
    )
    assert exact - 2 <= last <= exact


def _mesh_checks(state):
    """Closed, outward, as much volume as the state holds, and conforming --
    per material, and across materials for what is drawn with all shown."""
    meshes = voxel.build_meshes(state, buried=True)
    assert set(meshes) == set(state.present())
    for name, (vertices, faces, _interface, _neighbour) in meshes.items():
        a, b, c = (vertices[faces[:, i]].astype(np.float64) for i in range(3))
        signed = np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0
        assert signed == pytest.approx(state.volume(name), rel=1e-5), name
        assert _unmatched_edges(vertices, faces) == 0, name
    free = voxel.build_meshes(state, buried=False)
    offsets = np.cumsum([0] + [len(m[0]) for m in free.values()])[:-1]
    vertices = np.concatenate([m[0] for m in free.values()])
    faces = np.concatenate([m[1].astype(np.int64) + o for m, o in zip(free.values(), offsets)])
    assert _unmatched_edges(vertices, faces) == 0
    return meshes


def test_a_two_level_mesh_is_closed_conforming_and_holds_the_volume():
    state = layered_state(refine=4)
    voxel.deposit(state, "TiN", 0.013, planar=False)
    assert state.brick_keys.size
    _mesh_checks(state)
    # a film in refined cells only, on the window's edge too
    fine = np.zeros((2, 32, 32), np.uint8)
    fine[0] = 1
    fine[1, :, :1] = 3
    fine[1, 5:9, 13] = 2
    _mesh_checks(fine_state(fine, 4, [0.0, 0.1, 0.2]))


def test_pictures_of_a_fine_grid_are_drawn_coarser_by_the_majority(monkeypatch):
    state = layered_state(refine=4)
    monkeypatch.setattr(voxel, "SHOWN_CELLS", 32)
    small = voxel.shown(state, 32)
    assert small.refine == 2 and small is voxel.shown(state, 32)
    small.check()
    for k in range(state.n):
        fine = state.to_fine()[k].reshape(32, 2, 32, 2).transpose(0, 2, 1, 3).reshape(32, 32, 4)
        coarse = small.to_fine()[k]
        # every shown cell holds a label that has the most fine cells in it
        counts = np.stack([(fine == label).sum(axis=2) for label in range(5)], axis=2)
        assert (counts[np.arange(32)[:, None], np.arange(32)[None, :], coarse] == counts.max(axis=2)).all()
    _mesh_checks(small)


def test_views_of_a_two_level_state_match_its_fine_twin():
    state = layered_state(refine=4)
    plain = plain_twin(state)
    colors = {"Si": "#888888", "SiO2": "#99ccff", "SiN": "#ffcc44"}
    for view in (
        lambda st: voxel.top_view(st, colors, _rgb),
        lambda st: voxel.section(st, colors, _rgb, z_max=0.5, axis="y", position=0.53),
        lambda st: voxel.section(st, colors, _rgb, z_max=0.5, axis="x", position=0.41),
        lambda st: voxel.section(st, colors, _rgb, z_max=0.5, line=((0.1, 0.2), (0.9, 0.7))),
    ):
        a, b = view(state), view(plain)
        assert a["image"] == b["image"]


def test_boundaries_are_refined_to_the_resolution_in_powers_of_two(monkeypatch):
    from process_studio.kernels import slab

    monkeypatch.setattr(slab, "_window", lambda project: (0.0, 0.0, 1.6, 1.6))
    for resolution, refine in ((0.025, 1), (0.003125, 1), (0.003, 2), (0.001, 4), (0.0001, 32), (1e-6, 64)):
        monkeypatch.setattr(slab, "resolution_xy_um", lambda project, r=resolution: r)
        cell, got = voxel.grid_size(None)
        assert cell == pytest.approx(1.6 / 512) and got == refine, resolution


# -- z at the fine resolution -------------------------------------------------


def test_a_level_front_is_cut_at_its_exact_height():
    # Oxide etched from the top through a slit: the floor is flat, at exactly
    # the depth, whatever the slabs the march used.
    from shapely.geometry import box

    B, size = 4, 24
    fine = np.zeros((2, size * B, size * B), np.uint8)
    fine[0], fine[1] = 1, 2
    state = fine_state(fine, B, [0.0, 0.2, 0.5])
    opening = voxel.rasterize(state, box(0.4, 0.0, 0.6, 1.0))
    voxel.etch_isotropic(state, {"SiO2": 1.0}, 0.0937, opening, dz=state.cell, fine_z=state.cell / 8)
    state.check()
    assert np.isclose(state.z, 0.5 - 0.0937).any()
    k = int(np.searchsorted(state.z, 0.5 - 0.0937 + 1e-6)) - 1
    middle = int(0.5 / state.fine)
    assert state.sample_fine(k, middle, middle) == voxel.VOID
    assert state.sample_fine(k - 1, middle, middle) == state.known_id("SiO2")


def test_a_curved_front_is_placed_at_the_fine_z_step():
    # A round opening: every fine cell of every thin slab is taken exactly when
    # its centre is within the etch distance of the opening (to half a fine
    # cell's diagonal), and thin slabs are only where the front curves.
    from shapely.geometry import Point

    B, size = 8, 24
    S = B * size
    fine = np.zeros((2, S, S), np.uint8)
    fine[0], fine[1] = 1, 2
    state = fine_state(fine, B, [0.0, 0.2, 0.5])
    fz = state.cell / 8
    opening = voxel.rasterize(state, Point(0.47, 0.52).buffer(0.1, quad_segs=64))
    voxel.etch_isotropic(state, {"SiO2": 1.0}, 0.12, opening, dz=state.cell, fine_z=fz)
    state.check()
    thick = np.diff(state.z)
    # thin where it curves (equal neighbours merge where the wall is near upright)
    assert (thick[state.z[:-1] >= 0.38 - 1e-9] <= fz * (1 + 1e-6)).sum() >= 8
    assert thick[(state.z[:-1] < 0.38 - 1e-9) & (state.z[:-1] >= 0.2)].min() > 0.1  # whole below
    f = state.fine
    centres = (np.arange(S) + 0.5) * f
    X, Y = np.meshgrid(centres, centres)
    oy, ox = np.nonzero(opening.to_fine(B))
    gap_x = np.maximum(np.abs(X[..., None] - (ox + 0.5) * f) - f / 2, 0)
    gap_y = np.maximum(np.abs(Y[..., None] - (oy + 0.5) * f) - f / 2, 0)
    sideways = (gap_x**2 + gap_y**2).min(axis=-1)
    slack = 0.5 * np.sqrt(2 * f * f + fz * fz)
    grid = state.to_fine()
    for k in range(state.n):
        middle = 0.5 * (state.z[k] + state.z[k + 1])
        if not 0.2 < middle < 0.5:
            continue
        d = np.sqrt(sideways + (0.5 - middle) ** 2)
        taken = grid[k] == voxel.VOID
        assert taken[d < 0.12 - slack].all() and not taken[d > 0.12 + slack].any(), k


def test_equal_bricks_are_stored_once():
    fine = np.zeros((3, 32, 32), np.uint8)
    fine[:, :, :13] = 2
    fine[:, :, 13:] = 1
    state = fine_state(fine, 8, [0.0, 0.1, 0.2, 0.3])
    assert state.brick_keys.size == 3 * 4 and len(state.pool) == 1
    flipped = state.copy()
    voxel.flip(flipped, "y")  # the three equal slabs become one
    flipped.check()
    assert flipped.n == 1 and np.array_equal(flipped.to_fine()[0], fine[0][:, ::-1])
    assert len(state.pool) == 1  # the copy shared the pool and did not write it


def test_longer_etches_take_more_and_keep_what_shorter_ones_took():
    # A stack with a barrier and a sealed cavity under a round opening, etched
    # for longer and longer (the frames of an animation).
    from shapely.geometry import Point

    B, n = 4, 32
    S = n * B
    centres = (np.arange(S) + 0.5) / S
    X, Y = np.meshgrid(centres, centres)
    lower = np.full((S, S), 2, np.uint8)
    lower[(X >= 0.34) & (X < 0.36)] = 4
    cavity = np.full((S, S), 2, np.uint8)
    cavity[(X >= 0.66) & (X < 0.78) & (Y >= 0.40) & (Y < 0.60)] = voxel.VOID
    fine = np.stack([np.full((S, S), 1, np.uint8), lower, np.full((S, S), 3, np.uint8),
                     np.full((S, S), 2, np.uint8), cavity, np.full((S, S), 2, np.uint8)])
    start = fine_state(fine, B, [0.0, 0.20, 0.32, 0.34, 0.40, 0.48, 0.56])
    before = None
    for budget in (0.005, 0.05, 0.15, 0.3):
        state = start.copy()
        opening = voxel.rasterize(state, Point(0.5, 0.5).buffer(0.06, quad_segs=32))
        voxel.etch_isotropic(state, {"SiO2": 1.0, "SiN": 0.3}, budget, opening, dz=state.cell, fine_z=state.cell / 4)
        state.check()
        # thin slabs at the fine z step where the front curves, not slivers of them
        assert state.n < 2 * (0.56 - 0.2) / (state.cell / 4)
        heights = np.linspace(0.001, 0.559, 280)
        k = np.searchsorted(state.z, heights) - 1
        void = np.stack([state.to_fine()[kk] == voxel.VOID for kk in k])
        if before is not None:
            assert (void | ~before).all() and void.sum() > before.sum()
        before = void
        assert state.volume("TiN") == start.volume("TiN")


def test_a_round_etch_into_one_material_is_the_bowl_it_should_be():
    # A slab of one material is one square as wide as the window: finding
    # where the front lies level must not look at every cell of it with
    # every face (that ran out of memory), and the bowl is still right.
    import math

    from shapely.geometry import Point

    S = 256
    fine = np.full((1, S, S), 1, np.uint8)
    state = fine_state(fine, 1, [0.0, 0.5])
    before = state.volume("Si")
    R, r = 0.15, 0.12
    opening = voxel.rasterize(state, Point(0.5, 0.5).buffer(R, quad_segs=64))
    voxel.etch_isotropic(state, {"Si": 1.0}, r, opening, dz=0.01, fine_z=0.01)
    state.check()
    exact = math.pi * R * R * r + math.pi**2 * R * r * r / 2 + 2 / 3 * math.pi * r**3
    assert before - state.volume("Si") == pytest.approx(exact, rel=0.02)


@pytest.mark.parametrize("fraction", [0.0, 1.0])
def test_a_stop_is_honoured_inside_an_etch(fraction):
    from shapely.geometry import Point

    from process_studio.worker.errors import Cancelled

    B, size = 4, 16
    fine = np.zeros((2, B * size, B * size), np.uint8)
    fine[0], fine[1] = 1, 2
    state = fine_state(fine, B, [0.0, 0.2, 0.4])
    opening = voxel.rasterize(state, Point(0.5, 0.5).buffer(0.2))
    with pytest.raises(Cancelled):
        if fraction == 0.0:
            voxel.etch_isotropic(state, {"SiO2": 1.0}, 0.1, opening, should_cancel=lambda: True)
        else:
            voxel.etch_vertical(state, {"SiO2": 1.0}, 0.1, opening, should_cancel=lambda: True)


def _round_hole(B=4, size=32, R=0.3):
    from shapely.geometry import Point

    S = B * size
    fine = np.zeros((2, S, S), np.uint8)
    fine[0], fine[1] = 1, 2
    state = fine_state(fine, B, [0.0, 0.2, 0.4])
    voxel.etch_vertical(state, {"SiO2": 1.0}, 1.0, voxel.rasterize(state, Point(0.5, 0.5).buffer(R, quad_segs=256)))
    return state


def test_a_round_hole_is_drawn_with_cuts_that_follow_the_circle():
    from process_studio.kernels import voxel_cut

    R = 0.3
    state = _round_hole(R=R)
    state.check()
    k = state.n - 1
    cuts = state.to_fine_cuts()[k]
    yy, xx = np.nonzero(cuts)
    assert yy.size > 50
    code = voxel_cut.code_of(cuts[yy, xx])
    assert np.unique(code).size > 8  # not only the axis lines
    a, b = voxel_cut.START[code], voxel_cut.END[code]
    f = state.fine
    for t in np.linspace(0.0, 1.0, 5):
        px = (xx + voxel_cut.ANCHORS[a, 0] * (1 - t) + voxel_cut.ANCHORS[b, 0] * t) * f
        py = (yy + voxel_cut.ANCHORS[a, 1] * (1 - t) + voxel_cut.ANCHORS[b, 1] * t) * f
        assert np.abs(np.hypot(px - 0.5, py - 0.5) - R).max() < 0.3 * f
    hole = 0.2 - state.volume("SiO2") / 1.0
    assert hole / 0.2 == pytest.approx(np.pi * R * R, rel=2e-3)


def test_a_cut_state_meshes_closed_and_conforming_and_survives_storage(tmp_path):
    state = _round_hole()
    _mesh_checks(state)
    path = tmp_path / "cut.dfz"
    state.save(path)
    back = voxel.VoxelState.load(path)
    back.check()
    assert np.array_equal(back.to_fine_cuts(), state.to_fine_cuts())
    assert back.volume("SiO2") == pytest.approx(state.volume("SiO2"))


@pytest.mark.parametrize("axis", ["x", "y"])
def test_a_flip_mirrors_the_cuts(axis):
    state = _round_hole(R=0.27)
    # off centre, so a mirror is not the same state
    from shapely.geometry import Point

    voxel.etch_vertical(state, {"SiO2": 1.0}, 1.0, voxel.rasterize(state, Point(0.2, 0.25).buffer(0.1, quad_segs=128)))
    before = state.volume("SiO2")
    points = np.random.default_rng(5).random((2, 4000))
    k = state.n - 1
    was = state.sample(k, points[0], points[1])
    voxel.flip(state, axis)
    state.check()
    assert state.volume("SiO2") == pytest.approx(before)
    mx, my = (1.0 - points[0], points[1]) if axis == "y" else (points[0], 1.0 - points[1])
    now = state.sample(0, mx, my)  # the top slab is now the bottom one
    assert (now == was).mean() > 0.999


def test_filling_a_whole_cell_that_holds_a_cut_leaves_no_stale_brick():
    from process_studio.kernels import voxel_cut

    # a bulk cell of open fine cells, one of them cut against the SiO2 wall
    # beside it: a brick. A thick film fills the whole cell.
    fine = np.zeros((2, 32, 32), np.uint8)
    fine[0] = 1
    fine[1, :, :12] = 2
    cuts = np.zeros(fine.shape, np.uint16)
    cuts[1, 5, 12] = voxel_cut.pack(np.array(voxel_cut.CODE_OF[5, 1]), np.array(2))
    state = voxel.VoxelState.from_fine(
        (0.0, 0.0, 1.0, 1.0), fine, 4, np.array([0.0, 0.1, 0.2]), ["Si", "SiO2", "SiN"], cuts=cuts
    )
    state.check()
    assert state.labels[1, 1, 3] == voxel.MIXED
    voxel.deposit(state, "SiN", 0.3, planar=False)
    state.check()


def test_a_state_drawn_on_a_finer_or_coarser_grid_keeps_its_shape():
    state = _round_hole(B=4)
    rng = np.random.default_rng(11)
    x, y = rng.random(20000), rng.random(20000)
    k = state.n - 1
    finer = voxel.resample(state, 16)
    finer.check()
    # every line between two anchors runs through finer anchors: nothing moves
    assert (finer.sample(k, x, y) == state.sample(k, x, y)).mean() > 0.999
    for name in state.present():
        assert finer.volume(name) == pytest.approx(state.volume(name), rel=1e-12)
    coarser = voxel.resample(finer, 2)
    coarser.check()
    assert coarser.refine == 2
    assert (coarser.sample(k, x, y) == state.sample(k, x, y)).mean() > 0.98
    assert coarser.volume("SiO2") == pytest.approx(state.volume("SiO2"), rel=5e-3)


def test_the_polygon_view_holds_the_state_as_polygon_slabs_that_follow_the_circle():
    from process_studio.kernels import voxel_polygons
    from process_studio.kernels.slab import display_meshes

    R = 0.3
    state = _round_hole(B=8, R=R)
    slabs = voxel_polygons.polygon_state(state)
    assert voxel_polygons.polygon_state(state) is slabs  # built once
    slabs.device._state.validate()
    k = state.n - 1
    top = slabs.device._state.slabs[k]
    oxide = next(geom for material, geom in top.regions.items() if material.name == "SiO2")
    # the hole's rim is the cut lines' polygon, not a staircase of cells
    rim = [ring for poly in oxide.geoms for ring in poly.interiors]
    assert len(rim) == 1
    xy = np.asarray(rim[0].coords)
    assert np.abs(np.hypot(xy[:, 0] - 0.5, xy[:, 1] - 0.5) - R).max() < 0.4 * state.fine
    meshes = display_meshes(slabs, buried=True)
    for name, (vertices, faces, _interface, _neighbour) in meshes.items():
        a, b, c = (vertices[faces[:, i]].astype(np.float64) for i in range(3))
        signed = np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0
        assert signed == pytest.approx(state.volume(name), rel=2e-3), name
        assert _unmatched_edges(vertices, faces) == 0, name
    # far fewer triangles than a face per fine cell side
    cells = voxel.build_meshes(state, buried=True)
    assert sum(len(m[1]) for m in meshes.values()) < 0.5 * sum(len(m[1]) for m in cells.values())


def test_a_map_with_no_refined_cell_is_outlined_on_its_own_grid():
    from process_studio.kernels import voxel_vector

    # refined four times, but no brick yet: the outline is still the window
    labels = np.ones((8, 8), np.uint8)
    found = voxel_vector.field_loops(
        labels, np.zeros(0, np.int64), np.zeros((0, 4, 4), np.uint8), np.zeros((0, 4, 4), np.uint16),
        0.0, 0.0, 0.5 / 32, 0.5 / 32,
    )
    points, _starts = found[1]
    assert points.min(axis=0).tolist() == [0.0, 0.0] and points.max(axis=0).tolist() == [1.0, 1.0]


def _outline_area(picture, name):
    vec = picture["vector"]
    extent = picture["extent"]
    sx = (extent["horizontalMax"] - extent["horizontalMin"]) / vec["width"]
    sy = (extent["verticalMax"] - extent["verticalMin"]) / vec["height"]
    total = 0.0
    for shape in vec["shapes"]:
        if shape["material"] != name:
            continue
        points = np.frombuffer(base64.b64decode(shape["points"]), np.float32).reshape(-1, 2).astype(float)
        starts = list(np.frombuffer(base64.b64decode(shape["starts"]), np.int32)) + [len(points)]
        for a, b in zip(starts[:-1], starts[1:]):
            x, y = points[a:b, 0] * sx, points[a:b, 1] * sy
            total += 0.5 * (np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))
    return abs(total)


def test_the_pictures_are_outlines_that_hold_what_the_state_holds():
    state = _round_hole(R=0.3)
    k = state.n - 1
    thick = state.z[k + 1] - state.z[k]
    top = voxel.top_view(state, {}, _rgb, vector=True)
    assert top["image"] == ""
    # SiO2 around the hole, Si seen through it: exactly the cut cells' shares
    assert _outline_area(top, "SiO2") == pytest.approx(state.volume("SiO2") / thick, rel=1e-6)
    assert _outline_area(top, "Si") == pytest.approx(1.0 - state.volume("SiO2") / thick, rel=1e-6)
    for kwargs in ({"axis": "y", "position": 0.5}, {"axis": "x", "position": 0.37}):
        section = voxel.section(state, {}, _rgb, z_max=0.5, vector=True, **kwargs)
        assert section["image"] == ""
        assert _outline_area(section, "Si") == pytest.approx(0.2, rel=1e-6)
        # the hole's chord through SiO2 at the cut
        coord = kwargs["position"]
        chord = 2 * np.sqrt(0.3**2 - (coord - 0.5) ** 2)
        assert _outline_area(section, "SiO2") == pytest.approx(0.2 * (1.0 - chord), abs=0.2 * 2 * state.fine)
    line = voxel.section(state, {}, _rgb, z_max=0.5, vector=True, line=((0.0, 0.5), (1.0, 0.5)))
    assert _outline_area(line, "Si") == pytest.approx(0.2, rel=1e-3)


def test_pieces_found_per_distinct_grid_are_the_pieces_of_the_fine_cells():
    rng = np.random.default_rng(7)
    layers, ny, nx, B = 4, 6, 7, 4
    palette = rng.random((5, B, B)) < 0.6  # few grids: refined cells share them
    plain_cells = rng.random((layers, ny, nx)) < 0.5
    refined = rng.random((layers, ny, nx)) < 0.45
    choice = rng.integers(0, len(palette), size=(layers, ny, nx))
    dense = np.repeat(np.repeat(plain_cells, B, axis=1), B, axis=2)
    for k, y, x in zip(*np.nonzero(refined)):
        dense[k, y * B : (y + 1) * B, x * B : (x + 1) * B] = palette[choice[k, y, x]]
    keys = np.flatnonzero(refined.reshape(-1))
    k, rest = np.divmod(keys, ny * nx)
    iy, ix = np.divmod(rest, nx)
    fine = palette[choice.reshape(-1)[keys]]
    coarse_id, pieces = voxel.components2(plain_cells & ~refined, keys, fine, nx, ny)
    found = np.zeros(dense.shape, dtype=np.int64)
    found[:] = np.repeat(np.repeat(coarse_id, B, axis=1), B, axis=2)
    ids = np.zeros(fine.shape, dtype=np.int64)
    ids[fine] = pieces.at(np.arange(keys.size), np.ones(fine.shape, bool))
    for n in range(keys.size):
        found[k[n], iy[n] * B : (iy[n] + 1) * B, ix[n] * B : (ix[n] + 1) * B] = ids[n]
    expected = voxel.components(dense)
    assert ((found > 0) == dense).all()
    # the same partition: each piece of one is one piece of the other
    pairs = np.unique(np.stack([found[dense], expected[dense]]), axis=1)
    assert pairs.shape[1] == np.unique(found[dense]).size == np.unique(expected[dense]).size
    # and membership, read back per grid
    some = np.unique(expected[dense])[::2]
    chosen = np.unique(found[np.isin(expected, some) & dense])
    member = pieces.member(chosen)
    assert (member == (np.isin(ids, chosen) & fine)).all()


def test_the_column_walk_that_reads_slabs_as_it_goes_stops_where_the_full_walk_does():
    rng = np.random.default_rng(3)
    B = 4
    fine = rng.integers(0, 4, size=(6, 8 * B, 8 * B)).astype(np.uint8)
    state = voxel.VoxelState.from_fine((0.0, 0.0, 1.0, 1.0), fine, B, np.linspace(0.0, 0.6, 7), ["A", "B", "C"])
    table = np.zeros(256)
    table[1], table[2] = 1.0, 0.25  # C does not etch
    x, y = rng.random(500), rng.random(500)
    inside = rng.random(500) < 0.8
    for budget in (0.05, 0.2, 1.0):
        full = voxel._walk(state.sample(np.arange(state.n)[:, None], x[None, :], y[None, :]), state.z, table, budget, inside)
        assert np.array_equal(voxel._walk_at(state, x, y, table, budget, inside), full)
