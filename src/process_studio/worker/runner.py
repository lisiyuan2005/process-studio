"""Workspace document assembly and cached flow execution."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from ..kernel.grid import UniformGrid3D
from ..kernels import Kernel, get_kernel
from ..layout.quick_sketch import QuickSketch
from ..models import FlowBranch, ProjectDefinition, Recipe
from ..storage import ProjectRepository
from .errors import Cancelled, InvalidRequest, WorkspaceError
from .state_cache import STATE_CACHE
from .serialize import (
    branch_to_json,
    grid_dict,
    material_to_json,
    project_to_json,
    recipe_to_json,
)
from .workspace import (
    DigestCache,
    branch_digests,
    load_project,
    load_sketches,
    open_repository,
    sketch_to_json,
)

ProgressCallback = Callable[[dict[str, Any]], None]


def project_kernel(project: ProjectDefinition) -> Kernel:
    """The kernel this project was created with."""
    try:
        return get_kernel(project.kernel)
    except KeyError as error:
        raise WorkspaceError(error.args[0]) from error


def _branch_or_fail(repository: ProjectRepository, branch_id: str) -> FlowBranch:
    try:
        return repository.load_branch(branch_id)
    except KeyError as error:
        raise InvalidRequest(f"branch {branch_id!r} was not found") from error


def build_document(
    root: Path,
    repository: ProjectRepository,
    project: ProjectDefinition,
) -> dict[str, Any]:
    """Everything the client needs to render a workspace in one payload."""
    branches = repository.list_branches(project.id)
    if not branches:
        raise WorkspaceError("This project has no process branch.")
    active_id = project.active_branch_id or branches[0].id
    if all(branch.id != active_id for branch in branches):
        active_id = branches[0].id
    loaded = [repository.load_branch(branch.id) for branch in branches]
    recipes = repository.load_recipes()
    recipes_by_id = {recipe.id: recipe for recipe in recipes}
    for branch in loaded:
        migrated = False
        for step in branch.steps:
            migrated = step.detach_from_library(recipes_by_id) or migrated
        if migrated:
            repository.save_branch(project.id, branch)
    sketches = load_sketches(root)
    statuses = {
        branch.id: step_statuses(repository, branch, recipes, sketches, project)
        for branch in loaded
    }
    return {
        "root": str(root),
        "project": {**project_to_json(project), "activeBranchId": active_id},
        "branches": [branch_to_json(branch, recipes_by_id) for branch in loaded],
        "recipes": [recipe_to_json(recipe) for recipe in recipes],
        "materials": [material_to_json(material) for material in repository.load_materials()],
        "sketches": [
            sketch_to_json(sketch_id, sketch) for sketch_id, sketch in sorted(sketches.items())
        ],
        "stepStatuses": statuses,
    }


def step_statuses(
    repository: ProjectRepository,
    branch: FlowBranch,
    recipes: list[Recipe],
    sketches: Mapping[str, QuickSketch],
    project: ProjectDefinition,
) -> dict[str, str]:
    """Report what each step has stored.

    ``clean`` is a snapshot that matches the step's current digest. ``stale``
    is a snapshot that no longer matches, which is still a real result the
    views can show. ``dirty`` is a step that has never run and has nothing.
    """
    by_id = {recipe.id: recipe for recipe in recipes}
    digests = branch_digests(branch, by_id, sketches, project.grid)
    stored = DigestCache(repository).load(branch.id)
    with repository.connect() as connection:
        rows = connection.execute(
            "SELECT step_id FROM branch_snapshots WHERE branch_id=?",
            (branch.id,),
        ).fetchall()
    with_snapshot = {row["step_id"] for row in rows}
    statuses: dict[str, str] = {}
    invalidated = False
    for step, digest in zip(branch.steps, digests):
        stored_result = step.id in with_snapshot
        clean = not invalidated and stored_result and stored.get(step.id) == digest
        statuses[step.id] = "clean" if clean else "stale" if stored_result else "dirty"
        if not clean:
            # A changed step invalidates everything built on top of it.
            invalidated = True
    return statuses


def run_flow(
    root: Path,
    *,
    branch_id: str | None = None,
    project_id: str | None = None,
    through_step_id: str | None = None,
    force: bool = False,
    progress: ProgressCallback | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Execute the branch, reusing snapshots whose digest still matches.

    ``should_cancel`` is consulted between steps. A step that has finished
    stays stored, so a cancelled run resumes from where it stopped.
    """
    emit = progress or (lambda _event: None)
    cancelled = should_cancel or (lambda: False)
    repository = open_repository(root)
    project = load_project(repository, project_id)
    branch = _branch_or_fail(repository, branch_id or project.active_branch_id or "")
    recipes = repository.load_recipes()
    by_id = {recipe.id: recipe for recipe in recipes}
    sketches = load_sketches(root)
    digests = branch_digests(branch, by_id, sketches, project.grid)
    cache = DigestCache(repository)
    stored = cache.load(branch.id)
    kernel = project_kernel(project)
    materials = repository.load_materials()
    logger = lambda message: emit({"kind": "log", "message": message})  # noqa: E731

    if through_step_id and all(step.id != through_step_id for step in branch.steps):
        raise InvalidRequest(f"step {through_step_id!r} is not part of this branch")

    total = len(branch.steps)
    if through_step_id:
        total = next(
            index + 1 for index, step in enumerate(branch.steps) if step.id == through_step_id
        )
    state = kernel.initial_state(project, materials=materials)
    executed: list[str] = []
    cached: list[str] = []
    reusable = not force
    started = time.perf_counter()
    for index, (step, digest) in enumerate(zip(branch.steps, digests)):
        if index >= total:
            break
        if cancelled():
            raise Cancelled(
                f"Stopped before {step.name}; {len(executed)} step(s) ran and are kept."
            )
        if reusable and stored.get(step.id) == digest:
            try:
                state = _load_state(kernel, repository.snapshot_path(branch.id, step.id))
                cached.append(step.id)
                emit(
                    {
                        "kind": "progress",
                        "stepId": step.id,
                        "message": f"Cached {step.name}",
                        "completed": index + 1,
                        "total": total,
                    }
                )
                continue
            except KeyError:
                # The digest survived but the snapshot file did not; recompute.
                reusable = False
        reusable = False
        emit(
            {
                "kind": "progress",
                "stepId": step.id,
                "message": f"Running {step.name}",
                "completed": index,
                "total": total,
            }
        )
        started_step = time.perf_counter()
        state = kernel.run_step(
            state,
            step,
            project=project,
            recipes=by_id,
            sketches=sketches,
            logger=logger,
            materials=materials,
        )
        repository.save_snapshot(
            project.id,
            branch.id,
            step.id,
            state,
            suffix=kernel.info.snapshot_suffix,
        )
        # The state just computed is what the views will ask for next.
        STATE_CACHE.put(
            repository.snapshot_path(branch.id, step.id), state, kernel.state_bytes(state)
        )
        repository.log(
            project.id,
            f"{step.name} completed",
            step_id=step.id,
            elapsed_ms=(time.perf_counter() - started_step) * 1000.0,
        )
        cache.store(branch.id, step.id, digest)
        executed.append(step.id)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    emit(
        {
            "kind": "log",
            "message": f"Finished {len(executed)} step(s), {len(cached)} cache hit(s) in {elapsed_ms:.0f} ms",
        }
    )
    _warm_views_later(kernel, [repository.snapshot_path(branch.id, step_id) for step_id in executed])
    return {
        "branchId": branch.id,
        "executedStepIds": executed,
        "cachedStepIds": cached,
        "elapsedMs": elapsed_ms,
        "materials": kernel.state_materials(state),
        "stepStatuses": step_statuses(repository, branch, recipes, sketches, project),
    }


def _warm_views_later(kernel: Kernel, paths: list[Path]) -> None:
    """Prepare the views of freshly stored states while the user looks at the log.

    The last step is what the user opens first, so it goes first. A state
    that has already left the cache is skipped rather than reloaded: the
    warming is a courtesy, and the view builds what it needs on demand.
    """
    if not paths:
        return

    def work() -> None:
        for path in reversed(paths):
            state = STATE_CACHE.get(path)
            if state is None:
                continue
            try:
                kernel.warm_views(state)
            except Exception:  # noqa: BLE001 - a warm-up must never surface as an error
                continue

    threading.Thread(target=work, name="warm-views", daemon=True).start()


def state_for_step(
    root: Path,
    *,
    branch_id: str,
    step_id: str | None,
    project_id: str | None = None,
) -> tuple[Any, ProjectRepository, ProjectDefinition, Kernel]:
    """Load the geometry a view should show, or the bare wafer before step one."""
    repository = open_repository(root)
    project = load_project(repository, project_id)
    kernel = project_kernel(project)
    branch = _branch_or_fail(repository, branch_id or project.active_branch_id or "")
    if not step_id:
        state = kernel.initial_state(project, materials=repository.load_materials())
        return state, repository, project, kernel
    if all(step.id != step_id for step in branch.steps):
        raise InvalidRequest(f"step {step_id!r} is not part of this branch")
    try:
        path = repository.snapshot_path(branch.id, step_id)
    except KeyError as error:
        raise InvalidRequest(
            "This step has no stored result yet. Run the flow first."
        ) from error
    return _load_state(kernel, path), repository, project, kernel


def _load_state(kernel: Kernel, path: Path) -> Any:
    """A stored state, from memory when it was seen recently."""
    cached = STATE_CACHE.get(path)
    if cached is not None:
        return cached
    state = kernel.load_state(path)
    STATE_CACHE.put(path, state, kernel.state_bytes(state))
    return state


def discard_results(repository: ProjectRepository, project: ProjectDefinition) -> None:
    """Drop every stored result of this project.

    The stored states were computed at a resolution the project no longer
    uses, so they are not results of this project any more and go rather than
    linger as views of a setting nobody chose.
    """
    repository.delete_project_snapshots(project.id)
    cache = DigestCache(repository)
    for branch in repository.list_branches(project.id):
        loaded = repository.load_branch(branch.id)
        cache.forget(branch.id, [step.id for step in loaded.steps])


def apply_grid(
    repository: ProjectRepository,
    project: ProjectDefinition,
    grid: UniformGrid3D,
) -> ProjectDefinition:
    """Change the project grid and drop every result computed on the old one."""
    project.grid = grid_dict(grid)
    repository.save_project(project)
    discard_results(repository, project)
    return project


def apply_resolution(
    repository: ProjectRepository,
    project: ProjectDefinition,
    resolution_um: float,
    resolution_xy_um: float | None = None,
) -> ProjectDefinition:
    """Change a gridless kernel's resolution and drop the old results.

    ``resolution_xy_um`` None means the XY arcs follow the z step.
    """
    project.resolution_um = float(resolution_um)
    project.resolution_xy_um = None if resolution_xy_um is None else float(resolution_xy_um)
    repository.save_project(project)
    discard_results(repository, project)
    return project
