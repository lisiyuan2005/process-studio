"""The slab kernel: exact polygon slabs, from the DeviceFlow 0.2.0 core.

This is the engine ProcessFlow-Emulator runs, wrapped in Process Studio's
kernel interface. It keeps geometry as stacked slabs of exact polygons rather
than as sampled fields, so a film has the thickness it was given and a mask
edge is where the mask says it is, with no grid to converge. What it cannot
do is anything the slab model has no room for: a partly directional etch, a
patterned deposition, a selective polish.

Heights differ between the two kernels and are translated here. The level-set
project puts the wafer surface at z = 0 with the substrate below it, down to
the project's ``z_min``. A DeviceFlow device stands on its floor at z = 0 and
grows upward. The substrate is therefore ``|z_min|`` thick, and every height
this module reports to the client has ``z_min`` added, so both kernels show a
wafer surface at 0 and the same deposit at the same height.
"""

from __future__ import annotations

import base64
import functools
import io
import math
import threading
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import shapely
from PIL import Image, ImageDraw
from shapely.geometry import LineString, Point, Polygon, box

from deviceflow import Device
from deviceflow.cancellation import Cancelled as DeviceFlowCancelled, cancelling
from deviceflow.exceptions import DeviceFlowError
from deviceflow.mask import Mask
from deviceflow.state_io import decode_state, encode_state
from deviceflow._internal.mesh.triangulate import DEFAULT_ENGINE, ENGINES

from ..layout.quick_sketch import QuickSketch, SketchShape
from ..visualization import height_levels
from ..models import MaterialDefinition, ProcessStep, ProcessType, ProjectDefinition, Recipe
from ..worker.errors import Cancelled
from .base import KernelInfo

#: Snapping tolerance handed to DeviceFlow. It is the kernel's own default:
#: a cleanup epsilon for polygon arithmetic, not a simulation resolution.
GEOMETRY_GRID_UM = 1e-6

#: Conformal resolution used when a project does not name one.
DEFAULT_RESOLUTION_UM = 0.01

#: Segments per quarter circle when a Quick Sketch's round shape becomes a
#: polygon: 32 segments round the whole circle. Every segment of a circle
#: costs eight triangles of the display mesh -- two on each of the two
#: walls it is part of and one on each of the four caps it wedges -- so a
#: block with one circular recess and a pillar is 8N + 12 triangles, and
#: this was 64 (256 segments, 2060 triangles) for a chord that misses the
#: true circle by 26 picometres. At 32 segments that becomes 268 triangles
#: and a chord error of r/600, which is a nanometre on a 340 nm radius:
#: far under any resolution the kernel is asked to work at, and far under
#: a pixel. A circle much larger than the features around it is the one
#: case where this is coarse -- 32 segments of a 50 um radius miss by
#: 240 nm -- so a big round shape is better drawn as a polygon.
SKETCH_QUAD_SEGS = 8

SUBSTRATE_MATERIAL = "Si"
BACKGROUND_RGB = (247, 249, 252)
#: Cut positions offered along an axis. The geometry is continuous; this is
#: only how many stops the client's slider has.
SECTION_POSITIONS = 201
#: Longest edge of a rendered picture, before the sampling factor.
BASE_PIXELS = 900
MAXIMUM_PIXELS = 2600

_ROLES = {
    "semiconductor": "semiconductor",
    "dielectric": "dielectric",
    "metal": "metal",
    "mask": "resist",
    "resist": "resist",
    "substrate": "substrate",
}


class SlabError(ValueError):
    """A step the slab kernel cannot execute, explained in its message."""


def _window_height(project: ProjectDefinition) -> float:
    return float(project.grid["z_max"]) - float(project.grid["z_min"])


def _check_length(value: float, what: str, project: ProjectDefinition) -> None:
    """Refuse a length no window could hold; it is almost always a unit slip.

    Lengths are micrometres. A 50 µm film on a 1.2 µm window is what typing
    "50" for 50 nm produces, and running it would either take the kernel past
    its own ceilings or fill the window edge to edge, neither of which is
    what was meant.
    """
    height = _window_height(project)
    if value > height:
        raise SlabError(
            f"{what} of {value:g} µm is taller than the whole project window "
            f"({height:g} µm from z = {float(project.grid['z_min']):g} to "
            f"{float(project.grid['z_max']):g}). Lengths are in micrometres: 50 nm is 0.05."
        )


def _translate(error: DeviceFlowError, project: ProjectDefinition) -> SlabError:
    """DeviceFlow's own message, with the words that apply in this workspace."""
    message = str(error)
    if "z samples" in message:
        resolution_nm = resolution_um(project) * 1000.0
        message += (
            f" In this workspace that means: the conformal walk at the current "
            f"{resolution_nm:g} nm resolution needs more steps than the kernel allows "
            "for this film; use a coarser resolution (the nm button in the top bar) "
            "or a thinner film."
        )
    return SlabError(message)


def _role(category: str) -> str:
    return _ROLES.get(category.strip().lower(), "other")


def material_table(materials: Sequence[MaterialDefinition]) -> dict[str, dict[str, Any]]:
    """The project's materials in the shape DeviceFlow's registry reads."""
    return {
        material.name: {"role": _role(material.category), "color": material.color}
        for material in materials
    }


def _window(project: ProjectDefinition) -> tuple[float, float, float, float]:
    grid = project.grid
    return (
        float(grid["x_min"]),
        float(grid["y_min"]),
        float(grid["x_max"]),
        float(grid["y_max"]),
    )


def resolution_xy_um(project: ProjectDefinition) -> float:
    """The XY arc sagitta: its own value, or the z step when none is set."""
    value = getattr(project, "resolution_xy_um", None)
    return resolution_um(project) if value is None else float(value)


def resolution_um(project: ProjectDefinition) -> float:
    value = getattr(project, "resolution_um", None)
    return DEFAULT_RESOLUTION_UM if not value else float(value)


class SlabState:
    """A DeviceFlow device and the wafer offset the workspace displays it at."""

    def __init__(self, device: Device, z_offset: float) -> None:
        self.device = device
        self.z_offset = float(z_offset)
        #: Where this state was stored, so its display mesh can live beside it.
        self.path: Path | None = None
        #: The display mesh, once built, per triangulator: material ->
        #: (vertices, triangles, one flag per triangle saying it lies
        #: against another material, and which one: its index in the mesh
        #: order, -1 for none).
        self.display_meshes: dict[tuple[str, bool], DisplayMeshes] = {}
        #: Held while the mesh is being built, so a view asked for during
        #: the background build waits for it instead of building a second one.
        self.mesh_lock = threading.Lock()

    @property
    def priority(self) -> list[str]:
        """Materials that own geometry, in the order they first appear."""
        names: list[str] = []
        for slab in self.device._state.slabs:
            for material in slab.regions:
                if material.name not in names:
                    names.append(material.name)
        return names

    def working_copy(self) -> Device:
        """A device that can be advanced without touching this state."""
        return self.device._clone(self.device._state, self.device.history)

    def save(self, path: Path) -> None:
        Path(path).write_bytes(
            encode_state(
                self.device._state,
                self.device._materials,
                metadata={
                    "zOffsetUm": self.z_offset,
                    "deviceName": self.device.name,
                    "xyResolutionUm": self.device.xy_resolution,
                },
                conformal_resolution=self.device.conformal_resolution,
            )
        )
        self.path = Path(path)

    @classmethod
    def load(cls, path: Path) -> "SlabState":
        restored = decode_state(Path(path).read_bytes())
        resolution = restored.conformal_resolution
        if resolution is None:
            raise SlabError("this stored state does not record its conformal resolution")
        # Rebuilt field by field: Device() would create an empty state and a
        # new registry, and the restored regions are keyed by the restored
        # registry's material objects.
        device = Device.__new__(Device)
        device.name = str(restored.metadata.get("deviceName", "process-studio"))
        device.units = "um"
        device.grid = float(restored.state.grid)
        device.conformal_resolution = float(resolution)
        xy = restored.metadata.get("xyResolutionUm")
        device.xy_resolution = None if xy is None else float(xy)
        device._materials = restored.materials
        device.masks = _factory(device.grid)
        device._state = restored.state
        device._history = []
        device._meshes = None
        device.record_steps = False
        device._step_dir = None
        device._step_options = {"render": False}
        device.verbose = False
        device._snapshots = []
        state = cls(device, float(restored.metadata.get("zOffsetUm", 0.0)))
        state.path = Path(path)
        return state


#: Triangulators the 3D view can be built with; see mesh/triangulate.py.
MESH_ENGINES = ENGINES


DisplayMeshes = dict[str, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]]


def display_meshes(
    state: SlabState, engine: str | None = None, buried: bool = False
) -> DisplayMeshes:
    """The 3D view's triangles for each material, built once per stored state.

    Building the mesh is the slow part of the slab kernel: seconds for a few
    conformal films, longer for a filled and polished stack, and nothing in
    it depends on how the view is drawn. So it is built once and kept twice:
    on the state object, which the worker's state cache holds, and in a file
    beside the snapshot, so reopening the workspace tomorrow does not pay
    for it again. The vertices are welded and the triangles indexed, which
    is a third of the size of one vertex per corner; the viewer shades each
    face on its own from the indexed form.

    ``engine`` picks the triangulator (see ``mesh.triangulate``). The two
    describe the same solid, so this is a comparison switch rather than a
    setting that changes an answer; each is kept on its own, because a mesh
    built one way is not what the other asked to look at.

    ``buried`` asks for the faces that lie against another material as
    well. The view leaves them out while both materials are shown -- two
    copies of one face fight for the same pixels -- so they are worth
    nothing until one is hidden, and in a stack they are almost the whole
    mesh: a 5x5 hole block is 90,644 triangles with them and 6,572
    without, 1.98 s against 0.70 s. They are built when something asks to
    see behind a material, and kept separately once built.
    """
    engine = engine or DEFAULT_ENGINE
    key = (engine, bool(buried))
    built = state.display_meshes.get(key)
    if built is not None:
        return built
    with state.mesh_lock:
        if key not in state.display_meshes:
            state.display_meshes[key] = _load_or_build_meshes(state, engine, bool(buried))
        return state.display_meshes[key]


def _sidecar_path(state: SlabState, engine: str, buried: bool) -> Path | None:
    """Where this mesh is remembered beside the snapshot.

    The name carries both choices: a mesh without its buried faces is not
    the one an export or a peek behind a material needs, and neither is a
    mesh the other triangulator built.
    """
    if state.path is None:
        return None
    return state.path.with_name(
        f"{state.path.name}.mesh-{engine}-{'full' if buried else 'free'}.npz"
    )


def _load_or_build_meshes(state: SlabState, engine: str, buried: bool) -> DisplayMeshes:
    sidecar = _sidecar_path(state, engine, buried)
    meshes: DisplayMeshes | None = None
    if sidecar is not None and sidecar.is_file():
        try:
            with np.load(sidecar) as stored:
                # A file from before the engine was recorded, or from the
                # other one, describes the same solid but not the triangles
                # that were asked for.
                if str(stored["engine"]) != engine or bool(stored["buried"]) != buried:
                    raise KeyError("engine")
                names = [str(name) for name in stored["materials"]]
                meshes = {
                    name: (
                        stored[f"{index}.vertices"],
                        stored[f"{index}.faces"],
                        stored[f"{index}.interface"],
                        stored[f"{index}.neighbour"],
                    )
                    for index, name in enumerate(names)
                }
        except (OSError, KeyError, ValueError):
            meshes = None  # an unreadable or older sidecar is simply rebuilt
    if meshes is None:
        from deviceflow._internal.mesh.builder import build_material_meshes
        from deviceflow._internal.mesh.triangulate import using

        state.device._state.validate()
        with using(engine):
            built = build_material_meshes(state.device._state, manifold=False, buried=buried)
        meshes = {
            material.name: (
                # A material with no free surface has an empty mesh, whose
                # arrays come back flat; the view wants (n, 3) either way.
                np.ascontiguousarray(mesh.vertices, dtype=np.float32).reshape(-1, 3),
                np.ascontiguousarray(mesh.faces, dtype=np.uint32).reshape(-1, 3),
                # A face against another material is the same face in that
                # material's mesh. The viewer leaves such faces out while
                # that other material is shown, so two copies never fight for
                # the same pixels, and draws them once it is hidden; so each
                # face also says which material, by its place in this order.
                np.ascontiguousarray(mesh.metadata["interface_faces"], dtype=np.uint8),
                np.ascontiguousarray(mesh.metadata["neighbour_faces"], dtype=np.int16),
            )
            for material, mesh in built.items()
        }
        if sidecar is not None:
            arrays: dict[str, np.ndarray] = {
                "materials": np.array(list(meshes), dtype=str),
                "engine": np.array(engine),
                "buried": np.array(buried),
            }
            for index, (vertices, faces, interface, neighbour) in enumerate(meshes.values()):
                arrays[f"{index}.vertices"] = vertices
                arrays[f"{index}.faces"] = faces
                arrays[f"{index}.interface"] = interface
                arrays[f"{index}.neighbour"] = neighbour
            try:
                np.savez(sidecar, **arrays)
            except OSError:
                pass  # the view still works; it is only not remembered
    return meshes


def _factory(grid: float):
    from deviceflow.mask import MaskFactory

    return MaskFactory(grid=grid)


def _ensure_material(
    device: Device, name: str, materials: Sequence[MaterialDefinition] = ()
) -> None:
    """Define ``name`` in the device from the workspace's material library.

    The role and colour are handed over explicitly: a device restored from a
    stored step carries the library as it was when that step ran, so a
    material added to the library since would otherwise be unknown to it.
    """
    if name in device._materials:
        return
    definition = next((m for m in materials if m.name == name), None)
    if definition is None:
        device.material(name)
        return
    device.material(name, role=_role(definition.category), color=definition.color)


# -- masks -----------------------------------------------------------------


def _instances(shape: SketchShape) -> list[tuple[float, float]]:
    count_x, count_y, pitch_x, pitch_y = shape.array
    return [
        (
            (ix - (count_x - 1) / 2.0) * pitch_x,
            (iy - (count_y - 1) / 2.0) * pitch_y,
        )
        for iy in range(count_y)
        for ix in range(count_x)
    ]


def _primitive(shape: SketchShape, offset_x: float, offset_y: float):
    parameters = shape.parameters
    if shape.kind == "rectangle":
        center_x, center_y = parameters.get("center", (0.0, 0.0))
        width, height = (float(value) for value in parameters["size"])
        if min(width, height) <= 0.0:
            raise SlabError("rectangle size must be positive")
        center_x += offset_x
        center_y += offset_y
        return box(
            center_x - width / 2.0,
            center_y - height / 2.0,
            center_x + width / 2.0,
            center_y + height / 2.0,
        )
    if shape.kind == "circle":
        center_x, center_y = parameters.get("center", (0.0, 0.0))
        radius = float(parameters["radius"])
        if radius <= 0.0:
            raise SlabError("circle radius must be positive")
        return Point(center_x + offset_x, center_y + offset_y).buffer(
            radius, quad_segs=SKETCH_QUAD_SEGS
        )
    points = np.asarray(parameters["points"], dtype=float) + [offset_x, offset_y]
    if shape.kind == "polygon":
        if len(points) < 3:
            raise SlabError("a polygon needs at least three points")
        return Polygon(points)
    width = float(parameters["width"])
    if width <= 0.0:
        raise SlabError("path width must be positive")
    return LineString(points).buffer(
        width / 2.0, quad_segs=SKETCH_QUAD_SEGS, cap_style="round"
    )


def sketch_geometry(sketch: QuickSketch, window):
    """The sketch as exact polygons, with the same CSG order the fields use."""
    combined = None
    for shape in sketch.shapes:
        pieces = [_primitive(shape, x, y) for x, y in _instances(shape)]
        geometry = shapely.union_all([shapely.make_valid(piece) for piece in pieces])
        if combined is None:
            combined = window.difference(geometry) if shape.operation == "subtract" else geometry
        elif shape.operation == "merge":
            combined = combined.union(geometry)
        elif shape.operation == "subtract":
            combined = combined.difference(geometry)
        else:
            combined = combined.intersection(geometry)
    if combined is None:
        raise SlabError("this quick sketch has no shapes")
    return combined


def step_mask(
    device: Device,
    step: ProcessStep,
    *,
    project: ProjectDefinition,
    parameters: Mapping[str, Any],
    sketches: Mapping[str, QuickSketch],
) -> Mask | None:
    """The step's opening, or None for the whole device window."""
    x_min, y_min, x_max, y_max = device.bounds
    window = box(x_min, y_min, x_max, y_max)
    if step.mask_source == "none":
        return None
    if step.mask_source == "quick_sketch":
        sketch_id = str(parameters.get("sketch_id", "default"))
        if sketch_id not in sketches:
            raise SlabError(f"quick sketch {sketch_id!r} was not found")
        geometry = sketch_geometry(sketches[sketch_id], window)
        mask = Mask(shapely.make_valid(geometry), device.grid)
    else:
        if not project.gds_path:
            raise SlabError("the project has no GDS file")
        if step.layer is None or step.datatype is None:
            raise SlabError("GDS steps require a layer and a datatype")
        from deviceflow import Layout

        layout = Layout.from_gds(project.gds_path, window=None, grid=device.grid)
        mask = layout.mask(int(step.layer), int(step.datatype))
    if step.keep == "outside":
        mask = Mask(window, device.grid) - mask
    mask = mask.clip(device.bounds)
    if mask.is_empty:
        raise SlabError("this mask does not overlap the device window")
    return mask


# -- step translation ------------------------------------------------------


def _etch_rates(recipe: Recipe, parameters: Mapping[str, Any]) -> dict[str, float]:
    """The same rate table the level-set engine builds, from the same fields."""
    rates = {
        name: response.rate_um_per_min
        for name, response in recipe.material_responses.items()
    }
    overrides = parameters.get("material_rates", {})
    if isinstance(overrides, Mapping):
        rates.update({str(name): float(rate) for name, rate in overrides.items()})
    selected = str(parameters.get("material", ""))
    if selected and parameters.get("rate") is not None:
        rates[selected] = float(parameters["rate"])
    stop_materials = parameters.get("stop_materials", [])
    if isinstance(stop_materials, str):
        stop_materials = [name.strip() for name in stop_materials.split(",") if name.strip()]
    for name in stop_materials:
        rates[str(name)] = 0.0
    if not rates:
        if not selected:
            raise SlabError("an etch step needs at least one material response")
        rates[selected] = float(parameters.get("rate", 1.0))
    return rates


def _deposit(
    device: Device,
    step: ProcessStep,
    recipe: Recipe,
    parameters,
    logger,
    project: ProjectDefinition,
    mask: Mask | None,
    materials: Sequence[MaterialDefinition] = (),
) -> None:
    material = str(parameters.get("material") or recipe.output_material or "")
    if not material:
        raise SlabError("a deposition step needs an output material")
    thickness = float(parameters.get("target", parameters.get("thickness", 0.0)))
    if thickness <= 0.0:
        raise SlabError("deposition thickness must be greater than zero")
    _check_length(thickness, "A film", project)
    mode = str(parameters.get("mode", "conformal")).strip().lower()
    if mode in {"directional", "evaporation", "fill", "directional prism"}:
        raise SlabError(
            f"the slab kernel cannot deposit in {mode!r} mode; it offers 'conformal' "
            "(equal thickness on every surface) and 'planar' (from straight above, "
            "no sidewall coverage). Use a level-set project for the other modes."
        )
    if mode not in {"conformal", "planar"}:
        raise SlabError(f"unknown deposition mode {mode!r}; expected 'conformal' or 'planar'")
    _ensure_material(device, material, materials)
    square = project.fidelity == "simplified"
    kind = f"{mode} ({'square' if square else 'detailed'})"
    if mask is None:
        logger(f"SLAB deposit {material} {thickness:g} um {kind}")
        device.deposit(material, thickness, mode=mode, square=square)
        return
    logger(f"SLAB deposit {material} {thickness:g} um {kind} inside the mask")
    _masked_deposit(device, material, thickness, mode, mask, square)


def _masked_deposit(
    device: Device, material: str, thickness: float, mode: str, mask: Mask, square: bool = False
) -> None:
    """Deposit only where the mask is open: the lift-off result, exactly.

    DeviceFlow deposits over the whole window. The film is therefore grown
    as a stand-in material, then cut down to the mask's columns slab by slab
    and handed to the real material. That is what a lift-off leaves behind:
    the film wherever the resist was open, including the sidewalls inside
    the opening, and nothing where it was covered. The stand-in never keeps
    any geometry, so it appears in no view and no material list.
    """
    registry = device._materials
    real = registry.resolve(material)
    stand_in_name = f"{material} (masked deposit)"
    if stand_in_name not in registry:
        registry.add(stand_in_name, role=real.role)
    stand_in = registry.resolve(stand_in_name)
    device.deposit(stand_in_name, thickness, mode=mode, square=square)
    state = device._state
    opening = state.clean(mask._geom)
    changed = []
    for slab in state.slabs:
        grown = slab.regions.pop(stand_in, None)
        if grown is None:
            continue
        kept = state.clean(grown.intersection(opening))
        if not kept.is_empty:
            existing = slab.regions.get(real)
            slab.regions[real] = (
                kept if existing is None else state.clean(shapely.union_all([existing, kept]))
            )
        changed.append(slab)
    state.harmonize(changed)
    state.consolidate()
    state.validate()


def _etch(
    device: Device,
    step: ProcessStep,
    recipe: Recipe,
    parameters,
    mask: Mask | None,
    logger,
    project: ProjectDefinition,
    materials: Sequence[MaterialDefinition] = (),
) -> None:
    rates = _etch_rates(recipe, parameters)
    active = {name: rate for name, rate in rates.items() if rate > 0.0}
    if not active:
        raise SlabError("every material in this etch has rate zero; nothing would be removed")
    for name in rates:
        # An etch may list a material this device never grew; the kernel
        # only needs to know the name to give it a rate.
        _ensure_material(device, name, materials)
    fraction = float(parameters.get("directional_fraction", 1.0))
    if fraction not in (0.0, 1.0):
        raise SlabError(
            f"the slab kernel etches either straight down (directional_fraction 1) or "
            f"isotropically (0); this step asks for {fraction:g}. Use a level-set "
            "project for a mixed profile."
        )
    if fraction == 1.0:
        etch = device.etch
        profile = "vertical"
    else:
        # The simplified front is a box instead of a ball: square corners in
        # z, one sample per plane, so a wet etch costs a few offsets per step.
        square = project.fidelity == "simplified"
        etch = functools.partial(device.wet_etch, square=square)
        profile = "isotropic (square)" if square else "isotropic"
    if parameters.get("target") is not None:
        depth = float(parameters["target"])
        if depth <= 0.0:
            raise SlabError("etch depth must be greater than zero")
        _check_length(depth, "An etch depth", project)
        reference = max(active, key=lambda name: active[name])
        selectivity = {name: rates[name] / rates[reference] for name in rates}
        logger(f"SLAB etch {profile} {depth:g} um of {reference} ({len(rates)} material(s))")
        etch(mask, depth=depth, selectivity=selectivity, reference=reference)
        return
    if parameters.get("time_min") is None:
        raise SlabError("an etch step needs a target depth or a time")
    minutes = float(parameters["time_min"])
    if minutes <= 0.0:
        raise SlabError("etch time must be greater than zero")
    logger(f"SLAB etch {profile} for {minutes:g} min")
    etch(
        mask,
        rates={name: f"{rate}um/min" for name, rate in rates.items()},
        time=f"{minutes}min",
    )


def _oxidize(
    device: Device,
    step: ProcessStep,
    recipe: Recipe,
    parameters,
    mask: Mask | None,
    logger,
    project: ProjectDefinition,
    materials: Sequence[MaterialDefinition] = (),
) -> None:
    """The listed materials lose a skin from every exposed surface; it becomes the product."""
    rates = _etch_rates(recipe, parameters)
    active = {name: rate for name, rate in rates.items() if rate > 0.0}
    if not active:
        raise SlabError("an oxidation needs at least one material with a positive rate")
    product = str(parameters.get("material") or recipe.output_material or "SiO2")
    if product in active:
        raise SlabError(f"{product} cannot be both oxidised and the oxide it becomes")
    for name in (*rates, product):
        _ensure_material(device, name, materials)
    square = project.fidelity == "simplified"
    if parameters.get("target") is not None:
        depth = float(parameters["target"])
        if depth <= 0.0:
            raise SlabError("the consumed thickness must be greater than zero")
        _check_length(depth, "An oxidation depth", project)
        reference = max(active, key=lambda name: active[name])
        selectivity = {name: rates[name] / rates[reference] for name in rates}
        logger(f"SLAB oxidize {depth:g} um of {reference} into {product} ({len(active)} material(s))")
        device.oxidize(mask, depth=depth, selectivity=selectivity, reference=reference, product=product, square=square)
        return
    if parameters.get("time_min") is None:
        raise SlabError("an oxidation step needs a consumed thickness or a time")
    minutes = float(parameters["time_min"])
    if minutes <= 0.0:
        raise SlabError("oxidation time must be greater than zero")
    logger(f"SLAB oxidize for {minutes:g} min into {product}")
    device.oxidize(
        mask,
        rates={name: f"{rate}um/min" for name, rate in rates.items()},
        time=f"{minutes}min",
        product=product,
        square=square,
    )


def _cmp(device: Device, recipe: Recipe, parameters, z_offset: float, logger) -> None:
    selected = parameters.get("materials")
    stop = next(
        (name for name, response in recipe.material_responses.items() if response.stop_layer),
        parameters.get("stop_material"),
    )
    if selected or stop:
        logger(
            "SLAB cmp is unselective: it removes everything above the plane, so the "
            "step's material list and stop layer do not apply."
        )
    if parameters.get("target_z") is not None:
        height = float(parameters["target_z"]) - z_offset
    else:
        top = device.top
        if top is None:
            raise SlabError("this device has no geometry to polish")
        height = top - float(parameters.get("removal_amount", 0.0))
    if height <= 0.0:
        raise SlabError("the polish plane is at or below the wafer floor")
    logger(f"SLAB cmp to z={height + z_offset:g} um")
    device.cmp(height)


# -- pictures --------------------------------------------------------------


def _rgb(color: str) -> tuple[int, int, int]:
    value = color.strip().lstrip("#")
    if len(value) != 6:
        return (124, 131, 160)
    try:
        return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))
    except ValueError:
        return (124, 131, 160)


def _png(rgb: np.ndarray) -> str:
    buffer = io.BytesIO()
    Image.fromarray(rgb, mode="RGB").save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _raster(
    shapes: Sequence[tuple[Sequence[Sequence[tuple[float, float]]], str]],
    colors: Mapping[str, str],
    extent: tuple[float, float, float, float],
    pixels_per_um: float,
) -> np.ndarray:
    """Fill exact polygons into an image, holes included.

    Every material is drawn into its own bitmap and composited, so a cavity in
    one material shows what is really inside it rather than the background.
    """
    horizontal_min, horizontal_max, vertical_min, vertical_max = extent
    width = max(2, min(MAXIMUM_PIXELS, round((horizontal_max - horizontal_min) * pixels_per_um)))
    height = max(2, min(MAXIMUM_PIXELS, round((vertical_max - vertical_min) * pixels_per_um)))
    scale_x = (width - 1) / max(horizontal_max - horizontal_min, 1e-12)
    scale_y = (height - 1) / max(vertical_max - vertical_min, 1e-12)
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    canvas[...] = BACKGROUND_RGB
    for rings, name in shapes:
        if not rings:
            continue
        stencil = Image.new("1", (width, height), 0)
        pen = ImageDraw.Draw(stencil)
        for index, ring in enumerate(rings):
            points = [
                (
                    (x - horizontal_min) * scale_x,
                    (vertical_max - y) * scale_y,
                )
                for x, y in ring
            ]
            if len(points) >= 3:
                pen.polygon(points, fill=0 if index else 1, outline=0 if index else 1)
        canvas[np.asarray(stencil, dtype=bool)] = _rgb(colors.get(name, "#7c83a0"))
    return canvas


def _section_shapes(section, z_offset: float):
    # `polygons()` returns exterior rings only, which would paint over every
    # cavity; `_shapes` keeps the holes, which is what a filled picture needs.
    shapes, _, _, _ = section._shapes(1.0)
    return [
        ([[(s, z + z_offset) for s, z in ring] for ring in rings], name)
        for rings, _color, name in shapes
    ]


def _top_view_shapes(top_view):
    return [(rings, name) for rings, _color, name in top_view._shapes()]


def _rings(geometry) -> list[list[tuple[float, float]]]:
    """Exterior then holes of every polygon, the way ``_raster`` fills them."""
    from shapely.geometry.polygon import orient

    shapes = []
    parts = geometry.geoms if hasattr(geometry, "geoms") else [geometry]
    for polygon in parts:
        if polygon.is_empty or polygon.geom_type != "Polygon":
            continue
        polygon = orient(polygon)
        shapes.append(
            [list(polygon.exterior.coords[:-1])] + [list(r.coords[:-1]) for r in polygon.interiors]
        )
    return shapes


def _height_shapes(top_view, z_offset: float):
    """The top view split by surface height: (shapes, colours, legend levels).

    Every visible piece carries the height of the slab it belongs to, so
    the levels are exact: one per distinct plane the sky can see.
    """
    by_height: dict[float, list] = {}
    for _name, z_top, region in top_view._pieces:
        by_height.setdefault(round(float(z_top), 9), []).append(region)
    levels = height_levels([z + z_offset for z in by_height])
    shapes, colors = [], {}
    for index, level in enumerate(levels):
        name = f"z={level['z']:.6g}"
        colors[name] = level["color"]
        union = shapely.unary_union(by_height[round(level["z"] - z_offset, 9)])
        shapes.extend((rings, name) for rings in _rings(union))
    return shapes, colors, levels


class SlabKernel:
    """Exact slab geometry, from the DeviceFlow 0.2.0 core."""

    info = KernelInfo(
        id="slab",
        name="Slab (DeviceFlow)",
        version="0.2.0",
        summary=(
            "Exact polygon slabs from the DeviceFlow core, the engine "
            "ProcessFlow-Emulator runs. Planar and conformal deposition, "
            "blanket or inside a mask, vertical and isotropic etching, "
            "unselective CMP. No grid to converge; conformal deposition is "
            "walked at the set resolution."
        ),
        process_types=("deposit", "etch", "cmp", "no_geometry", "oxidation"),
        mask_sources=("none", "quick_sketch", "gds"),
        deposition_modes=("conformal", "planar"),
        directional_fractions=(0.0, 1.0),
        surfaces=True,
        spacing_role="conformal_resolution",
        spacing_presets_nm=(25.0, 10.0, 2.0),
        maximum_nodes=None,
        snapshot_suffix=".dfz",
    )

    def initial_state(
        self,
        project: ProjectDefinition,
        *,
        materials: Sequence[MaterialDefinition] = (),
    ) -> SlabState:
        """The bare wafer: one substrate slab, its top face at z = 0."""
        x_min, y_min, x_max, y_max = _window(project)
        z_offset = float(project.grid["z_min"])
        thickness = -z_offset
        if thickness <= 0.0:
            raise SlabError("the project window needs room below z = 0 for the substrate")
        device = Device(
            name=project.name,
            bounds=(x_min, y_min, x_max, y_max),
            grid=GEOMETRY_GRID_UM,
            conformal_resolution=resolution_um(project),
            xy_resolution=resolution_xy_um(project),
            materials=material_table(materials),
            verbose=False,
        )
        device.material(SUBSTRATE_MATERIAL)
        device.deposit(SUBSTRATE_MATERIAL, thickness, mode="planar")
        return SlabState(device, z_offset)

    def run_step(
        self,
        state: SlabState,
        step: ProcessStep,
        *,
        project: ProjectDefinition,
        recipes: Mapping[str, Recipe],
        sketches: Mapping[str, QuickSketch],
        logger: Callable[[str], None],
        materials: Sequence[MaterialDefinition] = (),
        should_cancel: Callable[[], bool] | None = None,
    ) -> SlabState:
        device = state.working_copy()
        if not step.enabled:
            logger(f"SKIP {step.name}: disabled")
            return SlabState(device, state.z_offset)
        recipe = step.effective_recipe(recipes)
        parameters = dict(recipe.parameters)
        logger(f"RUN {step.name} [{recipe.process_type.value}]")
        try:
            # The geometry walks hundreds of z samples inside these calls;
            # the hook lets it give up part-way instead of only between
            # steps. `device` is this step's own working copy, so a step
            # that stops leaves the caller's state untouched.
            with cancelling(should_cancel):
                if recipe.process_type is ProcessType.DEPOSIT:
                    mask = step_mask(
                        device, step, project=project, parameters=parameters, sketches=sketches
                    )
                    _deposit(device, step, recipe, parameters, logger, project, mask, materials)
                elif recipe.process_type is ProcessType.ETCH:
                    mask = step_mask(
                        device, step, project=project, parameters=parameters, sketches=sketches
                    )
                    _etch(device, step, recipe, parameters, mask, logger, project, materials)
                elif recipe.process_type is ProcessType.CMP:
                    _cmp(device, recipe, parameters, state.z_offset, logger)
                elif recipe.process_type is ProcessType.OXIDATION:
                    mask = step_mask(
                        device, step, project=project, parameters=parameters, sketches=sketches
                    )
                    _oxidize(device, step, recipe, parameters, mask, logger, project, materials)
        except DeviceFlowCancelled as error:
            raise Cancelled(f"Stopped during {step.name}; it did not finish.") from error
        except DeviceFlowError as error:
            raise _translate(error, project) from error
        return SlabState(device, state.z_offset)

    def load_state(self, path: Path) -> SlabState:
        return SlabState.load(path)

    def state_materials(self, state: SlabState) -> list[str]:
        return state.priority

    def warm_views(self, state: SlabState) -> None:
        """Build the display mesh now, so the 3D view does not have to.

        Only what the view opens with: the free surface. The buried faces
        cost several times as much and are worth nothing until someone
        hides a material to look behind it.
        """
        display_meshes(state)

    def state_bytes(self, state: SlabState) -> int:
        # Polygons are the bulk of a slab state: two doubles per coordinate
        # plus shapely's bookkeeping, which the factor of four stands in for.
        coordinates = sum(
            int(shapely.get_num_coordinates(region))
            for slab in state.device._state.slabs
            for region in slab.regions.values()
        )
        for meshes in state.display_meshes.values():
            coordinates += sum(
                (vertices.nbytes + faces.nbytes) // 8
                for vertices, faces, _interface, _neighbour in meshes.values()
            )
        return 64 * 1024 + coordinates * 4 * 16

    # -- views -------------------------------------------------------------

    def surfaces(
        self,
        state: SlabState,
        *,
        project: ProjectDefinition,
        interpolation: int = 1,
        materials: Sequence[str] | None = None,
        triangulation: str | None = None,
        buried: bool = False,
    ) -> dict[str, Any]:
        device = state.device
        engine = triangulation or DEFAULT_ENGINE
        meshes = display_meshes(state, engine, buried)
        selected = [
            name
            for name in state.priority
            if name in meshes and (materials is None or name in materials)
        ]
        # Faces are labelled with the material they lie against by its place
        # in the mesh order, which is the order the meshes were built in.
        order = list(meshes)
        payload = []
        for name in selected:
            vertices, faces, interface, neighbour = meshes[name]
            against = np.where(neighbour < 0, 255, neighbour).astype(np.uint8)
            # Slab faces are flat and meet at sharp edges, and a flat face is
            # two triangles however large it is. Shading them with averaged
            # vertex normals would blend the top face into the walls along
            # those two diagonals, which shows up as a dark cross on a square.
            # So no normals are sent: the viewer gives each triangle its own
            # corners and its face normal, which it does faster than the
            # worker can serialise three copies of every vertex.
            positions = vertices.copy()
            positions[:, 2] += np.float32(state.z_offset)
            payload.append(
                {
                    "material": name,
                    "positions": _encode(positions),
                    "normals": "",
                    "shading": "flat",
                    "indices": _encode(faces),
                    "interfaceFaces": _encode(interface),
                    "neighbourFaces": _encode(against),
                    "neighbourMaterials": order,
                    "vertexCount": int(len(positions)),
                    "triangleCount": int(len(faces)),
                }
            )
        x_min, y_min, x_max, y_max = device.bounds
        top = device.top or 0.0
        return {
            "interpolation": 1,
            "exact": True,
            "triangulation": engine,
            "buried": bool(buried),
            "bounds": {
                "xMin": x_min,
                "xMax": x_max,
                "yMin": y_min,
                "yMax": y_max,
                "zMin": state.z_offset,
                "zMax": max(float(project.grid["z_max"]), top + state.z_offset),
            },
            "surfaces": payload,
        }

    def section(
        self,
        state: SlabState,
        colors: Mapping[str, str],
        *,
        project: ProjectDefinition,
        axis: str = "y",
        position: float | None = None,
        interpolation: int = 1,
        line: tuple[tuple[float, float], tuple[float, float]] | None = None,
    ) -> dict[str, Any]:
        device = state.device
        x_min, y_min, x_max, y_max = device.bounds
        if line is not None:
            return self._line_section(
                state, colors, project=project, line=line, interpolation=interpolation
            )
        if axis == "y":
            coordinates = np.linspace(y_min, y_max, SECTION_POSITIONS)
        elif axis == "x":
            coordinates = np.linspace(x_min, x_max, SECTION_POSITIONS)
        else:
            raise SlabError("section axis must be 'x' or 'y'")
        default = 0.5 * (coordinates[0] + coordinates[-1])
        index = int(np.argmin(np.abs(coordinates - (default if position is None else position))))
        cut = float(coordinates[index])
        if axis == "y":
            start, end = (x_min, cut), (x_max, cut)
            horizontal = (x_min, x_max)
            horizontal_label = "x"
        else:
            start, end = (cut, y_min), (cut, y_max)
            horizontal = (y_min, y_max)
            horizontal_label = "y"
        section = device.cross_section(start, end)
        top = device.top or 0.0
        extent = (
            horizontal[0],
            horizontal[1],
            state.z_offset,
            max(float(project.grid["z_max"]), top + state.z_offset),
        )
        shapes = [
            ([[(s + horizontal[0], z) for s, z in ring] for ring in rings], name)
            for rings, name in _section_shapes(section, state.z_offset)
        ]
        pixels_per_um = _pixels_per_um(extent, interpolation)
        rgb = _raster(shapes, colors, extent, pixels_per_um)
        return {
            "image": _png(rgb),
            "axis": axis,
            "position": cut,
            "index": index,
            "interpolation": interpolation,
            "sampledSpacingUm": 1.0 / pixels_per_um,
            "exact": True,
            "width": int(rgb.shape[1]),
            "height": int(rgb.shape[0]),
            "horizontalAxis": horizontal_label,
            "extent": {
                "horizontalMin": extent[0],
                "horizontalMax": extent[1],
                "verticalMin": extent[2],
                "verticalMax": extent[3],
            },
            "positions": [float(value) for value in coordinates],
        }

    def _line_section(
        self,
        state: SlabState,
        colors: Mapping[str, str],
        *,
        project: ProjectDefinition,
        line: tuple[tuple[float, float], tuple[float, float]],
        interpolation: int,
    ) -> dict[str, Any]:
        """The exact cut along any line: DeviceFlow sections are not axis-bound."""
        device = state.device
        start, end = line
        length = float(math.hypot(end[0] - start[0], end[1] - start[1]))
        if length <= 0.0:
            raise SlabError("a section line needs two distinct points")
        section = device.cross_section(start, end)
        top = device.top or 0.0
        extent = (
            0.0,
            length,
            state.z_offset,
            max(float(project.grid["z_max"]), top + state.z_offset),
        )
        pixels_per_um = _pixels_per_um(extent, interpolation)
        rgb = _raster(_section_shapes(section, state.z_offset), colors, extent, pixels_per_um)
        return {
            "image": _png(rgb),
            "axis": "line",
            "position": 0.0,
            "index": 0,
            "interpolation": interpolation,
            "sampledSpacingUm": 1.0 / pixels_per_um,
            "exact": True,
            "width": int(rgb.shape[1]),
            "height": int(rgb.shape[0]),
            "horizontalAxis": "s",
            "extent": {
                "horizontalMin": 0.0,
                "horizontalMax": length,
                "verticalMin": extent[2],
                "verticalMax": extent[3],
            },
            "positions": [],
            "line": {
                "start": [float(start[0]), float(start[1])],
                "end": [float(end[0]), float(end[1])],
            },
        }

    def top_view(
        self,
        state: SlabState,
        colors: Mapping[str, str],
        *,
        project: ProjectDefinition,
        shading: str = "material",
    ) -> dict[str, Any]:
        device = state.device
        x_min, y_min, x_max, y_max = device.bounds
        extent = (x_min, x_max, y_min, y_max)
        top_view = device.top_view()
        levels: list[dict[str, Any]] = []
        if shading == "height":
            shapes, colors, levels = _height_shapes(top_view, state.z_offset)
        else:
            shapes = _top_view_shapes(top_view)
        rgb = _raster(shapes, colors, extent, _pixels_per_um(extent, 1))
        return {
            "image": _png(rgb),
            "exact": True,
            "shading": "height" if shading == "height" else "material",
            "levels": levels,
            "width": int(rgb.shape[1]),
            "height": int(rgb.shape[0]),
            "extent": {
                "horizontalMin": x_min,
                "horizontalMax": x_max,
                "verticalMin": y_min,
                "verticalMax": y_max,
            },
        }


def _encode(array: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(array).tobytes()).decode("ascii")


def _pixels_per_um(extent: tuple[float, float, float, float], interpolation: int) -> float:
    span = max(extent[1] - extent[0], extent[3] - extent[2], 1e-9)
    return BASE_PIXELS * max(1, int(interpolation)) / span
