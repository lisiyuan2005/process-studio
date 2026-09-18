"""Workspace discovery, sketch resolution and step cache bookkeeping.

A workspace is a directory holding ``process_studio.sqlite3``, its snapshot
directory and the Quick Sketch files. The layout is unchanged from earlier
versions, so existing workspaces open as they are.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..defaults import (
    default_branch,
    default_grid,
    default_materials,
    default_recipes,
    default_tools,
)
from ..kernels import default_kernel, get_kernel
from ..layout.quick_sketch import QuickSketch, SketchShape
from ..models import result_key, result_keys, FlowBranch, ProcessStep, ProjectDefinition, Recipe
from ..storage import ProjectRepository
from .errors import InvalidRequest, WorkspaceError
from .serialize import grid_dict

DATABASE_NAME = "process_studio.sqlite3"
SKETCH_DIRECTORY = "sketches"
LEGACY_DEFAULT_SKETCH = "default-sketch.json"
DEFAULT_PROJECT_ID = "default-project"


def database_path(root: Path) -> Path:
    return root / DATABASE_NAME


def open_repository(root: Path, *, create: bool = False) -> ProjectRepository:
    path = database_path(root)
    if not create and not path.is_file():
        raise WorkspaceError(f"{root} does not contain {DATABASE_NAME}.")
    try:
        return ProjectRepository(path)
    except (OSError, sqlite3.Error) as error:
        raise WorkspaceError(f"Cannot open workspace {root}: {error}") from error


def sketch_directory(root: Path) -> Path:
    return root / SKETCH_DIRECTORY


def sketch_path(root: Path, sketch_id: str) -> Path:
    safe = sketch_id.strip()
    if not safe or "/" in safe or "\\" in safe or safe.startswith("."):
        raise InvalidRequest(f"invalid sketch id: {sketch_id!r}")
    return sketch_directory(root) / f"{safe}.json"


def load_sketches(root: Path) -> dict[str, QuickSketch]:
    """Collect every sketch a step can reference.

    ``sketches/<id>.json`` is the current location. Files written by earlier
    versions next to the database are still read so existing workspaces keep
    working; the legacy ``default-sketch.json`` keeps the id ``default``.
    """
    sketches: dict[str, QuickSketch] = {}
    legacy = root / LEGACY_DEFAULT_SKETCH
    if legacy.is_file():
        sketches["default"] = _read_sketch(legacy)
    for path in sorted(root.glob("*.json")):
        if path.name == LEGACY_DEFAULT_SKETCH:
            continue
        sketch = _read_sketch(path, quiet=True)
        if sketch is not None:
            sketches.setdefault(path.stem, sketch)
    directory = sketch_directory(root)
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            sketch = _read_sketch(path, quiet=True)
            if sketch is not None:
                sketches[path.stem] = sketch
    return sketches


def _read_sketch(path: Path, *, quiet: bool = False) -> QuickSketch | None:
    try:
        return QuickSketch.load(path)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        if quiet:
            return None
        raise WorkspaceError(f"Cannot read sketch {path.name}: {error}") from error


def sketch_from_payload(payload: Mapping[str, Any], fallback_name: str = "sketch") -> QuickSketch:
    """A sketch from the client's JSON, validated the way the kernel needs it."""
    shapes = payload.get("shapes", [])
    if not isinstance(shapes, list):
        raise InvalidRequest("sketch shapes must be a list")
    try:
        sketch = QuickSketch(
            name=str(payload.get("name") or fallback_name),
            shapes=[
                SketchShape(
                    str(shape["kind"]),
                    str(shape.get("operation", "merge")),
                    dict(shape.get("parameters", {})),
                    tuple(shape.get("array", (1, 1, 0.0, 0.0))),  # type: ignore[arg-type]
                )
                for shape in shapes
            ],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise InvalidRequest(f"sketch is invalid: {error}") from error
    for index, shape in enumerate(sketch.shapes, start=1):
        parameters = shape.parameters
        try:
            if shape.kind == "rectangle":
                width, height = (float(value) for value in parameters["size"])
                if min(width, height) <= 0.0:
                    raise ValueError("rectangle size must be positive")
            elif shape.kind == "circle":
                if float(parameters["radius"]) <= 0.0:
                    raise ValueError("circle radius must be positive")
            elif shape.kind == "polygon":
                if len(parameters["points"]) < 3:
                    raise ValueError("a polygon needs at least three points")
            else:
                if len(parameters["points"]) < 2:
                    raise ValueError("a path needs at least two points")
                if float(parameters["width"]) <= 0.0:
                    raise ValueError("path width must be positive")
        except (KeyError, TypeError, ValueError) as error:
            raise InvalidRequest(f"shape {index} ({shape.kind}): {error}") from error
    return sketch


def save_sketch(root: Path, sketch_id: str, payload: Mapping[str, Any]) -> QuickSketch:
    shapes = payload.get("shapes", [])
    if not isinstance(shapes, list):
        raise InvalidRequest("sketch shapes must be a list")
    try:
        sketch = QuickSketch(
            name=str(payload.get("name") or sketch_id),
            shapes=[
                SketchShape(
                    str(shape["kind"]),
                    str(shape.get("operation", "merge")),
                    dict(shape.get("parameters", {})),
                    tuple(shape.get("array", (1, 1, 0.0, 0.0))),  # type: ignore[arg-type]
                )
                for shape in shapes
            ],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise InvalidRequest(f"sketch is invalid: {error}") from error
    path = sketch_path(root, sketch_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    sketch.save(path)
    return sketch


def sketch_to_json(sketch_id: str, sketch: QuickSketch) -> dict[str, Any]:
    return {
        "id": sketch_id,
        "name": sketch.name,
        "shapes": [
            {
                "kind": shape.kind,
                "operation": shape.operation,
                "parameters": dict(shape.parameters),
                "array": list(shape.array),
            }
            for shape in sketch.shapes
        ],
    }


def initialize_workspace(
    root: Path, name: str, kernel: str | None = None
) -> ProjectRepository:
    """Create the starter project a new workspace opens with.

    The kernel is fixed here and nowhere else: it decides how every later
    result is computed and stored, so it is part of what the project is.
    """
    try:
        chosen = get_kernel(kernel or default_kernel())
    except KeyError as error:
        # KeyError quotes its message when printed; the text itself is wanted.
        raise InvalidRequest(error.args[0]) from error
    root.mkdir(parents=True, exist_ok=True)
    repository = open_repository(root, create=True)
    if repository.list_projects():
        return repository
    grid = default_grid()
    branch = default_branch(chosen.info.id)
    project = ProjectDefinition(
        name.strip() or "Process Studio Project",
        grid_dict(grid),
        active_branch_id=branch.id,
        id=DEFAULT_PROJECT_ID,
        kernel=chosen.info.id,
        resolution_um=(
            None
            if chosen.info.spacing_role == "grid"
            else chosen.info.spacing_presets_nm[1] / 1000.0
        ),
    )
    for material in default_materials():
        repository.save_material(material)
    for recipe in default_recipes(chosen.info.id):
        repository.save_recipe(recipe)
    for tool in default_tools():
        repository.save_tool(tool)
    repository.save_project(project)
    repository.save_branch(project.id, branch)
    starter = QuickSketch(
        "default",
        [SketchShape("circle", parameters={"center": (0.0, 0.0), "radius": 0.22})],
    )
    path = sketch_path(root, "default")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        starter.save(path)
    return repository


def load_project(repository: ProjectRepository, project_id: str | None = None) -> ProjectDefinition:
    if project_id:
        try:
            return repository.load_project(project_id)
        except KeyError as error:
            raise InvalidRequest(f"project {project_id!r} was not found") from error
    try:
        return repository.load_project(DEFAULT_PROJECT_ID)
    except KeyError:
        projects = repository.list_projects()
        if not projects:
            raise WorkspaceError("This workspace has no project.")
        return projects[0]


def _plain_numbers(value: Any) -> Any:
    """The value with every whole float as an integer.

    The shell's JSON carries a whole number as ``1`` and a flow file's as
    ``1.0``; Python compares them equal but serialises them differently,
    and a digest that told them apart made every masked step stale after
    the desktop saved a workspace the CLI had built. Whole floats become
    integers, the form the shell has always stored, so the digests of
    workspaces built in the desktop stay what they were.
    """
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, Mapping):
        return {str(k): _plain_numbers(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_numbers(v) for v in value]
    return value


def _canonical(value: Any) -> str:
    return json.dumps(_plain_numbers(value), sort_keys=True, separators=(",", ":"), default=str)


def layout_fingerprint(
    gds_path: str | None, repository: "ProjectRepository | None" = None
) -> list[Any] | None:
    """What identifies the layout a step's mask is cut from.

    The file itself, not the path alone: importing the same GDS again
    copies it under a new name, and editing one in place keeps the name.
    Its size and modification time say both, and cost one stat -- a real
    layout runs to hundreds of megabytes, and this is read again every
    time the flow's status is refreshed, so reading the bytes is not on.

    The path goes in as the workspace stores it, not as it resolves here:
    an absolute one would make every step that cuts from the layout stale
    the moment the workspace was copied anywhere, which is the opposite of
    what the digest is for.
    """
    if not gds_path:
        return None
    named = gds_path if repository is None else repository.as_stored(gds_path)
    try:
        stat = Path(gds_path).stat()
    except OSError:
        return [named, None, None]  # gone: not the layout that ran
    return [named, stat.st_size, stat.st_mtime_ns]


def step_digest(
    previous: str,
    step: ProcessStep,
    recipe: Recipe | None,
    sketch: QuickSketch | None,
    grid: Mapping[str, Any],
    layout: list[Any] | None = None,
) -> str:
    """Hash everything a step's result depends on, including its history."""
    payload = {
        "previous": previous,
        "grid": dict(grid),
        "step": {
            "maskSource": step.mask_source,
            "layer": step.layer,
            "datatype": step.datatype,
            "keep": step.keep,
            "enabled": step.enabled,
        },
        "recipe": None
        if recipe is None
        else {
            "processType": recipe.process_type.value,
            "outputMaterial": recipe.output_material,
            "parameters": recipe.parameters,
            "materialResponses": {
                name: [response.rate_um_per_min, response.stop_layer]
                for name, response in recipe.material_responses.items()
            },
        },
        # Only for a step that cuts its mask from the layout: everything
        # else is unaffected by which layout the project carries.
        "layout": layout if step.mask_source == "gds" else None,
        "sketch": None
        if sketch is None
        else [
            [shape.kind, shape.operation, shape.parameters, list(shape.array)]
            for shape in sketch.shapes
        ],
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def branch_digests(
    branch: FlowBranch,
    recipes: Mapping[str, Recipe],
    sketches: Mapping[str, QuickSketch],
    grid: Mapping[str, Any],
    layout: list[Any] | None = None,
) -> list[str]:
    """Return one chained digest per step, in flow order."""
    digests: list[str] = []
    previous = "genesis"
    for step in branch.steps:
        recipe = step.effective_recipe(recipes)
        sketch_id = str(recipe.parameters.get("sketch_id", "default"))
        previous = step_digest(
            previous,
            step,
            recipe,
            sketches.get(sketch_id) if step.mask_source == "quick_sketch" else None,
            grid,
            layout,
        )
        digests.append(previous)
    return digests


class DigestCache:
    """Records which digest produced each stored snapshot.

    The kernel's snapshot table only says that a step ran. Keeping the digest
    beside it is what lets an unchanged prefix be reused instead of recomputed,
    and a stale digest is treated as no snapshot at all.
    """

    def __init__(self, repository: ProjectRepository) -> None:
        self.repository = repository
        with repository.connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS worker_step_digests (
                    branch_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    PRIMARY KEY(branch_id, step_id)
                )"""
            )

    def load(self, branch_id: str) -> dict[str, str]:
        with self.repository.connect() as connection:
            rows = connection.execute(
                "SELECT step_id, digest FROM worker_step_digests WHERE branch_id=?",
                (branch_id,),
            ).fetchall()
        return {row["step_id"]: row["digest"] for row in rows}

    def store(self, branch_id: str, step_id: str, digest: str) -> None:
        with self.repository.connect() as connection:
            connection.execute(
                """INSERT INTO worker_step_digests(branch_id, step_id, digest)
                VALUES (?, ?, ?)
                ON CONFLICT(branch_id, step_id) DO UPDATE SET digest=excluded.digest""",
                (branch_id, step_id, digest),
            )

    def forget(self, branch_id: str, step_ids: Iterable[str]) -> None:
        ids = list(step_ids)
        if not ids:
            return
        with self.repository.connect() as connection:
            connection.executemany(
                "DELETE FROM worker_step_digests WHERE branch_id=? AND step_id=?",
                [(branch_id, step_id) for step_id in ids],
            )
