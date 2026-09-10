"""Run the 1T1C flow once and export the true state after every process step."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path

from build_1t1c_demo import make_flow, make_recipes, make_sketches
from process_studio.engine import ProcessEngine
from process_studio.kernel.grid import UniformGrid3D
from process_studio.kernel.material_state import MaterialState
from process_studio.models import ProjectDefinition
from process_studio.simulation_settings import grid_for_target_spacing
from render_1t1c_adaptive import render_step_contact_sheet, render_step_state


def _configuration(spacing_nm: float, solver_order: int, tile_size: int | None) -> dict:
    return {
        "model": "parameterized 2x2 1T1C demo",
        "requested_spacing_nm": spacing_nm,
        "solver_order": solver_order,
        "tile_size": tile_size,
        "array_count": 2,
        "pitch_um": 0.45,
        "section_y_um": 0.225,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--image-dir", type=Path, required=True)
    parser.add_argument("--spacing-nm", type=float, default=6.25)
    parser.add_argument("--solver-order", type=int, choices=(1, 2), default=2)
    parser.add_argument("--tile-size", type=int, default=24)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    args.state_dir.mkdir(parents=True, exist_ok=True)
    args.image_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = args.state_dir / "metadata.json"
    requested = _configuration(args.spacing_nm, args.solver_order, args.tile_size)
    if metadata_path.exists():
        existing = json.loads(metadata_path.read_text(encoding="utf-8"))
        if existing["configuration"] != requested:
            raise RuntimeError("saved checkpoints use different numerical settings")
        if not args.resume:
            raise RuntimeError("output exists; pass --resume to continue it")
        metadata = existing
    else:
        metadata = {"configuration": requested, "steps": []}
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    base = UniformGrid3D(-.7, .7, -.7, .7, -.7, .7, 57, 57, 57)
    grid = grid_for_target_spacing(base, args.spacing_nm)
    project = ProjectDefinition("1T1C step export", dict(grid.__dict__))
    recipes = make_recipes()
    for recipe in recipes:
        if recipe.process_type.value == "etch":
            recipe.parameters["solver_order"] = args.solver_order
            recipe.parameters["tile_shape"] = args.tile_size
    flow = make_flow(recipes)
    engine = ProcessEngine(
        {recipe.id: recipe for recipe in recipes},
        sketches=make_sketches(2, .45),
        logger=lambda message: print(message, flush=True),
    )

    initial_path = args.state_dir / "step-00-initial.npz"
    if initial_path.exists():
        state = MaterialState.load(initial_path)
    else:
        state = MaterialState(grid)
        state.add_material("Si", grid.substrate())
        state.save(initial_path)
    if state.grid != grid:
        raise RuntimeError("saved checkpoint grid does not match the requested grid")

    completed = len(metadata["steps"])
    if completed:
        state = MaterialState.load(args.state_dir / f"step-{completed:02d}.npz")
        print(f"resuming after step {completed:02d}", flush=True)

    for index, step in enumerate(flow.steps, start=1):
        state_path = args.state_dir / f"step-{index:02d}.npz"
        image_path = args.image_dir / f"step-{index:02d}.png"
        if index <= completed:
            if not image_path.exists():
                restored = MaterialState.load(state_path)
                render_step_state(
                    restored, image_path, step_number=index, step_name=step.name
                )
            continue
        started = time.perf_counter()
        state = engine.run_step(state, step, project=project)
        elapsed = time.perf_counter() - started
        state.save(state_path)
        recipe = engine.recipes[step.recipe_id]
        resolved = dict(recipe.parameters)
        resolved.update(step.overrides)
        metadata["steps"].append(
            {
                "number": index,
                "name": step.name,
                "process_type": recipe.process_type.value,
                "tool": recipe.tool,
                "output_material": recipe.output_material,
                "resolved_parameters": resolved,
                "elapsed_seconds": elapsed,
                "state_file": state_path.name,
                "image_file": image_path.name,
                "materials": list(state.priority),
            }
        )
        metadata["actual_grid"] = {
            **asdict(grid),
            "spacing_nm": grid.dx * 1000.0,
            "node_count": grid.nx * grid.ny * grid.nz,
        }
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        print(f"checkpointed step {index:02d} in {elapsed:.3f} s", flush=True)
        render_step_state(state, image_path, step_number=index, step_name=step.name)

    render_step_contact_sheet(
        [args.state_dir / f"step-{index:02d}.npz" for index in range(1, 10)],
        [step.name for step in flow.steps],
        args.image_dir / "all-steps-aa.png",
    )
    print("all 1T1C steps exported", flush=True)


if __name__ == "__main__":
    main()
