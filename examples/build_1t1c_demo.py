"""Build a parameterized 2x2 3D 1T1C-inspired integration demo project."""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import ListedColormap

from process_studio.engine import ProcessEngine
from process_studio.kernel.grid import UniformGrid3D
from process_studio.kernel.material_state import MaterialState
from process_studio.layout.quick_sketch import QuickSketch, SketchShape
from process_studio.libraries import RecipeLibrary
from process_studio.models import (
    FlowBranch,
    MaterialDefinition,
    MaterialResponse,
    ProcessStep,
    ProcessType,
    ProjectDefinition,
    Recipe,
)
from process_studio.storage import ProjectRepository
from process_studio.visualization import downsampled_material_voxels, top_view_labels
from process_studio.worker.workspace import DigestCache, branch_digests


MATERIALS = [
    MaterialDefinition("Si", "Semiconductor", "#705db7", 1.0, id="material-si"),
    MaterialDefinition("Al2O3", "Dielectric", "#f0a52b", 0.92, id="material-al2o3"),
    MaterialDefinition("TiN", "Metal", "#c2a83b", 1.0, id="material-tin"),
    MaterialDefinition("W", "Metal", "#7c8792", 1.0, id="material-w"),
    MaterialDefinition("SiO2", "Dielectric", "#76bfd0", 0.72, id="material-sio2"),
    MaterialDefinition("Photoresist", "Mask", "#d95c76", 0.55, id="material-pr"),
]


def make_sketches(array_count: int, pitch: float) -> dict[str, QuickSketch]:
    common_array = (array_count, array_count, pitch, pitch)
    capacitor = QuickSketch(
        "capacitor_holes",
        [
            SketchShape(
                "circle",
                parameters={"center": (-0.09, 0.0), "radius": 0.12},
                array=common_array,
            )
        ],
    )
    channel = QuickSketch(
        "channel_pillars",
        [
            SketchShape(
                "circle",
                parameters={"center": (0.12, 0.0), "radius": 0.055},
                array=common_array,
            )
        ],
    )
    gate_dielectric = QuickSketch(
        "gate_dielectric",
        [
            SketchShape(
                "circle",
                parameters={"center": (0.12, 0.0), "radius": 0.078},
                array=common_array,
            ),
            SketchShape(
                "circle",
                operation="subtract",
                parameters={"center": (0.12, 0.0), "radius": 0.060},
                array=common_array,
            ),
        ],
    )
    gate = QuickSketch(
        "wordline_gate",
        [
            SketchShape(
                "circle",
                parameters={"center": (0.12, 0.0), "radius": 0.105},
                array=common_array,
            ),
            SketchShape(
                "circle",
                operation="subtract",
                parameters={"center": (0.12, 0.0), "radius": 0.082},
                array=common_array,
            ),
            SketchShape(
                "rectangle",
                parameters={"center": (0.0, 0.0), "size": (1.15, 0.055)},
                array=(1, array_count, 0.0, pitch),
            ),
        ],
    )
    return {
        "capacitor": capacitor,
        "channel": channel,
        "gate_dielectric": gate_dielectric,
        "gate": gate,
    }


def make_recipes() -> list[Recipe]:
    return [
        Recipe(
            "Resist Coat",
            ProcessType.DEPOSIT,
            tool="Spin Coater",
            output_material="Photoresist",
            parameters={"target": 0.15, "rate": 0.15},
            id="demo-resist-coat",
        ),
        Recipe(
            "Resist Develop",
            ProcessType.ETCH,
            tool="Track",
            parameters={"target": 0.2, "directional_fraction": 1.0, "surface_z": 0.15},
            material_responses={
                "Photoresist": MaterialResponse("Photoresist", 0.2),
                "Si": MaterialResponse("Si", 0.0, stop_layer=True),
            },
            id="demo-resist-open",
        ),
        Recipe(
            # No sketch: the patterned resist is what shapes this etch, the way
            # it does on a real tool. The mask edge is a solid, not a rule.
            "Capacitor Trench Etch",
            ProcessType.ETCH,
            tool="ICP-RIE",
            parameters={"target": 0.42, "directional_fraction": 0.92, "surface_z": 0.0},
            material_responses={
                "Si": MaterialResponse("Si", 0.12),
                "Photoresist": MaterialResponse("Photoresist", 0.0, stop_layer=True),
            },
            id="demo-etch",
        ),
        Recipe(
            "Resist Strip",
            ProcessType.ETCH,
            tool="Ash",
            parameters={"target": 0.2, "directional_fraction": 1.0, "surface_z": 0.15},
            material_responses={
                "Photoresist": MaterialResponse("Photoresist", 0.2),
                "Si": MaterialResponse("Si", 0.0, stop_layer=True),
            },
            id="demo-resist-strip",
        ),
        Recipe(
            "Capacitor Al2O3",
            ProcessType.DEPOSIT,
            tool="ALD",
            output_material="Al2O3",
            parameters={"target": 0.025, "rate": 0.002},
            id="demo-cap-oxide",
        ),
        Recipe(
            "Capacitor TiN",
            ProcessType.DEPOSIT,
            tool="ALD",
            output_material="TiN",
            parameters={"target": 0.025, "rate": 0.003},
            id="demo-cap-tin",
        ),
        Recipe(
            "Capacitor W Fill",
            ProcessType.DEPOSIT,
            tool="CVD-W",
            output_material="W",
            parameters={"target": 0.085, "rate": 0.02},
            id="demo-cap-w",
        ),
        Recipe(
            "Capacitor CMP",
            ProcessType.CMP,
            tool="CMP-01",
            parameters={"target_z": 0.0, "materials": "Al2O3,TiN,W"},
            material_responses={"Si": MaterialResponse("Si", 0.0, stop_layer=True)},
            id="demo-cap-cmp",
        ),
        Recipe(
            "ILD SiO2",
            ProcessType.DEPOSIT,
            tool="PECVD",
            output_material="SiO2",
            parameters={"target": 0.10, "rate": 0.04},
            id="demo-ild",
        ),
        Recipe(
            "Si Channel Pillars",
            ProcessType.DEPOSIT,
            tool="Epitaxy",
            output_material="Si",
            parameters={"target": 0.22, "mode": "directional", "base_z": 0.0},
            id="demo-channel",
        ),
        Recipe(
            "Gate Al2O3",
            ProcessType.DEPOSIT,
            tool="ALD",
            output_material="Al2O3",
            parameters={"target": 0.13, "mode": "directional", "base_z": 0.045},
            id="demo-gate-oxide",
        ),
        Recipe(
            "TiN Wordline Gate",
            ProcessType.DEPOSIT,
            tool="PVD",
            output_material="TiN",
            parameters={"target": 0.075, "mode": "directional", "base_z": 0.075},
            id="demo-gate",
        ),
    ]


def make_flow(recipes: list[Recipe]) -> FlowBranch:
    return FlowBranch(
        "main",
        [
            ProcessStep("Coat resist", "demo-resist-coat"),
            ProcessStep("Develop resist", "demo-resist-open", {"sketch_id": "capacitor"}, "quick_sketch"),
            ProcessStep("Etch capacitor trenches", "demo-etch"),
            ProcessStep("Strip resist", "demo-resist-strip"),
            ProcessStep("Deposit capacitor dielectric", "demo-cap-oxide"),
            ProcessStep("Deposit lower electrode", "demo-cap-tin"),
            ProcessStep("Fill capacitor metal", "demo-cap-w"),
            ProcessStep("Planarize capacitor", "demo-cap-cmp"),
            ProcessStep("Deposit interlayer dielectric", "demo-ild"),
            ProcessStep("Grow vertical channels", "demo-channel", {"sketch_id": "channel"}, "quick_sketch"),
            ProcessStep("Pattern gate dielectric", "demo-gate-oxide", {"sketch_id": "gate_dielectric"}, "quick_sketch"),
            ProcessStep("Pattern TiN wordlines", "demo-gate", {"sketch_id": "gate"}, "quick_sketch"),
        ],
        id="demo-main",
    )


def render_demo(state: MaterialState, flow: FlowBranch, output: Path) -> None:
    colors = {material.name: material.color for material in MATERIALS}
    labels, names = state.labels()
    color_list = ["#f7f9fc", *[colors[name] for name in names]]
    figure = plt.figure(figsize=(15, 8.5))
    figure.subplots_adjust(left=0.05, right=0.97, bottom=0.08, top=0.90, wspace=0.22, hspace=0.25)
    figure.suptitle("Process Studio — Parameterized 2×2 3D 1T1C Demo", fontsize=17, fontweight="bold")

    ax_3d = figure.add_subplot(2, 2, 1, projection="3d")
    voxels, _ = downsampled_material_voxels(state, maximum_axis=45)
    for name in names:
        filled = voxels[name].copy()
        # Cut away the front half to reveal trench capacitor layers.
        filled[:, : filled.shape[1] // 2, :] = False
        if filled.any():
            ax_3d.voxels(
                filled,
                facecolors=colors[name],
                edgecolors=(0.06, 0.08, 0.1, 0.10),
                linewidth=0.08,
                alpha=0.90 if name != "SiO2" else 0.45,
            )
    ax_3d.set_title("3D cutaway (interactive in the application)")
    ax_3d.set_xlabel("x")
    ax_3d.set_ylabel("y")
    ax_3d.set_zlabel("z")
    ax_3d.view_init(elev=26, azim=-54)
    ax_3d.set_box_aspect((1.0, 1.0, 0.85))

    ax_section = figure.add_subplot(2, 2, 2)
    # Pass AA through one row of cells rather than through the gap between rows.
    center_y = int(np.argmin(np.abs(state.grid.y - 0.225)))
    ax_section.imshow(
        labels[:, center_y, :] + 1,
        origin="lower",
        extent=(state.grid.x_min, state.grid.x_max, state.grid.z_min, state.grid.z_max),
        cmap=ListedColormap(color_list),
        vmin=0,
        vmax=len(color_list) - 1,
        interpolation="nearest",
        aspect="auto",
    )
    ax_section.set_title("AA section through stacked capacitor/transistor cells")
    ax_section.set_xlabel("x (µm)")
    ax_section.set_ylabel("z (µm)")

    ax_top = figure.add_subplot(2, 2, 3)
    top = top_view_labels(state)
    ax_top.imshow(
        top + 1,
        origin="lower",
        extent=(state.grid.x_min, state.grid.x_max, state.grid.y_min, state.grid.y_max),
        cmap=ListedColormap(color_list),
        vmin=0,
        vmax=len(color_list) - 1,
        interpolation="nearest",
    )
    ax_top.set_title("Top view")
    ax_top.set_xlabel("x (µm)")
    ax_top.set_ylabel("y (µm)")
    ax_top.set_aspect("equal")

    ax_flow = figure.add_subplot(2, 2, 4)
    ax_flow.axis("off")
    ax_flow.set_title("Saved process flow and materials", loc="left")
    flow_text = "\n".join(
        f"{index:02d}   {step.name}" for index, step in enumerate(flow.steps, start=1)
    )
    material_text = "   ".join(f"{name}" for name in names)
    ax_flow.text(0.02, 0.94, flow_text, va="top", family="monospace", fontsize=10.5, linespacing=1.45)
    ax_flow.text(0.02, 0.08, "Materials: " + material_text, fontsize=10, wrap=True)

    figure.text(
        0.5,
        0.025,
        "Visualization-oriented unit array; dimensions and recipes are editable and are not a calibrated foundry model.",
        ha="center",
        fontsize=10,
    )
    figure.savefig(output, dpi=180, bbox_inches="tight")
    plt.close(figure)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("process-studio-demo"))
    parser.add_argument("--array", type=int, default=2)
    parser.add_argument("--pitch", type=float, default=0.45)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    grid = UniformGrid3D(-0.7, 0.7, -0.7, 0.7, -0.7, 0.7, 57, 57, 57)
    state = MaterialState(grid)
    state.add_material("Si", grid.substrate())
    sketches = make_sketches(args.array, args.pitch)
    recipes = make_recipes()
    flow = make_flow(recipes)
    project = ProjectDefinition(
        "2x2 3D 1T1C Demo",
        dict(grid.__dict__),
        active_branch_id=flow.id,
        id="demo-project",
    )
    repository = ProjectRepository(args.output_dir / "process_studio.sqlite3")
    for material in MATERIALS:
        repository.save_material(material)
    for recipe in recipes:
        repository.save_recipe(recipe)
    repository.save_project(project)
    repository.save_branch(project.id, flow)
    for name, sketch in sketches.items():
        sketch.save(args.output_dir / f"{name}.json")
    RecipeLibrary(recipes).export_excel(args.output_dir / "recipe-template.xlsx")

    logs: list[str] = []
    engine = ProcessEngine(
        {recipe.id: recipe for recipe in recipes},
        sketches=sketches,
        repository=repository,
        logger=logs.append,
    )
    final_state = engine.run_branch(state, project, flow)
    # Record what produced each snapshot, or the desktop shell treats the whole
    # flow as stale and asks for a run the script already did.
    cache = DigestCache(repository)
    digests = branch_digests(
        flow, {recipe.id: recipe for recipe in recipes}, sketches, project.grid
    )
    for step, digest in zip(flow.steps, digests):
        cache.store(flow.id, step.id, digest)
    final_state.save(args.output_dir / "final-state.npz")
    render_demo(final_state, flow, args.output_dir / "1t1c-demo.png")
    (args.output_dir / "process.log").write_text("\n".join(logs), encoding="utf-8")
    print(f"saved demo project to {args.output_dir}")
    print(f"materials: {', '.join(final_state.priority)}")
    print(f"steps: {len(flow.steps)}; snapshots: {len(flow.steps)}")


if __name__ == "__main__":
    main()
