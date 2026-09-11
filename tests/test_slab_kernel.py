"""The slab kernel: what it computes, and what it refuses to pretend it can."""

from __future__ import annotations

import base64
from pathlib import Path

import numpy as np

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
    silicon = surfaces["surfaces"][0]
    assert silicon["triangleCount"] > 0
    assert surfaces["exact"] is True
    # The geometry is exact, welded and indexed, and carries no normals: the
    # viewer shades each face flat from it, so the wafer top is a set of
    # triangles whose three corners all sit at z = 0.
    import numpy as np

    assert silicon["normals"] == "" and silicon["shading"] == "flat"
    positions = np.frombuffer(base64.b64decode(silicon["positions"]), dtype=np.float32).reshape(-1, 3)
    faces = np.frombuffer(base64.b64decode(silicon["indices"]), dtype=np.uint32).reshape(-1, 3)
    assert len(faces) == silicon["triangleCount"] and faces.max() < len(positions)
    top = np.isclose(positions[faces][:, :, 2], 0.0).all(axis=1)
    assert top.any()
    assert positions[:, 2].min() == pytest.approx(-0.8)

    section = kernel.section(state, colors, project=project, axis="y")
    assert section["axis"] == "y"
    assert len(section["positions"]) > 1
    assert base64.b64decode(section["image"])[:4] == b"\x89PNG"
    assert section["extent"]["verticalMin"] == pytest.approx(-0.8)

    top = kernel.top_view(state, colors, project=project)
    assert base64.b64decode(top["image"])[:4] == b"\x89PNG"


def test_a_film_taller_than_the_window_is_a_unit_slip_and_says_so(kernel, project, sketches):
    """Typing 50 for 50 nm asks for a 50 µm film; the message names the unit."""
    materials = default_materials()
    state = kernel.initial_state(project, materials=materials)
    with pytest.raises(SlabError, match="micrometres: 50 nm is 0.05"):
        kernel.run_step(
            state,
            step(
                ProcessType.DEPOSIT,
                parameters={"target": 50, "mode": "conformal"},
                output_material="TiN",
            ),
            project=project,
            recipes={},
            sketches=sketches,
            logger=lambda _message: None,
            materials=materials,
        )
    with pytest.raises(SlabError, match="etch depth of 30 µm"):
        kernel.run_step(
            state,
            step(
                ProcessType.ETCH,
                parameters={"target": 30, "directional_fraction": 1.0},
                material_responses={"Si": MaterialResponse("Si", 0.1)},
            ),
            project=project,
            recipes={},
            sketches=sketches,
            logger=lambda _message: None,
            materials=materials,
        )


def test_a_masked_deposition_leaves_the_film_only_inside_the_opening(kernel, project, sketches):
    """The lift-off result: film in the circle, bare wafer around it."""
    materials = default_materials()
    state = kernel.initial_state(project, materials=materials)
    grown = kernel.run_step(
        state,
        step(
            ProcessType.DEPOSIT,
            mask_source="quick_sketch",
            parameters={"target": 0.04, "mode": "conformal", "sketch_id": "default"},
            output_material="Al2O3",
        ),
        project=project,
        recipes={},
        sketches=sketches,
        logger=lambda _message: None,
        materials=materials,
    )
    section = grown.device.cross_section((-0.8, 0.0), (0.8, 0.0))
    # Inside the 0.22 µm circle the film sits on the wafer; outside there is none.
    assert section.surface_z(0.8) == pytest.approx(0.8 + 0.04)
    assert section.surface_z(0.1) == pytest.approx(0.8)
    assert grown.device.volume("Al2O3") == pytest.approx(3.14159 * 0.22**2 * 0.04, rel=0.02)
    # The stand-in the kernel grew the film as is nowhere to be seen.
    assert grown.priority == ["Si", "Al2O3"]
    assert [s["material"] for s in kernel.surfaces(grown, project=project)["surfaces"]] == ["Si", "Al2O3"]

    # Keep "outside" deposits everywhere but the circle.
    inverse = kernel.run_step(
        state,
        step(
            ProcessType.DEPOSIT,
            mask_source="quick_sketch",
            keep="outside",
            parameters={"target": 0.04, "mode": "planar", "sketch_id": "default"},
            output_material="SiO2",
        ),
        project=project,
        recipes={},
        sketches=sketches,
        logger=lambda _message: None,
        materials=materials,
    )
    section = inverse.device.cross_section((-0.8, 0.0), (0.8, 0.0))
    assert section.surface_z(0.8) == pytest.approx(0.8)
    assert section.surface_z(0.1) == pytest.approx(0.8 + 0.04)


def test_a_masked_film_survives_a_save_and_a_later_step(tmp_path, kernel, project, sketches):
    materials = default_materials()
    state = kernel.initial_state(project, materials=materials)
    grown = kernel.run_step(
        state,
        step(
            ProcessType.DEPOSIT,
            mask_source="quick_sketch",
            parameters={"target": 0.05, "mode": "planar", "sketch_id": "default"},
            output_material="TiN",
        ),
        project=project, recipes={}, sketches=sketches, logger=lambda _m: None, materials=materials,
    )
    path = tmp_path / "masked.dfz"
    grown.save(path)
    restored = kernel.load_state(path)
    assert restored.priority == ["Si", "TiN"]
    # A blanket film over it lands on the patch and on the wafer beside it.
    covered = kernel.run_step(
        restored,
        step(ProcessType.DEPOSIT, parameters={"target": 0.02, "mode": "conformal"}, output_material="Al2O3"),
        project=project, recipes={}, sketches=sketches, logger=lambda _m: None, materials=materials,
    )
    section = covered.device.cross_section((-0.8, 0.0), (0.8, 0.0))
    assert section.surface_z(0.8) == pytest.approx(0.8 + 0.05 + 0.02)
    assert section.surface_z(0.1) == pytest.approx(0.8 + 0.02)


def test_repeated_conformal_films_of_one_material_mesh_as_one_solid(kernel, project):
    """Lining a square trench with four films of the same metal.

    The kernel it ships with grew each film as a ring snapped to the ring
    below, which pulled the ring's interface with the earlier film off it by
    a few nanometres: a crack between films that no view could show and that
    made the mesh non-manifold from the fourth film on. The films must build
    one sound solid, and each must still be its nominal thickness.
    """
    materials = default_materials()
    sketches = {
        "default": QuickSketch(
            "default",
            [SketchShape("rectangle", parameters={"center": (0.0, 0.0), "size": (0.6, 0.6)})],
        )
    }
    state = kernel.initial_state(project, materials=materials)
    state = kernel.run_step(
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
    for _ in range(4):
        state = kernel.run_step(
            state,
            step(ProcessType.DEPOSIT, parameters={"target": 0.06, "mode": "conformal"}, output_material="W"),
            project=project,
            recipes={},
            sketches=sketches,
            logger=lambda _message: None,
            materials=materials,
        )
    report = state.device.validate_mesh()
    assert report.valid, report.materials["W"].errors
    assert report.materials["W"].components == 1
    # Where the metal lies against the wafer, both meshes carry that face and
    # both mark it as an interface, which is what lets the viewer draw it once.
    payload = {s["material"]: s for s in kernel.surfaces(state, project=project)["surfaces"]}
    flags = {
        name: np.frombuffer(base64.b64decode(s["interfaceFaces"]), dtype=np.uint8)
        for name, s in payload.items()
    }
    assert flags["W"].any() and flags["Si"].any()
    assert 0 < flags["W"].sum() < len(flags["W"])
    section = state.device.cross_section((-0.8, 0.0), (0.8, 0.0))
    # Four films of 60 nm, read at distances along the section line: 240 nm
    # over the wafer at x = -0.7, and 240 nm over the trench floor at x = 0,
    # where the 600 nm opening is not yet pinched off.
    assert section.surface_z(0.1) == pytest.approx(0.8 + 0.24)
    assert section.surface_z(0.8) == pytest.approx(0.5 + 0.24)
    # The 3D view builds the same mesh, so it must come back too.
    assert {s["material"] for s in kernel.surfaces(state, project=project)["surfaces"]} == {"Si", "W"}


def test_the_3d_view_is_built_once_and_kept_beside_the_snapshot(tmp_path, kernel, project, sketches, monkeypatch):
    """The mesh is the slow part, so it is built once per stored state.

    The payload is welded, indexed geometry without normals; the viewer
    shades each face itself. After the first build the triangles live in a
    sidecar next to the snapshot, and a state loaded from disk later reads
    them instead of building them again.
    """
    import numpy as np
    import base64

    from process_studio.kernels import slab as slab_module

    materials = default_materials()
    state = kernel.initial_state(project, materials=materials)
    state = kernel.run_step(
        state,
        step(
            ProcessType.ETCH,
            mask_source="quick_sketch",
            parameters={"target": 0.3, "directional_fraction": 1.0, "sketch_id": "default"},
            material_responses={"Si": MaterialResponse("Si", 0.1)},
        ),
        project=project, recipes={}, sketches=sketches, logger=lambda _m: None, materials=materials,
    )
    path = tmp_path / "step.dfz"
    state.save(path)
    payload = kernel.surfaces(state, project=project)
    surface = payload["surfaces"][0]
    assert surface["normals"] == "" and surface["shading"] == "flat"
    positions = np.frombuffer(base64.b64decode(surface["positions"]), dtype=np.float32).reshape(-1, 3)
    indices = np.frombuffer(base64.b64decode(surface["indices"]), dtype=np.uint32)
    assert surface["vertexCount"] == len(positions) < surface["triangleCount"] * 3
    assert indices.max() < len(positions)
    interface = np.frombuffer(base64.b64decode(surface["interfaceFaces"]), dtype=np.uint8)
    # The wafer alone touches nothing, so no face of it is an interface.
    assert len(interface) == surface["triangleCount"] and not interface.any()
    # Heights are the project's: the wafer top sits at z = 0.
    assert positions[:, 2].max() == pytest.approx(0.0)

    sidecar = path.with_name(path.name + slab_module.MESH_SIDECAR)
    assert sidecar.is_file()

    # A fresh load must not build again: the builder is made to fail.
    from deviceflow._internal.mesh import builder

    def refuse(*_args, **_kwargs):
        raise AssertionError("the mesh was rebuilt although the sidecar exists")

    monkeypatch.setattr(builder, "build_material_meshes", refuse)
    reloaded = kernel.load_state(path)
    again = kernel.surfaces(reloaded, project=project)
    assert again["surfaces"][0]["positions"] == surface["positions"]
    assert again["surfaces"][0]["indices"] == surface["indices"]
    # And the same state object answers from memory the second time.
    assert kernel.surfaces(reloaded, project=project)["surfaces"][0]["indices"] == surface["indices"]


def test_the_xy_resolution_shapes_the_arcs_and_the_z_resolution_the_bands(kernel):
    """A finer z step adds bands; a finer XY sagitta adds ring vertices. They
    are separate numbers, so a project can shrink the staircase on a
    shoulder without paying for rounder corners in plan, or the reverse.

    The film coats a square mesa: its convex corners are where the XY arcs
    live (a round hole's corners are concave and stay sharp)."""
    from process_studio.kernels.slab import resolution_xy_um

    materials = default_materials()
    sketches = {
        "default": QuickSketch(
            "default",
            [SketchShape("rectangle", parameters={"center": (0.0, 0.0), "size": (0.4, 0.4)})],
        )
    }

    def film(z_nm: float, xy_nm: float | None):
        project = ProjectDefinition(
            "Split", grid_dict(default_grid()), kernel="slab",
            resolution_um=z_nm / 1000.0,
            resolution_xy_um=None if xy_nm is None else xy_nm / 1000.0,
        )
        assert resolution_xy_um(project) == pytest.approx((z_nm if xy_nm is None else xy_nm) / 1000.0)
        state = kernel.initial_state(project, materials=materials)
        state = kernel.run_step(
            state,
            step(ProcessType.DEPOSIT, parameters={"target": 0.05, "mode": "planar"}, output_material="W"),
            project=project, recipes={}, sketches=sketches, logger=lambda _m: None, materials=materials,
        )
        state = kernel.run_step(
            state,
            step(
                ProcessType.ETCH, mask_source="quick_sketch", keep="outside",
                parameters={"target": 0.05, "directional_fraction": 1.0, "sketch_id": "default"},
                material_responses={"W": MaterialResponse("W", 0.1)},
            ),
            project=project, recipes={}, sketches=sketches, logger=lambda _m: None, materials=materials,
        )
        state = kernel.run_step(
            state,
            step(ProcessType.DEPOSIT, parameters={"target": 0.06, "mode": "conformal"}, output_material="Al2O3"),
            project=project, recipes={}, sketches=sketches, logger=lambda _m: None, materials=materials,
        )
        slabs = state.device._state.slabs
        vertices = sum(
            int(shapely.get_num_coordinates(region))
            for slab in slabs for region in slab.regions.values()
        )
        return len(slabs), vertices, state

    import shapely

    coarse_slabs, coarse_vertices, _ = film(10.0, None)
    fine_z_slabs, fine_z_vertices, saved = film(2.0, 10.0)
    fine_xy_slabs, fine_xy_vertices, _ = film(10.0, 2.0)
    assert fine_z_slabs > coarse_slabs
    # A finer XY value keeps the z sampling; it may split one or two bands
    # that the coarser value had welded, never multiply them.
    assert coarse_slabs <= fine_xy_slabs <= coarse_slabs + 2
    assert fine_xy_vertices > coarse_vertices
    # Bands multiply the rings, so a finer z alone still costs more vertices
    # in total, but far fewer than a finer z and XY together would.
    assert fine_z_vertices / fine_z_slabs == pytest.approx(coarse_vertices / coarse_slabs, rel=0.25)
    # The split survives a save and a load.
    import tempfile
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "split.dfz"
        saved.save(path)
        loaded = kernel.load_state(path)
        assert loaded.device.conformal_resolution == pytest.approx(0.002)
        assert loaded.device.xy_resolution == pytest.approx(0.01)


def test_a_fine_z_step_rounds_a_shoulder_whatever_the_xy_value(kernel):
    """The staircase on a film's shoulder is bounded by the z step alone.

    A 13 nm film over a tungsten mesa, sampled every nanometre with the XY
    arcs left coarse at 20 nm: the outline above the mesa edge must follow
    the quarter circle of the film's radius to within about the z step.
    """
    import math

    materials = default_materials()
    project = ProjectDefinition(
        "Mesa", grid_dict(default_grid()), kernel="slab", resolution_um=0.001, resolution_xy_um=0.02
    )
    sketches = {
        "mesa": QuickSketch("mesa", [SketchShape("rectangle", parameters={"center": (0.0, 0.0), "size": (0.4, 0.4)})])
    }

    def run(state, item):
        return kernel.run_step(
            state, item, project=project, recipes={}, sketches=sketches, logger=lambda _m: None, materials=materials
        )

    state = kernel.initial_state(project, materials=materials)
    state = run(state, step(ProcessType.DEPOSIT, parameters={"target": 0.048, "mode": "planar"}, output_material="W"))
    state = run(
        state,
        step(
            ProcessType.ETCH, mask_source="quick_sketch", keep="outside",
            parameters={"target": 0.048, "directional_fraction": 1.0, "sketch_id": "mesa"},
            material_responses={"W": MaterialResponse("W", 0.1)},
        ),
    )
    thickness = 0.0133
    state = run(state, step(ProcessType.DEPOSIT, parameters={"target": thickness, "mode": "conformal"}, output_material="Al2O3"))
    section = state.device.cross_section((-0.8, 0.0), (0.8, 0.0))
    mesa_top = 0.8 + 0.048
    edge = 1.0  # the mesa's right edge, as a distance along the cut
    worst = 0.0
    for k in range(13):
        dz = k / 1000.0
        intervals = [i for i in section.intervals(mesa_top + dz + 0.0002) if i[2] == "Al2O3"]
        outer = max(b for _a, b, _m in intervals if b > edge)
        ideal = math.sqrt(thickness**2 - dz**2)
        worst = max(worst, abs((outer - edge) - ideal))
    assert worst < 0.0015, f"the shoulder departs from the circle by {worst * 1000:.2f} nm"
