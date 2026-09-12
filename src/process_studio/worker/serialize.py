"""Translation between the kernel dataclasses and the client's JSON shape.

The client speaks camelCase; the kernel keeps its snake_case dataclasses. Every
conversion lives here so a field rename cannot drift between the two sides.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..kernel.grid import UniformGrid3D
from ..kernels import default_kernel
from ..models import (
    FIDELITIES,
    FlowBranch,
    MaterialDefinition,
    MaterialResponse,
    ProcessStep,
    ProcessType,
    ProjectDefinition,
    Recipe,
    ToolDefinition,
    new_id,
)
from .errors import InvalidRequest


def grid_to_json(grid: UniformGrid3D) -> dict[str, Any]:
    return {
        "xMin": grid.x_min,
        "xMax": grid.x_max,
        "yMin": grid.y_min,
        "yMax": grid.y_max,
        "zMin": grid.z_min,
        "zMax": grid.z_max,
        "nx": grid.nx,
        "ny": grid.ny,
        "nz": grid.nz,
        "spacingUm": grid.dx,
        "nodeCount": grid.nx * grid.ny * grid.nz,
    }


def grid_from_json(payload: Mapping[str, Any]) -> UniformGrid3D:
    try:
        return UniformGrid3D(
            float(payload["xMin"]),
            float(payload["xMax"]),
            float(payload["yMin"]),
            float(payload["yMax"]),
            float(payload["zMin"]),
            float(payload["zMax"]),
            int(payload["nx"]),
            int(payload["ny"]),
            int(payload["nz"]),
        )
    except KeyError as error:
        raise InvalidRequest(f"grid is missing {error.args[0]}") from error
    except (TypeError, ValueError) as error:
        raise InvalidRequest(f"grid is invalid: {error}") from error


def grid_dict(grid: UniformGrid3D) -> dict[str, Any]:
    """Return the storage-side grid mapping used by ProjectDefinition."""
    return {
        "x_min": grid.x_min,
        "x_max": grid.x_max,
        "y_min": grid.y_min,
        "y_max": grid.y_max,
        "z_min": grid.z_min,
        "z_max": grid.z_max,
        "nx": grid.nx,
        "ny": grid.ny,
        "nz": grid.nz,
    }


def material_to_json(material: MaterialDefinition) -> dict[str, Any]:
    return {
        "id": material.id,
        "name": material.name,
        "category": material.category,
        "color": material.color,
        "opacity": material.opacity,
    }


def material_from_json(payload: Mapping[str, Any]) -> MaterialDefinition:
    try:
        return MaterialDefinition(
            str(payload["name"]),
            str(payload.get("category", "Other")),
            str(payload.get("color", "#7c83a0")),
            float(payload.get("opacity", 1.0)),
            id=str(payload.get("id") or new_id()),
        )
    except KeyError as error:
        raise InvalidRequest(f"material is missing {error.args[0]}") from error
    except (TypeError, ValueError) as error:
        raise InvalidRequest(f"material is invalid: {error}") from error


def tool_to_json(tool: ToolDefinition) -> dict[str, Any]:
    return {"id": tool.id, "name": tool.name, "group": tool.group, "notes": tool.notes}


def tool_from_json(payload: Mapping[str, Any]) -> ToolDefinition:
    try:
        return ToolDefinition(
            str(payload["name"]).strip(),
            str(payload.get("group") or ""),
            str(payload.get("notes") or ""),
            id=str(payload.get("id") or new_id()),
        )
    except KeyError as error:
        raise InvalidRequest(f"tool is missing {error.args[0]}") from error
    except (TypeError, ValueError) as error:
        raise InvalidRequest(f"tool is invalid: {error}") from error


def response_to_json(response: MaterialResponse) -> dict[str, Any]:
    return {
        "material": response.material,
        "rateUmPerMin": response.rate_um_per_min,
        "stopLayer": response.stop_layer,
    }


def response_from_json(payload: Mapping[str, Any]) -> MaterialResponse:
    try:
        return MaterialResponse(
            str(payload["material"]),
            float(payload.get("rateUmPerMin", 0.0)),
            bool(payload.get("stopLayer", False)),
        )
    except KeyError as error:
        raise InvalidRequest(f"material response is missing {error.args[0]}") from error
    except (TypeError, ValueError) as error:
        raise InvalidRequest(f"material response is invalid: {error}") from error


def recipe_to_json(recipe: Recipe) -> dict[str, Any]:
    return {
        "id": recipe.id,
        "name": recipe.name,
        "processType": recipe.process_type.value,
        "tool": recipe.tool,
        "group": recipe.group,
        "outputMaterial": recipe.output_material,
        "parameters": dict(recipe.parameters),
        "materialResponses": {
            name: response_to_json(response)
            for name, response in recipe.material_responses.items()
        },
    }


def recipe_from_json(payload: Mapping[str, Any]) -> Recipe:
    try:
        process_type = ProcessType(str(payload["processType"]))
    except KeyError as error:
        raise InvalidRequest("recipe is missing processType") from error
    except ValueError as error:
        raise InvalidRequest(f"unknown process type: {payload.get('processType')!r}") from error
    parameters = payload.get("parameters", {})
    if not isinstance(parameters, Mapping):
        raise InvalidRequest("recipe parameters must be an object")
    responses = payload.get("materialResponses", {})
    if not isinstance(responses, Mapping):
        raise InvalidRequest("recipe materialResponses must be an object")
    name = str(payload.get("name", "")).strip()
    if not name:
        raise InvalidRequest("recipe name cannot be empty")
    return Recipe(
        name,
        process_type,
        tool=str(payload.get("tool", "")),
        output_material=(
            None if payload.get("outputMaterial") in (None, "") else str(payload["outputMaterial"])
        ),
        parameters=dict(parameters),
        material_responses={
            str(key): response_from_json(value) for key, value in responses.items()
        },
        group=str(payload.get("group") or ""),
        id=str(payload.get("id") or new_id()),
    )


def step_to_json(step: ProcessStep, recipes: Mapping[str, Recipe]) -> dict[str, Any]:
    definition = step.effective_recipe(recipes)
    return {
        "id": step.id,
        "name": step.name,
        "processType": definition.process_type.value,
        "tool": definition.tool,
        "outputMaterial": definition.output_material,
        "parameters": dict(definition.parameters),
        "materialResponses": {
            name: response_to_json(response)
            for name, response in definition.material_responses.items()
        },
        "maskSource": step.mask_source,
        "layer": step.layer,
        "datatype": step.datatype,
        "keep": step.keep,
        "enabled": step.enabled,
    }


def step_from_json(payload: Mapping[str, Any]) -> ProcessStep:
    layer = payload.get("layer")
    datatype = payload.get("datatype")
    # Legacy clients send recipeId + overrides. Keep accepting that shape so an
    # existing workspace can be opened and migrated by the next save.
    if "processType" not in payload:
        overrides = payload.get("overrides", {})
        if not isinstance(overrides, Mapping):
            raise InvalidRequest("step overrides must be an object")
        try:
            return ProcessStep(
                str(payload.get("name", "")).strip() or "Step",
                str(payload["recipeId"]),
                dict(overrides),
                str(payload.get("maskSource", "none")),
                None if layer is None else int(layer),
                None if datatype is None else int(datatype),
                str(payload.get("keep", "inside")),
                bool(payload.get("enabled", True)),
                id=str(payload.get("id") or new_id()),
            )
        except KeyError as error:
            raise InvalidRequest(f"step is missing {error.args[0]}") from error
        except (TypeError, ValueError) as error:
            raise InvalidRequest(f"step is invalid: {error}") from error

    parameters = payload.get("parameters", {})
    responses = payload.get("materialResponses", {})
    if not isinstance(parameters, Mapping):
        raise InvalidRequest("step parameters must be an object")
    if not isinstance(responses, Mapping):
        raise InvalidRequest("step materialResponses must be an object")
    try:
        return ProcessStep(
            str(payload.get("name", "")).strip() or "Step",
            mask_source=str(payload.get("maskSource", "none")),
            layer=None if layer is None else int(layer),
            datatype=None if datatype is None else int(datatype),
            keep=str(payload.get("keep", "inside")),
            enabled=bool(payload.get("enabled", True)),
            id=str(payload.get("id") or new_id()),
            process_type=ProcessType(str(payload["processType"])),
            tool=str(payload.get("tool", "")),
            output_material=(
                None
                if payload.get("outputMaterial") in (None, "")
                else str(payload["outputMaterial"])
            ),
            parameters=dict(parameters),
            material_responses={
                str(name): response_from_json(response)
                for name, response in responses.items()
            },
        )
    except KeyError as error:
        raise InvalidRequest(f"step is missing {error.args[0]}") from error
    except (TypeError, ValueError) as error:
        raise InvalidRequest(f"step is invalid: {error}") from error


def branch_to_json(branch: FlowBranch, recipes: Mapping[str, Recipe]) -> dict[str, Any]:
    return {
        "id": branch.id,
        "name": branch.name,
        "parentBranchId": branch.parent_branch_id,
        "parentStepId": branch.parent_step_id,
        "steps": [step_to_json(step, recipes) for step in branch.steps],
    }


def branch_from_json(payload: Mapping[str, Any]) -> FlowBranch:
    steps = payload.get("steps", [])
    if not isinstance(steps, list):
        raise InvalidRequest("branch steps must be a list")
    return FlowBranch(
        str(payload.get("name", "")).strip() or "main",
        [step_from_json(step) for step in steps],
        payload.get("parentBranchId"),
        payload.get("parentStepId"),
        id=str(payload.get("id") or new_id()),
    )


def project_to_json(project: ProjectDefinition) -> dict[str, Any]:
    return {
        "id": project.id,
        "name": project.name,
        "grid": grid_to_json(UniformGrid3D(**project.grid)),
        "gdsPath": project.gds_path,
        "activeBranchId": project.active_branch_id,
        "kernel": project.kernel,
        "resolutionUm": project.resolution_um,
        "resolutionXyUm": project.resolution_xy_um,
        "sectionLines": [dict(line) for line in project.section_lines],
        "fidelity": project.fidelity,
    }


def section_lines_from_json(payload: Any) -> list[dict[str, Any]]:
    """Named AA–BB lines, checked: an id, a name, two distinct points in µm."""
    if payload is None:
        return []
    if not isinstance(payload, list):
        raise InvalidRequest("sectionLines must be a list")
    lines: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, Mapping):
            raise InvalidRequest(f"section line {index} must be an object")
        line_id = str(item.get("id") or new_id())
        if line_id in seen:
            raise InvalidRequest(f"section line id {line_id!r} appears twice")
        seen.add(line_id)
        points = []
        for key in ("start", "end"):
            point = item.get(key)
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise InvalidRequest(f"section line {index}: {key} must be [x, y] in micrometres")
            try:
                points.append([float(point[0]), float(point[1])])
            except (TypeError, ValueError) as error:
                raise InvalidRequest(f"section line {index}: {key} must be two numbers") from error
        if points[0] == points[1]:
            raise InvalidRequest(f"section line {index}: A and B must be different points")
        lines.append({
            "id": line_id,
            "name": str(item.get("name") or f"Line {index}").strip() or f"Line {index}",
            "start": points[0],
            "end": points[1],
        })
    return lines


def project_from_json(payload: Mapping[str, Any]) -> ProjectDefinition:
    grid = payload.get("grid")
    if not isinstance(grid, Mapping):
        raise InvalidRequest("project requires a grid object")
    name = str(payload.get("name", "")).strip()
    if not name:
        raise InvalidRequest("project name cannot be empty")
    project_id = str(payload.get("id") or "")
    if not project_id:
        raise InvalidRequest("project id cannot be empty")
    resolution = payload.get("resolutionUm")
    resolution_xy = payload.get("resolutionXyUm")
    fidelity = str(payload.get("fidelity") or "detailed")
    if fidelity not in FIDELITIES:
        raise InvalidRequest(f"project fidelity must be one of {FIDELITIES}, got {fidelity!r}.")
    return ProjectDefinition(
        name,
        grid_dict(grid_from_json(grid)),
        None if payload.get("gdsPath") in (None, "") else str(payload["gdsPath"]),
        payload.get("activeBranchId"),
        id=project_id,
        kernel=str(payload.get("kernel") or default_kernel()),
        resolution_um=None if resolution in (None, "") else float(resolution),
        resolution_xy_um=None if resolution_xy in (None, "") else float(resolution_xy),
        section_lines=section_lines_from_json(payload.get("sectionLines")),
        fidelity=fidelity,
    )
