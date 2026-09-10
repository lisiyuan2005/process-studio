"""Runs INSIDE Blender (``blender -b --python _blender_render.py -- job.json``).

Allowed: clear scene, import GLB, adjust shaders, hide objects, set camera,
lights, view transform, compositor outline, render PNG, save BLEND, write a
JSON report. Forbidden: any modifier or geometry edit. The report lists every
object's modifiers so the caller can assert the list is empty.

Style (see the DeviceFlow skill, sections 37-42): orthographic camera framed
on the imported bounding box; neutral studio lighting (ambient + key + a
camera-aligned "headlight" so deep trenches are not black); limited
reflections; no decorative environment unless an HDRI is given explicitly;
opaque materials by default; labels are added downstream, never here.
"""

import json
import math
import sys

import bpy
from mathutils import Vector

VIEWS = {
    # direction from the device towards the camera
    "iso": Vector((1.0, -1.0, 0.9)),
    "top": Vector((0.0, 0.0, 1.0)),
    "front": Vector((0.0, -1.0, 0.0)),
    "side": Vector((1.0, 0.0, 0.0)),
}


# --------------------------------------------------------------- materials --


def _nodes(mat):
    nt = mat.node_tree
    bsdf = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)
    out = next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL"), None)
    return nt, bsdf, out


def make_transparent(mat, alpha):
    """Semi-transparent shell whose back faces are fully transparent.

    Each material mesh is independently watertight, so at an interface the
    transparent material's back face coincides with the opaque neighbour's
    front face. Rendering the back faces invisible makes the interface
    always resolve to the opaque material and removes z-fighting.
    """
    nt, bsdf, out = _nodes(mat)
    if bsdf is None or out is None:
        return
    bsdf.inputs["Alpha"].default_value = 1.0
    transparent = nt.nodes.new("ShaderNodeBsdfTransparent")
    front = nt.nodes.new("ShaderNodeMixShader")  # fac=alpha: transparent -> principled
    front.inputs[0].default_value = float(alpha)
    nt.links.new(transparent.outputs[0], front.inputs[1])
    nt.links.new(bsdf.outputs[0], front.inputs[2])
    geometry = nt.nodes.new("ShaderNodeNewGeometry")
    sides = nt.nodes.new("ShaderNodeMixShader")  # fac=backfacing: front -> transparent
    nt.links.new(geometry.outputs["Backfacing"], sides.inputs[0])
    nt.links.new(front.outputs[0], sides.inputs[1])
    nt.links.new(transparent.outputs[0], sides.inputs[2])
    nt.links.new(sides.outputs[0], out.inputs["Surface"])
    mat.use_backface_culling = False
    if hasattr(mat, "surface_render_method"):
        mat.surface_render_method = "BLENDED"
    if hasattr(mat, "blend_method"):
        mat.blend_method = "BLEND"


def make_debug_normals(mat):
    """Diagnostic: grey where the camera sees a front face, red for a back face."""
    nt, bsdf, out = _nodes(mat)
    if out is None:
        return
    geometry = nt.nodes.new("ShaderNodeNewGeometry")
    emission_ok = nt.nodes.new("ShaderNodeEmission")
    emission_ok.inputs[0].default_value = (0.6, 0.6, 0.6, 1.0)
    emission_bad = nt.nodes.new("ShaderNodeEmission")
    emission_bad.inputs[0].default_value = (1.0, 0.05, 0.05, 1.0)
    mix = nt.nodes.new("ShaderNodeMixShader")
    nt.links.new(geometry.outputs["Backfacing"], mix.inputs[0])
    nt.links.new(emission_ok.outputs[0], mix.inputs[1])
    nt.links.new(emission_bad.outputs[0], mix.inputs[2])
    nt.links.new(mix.outputs[0], out.inputs["Surface"])
    mat.use_backface_culling = False


def interface_faces(obj):
    """Face indices touching another material, from the glTF mesh extras."""
    for holder in (obj.data, obj):
        value = holder.get("interface_faces") if hasattr(holder, "get") else None
        if value is not None:
            try:
                return [int(i) for i in value]
            except TypeError:
                return list(value.to_list()) if hasattr(value, "to_list") else []
    return []


def make_invisible(name):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    for node in list(nt.nodes):
        if node.type != "OUTPUT_MATERIAL":
            nt.nodes.remove(node)
    out = next(n for n in nt.nodes if n.type == "OUTPUT_MATERIAL")
    transparent = nt.nodes.new("ShaderNodeBsdfTransparent")
    nt.links.new(transparent.outputs[0], out.inputs["Surface"])
    if hasattr(mat, "surface_render_method"):
        mat.surface_render_method = "BLENDED"
    if hasattr(mat, "blend_method"):
        mat.blend_method = "BLEND"
    mat.use_backface_culling = False
    return mat


XRAY_SUFFIX = ".xray"


def is_xray_part(obj) -> bool:
    return obj.name.endswith(XRAY_SUFFIX)


def apply_materials(objs, job):
    hide = set(job.get("hide", []))
    transparent = job.get("transparent", {})
    names = {o.name for o in objs}
    for o in objs:
        name = o.name
        if is_xray_part(o):
            base = name[: -len(XRAY_SUFFIX)]
            if base in hide or base not in transparent or job.get("debug_normals"):
                o.hide_render = True
                o.hide_viewport = True
                continue
            # the see-through display part: exposed faces only, no shadows
            o.visible_shadow = False
            for mat in o.data.materials:
                if mat is not None and mat.use_nodes:
                    make_transparent(mat, transparent[base])
            continue
        if name in hide or (name in transparent and name + XRAY_SUFFIX in names and not job.get("debug_normals")):
            # hidden, or replaced by its .xray display part for this render
            o.hide_render = True
            o.hide_viewport = True
            continue
        for mat in o.data.materials:
            if mat is None or not mat.use_nodes:
                continue
            if job.get("debug_normals"):
                make_debug_normals(mat)
            elif name in transparent:
                make_transparent(mat, transparent[name])
        if name in transparent and not job.get("debug_normals"):
            # x-ray objects cast no shadows: like the alpha-blended layers of GDS
            # 3D viewers, they only tint what lies behind them. Shadow rays through
            # a stack of transparent faces would otherwise hit the bounce limit and
            # paint the geometry behind with the triangulation of the shell.
            o.visible_shadow = False
            # The faces touching another material coincide with that neighbour's
            # faces: shade them fully transparent (a material slot assignment, the
            # geometry is untouched) so the opaque neighbour shows without z-fighting.
            faces = interface_faces(o)
            if faces:
                o.data.materials.append(make_invisible(name + ".interface"))
                slot = len(o.data.materials) - 1
                polys = o.data.polygons
                for i in faces:
                    if i < len(polys):
                        polys[i].material_index = slot


# ------------------------------------------------------------------ camera --


def frame_camera(scene, objs, job):
    pts = [o.matrix_world @ Vector(c) for o in objs for c in o.bound_box]
    lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    centre = (lo + hi) / 2
    size = max(hi - lo)
    direction = VIEWS[job["view"]].normalized()
    cam_data = bpy.data.cameras.new("camera")
    cam_data.type = "ORTHO"
    cam_data.clip_end = size * 100
    cam = bpy.data.objects.new("camera", cam_data)
    scene.collection.objects.link(cam)
    cam.location = centre + direction * size * 5
    if job["view"] == "top":
        cam.rotation_euler = (0.0, 0.0, 0.0)
    else:
        cam.rotation_euler = direction.to_track_quat("Z", "Y").to_euler()
    scene.camera = cam
    res_x, res_y = job["resolution"]
    scene.render.resolution_x, scene.render.resolution_y = int(res_x), int(res_y)
    scene.render.resolution_percentage = 100
    inv = cam.matrix_world.inverted()
    local = [inv @ p for p in pts]
    ext_x = max(p.x for p in local) - min(p.x for p in local)
    ext_y = max(p.y for p in local) - min(p.y for p in local)
    aspect = res_x / res_y
    cam_data.ortho_scale = max(ext_x, ext_y * aspect) * (1 + job.get("margin", 0.15))
    return cam, direction, lo, hi


# ------------------------------------------------------------------ lights --


def _sun(scene, name, direction, energy, angle_deg):
    data = bpy.data.lights.new(name, "SUN")
    data.energy = energy
    data.angle = math.radians(angle_deg)
    obj = bpy.data.objects.new(name, data)
    scene.collection.objects.link(obj)
    obj.rotation_euler = Vector(direction).normalized().to_track_quat("Z", "Y").to_euler()
    return obj


def light_scene(scene, cam, direction, job):
    world = bpy.data.worlds.new("world")
    if not world.use_nodes:
        world.use_nodes = True
    nt = world.node_tree
    bg = nt.nodes["Background"]
    hdri = job.get("hdri")
    if hdri:
        env = nt.nodes.new("ShaderNodeTexEnvironment")
        env.image = bpy.data.images.load(hdri)
        nt.links.new(env.outputs["Color"], bg.inputs["Color"])
        bg.inputs["Strength"].default_value = float(job.get("hdri_strength", 1.0))
    else:
        bg.inputs[0].default_value = (*job.get("background", (0.85, 0.85, 0.85)), 1.0)
        bg.inputs[1].default_value = float(job.get("ambient", 0.35))
    scene.world = world
    # key: from the camera side and above -> top, left and right walls differ
    _sun(scene, "key", direction + Vector((0.0, 0.0, 1.2)), float(job.get("key", 4.0)), 8)
    # headlight: along the view direction, lights every surface the camera sees
    # (deep trenches, pockets) that the key and the ambient cannot reach
    headlight = float(job.get("headlight", 1.5))
    if headlight > 0:
        _sun(scene, "headlight", direction, headlight, 15)
    # soft fill from the opposite side keeps shadowed walls readable
    fill_dir = Vector((-direction.x, -direction.y, 0.6)) if job["view"] != "top" else Vector((1, 1, 1))
    _sun(scene, "fill", fill_dir, float(job.get("fill", 1.0)), 30)


# ----------------------------------------------------------------- outline --


def add_outline(scene, job):
    """Compositor Sobel edge detection on the normal pass: crisp per-pixel
    outlines of every face change, without touching the geometry."""
    scene.view_layers[0].use_pass_normal = True
    if hasattr(scene, "compositing_node_group"):  # Blender 4.4+ / 5.x
        tree = bpy.data.node_groups.new("Compositor", "CompositorNodeTree")
        scene.compositing_node_group = tree
        tree.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
        out = tree.nodes.new("NodeGroupOutput")
        out_socket = out.inputs[0]
    else:  # legacy scene compositor
        scene.use_nodes = True
        tree = scene.node_tree
        tree.nodes.clear()
        out = tree.nodes.new("CompositorNodeComposite")
        out_socket = out.inputs["Image"]
    layers = tree.nodes.new("CompositorNodeRLayers")
    sobel = tree.nodes.new("CompositorNodeFilter")
    if "Type" in sobel.inputs:
        sobel.inputs["Type"].default_value = "Sobel"
    else:
        sobel.filter_type = "SOBEL"
    tree.links.new(layers.outputs["Normal"], sobel.inputs["Image"])
    to_bw = tree.nodes.new("CompositorNodeRGBToBW")
    tree.links.new(sobel.outputs["Image"], to_bw.inputs[0])
    # edge strength -> line: white below the threshold, black above it
    ramp = tree.nodes.new("ShaderNodeValToRGB")
    threshold = float(job.get("outline_threshold", 0.08))
    ramp.color_ramp.elements[0].position = threshold * 0.6
    ramp.color_ramp.elements[0].color = (1, 1, 1, 1)
    ramp.color_ramp.elements[1].position = threshold * 2.0
    ramp.color_ramp.elements[1].color = (0, 0, 0, 1)
    tree.links.new(to_bw.outputs[0], ramp.inputs["Fac"])
    mix = tree.nodes.new("ShaderNodeMixRGB")
    mix.blend_type = "MULTIPLY"
    mix.inputs["Fac"].default_value = 1.0
    tree.links.new(layers.outputs["Image"], mix.inputs[1])
    tree.links.new(ramp.outputs["Color"], mix.inputs[2])
    tree.links.new(mix.outputs[0], out_socket)


# ------------------------------------------------------------------- main ---


def main(job):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=job["glb"], import_shading="FLAT", merge_vertices=False)
    scene = bpy.context.scene
    objs = [o for o in bpy.data.objects if o.type == "MESH"]

    apply_materials(objs, job)
    cam, direction, lo, hi = frame_camera(scene, objs, job)
    light_scene(scene, cam, direction, job)

    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = int(job["samples"])
    scene.cycles.transparent_max_bounces = 128  # x-ray through many thin shells
    scene.cycles.use_denoising = bool(job.get("denoise", True))
    scene.render.film_transparent = bool(job.get("film_transparent", False))
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA" if scene.render.film_transparent else "RGB"
    # Khronos PBR Neutral rolls off highlights without desaturating (no neon glow)
    try:
        scene.view_settings.view_transform = job.get("view_transform", "Khronos PBR Neutral")
    except TypeError:
        scene.view_settings.view_transform = "Standard"
    scene.view_settings.exposure = float(job.get("exposure", 0.0))
    if job.get("outline"):
        add_outline(scene, job)

    if job.get("png"):
        scene.render.filepath = job["png"]
        bpy.ops.render.render(write_still=True)
    if job.get("blend"):
        bpy.ops.wm.save_as_mainfile(filepath=job["blend"])

    report = {
        "engine": scene.render.engine,
        "view": job["view"],
        "samples": scene.cycles.samples,
        "resolution": list(job["resolution"]),
        "view_transform": scene.view_settings.view_transform,
        "outline": bool(job.get("outline")),
        "bounds": [list(lo), list(hi)],
        "objects": {},
    }
    report["xray_parts"] = [o.name for o in objs if is_xray_part(o)]
    for o in objs:
        if is_xray_part(o):
            continue
        alpha = None
        if o.name in job.get("transparent", {}):
            alpha = float(job["transparent"][o.name])
        report["objects"][o.name] = {
            "modifiers": [m.type for m in o.modifiers],
            "materials": [m.name for m in o.data.materials],
            "vertices": len(o.data.vertices),
            "polygons": len(o.data.polygons),
            "hidden": bool(o.hide_render) and o.name in job.get("hide", []),
            "alpha": alpha,
            "interface_faces": len(interface_faces(o)),
        }
    with open(job["report"], "w") as f:
        json.dump(report, f)


if __name__ == "__main__":
    job_path = sys.argv[sys.argv.index("--") + 1]
    with open(job_path) as f:
        main(json.load(f))
