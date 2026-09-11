"""Files the views can be saved as: a mesh of the 3D surfaces, a picture."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..kernels import Kernel
from ..models import ProjectDefinition
from .errors import InvalidRequest, WorkspaceError

MESH_FORMATS = ("glb", "gltf", "obj", "stl", "ply")


def write_mesh(
    kernel: Kernel,
    state: Any,
    project: ProjectDefinition,
    colors: Mapping[str, str],
    destination: Path,
    *,
    materials: Sequence[str] | None = None,
    interpolation: int = 1,
) -> dict[str, Any]:
    """Write the 3D surfaces of a state as one mesh file; returns what was written."""
    try:
        import numpy as np
        import trimesh
    except ImportError as error:  # pragma: no cover - the level-set-only build
        raise WorkspaceError(
            "Exporting a mesh needs the trimesh package, which this build does not include."
        ) from error
    suffix = destination.suffix.lower().lstrip(".")
    if suffix not in MESH_FORMATS:
        raise InvalidRequest(
            "the mesh file must end with " + ", ".join(f".{name}" for name in MESH_FORMATS)
        )
    payload = kernel.surfaces(
        state, project=project, interpolation=interpolation,
        materials=None if materials is None else [str(name) for name in materials],
    )
    scene = trimesh.Scene()
    counts: dict[str, int] = {}
    for surface in payload["surfaces"]:
        positions = np.frombuffer(base64.b64decode(surface["positions"]), dtype=np.float32).reshape(-1, 3)
        indices = np.frombuffer(base64.b64decode(surface["indices"]), dtype=np.uint32).reshape(-1, 3)
        mesh = trimesh.Trimesh(
            vertices=positions.astype(np.float64), faces=indices.astype(np.int64), process=False
        )
        color = colors.get(surface["material"], "#7c83a0").lstrip("#")
        rgb = [int(color[index : index + 2], 16) for index in (0, 2, 4)] if len(color) == 6 else [124, 131, 160]
        mesh.visual.face_colors = [*rgb, 255]
        scene.add_geometry(mesh, node_name=surface["material"], geom_name=surface["material"])
        counts[surface["material"]] = int(len(indices))
    if not counts:
        raise InvalidRequest("this state has no surfaces to export.")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if suffix in ("stl", "ply"):
            # One solid per file for the formats without named parts.
            scene.dump(concatenate=True).export(str(destination))
        else:
            scene.export(str(destination))
    except OSError as error:
        raise WorkspaceError(f"Cannot write {destination}: {error}") from error
    return {"path": str(destination), "triangles": counts, "bounds": payload["bounds"]}


def write_image(destination: Path, image: str) -> dict[str, Any]:
    """Write a base64 PNG (with or without a data-URL prefix) to disk."""
    if destination.suffix.lower() != ".png":
        raise InvalidRequest("the picture must be saved as .png")
    if "," in image[:40] and image.startswith("data:"):
        image = image.split(",", 1)[1]
    try:
        data = base64.b64decode(image, validate=True)
    except ValueError as error:
        raise InvalidRequest("the image is not valid base64") from error
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise InvalidRequest("the image is not a PNG")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    except OSError as error:
        raise WorkspaceError(f"Cannot write {destination}: {error}") from error
    return {"path": str(destination), "bytes": len(data)}
