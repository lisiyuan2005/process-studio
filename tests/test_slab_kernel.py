"""The slab kernel: what it computes, and what it refuses to pretend it can."""

from __future__ import annotations

import base64

import pytest

from process_studio.defaults import default_grid, default_materials
from process_studio.kernels import get_kernel
from process_studio.kernels.slab import SlabError, SlabState
from process_studio.layout.quick_sketch import QuickSketch, SketchShape
from process_studio.models import (
    MaterialResponse,
    ProcessStep,
    ProcessType,
    ProjectDefinition,
    Recipe,
)
from process_studio.worker.serialize import grid_dict

pytest.importorskip("shapely")
pytest.importorskip("trimesh")


@pytest.fixture()
def project() -> ProjectDefinition:
    return ProjectDefinition(
        "Slab project",
        grid_dict(default_grid()),
        kernel="slab",
        resolution_um=0.01,
    )


@pytest.fixture()
def kernel():
    return get_kernel("slab")


@pytest.fixture()
def sketches() -> dict[str, QuickSketch]:
    return {
        "default": QuickSketch(
            "default",
            [SketchShape("circle", parameters={"center": (0.0, 0.0), "radius": 0.22})],
        )
    }


def step(process_type: ProcessType, **fields) -> ProcessStep:
    parameters = fields.pop("parameters", {})
    responses = fields.pop("material_responses", {})
    return ProcessStep(
        fields.pop("name", "Step"),
        process_type=process_type,
        parameters=parameters,
        material_responses=responses,
        **fields,
    )


def surface_z(kernel, state, project) -> float:
    """Highest point of the geometry, in the project's own heights."""
    return kernel.surfaces(state, project=project)["bounds"]["zMin"] + (
        state.device.top or 0.0
    )


def test_the_bare_wafer_fills_the_window_up_to_zero(kernel, project):
    state = kernel.initial_state(project, materials=default_materials())
    assert state.priority == ["Si"]
    # The substrate is as thick as the project window is deep, and its top
    # face is the z = 0 the level-set kernel also puts it at.
    assert state.device.top == pytest.approx(0.8)
    assert state.z_offset == pytest.approx(-0.8)
    assert kernel.surfaces(state, project=project)["bounds"]["zMin"] == pytest.approx(-0.8)


def test_a_conformal_film_has_its_nominal_thickness_everywhere(kernel, project, sketches):
    materials = default_materials()
    state = kernel.initial_state(project, materials=materials)
    etched = kernel.run_step(
        state,
        step(
            ProcessType.ETCH,
            mask_source="quick_sketch",
            parameters={"target": 0.3, "directional_fraction": 1.0, "sketch_id": "default"},
            material_responses={"Si": MaterialResponse("Si", 0.1)},
        ),
        project=project,
        recipes={},
        sketches=sketches,
        logger=lambda _message: None,
        materials=materials,
    )
    grown = kernel.run_step(
        etched,
        step(
            ProcessType.DEPOSIT,
            parameters={"target": 0.04, "mode": "conformal"},
            output_material="Al2O3",
        ),
        project=project,
        recipes={},
        sketches=sketches,
        logger=lambda _message: None,
        materials=[*materials],
    )
    grown.device._materials.resolve("Al2O3")
    section = grown.device.cross_section((-0.8, 0.0), (0.8, 0.0))
    # Outside the trench the film sits on the wafer; inside it lines the floor
    # 0.3 um down. Both are the film thickness above what they cover, exactly.
    assert section.surface_z(0.05) == pytest.approx(0.8 + 0.04)
    assert section.surface_z(0.8) == pytest.approx(0.5 + 0.04)
    assert grown.priority == ["Si", "Al2O3"]


def test_a_stored_state_comes_back_unchanged(tmp_path, kernel, project):
    materials = default_materials()
    state = kernel.initial_state(project, materials=materials)
    path = tmp_path / "state.dfz"
    state.save(path)
    restored = kernel.load_state(path)
    assert isinstance(restored, SlabState)
    assert restored.priority == state.priority
    assert restored.z_offset == pytest.approx(state.z_offset)
    assert restored.device.top == pytest.approx(state.device.top)
    assert restored.device.conformal_resolution == pytest.approx(0.01)


def test_a_step_never_changes_the_state_it_was_given(kernel, project, sketches):
    materials = default_materials()
    state = kernel.initial_state(project, materials=materials)
    kernel.run_step(
        state,
        step(
            ProcessType.DEPOSIT,
            parameters={"target": 0.1, "mode": "planar"},
            output_material="SiO2",
        ),
        project=project,
        recipes={},
        sketches=sketches,
        logger=lambda _message: None,
        materials=materials,
    )
    assert state.priority == ["Si"]
    assert state.device.top == pytest.approx(0.8)


def test_a_mixed_etch_profile_is_refused_rather_than_approximated(kernel, project, sketches):
    materials = default_materials()
    state = kernel.initial_state(project, materials=materials)
    with pytest.raises(SlabError, match="directional_fraction"):
        kernel.run_step(
            state,
            step(
                ProcessType.ETCH,
                parameters={"target": 0.1, "directional_fraction": 0.5},
                material_responses={"Si": MaterialResponse("Si", 0.1)},
            ),
            project=project,
            recipes={},
            sketches=sketches,
            logger=lambda _message: None,
            materials=materials,
        )


def test_a_patterned_deposition_is_refused_rather_than_approximated(kernel, project, sketches):
    materials = default_materials()
    state = kernel.initial_state(project, materials=materials)
    with pytest.raises(SlabError, match="cannot deposit"):
        kernel.run_step(
            state,
            step(
                ProcessType.DEPOSIT,
                parameters={"target": 0.05, "mode": "fill"},
                output_material="W",
            ),
            project=project,
            recipes={},
            sketches=sketches,
            logger=lambda _message: None,
            materials=materials,
        )


def test_an_isotropic_etch_undercuts_the_mask(kernel, project, sketches):
    materials = default_materials()
    state = kernel.initial_state(project, materials=materials)
    wet = kernel.run_step(
        state,
        step(
            ProcessType.ETCH,
            mask_source="quick_sketch",
            parameters={"target": 0.1, "directional_fraction": 0.0, "sketch_id": "default"},
            material_responses={"Si": MaterialResponse("Si", 0.1)},
        ),
        project=project,
        recipes={},
        sketches=sketches,
        logger=lambda _message: None,
        materials=materials,
    )
    section = wet.device.cross_section((-0.8, 0.0), (0.8, 0.0))
    # An isotropic etch eats sideways as far as it eats down, so just under
    # the wafer surface the hole is wider than the 0.44 um mask that made it.
    assert section.opening_width(0.79) > 0.44
    assert section.opening_width(0.79) == pytest.approx(0.44 + 2 * 0.1, abs=0.02)


def test_cmp_planarizes_to_the_project_height(kernel, project, sketches):
    materials = default_materials()
    state = kernel.initial_state(project, materials=materials)
    grown = kernel.run_step(
        state,
        step(
            ProcessType.DEPOSIT,
            parameters={"target": 0.2, "mode": "planar"},
            output_material="SiO2",
        ),
        project=project,
        recipes={},
        sketches=sketches,
        logger=lambda _message: None,
        materials=materials,
    )
    polished = kernel.run_step(
        grown,
        step(ProcessType.CMP, parameters={"target_z": 0.05}),
        project=project,
        recipes={},
        sketches=sketches,
        logger=lambda _message: None,
        materials=materials,
    )
    # The plane is given in project heights, where the wafer surface is zero.
    assert polished.device.top == pytest.approx(0.85)


def test_the_views_are_pictures_of_this_state(kernel, project, sketches):
    materials = default_materials()
    colors = {material.name: material.color for material in materials}
    state = kernel.initial_state(project, materials=materials)
    surfaces = kernel.surfaces(state, project=project)
    assert [surface["material"] for surface in surfaces["surfaces"]] == ["Si"]
    assert surfaces["surfaces"][0]["triangleCount"] > 0
    assert surfaces["exact"] is True

    section = kernel.section(state, colors, project=project, axis="y")
    assert section["axis"] == "y"
    assert len(section["positions"]) > 1
    assert base64.b64decode(section["image"])[:4] == b"\x89PNG"
    assert section["extent"]["verticalMin"] == pytest.approx(-0.8)

    top = kernel.top_view(state, colors, project=project)
    assert base64.b64decode(top["image"])[:4] == b"\x89PNG"
