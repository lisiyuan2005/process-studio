"""The one picture the worker draws that is not a kernel's own.

The slab kernel renders its own sections, top views and meshes from exact
polygons (see ``kernels.slab``). What is left here is the mask preview: the
sketch editor's own picture of where a sketch exposes the wafer, which is
about the sketch rather than about any geometry a kernel has computed.

Everything else in this module was the level-set hand-off -- marching-cubes
surfaces and images sampled from its fields -- and went with that kernel.
"""

from __future__ import annotations

import base64
import io
from typing import Any

import numpy as np
from PIL import Image


def sketch_preview_image(
    sketch: Any,
    *,
    extent: tuple[float, float, float, float],
    keep: str = "inside",
    pixels: int = 640,
) -> dict[str, Any]:
    """Render where a sketch exposes the wafer, as the kernel would read it.

    The sketch's own signed distance is evaluated on a display raster over
    the project window, so the picture the editor shows is the same CSG the
    run will sample, not a drawing of the shapes. ``keep`` flips it the way
    a step's Keep setting does.
    """
    x_min, x_max, y_min, y_max = extent
    span = max(x_max - x_min, y_max - y_min, 1e-9)
    width = max(2, int(round((x_max - x_min) / span * pixels)))
    height = max(2, int(round((y_max - y_min) / span * pixels)))
    xs = np.linspace(x_min, x_max, width)
    ys = np.linspace(y_min, y_max, height)
    xx, yy = np.meshgrid(xs, ys)
    exposed = sketch.signed_distance(xx, yy) <= 0.0
    if keep == "outside":
        exposed = ~exposed
    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    rgba[exposed] = (31, 122, 224, 120)
    buffer = io.BytesIO()
    Image.fromarray(rgba[::-1], mode="RGBA").save(buffer, format="PNG")
    return {
        "image": base64.b64encode(buffer.getvalue()).decode("ascii"),
        "width": int(width),
        "height": int(height),
        "exposedFraction": float(exposed.mean()),
        "extent": {
            "horizontalMin": x_min,
            "horizontalMax": x_max,
            "verticalMin": y_min,
            "verticalMax": y_max,
        },
    }
