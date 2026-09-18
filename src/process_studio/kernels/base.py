"""What every simulation kernel must offer the workspace.

A project names one kernel when it is created and keeps it for life. The two
kernels do not share a state representation: one carries level-set fields on a
uniform grid, the other exact slab polygons, and neither can read the other's
snapshots. Everything above this module works through the interface here, so
the runner, the views and the RPC surface never branch on the kernel id.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from ..layout.quick_sketch import QuickSketch
from ..models import MaterialDefinition, ProcessStep, ProjectDefinition, Recipe


@dataclass(frozen=True)
class KernelInfo:
    """What a client needs to describe and drive one kernel."""

    id: str
    name: str
    version: str
    summary: str
    #: Process types the kernel can execute.
    process_types: tuple[str, ...]
    #: Mask sources the kernel understands.
    mask_sources: tuple[str, ...]
    #: Deposition modes accepted in a step's ``mode`` parameter.
    deposition_modes: tuple[str, ...]
    #: Values of ``directional_fraction`` the kernel accepts, or () for any.
    directional_fractions: tuple[float, ...]
    #: Whether the 3D view can ask for surfaces.
    surfaces: bool
    #: What the project's spacing setting means for this kernel.
    spacing_role: str
    spacing_presets_nm: tuple[float, ...]
    #: Node ceiling the spacing must respect, or None when there is no field.
    maximum_nodes: int | None
    #: Extension of a stored state file.
    snapshot_suffix: str

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "summary": self.summary,
            "processTypes": list(self.process_types),
            "maskSources": list(self.mask_sources),
            "depositionModes": list(self.deposition_modes),
            "directionalFractions": list(self.directional_fractions),
            "surfaces": self.surfaces,
            "spacingRole": self.spacing_role,
            "spacingPresetsNm": list(self.spacing_presets_nm),
            "maximumNodes": self.maximum_nodes,
        }


class State(Protocol):
    """The only thing the workspace does with a state is store it."""

    def save(self, path: Path) -> None: ...


class Kernel(Protocol):
    """One simulation core, wrapped for the workspace.

    Implementations own their state type end to end: they create it, advance
    it one step at a time, write and read it, and render every view of it.
    """

    info: KernelInfo

    def initial_state(
        self,
        project: ProjectDefinition,
        *,
        materials: Sequence[MaterialDefinition] = (),
    ) -> Any:
        """The wafer a run starts from, before the first step."""

    def run_step(
        self,
        state: Any,
        step: ProcessStep,
        *,
        project: ProjectDefinition,
        recipes: Mapping[str, Recipe],
        sketches: Mapping[str, QuickSketch],
        logger: Callable[[str], None],
        materials: Sequence[MaterialDefinition] = (),
        should_cancel: Callable[[], bool] | None = None,
    ) -> Any:
        """Advance one step, returning a new state and never mutating the old.

        ``should_cancel`` is asked, as often as the kernel can afford to,
        whether the caller has given up on this step; a kernel that honours
        it raises ``Cancelled`` rather than running to the end. A step that
        stops this way stores nothing, so the run resumes from the step
        before it.
        """

    def load_state(self, path: Path) -> Any:
        """Read back what ``state.save`` wrote."""

    def state_materials(self, state: Any) -> list[str]:
        """Materials present in the state, in the kernel's own order."""

    def state_bytes(self, state: Any) -> int:
        """Roughly what the state costs to keep in memory, for the cache budget."""

    def warm_views(self, state: Any, buried: bool = False) -> None:
        """Prepare whatever the views of this state need that is slow to make.

        Called in the background once a run has stored the state, so the
        first look at a step does not pay for it. A kernel whose views are
        cheap does nothing here.

        ``buried`` asks for the heavier mesh that a peek behind a hidden
        material needs, which is warmed only after every step has the one
        the 3D view opens with.
        """

    def surfaces(
        self,
        state: Any,
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
        """Triangles for the 3D view, one entry per material."""

    def section(
        self,
        state: Any,
        colors: Mapping[str, str],
        *,
        project: ProjectDefinition,
        axis: str = "y",
        position: float | None = None,
        interpolation: int = 1,
        line: tuple[tuple[float, float], tuple[float, float]] | None = None,
    ) -> dict[str, Any]:
        """A vertical cut, as a PNG plus the frame it was drawn in.

        ``line`` is a pair of (x, y) points in micrometres; when given, the
        cut runs from the first to the second and ``axis``/``position`` are
        ignored. The picture's horizontal axis is then the distance along it.
        """

    def top_view(
        self,
        state: Any,
        colors: Mapping[str, str],
        *,
        project: ProjectDefinition,
        shading: str = "material",
        hidden: Sequence[str] = (),
    ) -> dict[str, Any]:
        """The view from above, as a PNG plus the frame it was drawn in.

        ``shading`` is "material" (the topmost material's colour) or
        "height" (one palette colour per distinct surface height, listed
        under ``levels`` for a legend). ``hidden`` names materials to look
        through: they are neither drawn nor allowed to cover what is under
        them, so the view is of the structure without them.
        """
