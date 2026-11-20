"""The OpenVDB kernel: per-material level sets, measured against geometry
whose answer is known, and against the slab kernel on the same flow."""

from __future__ import annotations

import base64
import io
import math

import numpy as np
import pytest

pytest.importorskip("scipy")
pytest.importorskip("skfmm")

from process_studio.defaults import default_materials  # noqa: E402
from process_studio.grid import window_grid  # noqa: E402
from process_studio.kernels import get_kernel, kernel_ids, unavailable_kernels  # noqa: E402
from process_studio.layout.quick_sketch import QuickSketch, SketchShape  # noqa: E402
from process_studio.models import (  # noqa: E402
    MaterialResponse,
    ProcessStep,
    ProcessType,
    ProjectDefinition,
)
from process_studio.worker.errors import Cancelled  # noqa: E402
from process_studio.worker.serialize import grid_dict  # noqa: E402

if "openvdb" not in kernel_ids():
    pytest.skip(
        "the OpenVDB kernel is not built here: "
        + "; ".join(item["reason"] for item in unavailable_kernels() if item["id"] == "openvdb"),
        allow_module_level=True,
    )

from process_studio.kernels.openvdb.kernel import MAXIMUM_NODES, OpenVDBError  # noqa: E402

MATERIALS = default_materials()


def project(fidelity: str = "detailed", voxel: float = 0.005, kernel: str = "openvdb", **window) -> ProjectDefinition:
    bounds = dict(x_min=-0.2, x_max=0.2, y_min=-0.2, y_max=0.2, z_min=-0.1, z_max=0.3)
    bounds.update(window)
    p = ProjectDefinition(
        "vdb",
        grid_dict(window_grid(bounds["x_min"], bounds["x_max"], bounds["y_min"], bounds["y_max"], bounds["z_min"], bounds["z_max"])),
        kernel=kernel,
        resolution_um=voxel,
        voxel_um=voxel,
    )
    p.fidelity = fidelity
    return p


SKETCHES = {
    "hole": QuickSketch("hole", [SketchShape("circle", parameters={"center": (0.0, 0.0), "radius": 0.08})]),
    "left": QuickSketch("left", [SketchShape("rectangle", parameters={"center": (-0.1, 0.0), "size": (0.2, 0.4)})]),
    "pad": QuickSketch("pad", [SketchShape("rectangle", parameters={"center": (0.1, 0.0), "size": (0.1, 0.1)})]),
}


def step(kind, material=None, rates=None, sketch=None, **parameters) -> ProcessStep:
    if sketch:
        parameters["sketch_id"] = sketch
    return ProcessStep(
        "step",
        process_type=kind,
        parameters=parameters,
        material_responses={m: MaterialResponse(m, rate_um_per_min=r) for m, r in (rates or {}).items()},
        output_material=material,
        mask_source="quick_sketch" if sketch else "none",
    )


def run(kernel, p, steps, state=None, should_cancel=None):
    state = kernel.initial_state(p, materials=MATERIALS) if state is None else state
    for item in steps:
        state = kernel.run_step(
            state, item, project=p, recipes={}, sketches=SKETCHES,
            logger=lambda _m: None, materials=MATERIALS, should_cancel=should_cancel,
        )
    return state


def material_at(state, x, y, z):
    """The material at a point (None in the open), from the stored fields."""
    from scipy import ndimage

    lat = state.lattice
    dense = state.dense()
    coords = [
        [(x - lat.origin[0]) / lat.g],
        [(y - lat.origin[1]) / lat.g],
        [(z - lat.origin[2]) / lat.g - state._dense_k0],
    ]
    if ndimage.map_coordinates(state.solid(), coords, order=1, mode="nearest")[0] >= 0:
        return None
    values = {name: ndimage.map_coordinates(a, coords, order=1, mode="nearest")[0] for name, a in dense.items()}
    return min(values, key=values.get)


def crossing(state, start, end, samples=4001):
    """Where the material changes along a segment: the parameter values."""
    points = np.linspace(0.0, 1.0, samples)
    labels = [material_at(state, *(np.add(start, np.multiply(np.subtract(end, start), t)))) for t in points]
    return [
        (float((points[i] + points[i + 1]) / 2), labels[i], labels[i + 1])
        for i in range(samples - 1)
        if labels[i] != labels[i + 1]
    ]


@pytest.fixture(scope="module")
def kernel():
    return get_kernel("openvdb")


def test_the_kernel_is_offered_with_what_it_does(kernel):
    info = kernel.info.to_json()
    assert info["id"] == "openvdb"
    assert info["spacingRole"] == "voxel"
    assert set(info["processTypes"]) == {"deposit", "etch", "cmp", "no_geometry", "oxidation", "flip"}
    assert info["maskSources"] == ["none", "quick_sketch", "gds"]
    assert info["maximumNodes"] == MAXIMUM_NODES


def test_the_bare_wafer_is_substrate_up_to_zero_and_survives_a_file(kernel, tmp_path):
    p = project()
    state = kernel.initial_state(p, materials=MATERIALS)
    assert kernel.state_materials(state) == ["Si"]
    assert state.top() == pytest.approx(0.0, abs=1e-4)
    path = tmp_path / "state.vdbz"
    state.save(path)
    again = kernel.load_state(path)
    assert again.lattice == state.lattice
    assert again.top() == pytest.approx(0.0, abs=1e-4)


@pytest.mark.parametrize("fidelity", ["simplified", "detailed"])
def test_a_planar_film_is_its_thickness(kernel, fidelity):
    p = project(fidelity)
    state = run(kernel, p, [step(ProcessType.DEPOSIT, "SiO2", target=0.037, mode="planar")])
    assert state.top() == pytest.approx(0.037, abs=2e-4)
    found = crossing(state, (0.01, 0.01, -0.05), (0.01, 0.01, 0.1))
    heights = [-0.05 + t * 0.15 for t, *_ in found]
    assert [(a, b) for _, a, b in found] == [("Si", "SiO2"), ("SiO2", None)]
    assert heights == pytest.approx([0.0, 0.037], abs=3e-4)


def test_a_vertical_etch_cuts_the_mask_to_its_depth(kernel):
    p = project()
    state = run(kernel, p, [
        step(ProcessType.DEPOSIT, "SiO2", target=0.05, mode="planar"),
        step(ProcessType.ETCH, rates={"SiO2": 1.0, "Si": 0.5}, sketch="hole", time_min=0.08, directional_fraction=1.0),
    ])
    # 50 nm of oxide at 1 um/min is 0.05 min; the other 0.03 min take 15 nm of Si
    found = crossing(state, (0.0, 0.0, -0.08), (0.0, 0.0, 0.1))
    assert [(a, b) for _, a, b in found] == [("Si", None)]
    assert -0.08 + found[0][0] * 0.18 == pytest.approx(-0.015, abs=3e-4)
    across = crossing(state, (-0.15, 0.003, 0.025), (0.15, 0.003, 0.025))
    edges = [-0.15 + t * 0.3 for t, *_ in across]
    radius = math.sqrt(0.08**2 - 0.003**2)
    assert edges == pytest.approx([-radius, radius], abs=1e-3)


def test_a_zero_rate_material_stops_the_vertical_front(kernel):
    p = project()
    state = run(kernel, p, [
        step(ProcessType.DEPOSIT, "SiN", target=0.02, mode="planar"),
        step(ProcessType.DEPOSIT, "SiO2", target=0.03, mode="planar"),
        step(ProcessType.ETCH, rates={"SiO2": 1.0}, sketch="hole", time_min=1.0, directional_fraction=1.0),
    ])
    found = crossing(state, (0.0, 0.0, -0.05), (0.0, 0.0, 0.1))
    assert [(a, b) for _, a, b in found] == [("Si", "SiN"), ("SiN", None)]
    assert -0.05 + found[-1][0] * 0.15 == pytest.approx(0.02, abs=3e-4)


def test_the_two_film_models_differ_at_a_corner_only(kernel):
    """A 30 nm film over a step: square (simplified) fills the corner over the
    edge, round (detailed) keeps the distance; on the flat both are 30 nm."""
    flow = [
        step(ProcessType.ETCH, rates={"Si": 1.0}, sketch="left", target=0.06, directional_fraction=1.0),
        step(ProcessType.DEPOSIT, "SiN", target=0.03, mode="conformal"),
    ]
    square = run(kernel, project("simplified"), flow)
    round_ = run(kernel, project("detailed"), flow)
    for state in (square, round_):
        top = crossing(state, (0.1, 0.0, -0.05), (0.1, 0.0, 0.1))
        assert -0.05 + top[-1][0] * 0.15 == pytest.approx(0.03, abs=4e-4)
    # the step's edge is at x = 0, z = 0; a point 20 nm out and 20 nm up
    # is 28 nm from it: inside the square film's corner, outside the round
    corner = (-0.022, 0.0, 0.022)
    assert material_at(square, *corner) == "SiN"
    assert material_at(round_, *corner) is None


@pytest.mark.parametrize("fidelity", ["simplified", "detailed"])
def test_a_wet_etch_goes_round_a_barrier_never_through_it(kernel, fidelity):
    """SiN sealed under oxide is not reached however long the etch."""
    p = project(fidelity)
    state = run(kernel, p, [
        step(ProcessType.DEPOSIT, "SiN", target=0.02, mode="planar"),
        step(ProcessType.DEPOSIT, "SiO2", target=0.01, mode="planar"),
        step(ProcessType.ETCH, rates={"SiN": 1.0}, target=0.05, directional_fraction=0.0),
    ])
    assert "SiN" in kernel.state_materials(state)
    assert material_at(state, 0.0, 0.0, 0.01) == "SiN"


@pytest.mark.parametrize("fidelity", ["simplified", "detailed"])
def test_a_wet_etch_undercuts_a_layer_from_an_opening(kernel, fidelity):
    """A slot through oxide into SiN: the SiN goes back 40 nm from the slot's
    wall under the oxide, the oxide stays."""
    p = project(fidelity)
    state = run(kernel, p, [
        step(ProcessType.DEPOSIT, "SiN", target=0.03, mode="planar"),
        step(ProcessType.DEPOSIT, "SiO2", target=0.02, mode="planar"),
        step(ProcessType.ETCH, rates={"SiO2": 1.0, "SiN": 1.0}, sketch="left", target=0.05, directional_fraction=1.0),
        step(ProcessType.ETCH, rates={"SiN": 1.0}, target=0.04, directional_fraction=0.0),
    ])
    found = crossing(state, (-0.05, 0.0, 0.015), (0.15, 0.0, 0.015))
    assert [(a, b) for _, a, b in found] == [(None, "SiN")]
    assert -0.05 + found[0][0] * 0.2 == pytest.approx(0.04, abs=2.5e-3)
    assert material_at(state, 0.02, 0.0, 0.04) == "SiO2"


def test_oxidation_turns_the_surface_into_oxide_in_place(kernel):
    p = project("detailed")
    state = run(kernel, p, [
        step(ProcessType.OXIDATION, "SiO2", rates={"Si": 1.0}, target=0.012),
    ])
    found = crossing(state, (0.0, 0.0, -0.05), (0.0, 0.0, 0.05))
    assert [(a, b) for _, a, b in found] == [("Si", "SiO2"), ("SiO2", None)]
    heights = [-0.05 + t * 0.1 for t, *_ in found]
    assert heights == pytest.approx([-0.012, 0.0], abs=3e-4)


def test_an_oxide_cannot_be_its_own_source(kernel):
    with pytest.raises(OpenVDBError, match="cannot be both"):
        run(kernel, project(), [step(ProcessType.OXIDATION, "Si", rates={"Si": 1.0}, target=0.01)])


def test_cmp_cuts_everything_at_the_plane(kernel):
    p = project()
    state = run(kernel, p, [
        step(ProcessType.DEPOSIT, "SiO2", target=0.06, mode="planar"),
        step(ProcessType.CMP, target_z=0.025),
    ])
    assert state.top() == pytest.approx(0.025, abs=3e-4)


def test_a_flip_turns_the_stack_over_and_mirrors_it(kernel):
    p = project("detailed")
    state = run(kernel, p, [
        step(ProcessType.DEPOSIT, "SiO2", target=0.04, mode="planar"),
        step(ProcessType.ETCH, rates={"SiO2": 1.0}, sketch="pad", target=0.04, directional_fraction=1.0),
        step(ProcessType.FLIP, axis="y"),
    ])
    # 100 nm of Si under 40 nm of oxide, turned over: the oxide is at the
    # floor now, the Si above it, the top at the old floor's height
    assert state.top() == pytest.approx(0.04, abs=5e-4)
    assert material_at(state, -0.18, 0.0, -0.08) == "SiO2"
    assert material_at(state, -0.18, 0.0, 0.0) == "Si"
    # the pad cut at x = +0.1 is at x = -0.1 after the mirror, open at the
    # floor now; under x = +0.1 there is oxide
    assert material_at(state, -0.1, 0.0, -0.08) is None
    assert material_at(state, 0.1, 0.0, -0.08) == "SiO2"


def test_a_masked_deposit_is_a_lift_off(kernel):
    """Film only where the gas got in through the opening: none on the
    covered surface, none as a sheet off the resist's edge."""
    p = project("detailed")
    state = run(kernel, p, [
        step(ProcessType.DEPOSIT, "TiN", target=0.02, mode="conformal", sketch="pad"),
    ])
    assert material_at(state, 0.1, 0.0, 0.01) == "TiN"
    assert material_at(state, -0.1, 0.0, 0.01) is None
    # beside the pad, just outside its edge, there is nothing
    assert material_at(state, 0.1 + 0.05 + 0.008, 0.0, 0.01) is None
    found = crossing(state, (0.1, 0.0, -0.05), (0.1, 0.0, 0.1))
    assert -0.05 + found[-1][0] * 0.15 == pytest.approx(0.02, abs=4e-4)


def test_views_draw_the_state(kernel):
    p = project()
    state = run(kernel, p, [
        step(ProcessType.DEPOSIT, "SiO2", target=0.04, mode="planar"),
        step(ProcessType.ETCH, rates={"SiO2": 1.0}, sketch="hole", target=0.04, directional_fraction=1.0),
    ])
    colors = {"Si": "#808080", "SiO2": "#3080ff"}
    section = kernel.section(state, colors, project=p, axis="y", position=0.0)
    assert section["axis"] == "y" and section["exact"] is False
    assert len(section["positions"]) == 201
    line = kernel.section(state, colors, project=p, line=((-0.2, -0.2), (0.2, 0.2)))
    assert line["axis"] == "line" and line["extent"]["horizontalMax"] == pytest.approx(math.hypot(0.4, 0.4))
    top = kernel.top_view(state, colors, project=p)
    from PIL import Image

    image = np.asarray(Image.open(io.BytesIO(base64.b64decode(top["image"]))))
    # the centre is the hole's floor, Si; a corner the oxide
    assert tuple(image[image.shape[0] // 2, image.shape[1] // 2]) == (0x80, 0x80, 0x80)
    assert tuple(image[5, 5]) == (0x30, 0x80, 0xFF)
    surfaces = kernel.surfaces(state, project=p)
    assert {item["material"] for item in surfaces["surfaces"]} == {"Si", "SiO2"}
    for item in surfaces["surfaces"]:
        positions = np.frombuffer(base64.b64decode(item["positions"]), dtype=np.float32).reshape(-1, 3)
        faces = np.frombuffer(base64.b64decode(item["indices"]), dtype=np.uint32).reshape(-1, 3)
        normals = np.frombuffer(base64.b64decode(item["normals"]), dtype=np.float32).reshape(-1, 3)
        assert len(positions) == item["vertexCount"] and len(faces) == item["triangleCount"]
        assert np.allclose(np.linalg.norm(normals, axis=1), 1.0, atol=1e-3)
        # outward: the top faces of the oxide point up
        if item["material"] == "SiO2":
            top_faces = positions[faces].mean(axis=1)[:, 2] > 0.039
            a, b, c = (positions[faces[:, k]] for k in range(3))
            assert (np.cross(b - a, c - a)[top_faces][:, 2] > 0).mean() > 0.95
    buried = kernel.surfaces(state, project=p, buried=True)
    assert sum(i["triangleCount"] for i in buried["surfaces"]) > sum(i["triangleCount"] for i in surfaces["surfaces"])


def test_a_window_with_too_many_voxels_is_refused(kernel):
    p = project(voxel=0.0004)
    with pytest.raises(OpenVDBError, match="million"):
        kernel.initial_state(p, materials=MATERIALS)


def test_a_stopped_wet_etch_raises_cancelled(kernel):
    p = project("simplified")
    with pytest.raises(Cancelled):
        run(
            kernel, p,
            [step(ProcessType.ETCH, rates={"Si": 1.0}, target=0.03, directional_fraction=0.0)],
            should_cancel=lambda: True,
        )


@pytest.mark.parametrize("fidelity", ["simplified", "detailed"])
def test_it_draws_what_the_slab_kernel_draws(fidelity):
    """The same flow on both kernels: the sections agree but for a thin
    seam of pixels along the edges."""
    from PIL import Image

    flow = [
        step(ProcessType.DEPOSIT, "SiO2", target=0.04, mode="planar"),
        step(ProcessType.ETCH, rates={"SiO2": 1.0, "Si": 1.0}, sketch="hole", target=0.1, directional_fraction=1.0),
        step(ProcessType.DEPOSIT, "SiN", target=0.02, mode="conformal"),
        step(ProcessType.ETCH, rates={"SiN": 1.0}, target=0.012, directional_fraction=0.0),
        step(ProcessType.DEPOSIT, "TiN", target=0.015, mode="planar"),
    ]
    colors = {"Si": "#808080", "SiO2": "#3080ff", "SiN": "#20c020", "TiN": "#d0a020"}
    pictures = []
    for name in ("slab", "openvdb"):
        k = get_kernel(name)
        p = project(fidelity, kernel=name)
        state = run(k, p, flow)
        section = k.section(state, colors, project=p, axis="y", position=0.0)
        pictures.append(np.asarray(Image.open(io.BytesIO(base64.b64decode(section["image"])))).astype(int))
    differ = (np.abs(pictures[0] - pictures[1]).sum(axis=2) > 0).mean()
    assert differ < 0.015
