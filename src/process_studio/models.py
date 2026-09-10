"""User-facing project, material, recipe, and process-flow data models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4


def new_id() -> str:
    return uuid4().hex


class ProcessType(str, Enum):
    DEPOSIT = "deposit"
    ETCH = "etch"
    CMP = "cmp"
    NO_GEOMETRY = "no_geometry"


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
class Recipe:
    name: str
    process_type: ProcessType
    tool: str = ""
    output_material: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    material_responses: dict[str, MaterialResponse] = field(default_factory=dict)
    id: str = field(default_factory=new_id)

    def resolved_parameters(self, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        values = dict(self.parameters)
        if overrides:
            values.update(overrides)
        return values


@dataclass
class ProcessStep:
    name: str
    recipe_id: str
    overrides: dict[str, Any] = field(default_factory=dict)
    mask_source: str = "none"
    layer: int | None = None
    datatype: int | None = None
    keep: str = "inside"
    enabled: bool = True
    id: str = field(default_factory=new_id)

    def __post_init__(self) -> None:
        if self.mask_source not in {"none", "quick_sketch", "gds"}:
            raise ValueError("mask_source must be none, quick_sketch, or gds")
        if self.keep not in {"inside", "outside"}:
            raise ValueError("keep must be inside or outside")


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


def dataclass_dict(value: Any) -> dict[str, Any]:
    result = asdict(value)
    if isinstance(value, Recipe):
        result["process_type"] = value.process_type.value
    return result
