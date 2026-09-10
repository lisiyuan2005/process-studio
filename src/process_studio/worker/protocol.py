"""JSON-line RPC surface used by the Tauri shell.

One request per line on stdin, one response per line on stdout. Progress lines
are written as they happen so a long run reports what it is doing instead of
going quiet.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any, IO, Mapping

from . import PROTOCOL_VERSION, __version__
from ..kernel.grid import UniformGrid3D
from ..layout.gds import available_gds_layers
from ..libraries import RecipeLibrary
from ..models import ProcessType
from .errors import InvalidRequest, WorkerError, WorkspaceError
from .render import MAXIMUM_INTERPOLATION, MESHES_AVAILABLE, material_surfaces, section_image, top_view_image
from .runner import apply_grid, build_document, run_flow, state_for_step
from .serialize import (
    branch_from_json,
    grid_from_json,
    material_from_json,
    project_from_json,
    recipe_from_json,
)
from .workspace import (
    DigestCache,
    initialize_workspace,
    load_project,
    load_sketches,
    open_repository,
    save_sketch,
)

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


def _describe() -> dict[str, Any]:
    """Report what this build can do so the client never hard-codes it."""
    return {
        "workerVersion": __version__,
        "protocolVersion": PROTOCOL_VERSION,
        "processTypes": [process_type.value for process_type in ProcessType],
        "maskSources": list(MASK_SOURCES),
        "sketch": {"shapes": list(SKETCH_SHAPES), "operations": list(SKETCH_OPERATIONS)},
        "rendering": {
            "surfaces": MESHES_AVAILABLE,
            "maximumInterpolation": MAXIMUM_INTERPOLATION,
        },
        "numerics": {
            "solverOrders": [1, 2],
            "refinementFactors": [2, 4, 8],
            "defaultMaxNodes": 20_000_000,
        },
        "limits": {
            "interpolationIsDisplayOnly": True,
            "calibrated": False,
        },
    }


def _open(parameters: Mapping[str, Any]) -> dict[str, Any]:
    root = _root(parameters)
    repository = open_repository(root)
    project = load_project(repository, parameters.get("projectId"))
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
    repository.save_project(project)

    recipes = document.get("recipes")
    if isinstance(recipes, list):
        incoming = [recipe_from_json(recipe) for recipe in recipes]
        keep = {recipe.id for recipe in incoming}
        for recipe in incoming:
            repository.save_recipe(recipe)
        referenced = {
            step.recipe_id
            for branch in repository.list_branches(project.id)
            for step in branch.steps
        }
        with repository.connect() as connection:
            for row in connection.execute("SELECT id FROM recipes").fetchall():
                if row["id"] in keep:
                    continue
                if row["id"] in referenced:
                    raise InvalidRequest(
                        f"Recipe {row['id']!r} is still used by a step in this project."
                    )
                connection.execute("DELETE FROM recipes WHERE id=?", (row["id"],))

    materials = document.get("materials")
    if isinstance(materials, list):
        incoming_materials = [material_from_json(material) for material in materials]
        keep_names = {material.name for material in incoming_materials}
        for material in incoming_materials:
            repository.save_material(material)
        with repository.connect() as connection:
            for row in connection.execute("SELECT name FROM materials").fetchall():
                if row["name"] not in keep_names:
                    connection.execute("DELETE FROM materials WHERE name=?", (row["name"],))

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
                    DigestCache(repository).forget(branch.id, removed)
            repository.save_branch(project.id, branch)
    return build_document(root, repository, load_project(repository, project.id))


def _set_grid(parameters: Mapping[str, Any]) -> dict[str, Any]:
    root = _root(parameters)
    grid = grid_from_json(parameters.get("grid", {}))
    repository = open_repository(root)
    project = load_project(repository, parameters.get("projectId"))
    apply_grid(repository, project, grid)
    return build_document(root, repository, project)


def _view_state(parameters: Mapping[str, Any]):
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


def dispatch(request: Mapping[str, Any], output: IO[str]) -> Any:
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
        repository = initialize_workspace(root, name)
        return build_document(root, repository, load_project(repository))
    if method == "open_workspace":
        return _open(parameters)
    if method == "save_document":
        return _persist_document(parameters)
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
    if method == "run_flow":
        root = _root(parameters)

        def progress(event: dict[str, Any]) -> None:
            _write(output, {"kind": "event", "event": event})

        return run_flow(
            root,
            branch_id=parameters.get("branchId"),
            project_id=parameters.get("projectId"),
            through_step_id=parameters.get("throughStepId"),
            force=bool(parameters.get("force", False)),
            progress=progress,
        )
    if method == "get_surfaces":
        state, repository, _ = _view_state(parameters)
        materials = parameters.get("materials")
        payload = material_surfaces(
            state,
            interpolation=parameters.get("interpolation", 1),
            materials=None if materials is None else [str(name) for name in materials],
        )
        colors = _material_colors(repository)
        for surface in payload["surfaces"]:
            surface["color"] = colors.get(surface["material"], "#7c83a0")
        return payload
    if method == "get_section":
        state, repository, _ = _view_state(parameters)
        return section_image(
            state,
            _material_colors(repository),
            axis=str(parameters.get("axis", "y")),
            position=(
                None if parameters.get("position") is None else float(parameters["position"])
            ),
            interpolation=parameters.get("interpolation", 1),
        )
    if method == "get_top_view":
        state, repository, _ = _view_state(parameters)
        return top_view_image(state, _material_colors(repository))
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
    for raw_line in input_stream:
        if not raw_line.strip():
            continue
        request_id: Any = None
        try:
            request = json.loads(raw_line)
            if not isinstance(request, dict):
                raise InvalidRequest("Each input line must contain a JSON object.")
            request_id = request.get("id")
            if request.get("kind") not in (None, "request"):
                raise InvalidRequest("Input kind must be 'request'.")
            result = dispatch(request, output_stream)
            _write(
                output_stream,
                {"kind": "response", "id": request_id, "ok": True, "result": result},
            )
        except WorkerError as error:
            _write(
                output_stream,
                {
                    "kind": "response",
                    "id": request_id,
                    "ok": False,
                    "error": {"code": type(error).__name__, "message": str(error)},
                },
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            _write(
                output_stream,
                {
                    "kind": "response",
                    "id": request_id,
                    "ok": False,
                    "error": {"code": "InvalidRequest", "message": str(error)},
                },
            )
        except Exception as error:  # fail closed without leaking a traceback
            _write(
                output_stream,
                {
                    "kind": "response",
                    "id": request_id,
                    "ok": False,
                    "error": {"code": "InternalError", "message": str(error)},
                },
            )
    return 0
