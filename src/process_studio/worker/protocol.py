"""JSON-line RPC surface used by the Tauri shell.

One request per line on stdin, one response per line on stdout. Progress lines
are written as they happen so a long run reports what it is doing instead of
going quiet.
"""

from __future__ import annotations

import io
import json
import math
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, IO, Mapping

from . import PROTOCOL_VERSION, __version__
from ..grid import UniformGrid3D, window_grid
from ..kernels import available_kernels, build_variant, default_kernel, get_kernel
from ..layout.gds import available_gds_layers
from ..libraries import RecipeLibrary
from ..models import ProcessType
from .errors import InvalidRequest, WorkerError, WorkspaceError

#: Triangulators the 3D view can be built with. Spelt out rather than
#: imported: this module is the entry point of every build, and the list
#: lives in deviceflow's mesh code, which a build missing shapely cannot
#: import at all. A test keeps the two the same.
MESH_ENGINES = ("ears", "delaunay")
from .export import write_image, write_mesh
from .render import sketch_preview_image
from .files import (
    FLOW_FIELDS,
    copy_workspace,
    export_flow,
    export_library,
    import_flow,
    import_library,
    reveal_path,
)
from .update import check_update, install_update, open_url
from .runner import (
    apply_resolution,
    build_document,
    project_kernel,
    run_flow,
    state_for_step,
)
from .serialize import (
    branch_from_json,
    grid_dict,
    grid_from_json,
    grid_to_json,
    material_from_json,
    project_from_json,
    recipe_from_json,
    tool_from_json,
)
from .workspace import (
    result_keys,
    DigestCache,
    initialize_workspace,
    load_project,
    load_sketches,
    open_repository,
    save_sketch,
    sketch_from_payload,
)

#: How far the flat views may magnify their own raster. It costs no
#: computation -- the geometry is exact, only the picture gets more pixels.
MAXIMUM_INTERPOLATION = 4


def _checked_interpolation(value: Any) -> int:
    """A sampling factor a view may ask for."""
    try:
        factor = int(value)
    except (TypeError, ValueError) as error:
        raise InvalidRequest(f"interpolation must be a whole number, got {value!r}.") from error
    if not 1 <= factor <= MAXIMUM_INTERPOLATION:
        raise InvalidRequest(
            f"interpolation must be between 1 and {MAXIMUM_INTERPOLATION}, got {factor}."
        )
    return factor
MASK_SOURCES = ("none", "quick_sketch", "gds")
SKETCH_SHAPES = ("rectangle", "circle", "polygon", "path")
SKETCH_OPERATIONS = ("merge", "subtract", "intersect")


def _write(stream: IO[str], message: Mapping[str, Any]) -> None:
    stream.write(json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n")
    stream.flush()


def _root(parameters: Mapping[str, Any]) -> Path:
    root = parameters.get("root")
    if not isinstance(root, str) or not root:
        raise InvalidRequest("this method requires a workspace root.")
    return Path(root)


def _cli_command() -> dict[str, Any]:
    """How to reach this same worker from a shell, for the desktop's CLI panel.

    A packaged worker is its own command-line tool (the binary with
    arguments); in a source checkout the CLI is the module the console
    script points at, run by the interpreter the worker runs on.
    """
    if getattr(sys, "frozen", False):
        return {"command": [sys.executable], "packaged": True}
    return {"command": [sys.executable, "-m", "process_studio.cli"], "packaged": False}


def _run_cli(
    parameters: Mapping[str, Any], output: IO[str], cancel: threading.Event | None
) -> dict[str, Any]:
    """Run one command line of the CLI inside the worker, for the desktop's console.

    ``argv`` is the command without the program (``["steps", "list"]``);
    ``root`` is put in front as ``--root`` so the command works the open
    workspace; ``stdin`` stands in for a file argument of ``-``. Progress
    events go out on this request's own stream, so a ``run`` reports to
    the shell exactly like one started from its Run button, and the
    request's cancel flag stops it. The result carries the exit code,
    both output streams and, when the root is a workspace afterwards, the
    document as it now stands.
    """
    argv = parameters.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise InvalidRequest("run_cli requires argv, a non-empty list of strings.")
    root = parameters.get("root")
    stdin = parameters.get("stdin")
    if stdin is not None and not isinstance(stdin, str):
        raise InvalidRequest("run_cli stdin must be text.")
    from ..cli import main as cli_main

    out, err = io.StringIO(), io.StringIO()
    arguments = [*(["--root", str(root)] if root else []), *argv]
    code = cli_main(arguments, out=out, err=err, events=output, cancel=cancel, stdin=stdin)
    result: dict[str, Any] = {"exitCode": code, "stdout": out.getvalue(), "stderr": err.getvalue()}
    if root:
        try:
            repository = open_repository(Path(str(root)))
            result["document"] = build_document(Path(str(root)), repository, load_project(repository))
        except (WorkspaceError, InvalidRequest, OSError):
            pass
    return result


def _destination(parameters: Mapping[str, Any]) -> Path:
    destination = parameters.get("destination")
    if not isinstance(destination, str) or not destination:
        raise InvalidRequest("This method requires a destination path.")
    return Path(destination)


def _source(parameters: Mapping[str, Any]) -> Path:
    source = parameters.get("source")
    if not isinstance(source, str) or not source:
        raise InvalidRequest("This method requires a source path.")
    return Path(source)


def _describe() -> dict[str, Any]:
    """Report what this build can do so the client never hard-codes it."""
    return {
        "workerVersion": __version__,
        "protocolVersion": PROTOCOL_VERSION,
        "processTypes": [process_type.value for process_type in ProcessType],
        "maskSources": list(MASK_SOURCES),
        "sketch": {"shapes": list(SKETCH_SHAPES), "operations": list(SKETCH_OPERATIONS)},
        # The columns a flow table can be exported with, so the client
        # offers what this build actually writes rather than a copy of the
        # list that drifts from it.
        "flowColumns": [{"id": field, "label": heading} for field, heading, _width in FLOW_FIELDS],
        "kernels": [kernel.info.to_json() for kernel in available_kernels()],
        "defaultKernel": default_kernel(),
        "buildVariant": build_variant(),
        "rendering": {
            # The kernel hands over its own triangles, so a 3D view needs
            # nothing else installed; sampling is the raster density of the
            # flat views, which magnifies a picture without recomputing it.
            "surfaces": True,
            "maximumInterpolation": MAXIMUM_INTERPOLATION,
        },
        "limits": {
            "interpolationIsDisplayOnly": True,
            "calibrated": False,
        },
        "cli": _cli_command(),
    }


def _open(parameters: Mapping[str, Any]) -> dict[str, Any]:
    root = _root(parameters)
    repository = open_repository(root)
    # The window is about to be shown the whole library, so this workspace's
    # copy of it -- what it travels with, and the record of what this window
    # was shown -- is brought up to date here and nowhere else.
    repository.refresh_library_copy()
    project = load_project(repository, parameters.get("projectId"))
    return build_document(root, repository, project)


def _branch_name(parameters: Mapping[str, Any], repository, project, *, keeping: str = "") -> str:
    """A name for a branch: given, not blank, and not one already in use.

    Names are what the branch picker shows, so two branches called the
    same thing would leave the user choosing between them by position.
    """
    name = parameters.get("name")
    if not isinstance(name, str) or not name.strip():
        raise InvalidRequest("a branch needs a name.")
    name = name.strip()
    for branch in repository.list_branches(project.id):
        if branch.name == name and branch.id != keeping:
            raise InvalidRequest(f"this project already has a branch called {name!r}.")
    return name


def _create_branch(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Fork a branch at one of its steps: a process split.

    The new branch carries the steps up to and including that one and the
    results already computed for them -- the same steps have the same
    results -- so only what is added after the fork has to run.
    """
    root = _root(parameters)
    repository = open_repository(root)
    project = load_project(repository, parameters.get("projectId"))
    source_id = parameters.get("branchId")
    step_id = parameters.get("stepId")
    if not isinstance(source_id, str) or not source_id:
        raise InvalidRequest("create_branch requires branchId, the branch to fork.")
    if not isinstance(step_id, str) or not step_id:
        raise InvalidRequest("create_branch requires stepId, the step to fork after.")
    name = _branch_name(parameters, repository, project)
    try:
        branch = repository.create_branch(project.id, source_id, step_id, name)
    except KeyError as error:
        raise InvalidRequest(
            f"step {error.args[0]!r} is not on the branch being forked."
        ) from error
    DigestCache(repository).copy(
        source_id, branch.id, [key for step in branch.steps for key in result_keys(step.id)]
    )
    project.active_branch_id = branch.id
    repository.save_project(project)
    return {"branchId": branch.id, **build_document(root, repository, project)}


def _rename_branch(parameters: Mapping[str, Any]) -> dict[str, Any]:
    root = _root(parameters)
    repository = open_repository(root)
    project = load_project(repository, parameters.get("projectId"))
    branch_id = parameters.get("branchId")
    if not isinstance(branch_id, str) or not branch_id:
        raise InvalidRequest("rename_branch requires branchId.")
    name = _branch_name(parameters, repository, project, keeping=branch_id)
    try:
        repository.rename_branch(branch_id, name)
    except KeyError as error:
        raise InvalidRequest(f"branch {error.args[0]!r} was not found.") from error
    return build_document(root, repository, project)


def _delete_branch(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Drop a branch, and move off it if it was the one being worked on."""
    root = _root(parameters)
    repository = open_repository(root)
    project = load_project(repository, parameters.get("projectId"))
    branch_id = parameters.get("branchId")
    if not isinstance(branch_id, str) or not branch_id:
        raise InvalidRequest("delete_branch requires branchId.")
    branches = repository.list_branches(project.id)
    if not any(branch.id == branch_id for branch in branches):
        raise InvalidRequest(f"branch {branch_id!r} was not found.")
    if len(branches) < 2:
        raise InvalidRequest("a project keeps at least one branch; this is the only one.")
    children = [branch for branch in branches if branch.parent_branch_id == branch_id]
    if children:
        raise InvalidRequest(
            f"{len(children)} branch(es) were forked from this one "
            f"({', '.join(branch.name for branch in children)}); delete those first."
        )
    gone = next(branch for branch in branches if branch.id == branch_id)
    repository.delete_branch(branch_id)
    if project.active_branch_id == branch_id:
        # Back to where it was forked from, or to whatever is left.
        left = [branch for branch in branches if branch.id != branch_id]
        parent = next((b for b in left if b.id == gone.parent_branch_id), None)
        project.active_branch_id = (parent or left[0]).id
        repository.save_project(project)
    return build_document(root, repository, project)


def _persist_document(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Write the client's whole document back, then re-read what was stored."""
    root = _root(parameters)
    document = parameters.get("document")
    if not isinstance(document, Mapping):
        raise InvalidRequest("save_document requires a document object.")
    repository = open_repository(root)
    project = project_from_json(document.get("project", {}))
    stored = load_project(repository, project.id)
    if stored.id != project.id:
        raise InvalidRequest("Refusing to overwrite a different project id.")
    if stored.grid != project.grid:
        raise InvalidRequest(
            "Use set_grid to change the grid; it discards results computed on the old one."
        )
    if (
        stored.resolution_um != project.resolution_um
        or stored.resolution_xy_um != project.resolution_xy_um
    ):
        raise InvalidRequest(
            "Use set_grid to change the resolution; it discards results computed at the old one."
        )
    if project.kernel != stored.kernel:
        raise InvalidRequest(
            f"This project runs on the {stored.kernel!r} kernel and cannot be moved to "
            f"{project.kernel!r}. The kernels store geometry differently, so a result of "
            "one is not a result of the other. Create a new project to use another kernel."
        )
    project.kernel = stored.kernel
    repository.save_project(project)

    # Materials, tools and recipes are the shared library's, so what is saved
    # here is saved for every project. What the document leaves out is
    # measured against this workspace's own copy rather than the library:
    # a name this window never had is one another window has just added,
    # and a stale document not mentioning it is not a deletion.
    recipes = document.get("recipes")
    if isinstance(recipes, list):
        incoming = [recipe_from_json(recipe) for recipe in recipes]
        keep = {recipe.id for recipe in incoming}
        for recipe in incoming:
            repository.save_recipe(recipe)
        for stored in repository.own_recipes():
            if stored.id not in keep:
                repository.remove_recipe(stored.id)

    materials = document.get("materials")
    if isinstance(materials, list):
        incoming_materials = [material_from_json(material) for material in materials]
        keep_names = {material.name for material in incoming_materials}
        for material in incoming_materials:
            repository.save_material(material)
        for stored_material in repository.own_materials():
            if stored_material.name not in keep_names:
                repository.remove_material(stored_material.id)

    tools = document.get("tools")
    if isinstance(tools, list):
        incoming_tools = [tool_from_json(tool) for tool in tools]
        keep_tool_ids = {tool.id for tool in incoming_tools}
        for tool in incoming_tools:
            try:
                repository.save_tool(tool)
            except ValueError as error:
                raise InvalidRequest(str(error)) from error
        for stored_tool in repository.own_tools():
            if stored_tool.id not in keep_tool_ids:
                repository.remove_tool(stored_tool.id)

    branches = document.get("branches")
    if isinstance(branches, list):
        for payload in branches:
            branch = branch_from_json(payload)
            incoming_ids = {step.id for step in branch.steps}
            try:
                existing = repository.load_branch(branch.id)
            except KeyError:
                existing = None
            if existing is not None:
                dropped = next(
                    (step.id for step in existing.steps if step.id not in incoming_ids),
                    None,
                )
                if dropped is not None:
                    # Removing a step invalidates it and everything after it.
                    removed = repository.delete_step_and_dependents(branch.id, dropped)
                    DigestCache(repository).forget(
                        branch.id, [key for step_id in removed for key in result_keys(step_id)]
                    )
            repository.save_branch(project.id, branch)
    return build_document(root, repository, load_project(repository, project.id))


def _target_spacing(parameters: Mapping[str, Any], key: str = "targetSpacingNm") -> float:
    value = parameters.get(key)
    try:
        spacing = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise InvalidRequest(f"{key} must be a number") from error
    if not 0.1 <= spacing <= 1000.0:
        raise InvalidRequest(f"{key} must be between 0.1 and 1000")
    return spacing


def _target_spacing_xy(parameters: Mapping[str, Any]) -> float | None:
    """The XY arc sagitta a slab request asks for; None follows the z step."""
    if parameters.get("targetSpacingXyNm") in (None, ""):
        return None
    return _target_spacing(parameters, "targetSpacingXyNm")


#: The widest window the desktop will take, per axis, in micrometres. A
#: bigger one is a unit slip far more often than a real device.
MAXIMUM_WINDOW_UM = 200.0


def _window_from(parameters: Mapping[str, Any], current: UniformGrid3D) -> UniformGrid3D:
    """The project window a request asks for, or the current one.

    ``bounds`` carries xMin..zMax in micrometres. The wafer surface is z = 0
    in both kernels, with the substrate below it, so a window has to reach
    below zero to hold a wafer and above it to hold what is built on top.
    """
    bounds = parameters.get("bounds")
    if bounds is None:
        return current
    if not isinstance(bounds, Mapping):
        raise InvalidRequest("bounds must be an object with xMin..zMax in micrometres.")
    values = {}
    for key in ("xMin", "xMax", "yMin", "yMax", "zMin", "zMax"):
        try:
            values[key] = float(bounds[key])
        except (KeyError, TypeError, ValueError) as error:
            raise InvalidRequest(f"bounds.{key} must be a number in micrometres.") from error
        if not math.isfinite(values[key]):
            raise InvalidRequest(f"bounds.{key} must be finite.")
    for axis in ("x", "y", "z"):
        low, high = values[f"{axis}Min"], values[f"{axis}Max"]
        if high <= low:
            raise InvalidRequest(f"{axis}Max must be greater than {axis}Min.")
        if high - low > MAXIMUM_WINDOW_UM:
            raise InvalidRequest(
                f"The {axis} extent is {high - low:g} µm; the desktop takes at most "
                f"{MAXIMUM_WINDOW_UM:g} µm per axis. Lengths are in micrometres."
            )
    if not values["zMin"] < 0.0 < values["zMax"]:
        raise InvalidRequest(
            "The z range must reach below 0 (the substrate) and above 0 (what is built "
            "on it): the wafer surface is z = 0."
        )
    # The lattice counts are placeholders; the caller fits the real ones.
    return UniformGrid3D(
        values["xMin"], values["xMax"], values["yMin"], values["yMax"],
        values["zMin"], values["zMax"], current.nx, current.ny, current.nz,
    ) if _equal_spacing(values, current) else _placeholder_grid(values)


def _equal_spacing(values: Mapping[str, float], current: UniformGrid3D) -> bool:
    try:
        UniformGrid3D(
            values["xMin"], values["xMax"], values["yMin"], values["yMax"],
            values["zMin"], values["zMax"], current.nx, current.ny, current.nz,
        )
    except ValueError:
        return False
    return True


def _placeholder_grid(values: Mapping[str, float]) -> UniformGrid3D:
    """A valid lattice over the window at roughly 25 nm, for the fit to start from."""
    spacing = 0.025
    counts = [
        max(3, int(round((values[f"{axis}Max"] - values[f"{axis}Min"]) / spacing)) + 1)
        for axis in ("x", "y", "z")
    ]
    # Equal spacing is a class invariant; derive every count from x's spacing.
    dx = (values["xMax"] - values["xMin"]) / (counts[0] - 1)
    ny = max(3, int(round((values["yMax"] - values["yMin"]) / dx)) + 1)
    nz = max(3, int(round((values["zMax"] - values["zMin"]) / dx)) + 1)
    try:
        return UniformGrid3D(
            values["xMin"], values["xMax"], values["yMin"], values["yMax"],
            values["zMin"], values["zMax"], counts[0], ny, nz,
        )
    except ValueError as error:
        raise InvalidRequest(
            "Those bounds cannot be covered by one uniform spacing; use extents that "
            f"share a common step (for example multiples of 0.1 µm): {error}"
        ) from error


def _same_bounds(a: UniformGrid3D, b: UniformGrid3D) -> bool:
    return all(
        math.isclose(getattr(a, name), getattr(b, name), abs_tol=1e-12)
        for name in ("x_min", "x_max", "y_min", "y_max", "z_min", "z_max")
    )


def _as_window(window: UniformGrid3D) -> UniformGrid3D:
    """The window as a project stores it: bounds, with plausible node counts."""
    return window_grid(
        window.x_min, window.x_max, window.y_min, window.y_max, window.z_min, window.z_max
    )


def _plan_grid(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Report what a target resolution and window would mean.

    The kernel has no lattice: the number is the conformal walk step and the
    XY arc sagitta, which any positive value satisfies and which costs no
    nodes. So this exists to say whether it would change anything at all --
    applying it discards every stored result, and doing that for a value the
    project already has is the one outcome worth preventing.
    """
    root = _root(parameters)
    repository = open_repository(root)
    project = load_project(repository, parameters.get("projectId"))
    kernel = project_kernel(project)
    current = UniformGrid3D(**project.grid)
    window = _window_from(parameters, current)
    same_window = _same_bounds(window, current)
    spacing = _target_spacing(parameters)
    spacing_xy = _target_spacing_xy(parameters)
    current_xy = project.resolution_xy_um
    return {
        "kernel": kernel.info.id,
        "spacingRole": kernel.info.spacing_role,
        "grid": grid_to_json(current if same_window else _as_window(window)),
        "estimate": {
            "spacingNm": spacing,
            "spacingXyNm": spacing if spacing_xy is None else spacing_xy,
        },
        "maximumNodes": None,
        "withinLimit": True,
        "unchanged": same_window
        and abs(spacing / 1000.0 - (project.resolution_um or 0.0)) < 1e-12
        and (
            (spacing_xy is None and current_xy is None)
            or (
                spacing_xy is not None and current_xy is not None
                and abs(spacing_xy / 1000.0 - current_xy) < 1e-12
            )
        ),
    }


def _set_grid(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Apply a resolution, and a window when one is given.

    Both discard every stored result: the resolution is what the conformal
    walk and the arcs were computed at, and the window is the wafer itself
    (the substrate is as thick as the window is deep).
    """
    root = _root(parameters)
    repository = open_repository(root)
    project = load_project(repository, parameters.get("projectId"))
    kernel = project_kernel(project)
    current = UniformGrid3D(**project.grid)
    window = _window_from(parameters, current)
    if parameters.get("targetSpacingNm") is None:
        raise InvalidRequest(
            f"The {kernel.info.name} kernel has no grid to set; give targetSpacingNm "
            "to change the resolution it works at."
        )
    if not _same_bounds(window, current):
        project.grid = grid_dict(_as_window(window))
    spacing_xy = _target_spacing_xy(parameters)
    apply_resolution(
        repository, project, _target_spacing(parameters) / 1000.0,
        None if spacing_xy is None else spacing_xy / 1000.0,
    )
    return build_document(root, repository, project)


def _hidden_materials(parameters: Mapping[str, Any]) -> list[str]:
    """Materials the view is to look through, from the request."""
    names = parameters.get("hidden")
    if names is None:
        return []
    if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
        raise InvalidRequest("hidden must be a list of material names.")
    return [str(name) for name in names]


def _view_state(parameters: Mapping[str, Any]):
    """The state a view should draw, with the kernel that knows how to draw it."""
    root = _root(parameters)
    branch_id = parameters.get("branchId")
    if not isinstance(branch_id, str) or not branch_id:
        raise InvalidRequest("this method requires branchId.")
    step_id = parameters.get("stepId")
    return state_for_step(
        root,
        branch_id=branch_id,
        step_id=None if step_id in (None, "") else str(step_id),
        project_id=parameters.get("projectId"),
    )


def _section_line(value: Any) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """The AA–BB line of a section request: two (x, y) points in micrometres."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise InvalidRequest("line must be an object with start and end.")
    points = []
    for key in ("start", "end"):
        point = value.get(key)
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise InvalidRequest(f"line.{key} must be [x, y] in micrometres.")
        try:
            points.append((float(point[0]), float(point[1])))
        except (TypeError, ValueError) as error:
            raise InvalidRequest(f"line.{key} must be two numbers.") from error
    if points[0] == points[1]:
        raise InvalidRequest("A section line needs two distinct points.")
    return points[0], points[1]


def _material_colors(repository) -> dict[str, str]:
    return {material.name: material.color for material in repository.load_materials()}


def _import_gds(parameters: Mapping[str, Any]) -> dict[str, Any]:
    root = _root(parameters)
    source = parameters.get("source")
    if not isinstance(source, str) or not source:
        raise InvalidRequest("import_gds requires a source path.")
    source_path = Path(source)
    if not source_path.is_file() or source_path.suffix.lower() != ".gds":
        raise InvalidRequest("The selected layout must be an existing .gds file.")
    layouts = root / "layouts"
    layouts.mkdir(parents=True, exist_ok=True)
    destination = layouts / f"{int(time.time() * 1000)}-{source_path.name}"
    try:
        shutil.copy2(source_path, destination)
    except OSError as error:
        raise WorkspaceError(f"Cannot copy the GDS file: {error}") from error
    try:
        layers = available_gds_layers(destination)
    except Exception as error:  # gdstk raises library-specific errors
        destination.unlink(missing_ok=True)
        raise InvalidRequest(f"Cannot read the GDS file: {error}") from error
    repository = open_repository(root)
    project = load_project(repository, parameters.get("projectId"))
    project.gds_path = str(destination)
    repository.save_project(project)
    return {
        "fileName": source_path.name,
        "path": str(destination),
        "layers": [{"layer": layer, "datatype": datatype} for layer, datatype in layers],
        "document": build_document(root, repository, project),
    }


def _export_recipes(parameters: Mapping[str, Any]) -> dict[str, Any]:
    root = _root(parameters)
    destination = parameters.get("destination")
    if not isinstance(destination, str) or not destination:
        raise InvalidRequest("export_recipes_xlsx requires a destination path.")
    path = Path(destination)
    if path.suffix.lower() != ".xlsx":
        raise InvalidRequest("The export filename must end with .xlsx")
    repository = open_repository(root)
    RecipeLibrary(repository.load_recipes()).export_excel(path)
    return {"path": str(path)}


def _import_recipes(parameters: Mapping[str, Any]) -> dict[str, Any]:
    root = _root(parameters)
    source = parameters.get("source")
    if not isinstance(source, str) or not source:
        raise InvalidRequest("import_recipes_xlsx requires a source path.")
    path = Path(source)
    if not path.is_file():
        raise InvalidRequest("The selected recipe workbook does not exist.")
    try:
        library = RecipeLibrary.import_excel(path)
    except Exception as error:  # openpyxl raises workbook-specific errors
        raise InvalidRequest(f"Cannot read the recipe workbook: {error}") from error
    repository = open_repository(root)
    for recipe in library.recipes.values():
        repository.save_recipe(recipe)
    project = load_project(repository, parameters.get("projectId"))
    return {
        "imported": len(library.recipes),
        "document": build_document(root, repository, project),
    }


def _prepare_buried_faces_later(kernel: Any, state: Any, project: Any) -> None:
    """Build the mesh a peek behind a hidden material needs, in the background.

    The view that was just answered does not wait for it, and neither does
    the next request: each kind of mesh has its own lock, so this never
    holds up the one the 3D view opens with.
    """

    def work() -> None:
        try:
            kernel.warm_views(state, buried=True)
        except Exception:  # noqa: BLE001 - a warm-up must never surface as an error
            pass

    threading.Thread(target=work, name="warm-buried", daemon=True).start()


def dispatch(
    request: Mapping[str, Any],
    output: IO[str],
    cancel: threading.Event | None = None,
) -> Any:
    """Execute one request, writing progress events to ``output`` as it goes.

    ``cancel`` is set by the server when the client withdraws the request; a
    long method checks it at its natural boundaries and stops there.
    """
    method = request.get("method")
    parameters = request.get("params", {})
    if not isinstance(parameters, Mapping):
        raise InvalidRequest("params must be an object.")

    if method == "ping":
        return {"workerVersion": __version__, "protocolVersion": PROTOCOL_VERSION}
    if method == "describe":
        return _describe()
    if method == "create_workspace":
        root = _root(parameters)
        name = str(parameters.get("name") or "Process Studio Project")
        kernel = str(parameters.get("kernel") or default_kernel())
        repository = initialize_workspace(root, name, kernel)
        return build_document(root, repository, load_project(repository))
    if method == "open_workspace":
        return _open(parameters)
    if method == "save_document":
        return _persist_document(parameters)
    if method == "create_branch":
        return _create_branch(parameters)
    if method == "rename_branch":
        return _rename_branch(parameters)
    if method == "delete_branch":
        return _delete_branch(parameters)
    if method == "plan_grid":
        return _plan_grid(parameters)
    if method == "set_grid":
        return _set_grid(parameters)
    if method == "save_sketch":
        root = _root(parameters)
        sketch_id = parameters.get("sketchId")
        sketch = parameters.get("sketch")
        if not isinstance(sketch_id, str) or not sketch_id:
            raise InvalidRequest("save_sketch requires sketchId.")
        if not isinstance(sketch, Mapping):
            raise InvalidRequest("save_sketch requires a sketch object.")
        save_sketch(root, sketch_id, sketch)
        repository = open_repository(root)
        return build_document(root, repository, load_project(repository, parameters.get("projectId")))
    if method == "preview_mask":
        root = _root(parameters)
        payload = parameters.get("sketch")
        if not isinstance(payload, Mapping):
            raise InvalidRequest("preview_mask requires a sketch object.")
        repository = open_repository(root)
        project = load_project(repository, parameters.get("projectId"))
        grid = project.grid
        keep = str(parameters.get("keep", "inside"))
        if keep not in ("inside", "outside"):
            raise InvalidRequest("keep must be inside or outside.")
        return sketch_preview_image(
            sketch_from_payload(payload),
            extent=(grid["x_min"], grid["x_max"], grid["y_min"], grid["y_max"]),
            keep=keep,
        )
    if method == "run_cli":
        return _run_cli(parameters, output, cancel)
    if method == "check_update":
        return check_update()
    if method == "install_update":
        return install_update(
            parameters.get("url"),
            lambda message: _write(
                output,
                {"kind": "event", "id": request.get("id"), "event": {"kind": "log", "message": message}},
            ),
        )
    if method == "export_flow":
        return export_flow(
            _root(parameters), _destination(parameters), str(parameters.get("format") or "xlsx"),
            parameters.get("projectId"), parameters.get("columns"),
        )
    if method == "import_flow":
        return import_flow(_root(parameters), _source(parameters), output)
    if method == "export_library":
        return export_library(_root(parameters), str(parameters.get("kind") or ""), _destination(parameters))
    if method == "import_library":
        return import_library(
            _root(parameters), str(parameters.get("kind") or ""), _source(parameters), parameters.get("projectId")
        )
    if method == "copy_workspace":
        return copy_workspace(_root(parameters), _destination(parameters))
    if method == "reveal_path":
        return reveal_path(parameters.get("path"))
    if method == "open_url":
        return open_url(parameters.get("url"))
    if method == "run_flow":
        root = _root(parameters)

        def progress(event: dict[str, Any]) -> None:
            # Carrying the id lets the shell attribute progress to the run
            # that produced it, now that several requests can be in flight.
            _write(output, {"kind": "event", "id": request.get("id"), "event": event})

        return run_flow(
            root,
            branch_id=parameters.get("branchId"),
            project_id=parameters.get("projectId"),
            through_step_id=parameters.get("throughStepId"),
            force=bool(parameters.get("force", False)),
            from_step_id=parameters.get("fromStepId"),
            prepare_buried=bool(parameters.get("prepareBuried", False)),
            progress=progress,
            should_cancel=(lambda: False) if cancel is None else cancel.is_set,
        )
    if method == "get_surfaces":
        state, repository, project, kernel = _view_state(parameters)
        materials = parameters.get("materials")
        buried = bool(parameters.get("buried", False))
        triangulation = parameters.get("triangulation")
        if triangulation is not None:
            triangulation = str(triangulation)
            if triangulation not in MESH_ENGINES:
                raise InvalidRequest(
                    f"get_surfaces triangulation must be one of {', '.join(MESH_ENGINES)}."
                )
        payload = kernel.surfaces(
            state,
            project=project,
            interpolation=_checked_interpolation(parameters.get("interpolation", 1)),
            materials=None if materials is None else [str(name) for name in materials],
            triangulation=triangulation,
            buried=buried,
        )
        if not buried:
            # Looking at a step is the best guess there is that its buried
            # faces will be wanted: hiding a material is the next thing
            # anyone does, and that mesh costs several times the one just
            # sent. Prepared now, in the background, for this step alone --
            # preparing every step of a run took half a minute of a flow's
            # worth of them, most never looked at.
            _prepare_buried_faces_later(kernel, state, project)
        colors = _material_colors(repository)
        for surface in payload["surfaces"]:
            surface["color"] = colors.get(surface["material"], "#7c83a0")
        return payload
    if method == "get_section":
        state, repository, project, kernel = _view_state(parameters)
        axis = str(parameters.get("axis", "y"))
        if axis not in ("x", "y", "line"):
            raise InvalidRequest("get_section axis must be 'x', 'y' or 'line'.")
        return kernel.section(
            state,
            _material_colors(repository),
            project=project,
            axis=axis,
            position=(
                None if parameters.get("position") is None else float(parameters["position"])
            ),
            interpolation=_checked_interpolation(parameters.get("interpolation", 1)),
            line=_section_line(parameters.get("line")),
        )
    if method == "get_top_view":
        state, repository, project, kernel = _view_state(parameters)
        return kernel.top_view(
            state,
            _material_colors(repository),
            project=project,
            hidden=_hidden_materials(parameters),
            steps=parameters.get("steps", True) is not False,
        )
    if method == "export_mesh":
        state, repository, project, kernel = _view_state(parameters)
        destination = parameters.get("destination")
        if not isinstance(destination, str) or not destination:
            raise InvalidRequest("export_mesh requires a destination path.")
        materials = parameters.get("materials")
        return write_mesh(
            kernel, state, project, _material_colors(repository), Path(destination),
            materials=None if materials is None else [str(name) for name in materials],
            interpolation=_checked_interpolation(parameters.get("interpolation", 1)),
        )
    if method == "save_image":
        destination = parameters.get("destination")
        image = parameters.get("image")
        if not isinstance(destination, str) or not destination:
            raise InvalidRequest("save_image requires a destination path.")
        if not isinstance(image, str) or not image:
            raise InvalidRequest("save_image requires the image as base64 PNG.")
        return write_image(Path(destination), image)
    if method == "import_gds":
        return _import_gds(parameters)
    if method == "export_recipes_xlsx":
        return _export_recipes(parameters)
    if method == "import_recipes_xlsx":
        return _import_recipes(parameters)
    if method == "gds_layers":
        root = _root(parameters)
        repository = open_repository(root)
        project = load_project(repository, parameters.get("projectId"))
        if not project.gds_path:
            return {"layers": []}
        return {
            "layers": [
                {"layer": layer, "datatype": datatype}
                for layer, datatype in available_gds_layers(project.gds_path)
            ]
        }
    if method == "list_sketches":
        root = _root(parameters)
        return {"sketches": sorted(load_sketches(root))}
    raise InvalidRequest(f"Unknown RPC method: {method!r}")


def serve(input_stream: IO[str] = sys.stdin, output_stream: IO[str] = sys.stdout) -> int:
    """Serve requests until the input closes. See :mod:`.server`."""
    from .server import Server

    return Server(input_stream, output_stream).run()
