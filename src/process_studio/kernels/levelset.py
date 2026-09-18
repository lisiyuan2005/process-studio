"""The level-set kernel: signed distance fields on a uniform grid.

This is Process Studio's original core, wrapped in the kernel interface. The
behaviour is unchanged: the engine, the material state and the renderers are
the same code the project has always run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..engine import ProcessEngine
from ..kernel.grid import UniformGrid3D
from ..kernel.material_state import MaterialState
from ..layout.quick_sketch import QuickSketch
from ..models import MaterialDefinition, ProcessStep, ProjectDefinition, Recipe
from ..simulation_settings import MAXIMUM_NODES, SPACING_PRESETS_NM
from ..worker.render import (
    MESHES_AVAILABLE,
    line_section_image,
    material_surfaces,
    section_image,
    top_view_image,
)
from .base import KernelInfo

SUBSTRATE_MATERIAL = "Si"


class LevelSetKernel:
    """Level-set process simulation on the project's uniform grid."""

    info = KernelInfo(
        id="levelset",
        name="Level set",
        version="0.5.0",
        summary=(
            "Signed distance fields on a uniform grid. Directional, isotropic "
            "and mixed etching, conformal and patterned deposition, ideal CMP. "
            "Accuracy follows the grid spacing."
        ),
        process_types=("deposit", "etch", "cmp", "no_geometry"),
        mask_sources=("none", "quick_sketch", "gds"),
        deposition_modes=("conformal", "directional", "evaporation", "fill"),
        directional_fractions=(),
        surfaces=MESHES_AVAILABLE,
        spacing_role="grid",
        spacing_presets_nm=SPACING_PRESETS_NM,
        maximum_nodes=MAXIMUM_NODES,
        snapshot_suffix=".npz",
    )

    def initial_state(
        self,
        project: ProjectDefinition,
        *,
        materials: Sequence[MaterialDefinition] = (),
    ) -> MaterialState:
        """The starting wafer every run begins from.

        Runs always replay from here rather than from a stored final state, so
        a refined grid never inherits an interpolated coarse result.
        """
        grid = UniformGrid3D(**project.grid)
        state = MaterialState(grid)
        state.add_material(SUBSTRATE_MATERIAL, grid.substrate())
        return state

    def run_step(
        self,
        state: MaterialState,
        step: ProcessStep,
        *,
        project: ProjectDefinition,
        recipes: Mapping[str, Recipe],
        sketches: Mapping[str, QuickSketch],
        logger: Callable[[str], None],
        materials: Sequence[MaterialDefinition] = (),
        should_cancel: Callable[[], bool] | None = None,
    ) -> MaterialState:
        engine = ProcessEngine(recipes, sketches=sketches, logger=logger)
        return engine.run_step(state, step, project=project)

    def load_state(self, path: Path) -> MaterialState:
        return MaterialState.load(path)

    def state_materials(self, state: MaterialState) -> list[str]:
        return list(state.priority)

    def warm_views(self, state: MaterialState, buried: bool = False) -> None:
        """Marching cubes is fast enough to run when asked."""

    def state_bytes(self, state: MaterialState) -> int:
        return sum(int(field.nbytes) for field in state.fields.values())

    def surfaces(
        self,
        state: MaterialState,
        *,
        project: ProjectDefinition,
        interpolation: int = 1,
        materials: Sequence[str] | None = None,
        #: Which triangulator built the mesh; only the slab kernel
        #: has a choice, and the level-set kernel marches cubes.
        triangulation: str | None = None,
        #: Include the faces that lie against another material.
        #: Only the slab kernel can leave them out.
        buried: bool = False,
    ) -> dict[str, Any]:
        return material_surfaces(
            state,
            interpolation=interpolation,
            materials=None if materials is None else list(materials),
        )

    def section(
        self,
        state: MaterialState,
        colors: Mapping[str, str],
        *,
        project: ProjectDefinition,
        axis: str = "y",
        position: float | None = None,
        interpolation: int = 1,
        line: tuple[tuple[float, float], tuple[float, float]] | None = None,
    ) -> dict[str, Any]:
        if line is not None:
            return line_section_image(
                state, colors, start=line[0], end=line[1], interpolation=interpolation
            )
        return section_image(
            state, colors, axis=axis, position=position, interpolation=interpolation
        )

    def top_view(
        self,
        state: MaterialState,
        colors: Mapping[str, str],
        *,
        project: ProjectDefinition,
        shading: str = "material",
        hidden: Sequence[str] = (),
    ) -> dict[str, Any]:
        return top_view_image(state, colors, shading=shading, hidden=hidden)
