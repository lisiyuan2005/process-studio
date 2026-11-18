"""The OpenVDB kernel: per-material level sets on a voxel lattice.

Where the slab kernel keeps exact polygons, this one keeps every material
as a signed distance field in an OpenVDB grid and places surfaces between
voxel centres, so its geometry is as fine as the voxel size the project
sets (the resolution button) -- no finer, and no coarser than that either
on a curved or tilted face, which is where the slab kernel's cost grows.

The steps mean what they mean on the slab kernel, and the two film models
are the same two shapes: **simplified** grows films and etch fronts with
square corners in z (a cylinder: round in XY, flat on top), **detailed**
with round ones (a ball: the exact Euclidean offset, and a fast-marching
front for an isotropic etch). The window is closed: its four walls and
its floor are not surfaces, the wafer simply continues past them.
"""

from __future__ import annotations

import base64
import io
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import shapely
from PIL import Image
import skfmm  # noqa: F401  (the detailed isotropic front; the kernel needs it)
from scipy import ndimage
from shapely.geometry import box

from ...layout.quick_sketch import QuickSketch
from ...models import MaterialDefinition, ProcessStep, ProcessType, ProjectDefinition, Recipe
from ...worker.errors import Cancelled
from ..base import KernelInfo
from ..slab import (
    BACKGROUND_RGB,
    BASE_PIXELS,
    MAXIMUM_PIXELS,
    SECTION_POSITIONS,
    STEP_LINE_SHADE,
    SUBSTRATE_MATERIAL,
    _check_length,
    _deposit_thickness,
    _etch_rates,
    _rgb,
    step_mask,
)
from . import _vdbcore as vc
from . import ops
from .field import HALF_WIDTH
from .state import Lattice, VdbState, initial

#: Voxel size when a project names none.
DEFAULT_VOXEL_UM = 0.005
#: Voxels a project window may hold. Every material is a dense block of
#: this many floats while a step runs, so the ceiling is a memory one.
MAXIMUM_NODES = 40_000_000


class OpenVDBError(ValueError):
    """A step the OpenVDB kernel cannot execute, explained in its message."""


def voxel_um(project: ProjectDefinition) -> float:
    value = getattr(project, "resolution_um", None)
    return DEFAULT_VOXEL_UM if not value else float(value)


def node_count(grid: Mapping[str, Any], spacing_um: float) -> int:
    spans = [
        float(grid[high]) - float(grid[low])
        for low, high in (("x_min", "x_max"), ("y_min", "y_max"), ("z_min", "z_max"))
    ]
    return int(np.prod([max(1, math.ceil(span / spacing_um - 1e-6)) for span in spans]))


def lattice_for(project: ProjectDefinition) -> Lattice:
    g = voxel_um(project)
    grid = project.grid
    nodes = node_count(grid, g)
    if nodes > MAXIMUM_NODES:
        raise OpenVDBError(
            f"a {g * 1000:g} nm voxel makes this window {nodes / 1e6:.1f} million voxels; "
            f"the OpenVDB kernel works up to {MAXIMUM_NODES / 1e6:.0f} million. Use a coarser "
            "resolution (the nm button in the top bar) or a smaller window."
        )
    bounds = (grid["x_min"], grid["y_min"], grid["x_max"], grid["y_max"])
    return Lattice.for_window(bounds, float(grid["z_min"]), g)


def _opening_fn(mask_geometry):
    """The mask's opening as a signed XY distance on a block's columns
    (negative inside), with the window edge not counted as an edge: past
    it the wafer, and the mask, continue."""
    if mask_geometry is None:
        return None

    def build(block) -> np.ndarray:
        lat = block.lattice
        window = box(lat.x0, lat.y0, lat.x1, lat.y1)
        x, y, _ = block.axes()
        xs, ys = np.broadcast_arrays(x[:, :, 0], y[:, :, 0])
        if lat.periodic:
            xs = lat.x0 + np.mod(xs - lat.x0, lat.x1 - lat.x0)
            ys = lat.y0 + np.mod(ys - lat.y0, lat.y1 - lat.y0)
        else:
            xs = np.clip(xs, lat.x0, lat.x1)
            ys = np.clip(ys, lat.y0, lat.y1)
        inside = shapely.contains_xy(mask_geometry, xs, ys) | shapely.intersects_xy(
            mask_geometry.boundary, xs, ys
        )
        edges = mask_geometry.boundary.difference(window.exterior.buffer(1e-9 * max(lat.g, 1e-9)))
        if edges.is_empty:
            far = 1e3 * (lat.x1 - lat.x0 + lat.y1 - lat.y0)
            return np.where(inside, -far, far).astype(np.float32)
        points = shapely.points(xs.ravel(), ys.ravel())
        distance = shapely.distance(points, edges).reshape(xs.shape)
        return np.where(inside, -distance, distance).astype(np.float32)

    return build


def _mask(state: VdbState, step: ProcessStep, project, parameters, sketches):
    lat = state.lattice
    stand_in = SimpleNamespace(bounds=(lat.x0, lat.y0, lat.x1, lat.y1), grid=1e-6)
    mask = step_mask(stand_in, step, project=project, parameters=parameters, sketches=sketches)
    return None if mask is None else shapely.make_valid(mask._geom)


def _png(rgb: np.ndarray) -> str:
    buffer = io.BytesIO()
    Image.fromarray(rgb, mode="RGB").save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _encode(array: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(array).tobytes()).decode("ascii")


def _pixels(extent, interpolation: int = 1) -> tuple[int, int]:
    h0, h1, v0, v1 = extent
    span = max(h1 - h0, v1 - v0, 1e-9)
    per_um = BASE_PIXELS * max(1, int(interpolation)) / span
    width = max(2, min(MAXIMUM_PIXELS, round((h1 - h0) * per_um)))
    height = max(2, min(MAXIMUM_PIXELS, round((v1 - v0) * per_um)))
    return width, height


def _paint(
    fields: Sequence[np.ndarray],
    names: Sequence[str],
    colors: Mapping[str, str],
    solid: np.ndarray | None = None,
) -> np.ndarray:
    """Each pixel in the colour of the material whose field is lowest there,
    where the solid is (``solid``, or else any field negative); the
    background elsewhere."""
    stack = np.stack(fields)
    which = stack.argmin(axis=0)
    inside = (stack.min(axis=0) < 0) if solid is None else (solid < 0)
    palette = np.array([_rgb(colors.get(name, "#7c83a0")) for name in names], dtype=np.uint8)
    rgb = np.empty(which.shape + (3,), dtype=np.uint8)
    rgb[...] = BACKGROUND_RGB
    rgb[inside] = palette[which[inside]]
    return rgb


class OpenVDBKernel:
    """Per-material level sets, through OpenVDB."""

    info = KernelInfo(
        id="openvdb",
        name="OpenVDB (level set)",
        version=f"0.1.0 / OpenVDB {vc.OPENVDB_VERSION}",
        summary=(
            "Every material a signed distance field in an OpenVDB grid; surfaces "
            "placed between voxel centres. Planar and conformal deposition, blanket "
            "or inside a mask, vertical and isotropic etching, oxidation, flip, CMP. "
            "Its cost follows the voxel count, not the shape: a curved or tilted "
            "face is no dearer than a flat one."
        ),
        process_types=("deposit", "etch", "cmp", "no_geometry", "oxidation", "flip"),
        mask_sources=("none", "quick_sketch", "gds"),
        deposition_modes=("conformal", "planar"),
        directional_fractions=(0.0, 1.0),
        surfaces=True,
        spacing_role="voxel",
        spacing_presets_nm=(10.0, 5.0, 2.0),
        maximum_nodes=MAXIMUM_NODES,
        snapshot_suffix=".vdbz",
    )

    # -- runs ---------------------------------------------------------------

    def initial_state(self, project: ProjectDefinition, *, materials: Sequence[MaterialDefinition] = ()) -> VdbState:
        if float(project.grid["z_min"]) >= 0.0:
            raise OpenVDBError("the project window needs room below z = 0 for the substrate")
        return initial(lattice_for(project), SUBSTRATE_MATERIAL)

    def run_step(
        self,
        state: VdbState,
        step: ProcessStep,
        *,
        project: ProjectDefinition,
        recipes: Mapping[str, Recipe],
        sketches: Mapping[str, QuickSketch],
        logger: Callable[[str], None],
        materials: Sequence[MaterialDefinition] = (),
        should_cancel: Callable[[], bool] | None = None,
    ) -> VdbState:
        if not step.enabled:
            logger(f"SKIP {step.name}: disabled")
            return state
        lat = lattice_for(project)
        if lat != state.lattice:
            raise OpenVDBError(
                "this state was computed on another voxel lattice than the project's; "
                "run the flow again from the start"
            )
        recipe = step.effective_recipe(recipes)
        parameters = dict(recipe.parameters)
        kind = recipe.process_type
        logger(f"RUN {step.name} [{kind.value}]")
        if parameters.get("resolution") not in (None, ""):
            logger(
                f"RESOLUTION this step asks for its own; the OpenVDB kernel works on the "
                f"project's {lat.g * 1000:g} nm voxels throughout"
            )

        def check() -> None:
            if should_cancel is not None and should_cancel():
                raise Cancelled(f"Stopped during {step.name}; it did not finish.")

        check()
        square = project.fidelity == "simplified"
        if kind is ProcessType.DEPOSIT:
            return self._deposit(state, step, recipe, parameters, project, sketches, logger, square)
        if kind is ProcessType.ETCH:
            return self._etch(state, step, recipe, parameters, project, sketches, logger, square, check)
        if kind is ProcessType.OXIDATION:
            return self._oxidize(state, step, recipe, parameters, project, sketches, logger, square, check)
        if kind is ProcessType.CMP:
            return self._cmp(state, recipe, parameters, logger)
        if kind is ProcessType.FLIP:
            axis = str(parameters.get("axis", "y")).strip().lower()
            if axis not in ("x", "y"):
                raise OpenVDBError(f"a flip turns the wafer about the x or the y axis, not {axis!r}")
            top = state.top()
            if top is None:
                raise OpenVDBError("there is nothing to flip: this state has no material")
            logger(f"VDB flip about the {axis} axis; {top - lat.z_floor:g} um tall")
            return ops.flip(state, axis, top)
        return state

    def _deposit(self, state, step, recipe, parameters, project, sketches, logger, square):
        material = str(parameters.get("material") or recipe.output_material or "")
        if not material:
            raise OpenVDBError("a deposition step needs an output material")
        thickness = _deposit_thickness(parameters)
        if thickness <= 0.0:
            raise OpenVDBError("deposition thickness must be greater than zero")
        _check_length(thickness, "A film", project)
        mode = str(parameters.get("mode", "conformal")).strip().lower()
        if mode in {"directional", "evaporation", "fill", "directional prism"}:
            raise OpenVDBError(
                f"this kernel cannot deposit in {mode!r} mode; it offers 'conformal' "
                "(equal thickness on every surface) and 'planar' (from straight above, "
                "sidewalls covered, nothing under an overhang)."
            )
        if mode not in {"conformal", "planar"}:
            raise OpenVDBError(f"unknown deposition mode {mode!r}; expected 'conformal' or 'planar'")
        geometry = _mask(state, step, project, parameters, sketches)
        shape = "square" if square else "detailed"
        where = " inside the mask" if geometry is not None else ""
        logger(f"VDB deposit {material} {thickness:g} um {mode} ({shape}){where}")
        return ops.deposit(
            state, material, thickness, planar=mode == "planar", square=square,
            opening_fn=_opening_fn(geometry),
        )

    @staticmethod
    def _budget(rates, parameters, project, what: str) -> tuple[float, str]:
        """(time in minutes, how it was given) from a target depth or a time."""
        active = {name: rate for name, rate in rates.items() if rate > 0.0}
        if parameters.get("target") is not None:
            depth = float(parameters["target"])
            if depth <= 0.0:
                raise OpenVDBError(f"{what} depth must be greater than zero")
            _check_length(depth, f"An {what} depth", project)
            reference = max(active, key=lambda name: active[name])
            return depth / active[reference], f"{depth:g} um of {reference}"
        if parameters.get("time_min") is None:
            raise OpenVDBError(f"an {what} step needs a target depth or a time")
        minutes = float(parameters["time_min"])
        if minutes <= 0.0:
            raise OpenVDBError(f"{what} time must be greater than zero")
        return minutes, f"for {minutes:g} min"

    def _etch(self, state, step, recipe, parameters, project, sketches, logger, square, check):
        rates = _etch_rates(recipe, parameters)
        if not any(rate > 0.0 for rate in rates.values()):
            raise OpenVDBError("every material in this etch has rate zero; nothing would be removed")
        fraction = float(parameters.get("directional_fraction", 1.0))
        if fraction not in (0.0, 1.0):
            raise OpenVDBError(
                f"this kernel etches either straight down (directional_fraction 1) or "
                f"isotropically (0); this step asks for {fraction:g}. A mixed profile "
                "has to be built as the two steps it is made of."
            )
        minutes, said = self._budget(rates, parameters, project, "etch")
        geometry = _mask(state, step, project, parameters, sketches)
        opening = _opening_fn(geometry)
        if fraction == 1.0:
            logger(f"VDB etch vertical {said} ({len(rates)} material(s))")
            return ops.vertical_etch(state, rates, minutes, opening_fn=opening)
        logger(f"VDB etch isotropic ({'square' if square else 'detailed'}) {said}")
        return ops.isotropic(state, rates, minutes, square=square, opening_fn=opening, check=check)

    def _oxidize(self, state, step, recipe, parameters, project, sketches, logger, square, check):
        rates = _etch_rates(recipe, parameters)
        active = {name: rate for name, rate in rates.items() if rate > 0.0}
        if not active:
            raise OpenVDBError("an oxidation needs at least one material with a positive rate")
        product = str(parameters.get("material") or recipe.output_material or "SiO2")
        if product in active:
            raise OpenVDBError(f"{product} cannot be both oxidised and the oxide it becomes")
        minutes, said = self._budget(rates, parameters, project, "oxidation")
        geometry = _mask(state, step, project, parameters, sketches)
        logger(f"VDB oxidize {said} into {product}")
        return ops.isotropic(
            state, rates, minutes, square=square, opening_fn=_opening_fn(geometry),
            product=product, check=check,
        )

    def _cmp(self, state, recipe, parameters, logger):
        selected = parameters.get("materials")
        stop = next(
            (name for name, response in recipe.material_responses.items() if response.stop_layer),
            parameters.get("stop_material"),
        )
        if selected or stop:
            logger(
                "VDB cmp is unselective: it removes everything above the plane, so the "
                "step's material list and stop layer do not apply."
            )
        if parameters.get("target_z") is not None:
            height = float(parameters["target_z"])
        else:
            top = state.top()
            if top is None:
                raise OpenVDBError("this state has no geometry to polish")
            height = top - float(parameters.get("removal_amount", 0.0))
        if height <= state.lattice.z_floor:
            raise OpenVDBError("the polish plane is at or below the wafer floor")
        logger(f"VDB cmp to z={height:g} um")
        return ops.cmp(state, height)

    def load_state(self, path: Path) -> VdbState:
        return VdbState.load_file(path)

    def state_materials(self, state: VdbState) -> list[str]:
        return state.materials()

    def state_bytes(self, state: VdbState) -> int:
        return state.nbytes()

    def warm_views(self, state: VdbState, buried: bool = False) -> None:
        self._meshes(state, buried)

    # -- views ----------------------------------------------------------------

    def _extent_top(self, state: VdbState, project) -> float:
        top = state.top()
        return max(float(project.grid["z_max"]), top if top is not None else 0.0)

    def section(
        self,
        state: VdbState,
        colors: Mapping[str, str],
        *,
        project: ProjectDefinition,
        axis: str = "y",
        position: float | None = None,
        interpolation: int = 1,
        line: tuple[tuple[float, float], tuple[float, float]] | None = None,
        vector: bool = False,
    ) -> dict[str, Any]:
        lat = state.lattice
        dense = state.dense()
        names = list(dense)
        z_top = self._extent_top(state, project)
        k0 = state._dense_k0
        if line is not None:
            start, end = line
            length = float(math.hypot(end[0] - start[0], end[1] - start[1]))
            if length <= 0.0:
                raise OpenVDBError("a section line needs two distinct points")
            extent = (0.0, length, lat.z_floor, z_top)
            width, height = _pixels(extent, interpolation)
            s = np.linspace(0.0, length, width)
            px = start[0] + (end[0] - start[0]) * s / length
            py = start[1] + (end[1] - start[1]) * s / length
            v = np.linspace(z_top, lat.z_floor, height)
            fi = np.broadcast_to(((px - lat.origin[0]) / lat.g)[None, :], (height, width))
            fj = np.broadcast_to(((py - lat.origin[1]) / lat.g)[None, :], (height, width))
            fk = np.broadcast_to(((v - lat.origin[2]) / lat.g - k0)[:, None], (height, width))
            fields = [
                ndimage.map_coordinates(dense[name], [fi, fj, fk], order=1, mode="nearest")
                for name in names
            ] or [np.ones((height, width))]
            solid = state.solid()
            inside = None if solid is None else ndimage.map_coordinates(solid, [fi, fj, fk], order=1, mode="nearest")
            rgb = _paint(fields, names or ["?"], colors, inside)
            return {
                "image": _png(rgb), "axis": "line", "position": 0.0, "index": 0,
                "interpolation": interpolation, "sampledSpacingUm": lat.g, "exact": False,
                "width": width, "height": height, "horizontalAxis": "s",
                "extent": {"horizontalMin": 0.0, "horizontalMax": length,
                           "verticalMin": lat.z_floor, "verticalMax": z_top},
                "positions": [],
                "line": {"start": [float(start[0]), float(start[1])], "end": [float(end[0]), float(end[1])]},
            }
        if axis == "y":
            coordinates = np.linspace(lat.y0, lat.y1, SECTION_POSITIONS)
            horizontal = (lat.x0, lat.x1)
        elif axis == "x":
            coordinates = np.linspace(lat.x0, lat.x1, SECTION_POSITIONS)
            horizontal = (lat.y0, lat.y1)
        else:
            raise OpenVDBError("section axis must be 'x' or 'y'")
        default = 0.5 * (coordinates[0] + coordinates[-1])
        index = int(np.argmin(np.abs(coordinates - (default if position is None else position))))
        cut = float(coordinates[index])
        extent = (horizontal[0], horizontal[1], lat.z_floor, z_top)
        width, height = _pixels(extent, interpolation)
        h = np.linspace(horizontal[0], horizontal[1], width)
        v = np.linspace(z_top, lat.z_floor, height)
        across = 1 if axis == "y" else 0
        along_origin = lat.origin[0] if axis == "y" else lat.origin[1]
        cut_index = (cut - (lat.origin[1] if axis == "y" else lat.origin[0])) / lat.g
        fh = np.broadcast_to(((h - along_origin) / lat.g)[None, :], (height, width))
        fk = np.broadcast_to(((v - lat.origin[2]) / lat.g - k0)[:, None], (height, width))
        def cut_plane(volume: np.ndarray) -> np.ndarray:
            plane = ops._resample(volume, np.array([cut_index]), across)
            plane = plane[:, 0, :] if axis == "y" else plane[0, :, :]
            return ndimage.map_coordinates(plane, [fh, fk], order=1, mode="nearest")

        fields = [cut_plane(dense[name]) for name in names]
        solid = state.solid()
        inside = None if solid is None else cut_plane(solid)
        rgb = _paint(fields or [np.ones((height, width))], names or ["?"], colors, inside)
        return {
            "image": _png(rgb), "axis": axis, "position": cut, "index": index,
            "interpolation": interpolation, "sampledSpacingUm": lat.g, "exact": False,
            "width": width, "height": height, "horizontalAxis": "x" if axis == "y" else "y",
            "extent": {"horizontalMin": extent[0], "horizontalMax": extent[1],
                       "verticalMin": extent[2], "verticalMax": extent[3]},
            "positions": [float(value) for value in coordinates],
        }

    def top_view(
        self,
        state: VdbState,
        colors: Mapping[str, str],
        *,
        project: ProjectDefinition,
        hidden: Sequence[str] = (),
        steps: bool = True,
        vector: bool = False,
    ) -> dict[str, Any]:
        lat = state.lattice
        dense = state.dense()
        names = [name for name in dense if name not in set(hidden)]
        extent = (lat.x0, lat.x1, lat.y0, lat.y1)
        width, height = _pixels(extent, 1)
        result_extent = {"horizontalMin": lat.x0, "horizontalMax": lat.x1,
                         "verticalMin": lat.y0, "verticalMax": lat.y1}
        if not names:
            rgb = np.empty((height, width, 3), dtype=np.uint8)
            rgb[...] = BACKGROUND_RGB
            return {"image": _png(rgb), "exact": False, "width": width, "height": height, "extent": result_extent}
        solid = np.minimum.reduce([dense[name] for name in names])
        inside = solid < 0
        nz = solid.shape[2]
        any_inside = inside.any(axis=2)
        k_top = np.where(any_inside, nz - 1 - np.argmax(inside[:, :, ::-1], axis=2), 0)
        ii, jj = np.indices(k_top.shape)
        heads = [np.where(any_inside, dense[name][ii, jj, k_top], np.float32(1.0)) for name in names]
        # sub-voxel height of each column's top, for the step lines
        upper = np.minimum(k_top + 1, nz - 1)
        a0, a1 = solid[ii, jj, k_top], solid[ii, jj, upper]
        frac = np.clip(-a0 / np.maximum(a1 - a0, 1e-12), 0.0, 1.0)
        z_top = np.where(any_inside, k_top + frac, -1.0)
        x = np.linspace(lat.x0, lat.x1, width)
        y = np.linspace(lat.y1, lat.y0, height)
        fi = np.broadcast_to(((x - lat.origin[0]) / lat.g)[None, :], (height, width))
        fj = np.broadcast_to(((y - lat.origin[1]) / lat.g)[:, None], (height, width))
        fields = [ndimage.map_coordinates(head, [fi, fj], order=1, mode="nearest") for head in heads]
        rgb = _paint(fields, names, colors)
        if steps:
            stack = np.stack(fields)
            label = np.where(stack.min(axis=0) < 0, stack.argmin(axis=0), -1)
            ni = np.clip(np.rint(fi).astype(int), 0, lat.nx - 1)
            nj = np.clip(np.rint(fj).astype(int), 0, lat.ny - 1)
            height_px = z_top[ni, nj]
            edge = np.zeros(label.shape, dtype=bool)
            for axis_ in (0, 1):
                d_label = np.diff(label, axis=axis_) == 0
                d_height = np.abs(np.diff(height_px, axis=axis_)) > 0.5
                hit = d_label & d_height & (np.take(label, range(label.shape[axis_] - 1), axis=axis_) >= 0)
                if axis_ == 0:
                    edge[:-1, :] |= hit
                    edge[1:, :] |= hit
                else:
                    edge[:, :-1] |= hit
                    edge[:, 1:] |= hit
            rgb[edge] = (rgb[edge].astype(np.float32) * STEP_LINE_SHADE).astype(np.uint8)
        return {"image": _png(rgb), "exact": False, "width": width, "height": height, "extent": result_extent}

    def _meshes(self, state: VdbState, buried: bool) -> dict[str, tuple]:
        key = "buried" if buried else "free"
        if key in state.meshes:
            return state.meshes[key]
        lat = state.lattice
        dense = state.dense()
        names = list(dense)
        k0 = state._dense_k0
        pad = 2
        bg = np.float32(HALF_WIDTH * lat.g)
        shape = next(iter(dense.values())).shape if dense else (lat.nx, lat.ny, 1)
        i = np.arange(-pad, lat.nx + pad)
        j = np.arange(-pad, lat.ny + pad)
        k = np.arange(k0, k0 + shape[2])
        x = lat.x_of(i)[:, None, None]
        y = lat.y_of(j)[None, :, None]
        z = lat.z_of(k)[None, None, :]
        window = np.maximum.reduce([
            lat.x0 - x + 0 * y + 0 * z, x - lat.x1 + 0 * y + 0 * z,
            lat.y0 - y + 0 * x + 0 * z, y - lat.y1 + 0 * x + 0 * z,
            lat.z_floor - z + 0 * x + 0 * y,
        ]).astype(np.float32)
        stack = np.stack([dense[name] for name in names]) if names else None
        out: dict[str, tuple] = {}
        for index, name in enumerate(names):
            padded = np.pad(dense[name], ((pad, pad), (pad, pad), (0, 0)), mode="constant", constant_values=bg)
            clipped = np.ascontiguousarray(np.maximum(padded, window), dtype=np.float32)
            sdf = vc.from_dense(clipped, (-pad, -pad, k0), lat.g, lat.origin, HALF_WIDTH)
            vertices, faces = vc.surface_mesh(sdf)
            vertices = np.asarray(vertices, dtype=np.float32)
            faces = np.asarray(faces, dtype=np.int64)
            if len(faces) == 0:
                continue
            a, b, c = vertices[faces[:, 0]], vertices[faces[:, 1]], vertices[faces[:, 2]]
            face_normal = np.cross(b - a, c - a)
            # OpenVDB winds a level set's polygons inward; the view wants them outward
            centre = (a + b + c) / 3.0
            probe = centre + 0.25 * lat.g * face_normal / np.maximum(np.linalg.norm(face_normal, axis=1, keepdims=True), 1e-12)
            if _sample(dense[name], probe, lat, k0).mean() < 0:
                faces = faces[:, ::-1].copy()
                face_normal = -face_normal
            unit = face_normal / np.maximum(np.linalg.norm(face_normal, axis=1, keepdims=True), 1e-12)
            outside = centre + 0.6 * lat.g * unit
            neighbour = np.full(len(faces), 255, dtype=np.uint8)
            if stack is not None and len(names) > 1:
                values = np.stack([_sample(dense[other], outside, lat, k0) for other in names])
                values[index] = np.inf
                best = values.argmin(axis=0)
                hit = values.min(axis=0) < 0
                neighbour[hit] = best[hit].astype(np.uint8)
            if not buried:
                keep = neighbour == 255
                faces, neighbour, face_normal = faces[keep], neighbour[keep], face_normal[keep]
                if len(faces) == 0:
                    continue
            # keep only the vertices the kept faces use
            used, faces = np.unique(faces, return_inverse=True)
            faces = faces.reshape(-1, 3)
            vertices = vertices[used]
            normals = np.zeros_like(vertices)
            for corner in range(3):
                np.add.at(normals, faces[:, corner], face_normal)
            normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-12)
            out[name] = (vertices, faces.astype(np.uint32), normals.astype(np.float32), neighbour)
        state.meshes[key] = out
        return out

    def surfaces(
        self,
        state: VdbState,
        *,
        project: ProjectDefinition,
        interpolation: int = 1,
        materials: Sequence[str] | None = None,
        triangulation: str | None = None,
        buried: bool = False,
    ) -> dict[str, Any]:
        lat = state.lattice
        meshes = self._meshes(state, buried)
        order = list(state.dense())
        payload = []
        for name, (vertices, faces, normals, neighbour) in meshes.items():
            if materials is not None and name not in materials:
                continue
            payload.append({
                "material": name,
                "positions": _encode(vertices),
                "normals": _encode(normals),
                "shading": "smooth",
                "indices": _encode(faces),
                "interfaceFaces": _encode((neighbour != 255).astype(np.uint8)),
                "neighbourFaces": _encode(neighbour),
                "neighbourMaterials": order,
                "vertexCount": int(len(vertices)),
                "triangleCount": int(len(faces)),
            })
        return {
            "interpolation": 1,
            "exact": False,
            "triangulation": "openvdb",
            "buried": bool(buried),
            "bounds": {
                "xMin": lat.x0, "xMax": lat.x1, "yMin": lat.y0, "yMax": lat.y1,
                "zMin": lat.z_floor, "zMax": self._extent_top(state, project),
            },
            "surfaces": payload,
        }


def _sample(field: np.ndarray, points: np.ndarray, lat: Lattice, k0: int) -> np.ndarray:
    """``field`` (the window, layers from ``k0``) at world ``points``, trilinear."""
    coords = [
        (points[:, 0] - lat.origin[0]) / lat.g,
        (points[:, 1] - lat.origin[1]) / lat.g,
        (points[:, 2] - lat.origin[2]) / lat.g - k0,
    ]
    return ndimage.map_coordinates(field, coords, order=1, mode="nearest")
