"""A whole process flow as one file, to write out and to apply.

The file is what a person would write by hand: the project window, the
materials, the sketches and the ordered steps, in JSON, YAML or TOML. Applying
it to a workspace replaces the flow; steps keep their identity by position,
so a file that changes one parameter re-runs only from that step, exactly as
an edit in the desktop would.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from ..models import new_id
from ..worker.errors import InvalidRequest
from .session import Session

PROCESS_TYPES = ("deposit", "etch", "cmp", "no_geometry")


# -- reading and writing the file ------------------------------------------


def read_flow(path: Path) -> dict[str, Any]:
    return parse_flow(path.read_text(encoding="utf-8"), path.suffix.lower(), path.name)


def parse_flow(text: str, suffix: str, name: str = "the flow") -> dict[str, Any]:
    """A flow document from its text; ``suffix`` says which syntax, "" guesses.

    Text with no suffix (pasted into the desktop's console, or piped in as
    ``flow apply -``) is JSON when it opens with a brace and YAML otherwise.
    """
    if not suffix:
        suffix = ".json" if text.lstrip().startswith("{") else ".yaml"
    if suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as error:  # pragma: no cover - depends on the install
            raise InvalidRequest(
                "Reading YAML needs the pyyaml package; install it or use JSON."
            ) from error
        loaded = yaml.safe_load(text)
    elif suffix == ".toml":
        import tomllib

        loaded = tomllib.loads(text)
    elif suffix == ".json":
        loaded = json.loads(text)
    else:
        raise InvalidRequest(f"{name}: a flow file is .json, .yaml, .yml or .toml.")
    if not isinstance(loaded, Mapping):
        raise InvalidRequest(f"{name}: the flow file must be an object at the top level.")
    return dict(loaded)


def write_flow(path: Path, flow: Mapping[str, Any]) -> None:
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as error:  # pragma: no cover - depends on the install
            raise InvalidRequest(
                "Writing YAML needs the pyyaml package; install it or use .json."
            ) from error
        text = yaml.safe_dump(dict(flow), sort_keys=False, allow_unicode=True)
    elif suffix == ".json":
        text = json.dumps(flow, indent=2, ensure_ascii=False) + "\n"
    else:
        raise InvalidRequest(f"{path.name}: a flow is written as .json or .yaml.")
    path.write_text(text, encoding="utf-8")


# -- between the document and the file -------------------------------------


def flow_from_document(document: Mapping[str, Any]) -> dict[str, Any]:
    project = document["project"]
    grid = project["grid"]
    flow: dict[str, Any] = {
        "name": project["name"],
        "kernel": project["kernel"],
        "window": {
            "x": [grid["xMin"], grid["xMax"]],
            "y": [grid["yMin"], grid["yMax"]],
            "z": [grid["zMin"], grid["zMax"]],
        },
    }
    if project.get("resolutionUm") is not None:
        flow["resolution_nm"] = project["resolutionUm"] * 1000.0
        if project.get("resolutionXyUm") is not None:
            flow["resolution_xy_nm"] = project["resolutionXyUm"] * 1000.0
    else:
        flow["spacing_nm"] = grid["spacingUm"] * 1000.0
    flow["materials"] = [
        {
            "name": material["name"],
            "category": material["category"],
            "color": material["color"],
            "opacity": material["opacity"],
        }
        for material in document["materials"]
    ]
    lines = document["project"].get("sectionLines", [])
    if lines:
        flow["section_lines"] = [
            {"name": line["name"], "start": list(line["start"]), "end": list(line["end"])} for line in lines
        ]
    flow["sketches"] = {
        sketch["id"]: {"name": sketch["name"], "shapes": sketch["shapes"]}
        for sketch in document.get("sketches", [])
    }
    flow["steps"] = [spec_from_step(step) for step in Session.steps(document)]
    return flow


def spec_from_step(step: Mapping[str, Any]) -> dict[str, Any]:
    spec: dict[str, Any] = {"name": step["name"], "type": step["processType"]}
    if step.get("tool"):
        spec["tool"] = step["tool"]
    if step.get("outputMaterial"):
        spec["material"] = step["outputMaterial"]
    parameters = dict(step.get("parameters", {}))
    sketch_id = parameters.pop("sketch_id", None)
    if parameters:
        spec["parameters"] = parameters
    source = step.get("maskSource", "none")
    if source == "quick_sketch":
        spec["mask"] = f"sketch:{sketch_id or 'default'}"
    elif source == "gds":
        spec["mask"] = f"gds:{step.get('layer')}/{step.get('datatype') or 0}"
    if source != "none" and step.get("keep") == "outside":
        spec["keep"] = "outside"
    responses = step.get("materialResponses", {})
    rates = {
        name: response["rateUmPerMin"]
        for name, response in responses.items()
        if not response.get("stopLayer")
    }
    stops = [name for name, response in responses.items() if response.get("stopLayer")]
    if rates:
        spec["rates"] = rates
    if stops:
        spec["stop"] = stops
    if not step.get("enabled", True):
        spec["enabled"] = False
    return spec


def parse_mask(text: str | None) -> dict[str, Any]:
    """Step fields for a mask written as none, sketch:ID or gds:LAYER/DATATYPE."""
    if text is None or text.strip().lower() in ("", "none"):
        return {"maskSource": "none", "layer": None, "datatype": None}
    kind, _, rest = text.strip().partition(":")
    kind = kind.lower()
    if kind in ("sketch", "quick_sketch"):
        return {"maskSource": "quick_sketch", "layer": None, "datatype": None, "sketch_id": rest or "default"}
    if kind == "gds":
        layer_text, _, datatype_text = rest.partition("/")
        try:
            return {
                "maskSource": "gds",
                "layer": int(layer_text),
                "datatype": int(datatype_text) if datatype_text else 0,
            }
        except ValueError as error:
            raise InvalidRequest(f"a GDS mask is gds:LAYER or gds:LAYER/DATATYPE, not {text!r}") from error
    raise InvalidRequest(f"unknown mask {text!r}: use none, sketch:ID or gds:LAYER/DATATYPE.")


def step_from_spec(spec: Mapping[str, Any], *, step_id: str | None = None) -> dict[str, Any]:
    """A document step from a flow-file entry."""
    process_type = str(spec.get("type") or spec.get("processType") or "")
    if process_type not in PROCESS_TYPES:
        raise InvalidRequest(
            f"step {spec.get('name', '?')!r}: type must be one of {', '.join(PROCESS_TYPES)}."
        )
    parameters = dict(spec.get("parameters", {}))
    mask = parse_mask(spec.get("mask"))
    sketch_id = mask.pop("sketch_id", None)
    if sketch_id is not None:
        parameters["sketch_id"] = sketch_id
    responses: dict[str, Any] = {}
    for name, rate in dict(spec.get("rates", {})).items():
        responses[str(name)] = {"material": str(name), "rateUmPerMin": float(rate), "stopLayer": False}
    for name in list(spec.get("stop", [])):
        responses[str(name)] = {"material": str(name), "rateUmPerMin": 0.0, "stopLayer": True}
    keep = str(spec.get("keep", "inside"))
    if keep not in ("inside", "outside"):
        raise InvalidRequest(f"step {spec.get('name', '?')!r}: keep must be inside or outside.")
    return {
        "id": step_id or new_id(),
        "name": str(spec.get("name") or "Step"),
        "processType": process_type,
        "tool": str(spec.get("tool", "")),
        "outputMaterial": spec.get("material") or None,
        "parameters": parameters,
        "materialResponses": responses,
        **mask,
        "keep": keep,
        "enabled": bool(spec.get("enabled", True)),
    }


def apply_flow(session: Session, flow: Mapping[str, Any]) -> dict[str, Any]:
    """Make the workspace match the flow file; returns the saved document."""
    document = session.document()
    project = document["project"]
    if "kernel" in flow and flow["kernel"] != project["kernel"]:
        raise InvalidRequest(
            f"the file wants the {flow['kernel']!r} kernel but this workspace runs on "
            f"{project['kernel']!r}; a kernel is chosen when a workspace is created."
        )

    # The window and the resolution first: changing either discards results.
    grid = project["grid"]
    bounds = {
        "xMin": grid["xMin"], "xMax": grid["xMax"],
        "yMin": grid["yMin"], "yMax": grid["yMax"],
        "zMin": grid["zMin"], "zMax": grid["zMax"],
    }
    window = flow.get("window")
    if isinstance(window, Mapping):
        for axis in ("x", "y", "z"):
            if axis in window:
                low, high = window[axis]
                bounds[f"{axis}Min"], bounds[f"{axis}Max"] = float(low), float(high)
    spacing_xy_nm: float | None = None
    if project.get("resolutionUm") is not None:
        spacing_nm = float(flow.get("resolution_nm", project["resolutionUm"] * 1000.0))
        if "resolution_xy_nm" in flow:
            spacing_xy_nm = None if flow["resolution_xy_nm"] in (None, 0, "") else float(flow["resolution_xy_nm"])
        elif project.get("resolutionXyUm") is not None:
            spacing_xy_nm = project["resolutionXyUm"] * 1000.0
    else:
        spacing_nm = float(flow.get("spacing_nm", grid["spacingUm"] * 1000.0))
    window_changed = any(
        abs(bounds[key] - grid[key]) > 1e-9
        for key in ("xMin", "xMax", "yMin", "yMax", "zMin", "zMax")
    )
    current_nm = (
        project["resolutionUm"] * 1000.0
        if project.get("resolutionUm") is not None
        else grid["spacingUm"] * 1000.0
    )
    current_xy = project.get("resolutionXyUm")
    current_xy_nm = None if current_xy is None else current_xy * 1000.0
    xy_changed = (spacing_xy_nm is None) != (current_xy_nm is None) or (
        spacing_xy_nm is not None and current_xy_nm is not None and abs(spacing_xy_nm - current_xy_nm) > 1e-9
    )
    if window_changed or abs(spacing_nm - current_nm) > 1e-9 or xy_changed:
        document = session.call(
            "set_grid", root=str(session.root), targetSpacingNm=spacing_nm,
            targetSpacingXyNm=spacing_xy_nm, bounds=bounds,
        )
        session.note("Window or resolution changed: stored results were discarded.")

    if "name" in flow:
        document["project"]["name"] = str(flow["name"])
    if isinstance(flow.get("section_lines"), list):
        document["project"]["sectionLines"] = [
            {"id": new_id(), "name": str(item.get("name") or f"Line {index}"), "start": list(item["start"]), "end": list(item["end"])}
            for index, item in enumerate(flow["section_lines"], start=1)
        ]

    materials = flow.get("materials")
    if isinstance(materials, list):
        by_name = {material["name"]: material for material in document["materials"]}
        for entry in materials:
            if isinstance(entry, str):
                entry = {"name": entry}
            name = str(entry["name"])
            current = by_name.get(name, {"name": name, "category": "Other", "color": "#7c83a0", "opacity": 1.0})
            by_name[name] = {
                **current,
                "category": str(entry.get("category", current["category"])),
                "color": str(entry.get("color", current["color"])),
                "opacity": float(entry.get("opacity", current["opacity"])),
            }
        document["materials"] = list(by_name.values())

    sketches = flow.get("sketches")
    if isinstance(sketches, Mapping):
        for sketch_id, payload in sketches.items():
            session.call(
                "save_sketch",
                root=str(session.root),
                sketchId=str(sketch_id),
                sketch={"name": payload.get("name", sketch_id), "shapes": payload.get("shapes", [])},
            )

    specs = flow.get("steps")
    if isinstance(specs, list):
        existing = Session.steps(document)
        branch = Session.branch(document)
        branch["steps"] = [
            step_from_spec(spec, step_id=existing[index]["id"] if index < len(existing) else None)
            for index, spec in enumerate(specs)
        ]
    return session.save(document)
