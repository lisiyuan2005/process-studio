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
    # The view leaves such faces out until a material is hidden, so this asks
    # for them.
    payload = {
        s["material"]: s
        for s in kernel.surfaces(state, project=project, buried=True)["surfaces"]
    }
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

    sidecar = path.with_name(path.name + ".mesh-ears-free.npz")
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


def test_each_face_names_the_material_it_lies_against(kernel):
    """A face between two materials is drawn only while the material on its
    other side is hidden, so the viewer must know which material that is:
    a film's faces against the wafer, against the mesa and against the film
    on top of it are told apart, and the payload names them by index."""
    import base64

    import numpy as np

    from deviceflow._internal.mesh.builder import build_material_meshes

    materials = default_materials()
    project = ProjectDefinition(
        "Mesa", grid_dict(default_grid()), kernel="slab", resolution_um=0.002, resolution_xy_um=0.002
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
    state = run(state, step(ProcessType.DEPOSIT, parameters={"target": 0.010, "mode": "conformal"}, output_material="Al2O3"))
    state = run(state, step(ProcessType.DEPOSIT, parameters={"target": 0.010, "mode": "conformal"}, output_material="TiN"))

    built = build_material_meshes(state.device._state, manifold=False)
    order = [material.name for material in built]
    film = built[[m for m in built if m.name == "Al2O3"][0]]
    against = film.metadata["neighbour_faces"]
    assert set(np.unique(against)) == {-1, order.index("Si"), order.index("W"), order.index("TiN")}
    assert np.array_equal(film.metadata["interface_faces"], against >= 0)
    # The wafer's top is against the mesa where it stands and the film elsewhere.
    wafer = built[[m for m in built if m.name == "Si"][0]]
    up = wafer.face_normals[:, 2] > 0.999
    assert set(np.unique(wafer.metadata["neighbour_faces"][up])) == {order.index("W"), order.index("Al2O3")}

    # Asking for the buried faces, since naming what a face lies against is
    # only interesting for the faces that lie against something.
    payload = kernel.surfaces(state, project=project, buried=True)
    surface = next(item for item in payload["surfaces"] if item["material"] == "Al2O3")
    faces = np.frombuffer(base64.b64decode(surface["neighbourFaces"]), dtype=np.uint8)
    assert len(faces) == surface["triangleCount"]
    names = surface["neighbourMaterials"]
    assert {names[index] for index in np.unique(faces) if index != 255} == {"Si", "W", "TiN"}
    assert 255 in faces  # the film's walls on the window edge lie against nothing


def test_an_isotropic_etch_steps_by_the_real_barrier_layer(kernel):
    """The wet etch advances in steps no larger than half the thinnest layer
    it must not jump over. That layer is the 25 nm oxide between two
    sacrificial nitrides, not the 4 nm slab a film's sampling planes cut it
    into; a liner that continues above and below is no barrier at all.
    Counting slabs, a replacement-gate flow took a hundred-odd steps of
    which every one re-noded the whole stack."""
    from deviceflow._internal.geometry.state import znorm
    from deviceflow.process.isotropic_etch import _barrier_thickness

    materials = default_materials()
    project = ProjectDefinition(
        "Gate", grid_dict(default_grid()), kernel="slab", resolution_um=0.004, resolution_xy_um=0.004
    )
    sketches = {
        "slit": QuickSketch("slit", [SketchShape("rectangle", parameters={"center": (0.0, 0.0), "size": (0.12, 1.6)})])
    }

    def run(state, item):
        return kernel.run_step(
            state, item, project=project, recipes={}, sketches=sketches, logger=lambda _m: None, materials=materials
        )

    state = kernel.initial_state(project, materials=materials)
    for name, thickness in (("SiO2", 0.025), ("SiN", 0.03), ("SiO2", 0.025), ("SiN", 0.03), ("SiO2", 0.05)):
        state = run(state, step(ProcessType.DEPOSIT, parameters={"target": thickness, "mode": "planar"}, output_material=name))
    state = run(
        state,
        step(
            ProcessType.ETCH, mask_source="quick_sketch",
            parameters={"target": 0.16, "directional_fraction": 1.0, "sketch_id": "slit"},
            material_responses={"SiO2": MaterialResponse("SiO2", 1.0), "SiN": MaterialResponse("SiN", 1.0)},
        ),
    )
    geometry = state.device._state
    nitride = state.device._materials._by_name["SiN"]
    # Split the middle oxide into 4 nm slabs, as a film's sampling elsewhere would.
    for z in (0.859, 0.863, 0.867, 0.871):
        geometry.split_at(znorm(z))
    assert min(s.thickness for s in geometry.slabs) == pytest.approx(0.004)
    assert _barrier_thickness(geometry, {nitride: 0.3}) == pytest.approx(0.025)

    # The etch itself: both nitrides recede from the slit walls, the oxides stay.
    state = run(
        state,
        step(
            ProcessType.ETCH, parameters={"target": 0.2, "directional_fraction": 0.0},
            material_responses={"SiN": MaterialResponse("SiN", 1.0)},
        ),
    )
    section = state.device.cross_section((-0.8, 0.0), (0.8, 0.0))
    nitride_rows = [i for i in section.intervals(0.8 + 0.025 + 0.015) if i[2] == "SiN"]
    assert nitride_rows and all(b <= 0.8 - 0.06 - 0.185 or a >= 0.8 + 0.06 + 0.185 for a, b, _m in nitride_rows)
    oxide_rows = [i for i in section.intervals(0.8 + 0.025 + 0.03 + 0.012) if i[2] == "SiO2"]
    assert oxide_rows and any(b - a > 0.7 for a, b, _m in oxide_rows)


def run(kernel, state, process_step, project, sketches, materials):
    return kernel.run_step(
        state,
        process_step,
        project=project,
        recipes={},
        sketches=sketches,
        logger=lambda _message: None,
        materials=materials,
    )


def test_a_planar_film_lands_on_every_surface_seen_from_above(kernel, project, sketches):
    project.fidelity = "simplified"
    materials = default_materials()
    state = kernel.initial_state(project, materials=materials)
    trenched = run(
        kernel,
        state,
        step(
            ProcessType.ETCH,
            mask_source="quick_sketch",
            parameters={"target": 0.3, "directional_fraction": 1.0, "sketch_id": "default"},
            material_responses={"Si": MaterialResponse("Si", 0.1)},
        ),
        project, sketches, materials,
    )
    filled = run(
        kernel,
        trenched,
        step(ProcessType.DEPOSIT, parameters={"target": 0.1, "mode": "planar"}, output_material="W"),
        project, sketches, materials,
    )
    device = filled.device
    section = device.cross_section((-0.8, 0.0), (0.8, 0.0))
    # The wafer top and the trench floor both rise by the film thickness, so
    # the trench keeps its depth instead of being bridged at the top. (The
    # argument is the distance along the line: 0.8 is the trench centre.)
    assert section.surface_z(0.2) == pytest.approx(0.9)
    assert section.surface_z(0.8) == pytest.approx(0.6)
    assert device.material_at(0.0, 0.0, 0.55).name == "W"
    assert device.material_at(0.0, 0.0, 0.7) is None
    # The wall faces the sky, so it is coated too: the hole narrows by t at
    # every height, the top film included (no rounding at the mouth).
    assert device.material_at(0.18, 0.0, 0.75).name == "W"
    assert device.material_at(0.05, 0.0, 0.75) is None
    assert device.material_at(0.13, 0.0, 0.85).name == "W"
    assert device.material_at(0.11, 0.0, 0.85) is None


def _undercut(kernel, project, materials):
    """A lower layer ending at x = 0 under a roof reaching to x = 0.2: the
    recess between them opens sideways onto the wafer to the right."""
    from shapely.geometry import box

    state = kernel.initial_state(project, materials=materials)
    device = state.device
    oxide = device.material("SiO2")
    x0, y0, x1, y1 = device._state.bounds
    device._state.add_slab(0.8, 0.9, {oxide: box(x0, y0, 0.0, y1)})
    device._state.add_slab(0.9, 1.0, {oxide: box(x0, y0, 0.2, y1)})
    return state


def test_a_planar_film_leaves_a_recess_under_an_overhang_empty(kernel, project, sketches):
    project.fidelity = "simplified"
    materials = default_materials()
    state = kernel.initial_state(project, materials=materials)
    device = state.device
    oxide = device.material("SiO2")
    x0, y0, x1, y1 = device._state.bounds
    from shapely.geometry import box

    # A lower layer ending at x = 0 under a roof that reaches to x = 0.2:
    # the recess between them opens sideways onto the wafer to the right.
    device._state.add_slab(0.8, 0.9, {oxide: box(x0, y0, 0.0, y1)})
    device._state.add_slab(0.9, 1.0, {oxide: box(x0, y0, 0.2, y1)})
    covered = run(
        kernel,
        state,
        step(ProcessType.DEPOSIT, parameters={"target": 0.05, "mode": "planar"}, output_material="W"),
        project, sketches, materials,
    )
    device = covered.device
    assert device.material_at(0.1, 0.0, 0.85) is None, "under the roof nothing arrives"
    assert device.material_at(0.02, 0.0, 0.85) is None, "the recess's back wall is shadowed too"
    assert device.material_at(0.4, 0.0, 0.82).name == "W", "the open wafer is coated"
    assert device.material_at(0.22, 0.0, 0.95).name == "W", "the roof's riser faces the sky"
    # No lip hangs down from the roof's lower edge: the mouth of the recess
    # (0.1 high) is narrowed only by the floor film, and stays open.
    assert device.material_at(0.22, 0.0, 0.88) is None
    assert device.material_at(0.22, 0.0, 0.82).name == "W"
    assert device.material_at(0.3, 0.0, 0.87) is None
    # The roof's top film reaches out over its riser film: a square corner.
    assert device.material_at(0.23, 0.0, 1.02).name == "W"
    assert device.material_at(0.26, 0.0, 1.02) is None
    assert device.material_at(-0.4, 0.0, 1.02).name == "W"


def test_a_material_added_to_the_library_after_a_step_was_stored_is_known(
    tmp_path, kernel, project, sketches
):
    from process_studio.models import MaterialDefinition

    old_library = default_materials()
    state = kernel.initial_state(project, materials=old_library)
    path = tmp_path / "wafer.dfz"
    state.save(path)
    restored = kernel.load_state(path)
    library = [*old_library, MaterialDefinition("SiN-trap", "Dielectric", "#2f7f7f")]
    grown = run(
        kernel,
        restored,
        step(ProcessType.DEPOSIT, parameters={"target": 0.02, "mode": "planar"}, output_material="SiN-trap"),
        project, sketches, library,
    )
    assert grown.priority == ["Si", "SiN-trap"]
    assert grown.device._materials.resolve("SiN-trap").role == "dielectric"
    # An etch that names it is fine too, on a state that never grew it.
    etched = run(
        kernel,
        restored,
        step(
            ProcessType.ETCH,
            parameters={"target": 0.01, "directional_fraction": 1.0},
            material_responses={"SiN-trap": MaterialResponse("SiN-trap", 0.1), "Si": MaterialResponse("Si", 0.1)},
        ),
        project, sketches, library,
    )
    assert etched.priority == ["Si"]


def test_the_top_view_can_be_coloured_by_surface_height(kernel, project, sketches):
    materials = default_materials()
    state = kernel.initial_state(project, materials=materials)
    trenched = run(
        kernel,
        state,
        step(
            ProcessType.ETCH,
            mask_source="quick_sketch",
            parameters={"target": 0.3, "directional_fraction": 1.0, "sketch_id": "default"},
            material_responses={"Si": MaterialResponse("Si", 0.1)},
        ),
        project, sketches, materials,
    )
    by_material = kernel.top_view(trenched, {"Si": "#7f7f7f"}, project=project)
    assert by_material["shading"] == "material" and by_material["levels"] == []
    by_height = kernel.top_view(trenched, {"Si": "#7f7f7f"}, project=project, shading="height")
    assert by_height["shading"] == "height"
    # Two planes face the sky: the trench floor and the wafer top, in project heights.
    assert [level["z"] for level in by_height["levels"]] == pytest.approx([-0.3, 0.0])
    assert by_height["levels"][0]["color"] != by_height["levels"][1]["color"]
    assert by_height["image"] != by_material["image"]


def test_the_top_view_can_look_through_a_material(kernel, project, sketches):
    """A blanket film hides everything; taking it away shows what it covered."""
    materials = default_materials()
    state = kernel.initial_state(project, materials=materials)
    trenched = run(
        kernel,
        state,
        step(
            ProcessType.ETCH,
            mask_source="quick_sketch",
            parameters={"target": 0.3, "directional_fraction": 1.0, "sketch_id": "default"},
            material_responses={"Si": MaterialResponse("Si", 0.1)},
        ),
        project, sketches, materials,
    )
    covered = run(
        kernel,
        trenched,
        step(ProcessType.DEPOSIT, parameters={"target": 0.05, "mode": "planar"}, output_material="W"),
        project, sketches, materials,
    )
    device = covered.device

    assert device.top_view().materials == ["W"]
    through = device.top_view(["W"])
    # The tungsten is neither drawn nor able to cover what is under it, so
    # the wafer and the trench floor are what the sky sees.
    assert through.materials == ["Si"]
    assert through.area("W") == 0.0
    assert through.height_at(0.0, 0.0) == pytest.approx(0.5)
    assert through.height_at(0.79, 0.79) == pytest.approx(0.8)
    # And with everything hidden there is nothing left but the void.
    empty = device.top_view(["W", "Si"])
    assert empty.materials == []
    assert empty.area(None) == pytest.approx(device.top_view().area("W"))


def test_the_detailed_planar_film_grows_lips_on_both_sides_of_a_mouth(kernel, project, sketches):
    materials = default_materials()
    state = _undercut(kernel, project, materials)
    covered = run(
        kernel, state,
        step(ProcessType.DEPOSIT, parameters={"target": 0.05, "mode": "planar"}, output_material="W"),
        project, sketches, materials,
    )
    device = covered.device
    # The overhang's lower edge curls a lip down (sputter-like)...
    assert device.material_at(0.21, 0.0, 0.88).name == "W"
    # ...the recess behind it still gets nothing, and the open wafer is coated.
    assert device.material_at(0.1, 0.0, 0.85) is None
    assert device.material_at(0.4, 0.0, 0.82).name == "W"
    # The mouth is 0.1 high: lip and floor film meet, so it is pinched off.
    assert device.material_at(0.2, 0.0, 0.85).name == "W"


def test_the_simplified_conformal_film_coats_undersides_and_pinches_off(kernel, project, sketches):
    project.fidelity = "simplified"
    materials = default_materials()
    state = _undercut(kernel, project, materials)
    coated = run(
        kernel, state,
        step(ProcessType.DEPOSIT, parameters={"target": 0.02, "mode": "conformal"}, output_material="W"),
        project, sketches, materials,
    )
    device = coated.device
    assert device.material_at(0.1, 0.0, 0.89).name == "W", "the roof's underside"
    assert device.material_at(0.1, 0.0, 0.81).name == "W", "the recess floor"
    assert device.material_at(0.01, 0.0, 0.85).name == "W", "the recess's back wall"
    assert device.material_at(0.1, 0.0, 0.85) is None, "the recess is 0.1 high and 0.02 films leave it open"
    # Square corner: the roof's top film reaches out over its riser film.
    assert device.material_at(0.21, 0.0, 1.01).name == "W"
    assert device.material_at(0.23, 0.0, 1.01) is None
    # A hole narrower than twice the thickness pinches off and fills.
    trenched = run(
        kernel, kernel.initial_state(project, materials=materials),
        step(
            ProcessType.ETCH, mask_source="quick_sketch",
            parameters={"target": 0.3, "directional_fraction": 1.0, "sketch_id": "default"},
            material_responses={"Si": MaterialResponse("Si", 0.1)},
        ),
        project, sketches, materials,
    )
    filled = run(
        kernel, trenched,
        step(ProcessType.DEPOSIT, parameters={"target": 0.25, "mode": "conformal"}, output_material="W"),
        project, sketches, materials,
    )
    assert filled.device.material_at(0.0, 0.0, 0.6).name == "W"
    assert filled.device.material_at(0.0, 0.0, 0.79).name == "W"
    assert filled.device.material_at(0.0, 0.0, 1.0).name == "W"
    assert filled.device.top == pytest.approx(1.05)


def test_the_simplified_isotropic_etch_creeps_the_same_layer_with_square_ends(kernel, project, sketches):
    materials = default_materials()
    deposit = lambda name, t: step(  # noqa: E731
        ProcessType.DEPOSIT, parameters={"target": t, "mode": "planar"}, output_material=name
    )
    results = {}
    for fidelity in ("detailed", "simplified"):
        project.fidelity = fidelity
        state = kernel.initial_state(project, materials=materials)
        for name, t in (("SiO2", 0.05), ("SiN", 0.04), ("SiO2", 0.05)):
            state = run(kernel, state, deposit(name, t), project, sketches, materials)
        state = run(
            kernel, state,
            step(
                ProcessType.ETCH, mask_source="quick_sketch",
                parameters={"target": 0.2, "directional_fraction": 1.0, "sketch_id": "default"},
                material_responses={name: MaterialResponse(name, 0.1) for name in ("SiO2", "SiN", "Si")},
            ),
            project, sketches, materials,
        )
        state = run(
            kernel, state,
            step(
                ProcessType.ETCH,
                parameters={"target": 0.1, "directional_fraction": 0.0},
                material_responses={"SiN": MaterialResponse("SiN", 0.1)},
            ),
            project, sketches, materials,
        )
        results[fidelity] = state.device
    for fidelity, device in results.items():
        # The nitride recedes 0.1 from the hole wall (r 0.22) between the oxides, which stay.
        assert device.material_at(0.3, 0.0, 0.87) is None, fidelity
        assert device.material_at(0.33, 0.0, 0.87).name == "SiN", fidelity
        assert device.material_at(0.3, 0.0, 0.82).name == "SiO2", fidelity
        assert device.material_at(0.3, 0.0, 0.92).name == "SiO2", fidelity
    # The square front ends flat: the whole recess height is open at r = 0.31.
    for z in (0.855, 0.87, 0.885):
        assert results["simplified"].material_at(0.31, 0.0, z) is None


def test_oxidation_turns_the_exposed_skin_into_oxide_without_swelling(kernel, project, sketches):
    materials = default_materials()
    state = kernel.initial_state(project, materials=materials)
    trenched = run(
        kernel, state,
        step(
            ProcessType.ETCH, mask_source="quick_sketch",
            parameters={"target": 0.3, "directional_fraction": 1.0, "sketch_id": "default"},
            material_responses={"Si": MaterialResponse("Si", 0.1)},
        ),
        project, sketches, materials,
    )
    solid_before = sum(trenched.device.volume(m) for m in ("Si",))
    oxidised = run(
        kernel, trenched,
        step(
            ProcessType.OXIDATION,
            parameters={"target": 0.02},
            output_material="SiO2",
            material_responses={"Si": MaterialResponse("Si", 0.1)},
        ),
        project, sketches, materials,
    )
    device = oxidised.device
    # The skin is oxide on the top, the trench floor and the trench wall.
    assert device.material_at(0.6, 0.0, 0.79).name == "SiO2"
    assert device.material_at(0.6, 0.0, 0.77).name == "Si"
    # The floor and the wall recede into the silicon: the oxide sits where
    # silicon was, and the trench is as open as before (no swelling).
    assert device.material_at(0.0, 0.0, 0.49).name == "SiO2"
    assert device.material_at(0.0, 0.0, 0.51) is None
    assert device.material_at(0.23, 0.0, 0.65).name == "SiO2"
    assert device.material_at(0.21, 0.0, 0.65) is None
    assert device.material_at(0.0, 0.0, 0.7) is None, "the trench stays open"
    # Nothing grew outward: the solid's outline is what it was.
    assert device.top == pytest.approx(0.8)
    total = device.volume("Si") + device.volume("SiO2")
    assert total == pytest.approx(solid_before, rel=1e-6)
    assert device.volume("SiO2") > 0
    assert oxidised.priority == ["Si", "SiO2"]


def test_oxidation_refuses_to_oxidise_the_oxide_itself(kernel, project, sketches):
    materials = default_materials()
    state = kernel.initial_state(project, materials=materials)
    with pytest.raises(SlabError, match="cannot be both"):
        run(
            kernel, state,
            step(ProcessType.OXIDATION, parameters={"target": 0.02}, output_material="SiO2",
                 material_responses={"SiO2": MaterialResponse("SiO2", 0.1)}),
            project, sketches, materials,
        )
