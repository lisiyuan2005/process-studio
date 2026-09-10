"""GLB export: the handoff boundary to Blender.

One node + one watertight mesh per material, both named after the material.
The faces that touch another material are listed by index in the mesh
extras (``interface_faces``) so a renderer can shade them separately for
x-ray views; the mesh itself is never split. Coordinates stay in
micrometres (1 glTF unit = 1 um; recorded in the scene extras). glTF is
Y-up, so a Z-up -> Y-up rotation is applied on each node; Blender's importer
converts back to Z-up, so the device appears unchanged.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import trimesh
from trimesh.visual.material import PBRMaterial

from ..material import Material

# (x, y, z) -> (x, z, -y): rotation of -90 degrees about X.
Z_UP_TO_Y_UP = np.array(
    [[1, 0, 0, 0], [0, 0, 1, 0], [0, -1, 0, 0], [0, 0, 0, 1]], dtype=np.float64
)


XRAY_SUFFIX = ".xray"


def export_glb(path, meshes: dict[Material, trimesh.Trimesh], device_name: str, history, xray=()) -> Path:
    """Write the GLB. ``xray``: material names that additionally get a
    ``Name.xray`` node holding only their faces exposed to void — a display
    part for see-through renders (the coincident interface faces of a
    transparent shell would otherwise fight the opaque neighbour's faces).
    The deliverable mesh per material is always the complete watertight one."""
    path = Path(path)
    scene = trimesh.Scene()
    scene.metadata["name"] = device_name
    scene.metadata["units"] = "um"
    scene.metadata["generator"] = "deviceflow"
    scene.metadata["history"] = list(history)
    for material, mesh in meshes.items():
        m = mesh.copy()
        m.visual = trimesh.visual.TextureVisuals(material=_pbr(material))
        m.metadata["material"] = material.name
        m.metadata["role"] = material.role
        iface = mesh.metadata.get("interface_faces")
        # face indices touching another material (same triangle order as in the file):
        # a renderer may shade them differently, never remove them
        m.metadata["interface_faces"] = [int(i) for i in np.nonzero(iface)[0]] if iface is not None else []
        scene.add_geometry(m, node_name=material.name, geom_name=material.name, transform=Z_UP_TO_Y_UP)
        if material.name in xray and iface is not None and iface.any():
            exposed = mesh.submesh([np.nonzero(~iface)[0]], append=True)
            exposed.visual = trimesh.visual.TextureVisuals(material=_pbr(material))
            exposed.metadata["material"] = material.name
            exposed.metadata["xray"] = True
            name = material.name + XRAY_SUFFIX
            scene.add_geometry(exposed, node_name=name, geom_name=name, transform=Z_UP_TO_Y_UP)
    path.parent.mkdir(parents=True, exist_ok=True)
    scene.export(str(path), file_type="glb")
    return path


def _pbr(material: Material) -> PBRMaterial:
    rgba = np.asarray(material.color, dtype=np.float64)
    metallic = float(material.render.get("metallic", 0.0))
    roughness = float(material.render.get("roughness", 0.5))
    alpha = float(rgba[3])
    return PBRMaterial(
        name=material.name,
        baseColorFactor=rgba,
        metallicFactor=metallic,
        roughnessFactor=roughness,
        alphaMode="BLEND" if alpha < 1.0 else None,
        doubleSided=False,
    )
