"""User-facing project, material, recipe, and process-flow data models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping
from uuid import uuid4


def new_id() -> str:
    return uuid4().hex


#: How the slab kernel shapes films. "detailed" rounds corners with the
#: film's radius and samples in z at the resolution; "simplified" gives
#: square corners from one sample per plane and is much faster; "voxel"
#: keeps the slabs but draws x and y on a grid of cells, which is faster
#: again by orders of magnitude and exact in height, not sideways.
FIDELITIES = ("detailed", "simplified", "voxel")


def result_key(step_id: str, fidelity: str) -> str:
    """The key a step's stored result lives under.

    Results of every fidelity are kept side by side: the detailed one
    under the bare step id (what every workspace already has), the others
    under a suffixed id. Switching the project's fidelity
    then shows what was computed in that mode without a rerun.
    """
    return step_id if fidelity == "detailed" else f"{step_id}~{fidelity}"


def result_keys(step_id: str) -> list[str]:
    """Every key a step's results may live under, for forgetting them all."""
    return [result_key(step_id, fidelity) for fidelity in FIDELITIES]


class ProcessType(str, Enum):
    DEPOSIT = "deposit"
    ETCH = "etch"
    CMP = "cmp"
    NO_GEOMETRY = "no_geometry"
    #: The exposed skin of the listed materials becomes the output material
    #: (slab kernel only): rates and target as for an etch, no swelling.
    OXIDATION = "oxidation"
    #: Turn the wafer over, so the steps that follow act on what was the
    #: backside. Nothing is added or removed; the stack is mirrored.
    FLIP = "flip"


@dataclass
class MaterialDefinition:
    name: str
    category: str = "Other"
    color: str = "#7c83a0"
    opacity: float = 1.0
    id: str = field(default_factory=new_id)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("material name cannot be empty")
        if not 0.0 <= self.opacity <= 1.0:
            raise ValueError("opacity must be between 0 and 1")


@dataclass
class MaterialResponse:
    material: str
    rate_um_per_min: float = 0.0
    stop_layer: bool = False

    def __post_init__(self) -> None:
        if self.rate_um_per_min < 0:
            raise ValueError("material response rate cannot be negative")


@dataclass
class ToolDefinition:
    """A machine or bench a step runs on: a name to pick from, grouped."""

    name: str
    #: A path such as "Etch/Dry"; "/" separates a group from its subgroups.
    group: str = ""
    notes: str = ""
    #: The recipes loaded on this machine, by the names the lab calls them
    #: ("Siva_HZO_300C"). A step records which one it ran with, in its
    #: experiment values; nothing here is a set of parameters, because the
    #: machine's own recipe book is the machine's.
    recipes: list[str] = field(default_factory=list)
    id: str = field(default_factory=new_id)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("tool name cannot be empty")
        self.group = normalize_group(self.group)
        seen: dict[str, None] = {}
        for recipe in self.recipes:
            name = str(recipe).strip()
            if name:
                seen.setdefault(name, None)
        self.recipes = list(seen)


def normalize_group(value: str | None) -> str:
    """Trim a group path: no empty segments, no stray slashes."""
    if not value:
        return ""
    return "/".join(part.strip() for part in str(value).split("/") if part.strip())


def normalize_loop(value: Any) -> dict[str, Any] | None:
    """A step's loop membership, checked: {"id", "name", "repeat", "iteration"}.

    A loop is a block of steps repeated ``repeat`` times. Every iteration
    is a real step in the flow (so each has its own result), tagged with
    the loop it belongs to and which iteration it is (0-based); the
    desktop keeps the iterations identical. None means the step is on its
    own.
    """
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("a step's loop must be an object")
    try:
        repeat = int(value.get("repeat", 1))
        iteration = int(value.get("iteration", 0))
    except (TypeError, ValueError) as error:
        raise ValueError("a loop's repeat and iteration must be whole numbers") from error
    if repeat < 1:
        raise ValueError("a loop repeats at least once")
    if not 0 <= iteration < repeat:
        raise ValueError("a loop iteration must be between 0 and repeat - 1")
    loop_id = str(value.get("id") or "").strip()
    if not loop_id:
        raise ValueError("a loop needs an id")
    return {
        "id": loop_id,
        "name": str(value.get("name") or "Loop"),
        "repeat": repeat,
        "iteration": iteration,
    }


@dataclass
class Recipe:
    """A saved step to start another step from -- a **step template**.

    The interface calls it that, because "recipe" in a lab means the recipe
    loaded on a machine ("Siva_HZO_300C"), which lives on the tool
    (``ToolDefinition.recipes``) and is recorded in a step's experiment
    values. The class keeps its name so stored workspaces, the RPC document
    and the flow file do not have to be rewritten for a word.
    """

    name: str
    process_type: ProcessType
    tool: str = ""
    output_material: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    #: What the tool is actually set to, when that is not the same as what
    #: the kernel is asked to build. ``None`` means the two are the same --
    #: the usual case, and the one that needs nothing maintained. Nothing
    #: here is ever read by a kernel; see ``experiment_values``.
    experiment_parameters: dict[str, Any] | None = None
    material_responses: dict[str, MaterialResponse] = field(default_factory=dict)
    #: Where the recipe sits in the library below its process type, e.g.
    #: "ALD/Oxides"; empty means directly under the type.
    group: str = ""
    id: str = field(default_factory=new_id)

    def __post_init__(self) -> None:
        self.group = normalize_group(self.group)

    def resolved_parameters(self, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        values = dict(self.parameters)
        if overrides:
            values.update(overrides)
        return values

    def experiment_values(self) -> dict[str, Any]:
        """The experiment set, which is the simulation set until it differs."""
        return dict(self.parameters if self.experiment_parameters is None else self.experiment_parameters)


@dataclass
class ProcessStep:
    name: str
    # recipe_id/overrides are retained only to open projects made before the
    # step-owned process definition was introduced. New clients leave them empty.
    recipe_id: str | None = None
    overrides: dict[str, Any] = field(default_factory=dict)
    mask_source: str = "none"
    layer: int | None = None
    datatype: int | None = None
    keep: str = "inside"
    enabled: bool = True
    id: str = field(default_factory=new_id)
    process_type: ProcessType | None = None
    tool: str = ""
    output_material: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    #: What the tool is set to for this step, when that differs from what
    #: the kernel is asked to build. ``None`` means the two are the same.
    #: A kernel never reads it and a step's digest never covers it, so
    #: writing down what the machine did cannot make a result stale.
    experiment_parameters: dict[str, Any] | None = None
    material_responses: dict[str, MaterialResponse] = field(default_factory=dict)
    #: The repeated block this step belongs to, see ``normalize_loop``. It is
    #: bookkeeping for the editor: a result never depends on it.
    loop: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.process_type is not None and not isinstance(self.process_type, ProcessType):
            self.process_type = ProcessType(self.process_type)
        self.loop = normalize_loop(self.loop)
        self.material_responses = {
            name: (
                response
                if isinstance(response, MaterialResponse)
                else MaterialResponse(**response)
            )
            for name, response in self.material_responses.items()
        }
        if self.mask_source not in {"none", "quick_sketch", "gds"}:
            raise ValueError("mask_source must be none, quick_sketch, or gds")
        if self.keep not in {"inside", "outside"}:
            raise ValueError("keep must be inside or outside")

    @classmethod
    def from_recipe(
        cls,
        name: str,
        recipe: Recipe,
        *,
        parameters: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> "ProcessStep":
        """Copy a library recipe into an independent process step."""
        return cls(
            name,
            process_type=recipe.process_type,
            tool=recipe.tool,
            output_material=recipe.output_material,
            parameters=dict(recipe.parameters if parameters is None else parameters),
            experiment_parameters=(
                None if recipe.experiment_parameters is None else dict(recipe.experiment_parameters)
            ),
            material_responses={
                material: MaterialResponse(
                    response.material,
                    response.rate_um_per_min,
                    response.stop_layer,
                )
                for material, response in recipe.material_responses.items()
            },
            **kwargs,
        )

    def effective_recipe(self, recipes: Mapping[str, Recipe]) -> Recipe:
        """Return this step's private definition, migrating legacy references lazily."""
        if self.process_type is not None:
            parameters = dict(self.parameters)
            parameters.update(self.overrides)
            return Recipe(
                self.name,
                self.process_type,
                tool=self.tool,
                output_material=self.output_material,
                parameters=parameters,
                experiment_parameters=(
                    None if self.experiment_parameters is None else dict(self.experiment_parameters)
                ),
                material_responses=dict(self.material_responses),
                id=f"step-definition-{self.id}",
            )
        if not self.recipe_id or self.recipe_id not in recipes:
            raise KeyError(f"step {self.name!r} has no process definition")
        source = recipes[self.recipe_id]
        return Recipe(
            self.name,
            source.process_type,
            tool=source.tool,
            output_material=source.output_material,
            parameters=source.resolved_parameters(self.overrides),
            material_responses=dict(source.material_responses),
            id=f"step-definition-{self.id}",
        )

    def experiment_values(self) -> dict[str, Any]:
        """The experiment set, which is the simulation set until it differs."""
        return dict(self.parameters if self.experiment_parameters is None else self.experiment_parameters)

    def detach_from_library(self, recipes: Mapping[str, Recipe]) -> bool:
        """Materialize a legacy recipe reference into this step once."""
        if self.process_type is not None:
            return False
        definition = self.effective_recipe(recipes)
        self.recipe_id = None
        self.overrides = {}
        self.process_type = definition.process_type
        self.tool = definition.tool
        self.output_material = definition.output_material
        self.parameters = dict(definition.parameters)
        self.material_responses = dict(definition.material_responses)
        return True


@dataclass
class FlowBranch:
    name: str
    steps: list[ProcessStep] = field(default_factory=list)
    parent_branch_id: str | None = None
    parent_step_id: str | None = None
    id: str = field(default_factory=new_id)


@dataclass
class ProjectDefinition:
    name: str
    grid: dict[str, Any]
    gds_path: str | None = None
    active_branch_id: str | None = None
    id: str = field(default_factory=new_id)
    #: Which simulation kernel this project is built on. Recorded when the
    #: project is created and never changed afterwards, so a project made on
    #: a kernel this build no longer has is refused instead of run on another
    #: one. There is one kernel now; the field stays because the refusal does.
    kernel: str = "slab"
    #: Length the kernel resolves geometry at when it is not the grid: the
    #: slab kernel's conformal-deposition resolution. None means its default.
    resolution_um: float | None = None
    #: The XY arc sagitta of the slab kernel when it differs from the z step.
    #: None means the same as ``resolution_um``.
    resolution_xy_um: float | None = None
    #: Named AA–BB section lines, each {"id", "name", "start": [x, y], "end": [x, y]}
    #: in micrometres. They are the user's bookmarks into the geometry.
    section_lines: list[dict[str, Any]] = field(default_factory=list)
    #: How films are shaped: "simplified" (square corners, one sample per
    #: plane, much faster), "detailed" (rounded, sampled at the
    #: resolution) or "voxel" (square films on a grid of cells, a quick look
    #: at a whole flow). Results of each are stored side by side, so switching
    #: back shows what was computed before without a rerun. New projects
    #: start simplified: it is what this is used for, and a detailed film is
    #: a deliberate look at one step rather than how a flow is built.
    fidelity: str = "simplified"


def dataclass_dict(value: Any) -> dict[str, Any]:
    result = asdict(value)
    if isinstance(value, Recipe):
        result["process_type"] = value.process_type.value
    return result
