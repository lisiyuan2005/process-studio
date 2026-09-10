"""Geometry hand-off for the desktop viewport.

The 3D view receives the marching-cubes surface of each material's own level
set. The 2D views receive images sampled from the same fields. Nothing here
smooths, closes holes or mirrors geometry: what the client draws is what the
kernel computed, at the sampling density the client asked for.
"""

from __future__ import annotations

import base64
import io
from typing import Any, Mapping

import numpy as np
from PIL import Image
from scipy.ndimage import zoom

from ..kernel.material_state import MaterialState
from ..visualization import top_view_labels
from .errors import InvalidRequest

try:  # scikit-image is the optional [render] extra
    from skimage.measure import marching_cubes

    MESHES_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without the extra
    marching_cubes = None  # type: ignore[assignment]
    MESHES_AVAILABLE = False

MAXIMUM_INTERPOLATION = 4
BACKGROUND_RGB = (247, 249, 252)


def _encode(array: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(array).tobytes()).decode("ascii")


def _checked_interpolation(value: Any) -> int:
    try:
        factor = int(value)
    except (TypeError, ValueError) as error:
        raise InvalidRequest("interpolation must be an integer") from error
    if not 1 <= factor <= MAXIMUM_INTERPOLATION:
        raise InvalidRequest(f"interpolation must be between 1 and {MAXIMUM_INTERPOLATION}")
    return factor


def _resample(field: np.ndarray, factor: int) -> np.ndarray:
    if factor <= 1:
        return field
    # Linear interpolation keeps the zero crossing where it is; cubic splines
    # overshoot around thin films and would move the interface.
    return zoom(field, factor, order=1, mode="nearest", prefilter=False)


def material_surface(
    state: MaterialState,
    material: str,
    interpolation: int,
) -> dict[str, Any] | None:
    """Return one material's zero isosurface as raw triangle arrays."""
    if not MESHES_AVAILABLE:
        raise InvalidRequest(
            "Surface extraction needs scikit-image. Install the render extra: "
            'pip install -e ".[render]"'
        )
    field = _resample(state.fields[material], interpolation)
    if field.min() > 0.0 or field.max() < 0.0:
        return None
    nz, ny, nx = field.shape
    grid = state.grid
    spacing = (
        (grid.z_max - grid.z_min) / (nz - 1),
        (grid.y_max - grid.y_min) / (ny - 1),
        (grid.x_max - grid.x_min) / (nx - 1),
    )
    vertices_zyx, faces, normals_zyx, _ = marching_cubes(
        field,
        level=0.0,
        spacing=spacing,
        step_size=1,
        allow_degenerate=False,
        method="lewiner",
    )
    positions = np.column_stack(
        (
            vertices_zyx[:, 2] + grid.x_min,
            vertices_zyx[:, 1] + grid.y_min,
            vertices_zyx[:, 0] + grid.z_min,
        )
    ).astype(np.float32)
    normals = np.column_stack(
        (normals_zyx[:, 2], normals_zyx[:, 1], normals_zyx[:, 0])
    ).astype(np.float32)
    return {
        "material": material,
        "positions": _encode(positions),
        "normals": _encode(normals),
        "indices": _encode(faces.astype(np.uint32)),
        "vertexCount": int(len(positions)),
        "triangleCount": int(len(faces)),
    }


def material_surfaces(
    state: MaterialState,
    *,
    interpolation: int = 1,
    materials: list[str] | None = None,
) -> dict[str, Any]:
    factor = _checked_interpolation(interpolation)
    selected = [name for name in state.priority if materials is None or name in materials]
    surfaces = []
    for name in selected:
        surface = material_surface(state, name, factor)
        if surface is not None:
            surfaces.append(surface)
    grid = state.grid
    return {
        "interpolation": factor,
        "sampledSpacingUm": grid.dx / factor,
        "bounds": {
            "xMin": grid.x_min,
            "xMax": grid.x_max,
            "yMin": grid.y_min,
            "yMax": grid.y_max,
            "zMin": grid.z_min,
            "zMax": grid.z_max,
        },
        "surfaces": surfaces,
    }


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


def _colorize(labels: np.ndarray, names: list[str], colors: Mapping[str, str]) -> np.ndarray:
    rgb = np.zeros((*labels.shape, 3), dtype=np.uint8)
    rgb[...] = BACKGROUND_RGB
    for index, name in enumerate(names):
        rgb[labels == index] = _rgb(colors.get(name, "#7c83a0"))
    return rgb


def _resampled_labels(
    state: MaterialState,
    slices: tuple[Any, ...],
    factor: int,
) -> tuple[np.ndarray, list[str]]:
    """Label a 2D cut, optionally interpolating the fields before comparing."""
    names = list(state.priority)
    if not names:
        return np.full((1, 1), -1, dtype=np.int16), names
    planes = [_resample(state.fields[name][slices], factor) for name in names]
    labels = np.full(planes[0].shape, -1, dtype=np.int16)
    for index, plane in enumerate(planes):
        labels[plane <= 0.0] = index
    return labels, names


def section_image(
    state: MaterialState,
    colors: Mapping[str, str],
    *,
    axis: str = "y",
    position: float | None = None,
    interpolation: int = 1,
) -> dict[str, Any]:
    """Render a vertical cut through the state as a PNG."""
    factor = _checked_interpolation(interpolation)
    grid = state.grid
    if axis == "y":
        coordinates = grid.y
        default = 0.5 * (grid.y_min + grid.y_max)
        index = int(np.argmin(np.abs(coordinates - (default if position is None else position))))
        slices = (slice(None), index, slice(None))
        horizontal = (grid.x_min, grid.x_max)
        horizontal_label = "x"
    elif axis == "x":
        coordinates = grid.x
        default = 0.5 * (grid.x_min + grid.x_max)
        index = int(np.argmin(np.abs(coordinates - (default if position is None else position))))
        slices = (slice(None), slice(None), index)
        horizontal = (grid.y_min, grid.y_max)
        horizontal_label = "y"
    else:
        raise InvalidRequest("section axis must be 'x' or 'y'")
    labels, names = _resampled_labels(state, slices, factor)
    # Rows run bottom-up in the kernel; images are written top-down.
    rgb = _colorize(labels, names, colors)[::-1]
    return {
        "image": _png(rgb),
        "axis": axis,
        "position": float(coordinates[index]),
        "index": index,
        "interpolation": factor,
        "sampledSpacingUm": grid.dx / factor,
        "width": int(rgb.shape[1]),
        "height": int(rgb.shape[0]),
        "horizontalAxis": horizontal_label,
        "extent": {
            "horizontalMin": horizontal[0],
            "horizontalMax": horizontal[1],
            "verticalMin": grid.z_min,
            "verticalMax": grid.z_max,
        },
        "positions": [float(value) for value in coordinates],
    }


def top_view_image(state: MaterialState, colors: Mapping[str, str]) -> dict[str, Any]:
    """Render the native-resolution top view.

    The top view reports the topmost occupied label per column, so it is not
    interpolated: an upsampled picture here would invent coverage the kernel
    never computed.
    """
    labels = top_view_labels(state)
    names = list(state.priority)
    rgb = _colorize(labels, names, colors)[::-1]
    grid = state.grid
    return {
        "image": _png(rgb),
        "width": int(rgb.shape[1]),
        "height": int(rgb.shape[0]),
        "extent": {
            "horizontalMin": grid.x_min,
            "horizontalMax": grid.x_max,
            "verticalMin": grid.y_min,
            "verticalMax": grid.y_max,
        },
    }
