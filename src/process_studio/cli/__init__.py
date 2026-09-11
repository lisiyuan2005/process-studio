"""``process-studio``: the workspace from a terminal.

Every command is a thin layer over the worker the desktop shell uses, so what
the CLI writes the desktop can open, and the other way round. ``--json``
turns each command's result into machine-readable output for scripts.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..models import new_id
from ..worker.errors import Cancelled, InvalidRequest, WorkerError, WorkspaceError
from .flowfile import (
    PROCESS_TYPES,
    apply_flow,
    flow_from_document,
    parse_mask,
    read_flow,
    write_flow,
)
from .session import (
    STATUS_WORDS,
    STEP_HEADER,
    Session,
    describe_mask,
    find_root,
    format_number,
    is_workspace,
    step_rows,
    table,
)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_WORKSPACE = 3
EXIT_RUN = 4
EXIT_INTERRUPTED = 130

# The same starting values the desktop gives a new step.
STEP_DEFAULTS: dict[str, tuple[str, dict[str, Any]]] = {
    "deposit": ("New deposition", {"target": 0.05}),
    "etch": ("New etch", {"target": 0.1, "directional_fraction": 1.0}),
    "cmp": ("New CMP", {"target_z": 0.0}),
    "no_geometry": ("New process note", {}),
}


# -- small parsers -------------------------------------------------------------


def parse_value(text: str) -> Any:
    """``--set target=0.04``: numbers, true/false and JSON stay typed; the rest is text."""
    try:
        return json.loads(text)
    except ValueError:
        return text


def parse_assignment(text: str) -> tuple[str, Any]:
    key, separator, value = text.partition("=")
    if not separator or not key.strip():
        raise InvalidRequest(f"expected KEY=VALUE, got {text!r}")
    return key.strip(), parse_value(value.strip())


def parse_rate(text: str) -> tuple[str, float]:
    material, separator, value = text.partition("=")
    if not separator:
        raise InvalidRequest(f"expected MATERIAL=RATE in µm/min, got {text!r}")
    try:
        return material.strip(), float(value)
    except ValueError as error:
        raise InvalidRequest(f"the rate for {material.strip()} must be a number, got {value!r}") from error


def apply_step_options(step: dict[str, Any], args: argparse.Namespace) -> None:
    """Edit a step in place from the shared ``steps add`` / ``steps set`` options."""
    if getattr(args, "name", None):
        step["name"] = args.name
    if getattr(args, "tool", None) is not None:
        step["tool"] = args.tool
    if getattr(args, "material", None) is not None:
        step["outputMaterial"] = args.material or None
    for assignment in getattr(args, "set", None) or []:
        key, value = parse_assignment(assignment)
        step["parameters"][key] = value
    for key in getattr(args, "unset", None) or []:
        step["parameters"].pop(key, None)
    if getattr(args, "mask", None) is not None:
        mask = parse_mask(args.mask)
        sketch_id = mask.pop("sketch_id", None)
        step.update(mask)
        if sketch_id is not None:
            step["parameters"]["sketch_id"] = sketch_id
        else:
            step["parameters"].pop("sketch_id", None)
    if getattr(args, "keep", None):
        step["keep"] = args.keep
    for text in getattr(args, "rate", None) or []:
        material, rate = parse_rate(text)
        step["materialResponses"][material] = {
            "material": material, "rateUmPerMin": rate, "stopLayer": False,
        }
    for material in getattr(args, "stop", None) or []:
        step["materialResponses"][material] = {
            "material": material, "rateUmPerMin": 0.0, "stopLayer": True,
        }
    for material in getattr(args, "no_response", None) or []:
        step["materialResponses"].pop(material, None)


def step_reference(session: Session, document: Mapping[str, Any], reference: str | None) -> str:
    """A view's step id: a number or name, ``0`` for the bare wafer, none for the last step."""
    steps = Session.steps(document)
    if reference is None:
        return steps[-1]["id"] if steps else ""
    if reference.strip() in ("0", "wafer", "initial"):
        return ""
    return steps[session.step_index(document, reference)]["id"]


def summarize_project(document: Mapping[str, Any]) -> list[str]:
    project = document["project"]
    grid = project["grid"]
    lines = [
        f"Workspace  {document['root']}",
        f"Project    {project['name']}",
        f"Kernel     {project['kernel']}",
        f"Window     x {format_number(grid['xMin'])}..{format_number(grid['xMax'])}  "
        f"y {format_number(grid['yMin'])}..{format_number(grid['yMax'])}  "
        f"z {format_number(grid['zMin'])}..{format_number(grid['zMax'])} µm",
    ]
    if project.get("resolutionUm") is not None:
        z_nm = project["resolutionUm"] * 1000.0
        xy = project.get("resolutionXyUm")
        lines.append(
            f"Resolution z step {format_number(z_nm)} nm, XY arcs "
            + (f"{format_number(xy * 1000.0)} nm" if xy is not None else "same as z")
            + " (conformal deposition)"
        )
    else:
        lines.append(
            f"Grid       {format_number(grid['spacingUm'] * 1000.0)} nm · "
            f"{grid['nx']}×{grid['ny']}×{grid['nz']} nodes"
        )
    if project.get("gdsPath"):
        lines.append(f"GDS        {project['gdsPath']}")
    steps = Session.steps(document)
    statuses = [Session.status_of(document, step["id"]) for step in steps]
    counts = {word: statuses.count(key) for key, word in STATUS_WORDS.items()}
    lines.append(
        f"Steps      {len(steps)} on branch {Session.branch(document)['name']!r}: "
        + ", ".join(f"{count} {word}" for word, count in counts.items() if count)
        if steps
        else "Steps      none"
    )
    lines.append(f"Materials  {', '.join(material['name'] for material in document['materials'])}")
    return lines


# -- commands ------------------------------------------------------------------


def cmd_new(session: Session, args: argparse.Namespace) -> int:
    root = Path(args.directory).expanduser().resolve()
    existed = is_workspace(root)
    document = session.call(
        "create_workspace", root=str(root), name=args.name or root.name, kernel=args.kernel
    )
    if existed:
        session.note(f"{root} is already a workspace; opened it unchanged.")
    session.emit(document, lambda: summarize_project(document))
    return EXIT_OK


def cmd_kernels(session: Session, args: argparse.Namespace) -> int:
    described = session.call("describe")
    rows = [
        [kernel["id"], kernel["name"], kernel["version"], "yes" if kernel["id"] == described["defaultKernel"] else ""]
        for kernel in described["kernels"]
    ]
    session.emit(
        {"kernels": described["kernels"], "defaultKernel": described["defaultKernel"], "buildVariant": described["buildVariant"]},
        lambda: table(("Id", "Kernel", "Version", "Default"), rows) + [f"Build: {described['buildVariant']}"],
    )
    return EXIT_OK


def cmd_info(session: Session, args: argparse.Namespace) -> int:
    document = session.document()
    session.emit(document, lambda: summarize_project(document))
    return EXIT_OK


def cmd_status(session: Session, args: argparse.Namespace) -> int:
    document = session.document()
    session.emit(
        {"stepStatuses": document["stepStatuses"], "steps": Session.steps(document)},
        lambda: table(STEP_HEADER, step_rows(document)),
    )
    return EXIT_OK


def cmd_steps_list(session: Session, args: argparse.Namespace) -> int:
    document = session.document()
    session.emit(Session.steps(document), lambda: table(STEP_HEADER, step_rows(document)))
    return EXIT_OK


def cmd_steps_show(session: Session, args: argparse.Namespace) -> int:
    document = session.document()
    index = session.step_index(document, args.step)
    step = Session.steps(document)[index]

    def text() -> list[str]:
        lines = [
            f"{index + 1}. {step['name']}  [{step['processType'].replace('_', ' ')}]",
            f"   status    {STATUS_WORDS[Session.status_of(document, step['id'])]}"
            + ("" if step.get("enabled", True) else " (skipped)"),
            f"   tool      {step.get('tool') or '-'}",
            f"   material  {step.get('outputMaterial') or '-'}",
            f"   mask      {describe_mask(step)}",
        ]
        parameters = {key: value for key, value in step["parameters"].items() if key != "sketch_id"}
        lines.append("   parameters")
        for key, value in parameters.items():
            lines.append(f"     {key} = {format_number(value)}")
        if not parameters:
            lines.append("     (none)")
        if step["materialResponses"]:
            lines.append("   material responses")
            for name, response in step["materialResponses"].items():
                if response.get("stopLayer"):
                    lines.append(f"     {name}: stop layer")
                else:
                    lines.append(f"     {name}: {format_number(response['rateUmPerMin'])} µm/min")
        lines.append(f"   id        {step['id']}")
        return lines

    session.emit(step, text)
    return EXIT_OK


def _save_and_report(session: Session, document: Mapping[str, Any], message: str) -> int:
    saved = session.save(document)
    session.note(message)
    session.emit(Session.steps(saved), lambda: table(STEP_HEADER, step_rows(saved)))
    return EXIT_OK


def cmd_steps_add(session: Session, args: argparse.Namespace) -> int:
    document = session.document()
    steps = Session.steps(document)
    name, parameters = STEP_DEFAULTS[args.type]
    step = {
        "id": new_id(),
        "name": name,
        "processType": args.type,
        "tool": "",
        "outputMaterial": None,
        "parameters": dict(parameters),
        "materialResponses": {},
        "maskSource": "none",
        "layer": None,
        "datatype": None,
        "keep": "inside",
        "enabled": True,
    }
    apply_step_options(step, args)
    if args.before is not None:
        position = session.step_index(document, args.before)
    elif args.after is not None:
        position = session.step_index(document, args.after) + 1
    else:
        position = len(steps)
    steps.insert(position, step)
    return _save_and_report(session, document, f"Added step {position + 1}: {step['name']}")


def cmd_steps_set(session: Session, args: argparse.Namespace) -> int:
    document = session.document()
    index = session.step_index(document, args.step)
    step = Session.steps(document)[index]
    apply_step_options(step, args)
    return _save_and_report(session, document, f"Updated step {index + 1}: {step['name']}")


def cmd_steps_rm(session: Session, args: argparse.Namespace) -> int:
    document = session.document()
    steps = Session.steps(document)
    indexes = sorted({session.step_index(document, reference) for reference in args.step}, reverse=True)
    names = [steps[index]["name"] for index in reversed(indexes)]
    for index in indexes:
        del steps[index]
    return _save_and_report(session, document, f"Removed {', '.join(names)}; later steps need a re-run.")


def cmd_steps_dup(session: Session, args: argparse.Namespace) -> int:
    document = session.document()
    steps = Session.steps(document)
    index = session.step_index(document, args.step)
    copy = json.loads(json.dumps(steps[index]))
    copy["id"] = new_id()
    copy["name"] = args.name or f"{steps[index]['name']} copy"
    steps.insert(index + 1, copy)
    return _save_and_report(session, document, f"Duplicated step {index + 1} as step {index + 2}: {copy['name']}")


def cmd_steps_mv(session: Session, args: argparse.Namespace) -> int:
    document = session.document()
    steps = Session.steps(document)
    index = session.step_index(document, args.step)
    if not args.position.isdigit() or not 1 <= int(args.position) <= len(steps):
        raise InvalidRequest(f"the new position must be a number from 1 to {len(steps)}.")
    target = int(args.position) - 1
    step = steps.pop(index)
    steps.insert(target, step)
    return _save_and_report(session, document, f"Moved {step['name']} to position {target + 1}")


def cmd_steps_enable(session: Session, args: argparse.Namespace, enabled: bool) -> int:
    document = session.document()
    steps = Session.steps(document)
    names = []
    for reference in args.step:
        step = steps[session.step_index(document, reference)]
        step["enabled"] = enabled
        names.append(step["name"])
    verb = "Included" if enabled else "Skipped"
    return _save_and_report(session, document, f"{verb} {', '.join(names)}")


def cmd_run(session: Session, args: argparse.Namespace) -> int:
    document = session.document()
    branch = Session.branch(document)
    through = None
    if args.through is not None:
        through = Session.steps(document)[session.step_index(document, args.through)]["id"]
    result = session.call(
        "run_flow", root=str(session.root), branchId=branch["id"], throughStepId=through, force=args.force
    )
    after = session.document()

    def text() -> list[str]:
        return [
            f"Ran {len(result['executedStepIds'])} step(s), reused {len(result['cachedStepIds'])}, "
            f"in {result['elapsedMs'] / 1000.0:.1f} s",
            *table(STEP_HEADER, step_rows(after)),
        ]

    session.emit({**result, "steps": Session.steps(after)}, text)
    return EXIT_OK


def cmd_window(session: Session, args: argparse.Namespace) -> int:
    document = session.document()
    project = document["project"]
    grid = project["grid"]
    changes = {axis: getattr(args, axis) for axis in ("x", "y", "z") if getattr(args, axis) is not None}
    if not changes and args.spacing is None and args.spacing_xy is None:
        session.emit(
            {"grid": grid, "resolutionUm": project.get("resolutionUm"), "resolutionXyUm": project.get("resolutionXyUm")},
            lambda: summarize_project(document)[3:5],
        )
        return EXIT_OK
    bounds = {key: grid[key] for key in ("xMin", "xMax", "yMin", "yMax", "zMin", "zMax")}
    for axis, (low, high) in changes.items():
        bounds[f"{axis}Min"], bounds[f"{axis}Max"] = low, high
    if args.spacing is not None:
        spacing = args.spacing
    elif project.get("resolutionUm") is not None:
        spacing = project["resolutionUm"] * 1000.0
    else:
        spacing = grid["spacingUm"] * 1000.0
    if args.spacing_xy is not None:
        spacing_xy: float | None = None if args.spacing_xy <= 0 else args.spacing_xy
    elif project.get("resolutionXyUm") is not None:
        spacing_xy = project["resolutionXyUm"] * 1000.0
    else:
        spacing_xy = None
    if not args.yes:
        session.note("Changing the window or the spacing discards every stored result (pass --yes to skip this note).")
    saved = session.call(
        "set_grid", root=str(session.root), targetSpacingNm=spacing, targetSpacingXyNm=spacing_xy, bounds=bounds
    )
    session.emit(
        {
            "grid": saved["project"]["grid"],
            "resolutionUm": saved["project"].get("resolutionUm"),
            "resolutionXyUm": saved["project"].get("resolutionXyUm"),
        },
        lambda: summarize_project(saved)[3:5] + ["Stored results were discarded; run the flow again."],
    )
    return EXIT_OK


def _write_png(payload: Mapping[str, Any], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(base64.b64decode(payload["image"]))


def _named_line(document: Mapping[str, Any], reference: str) -> dict[str, Any]:
    lines = document["project"].get("sectionLines", [])
    wanted = reference.strip().lower()
    for line in lines:
        if line["id"] == reference or line["name"].lower() == wanted:
            return line
    if reference.strip().isdigit() and 1 <= int(reference) <= len(lines):
        return lines[int(reference) - 1]
    raise InvalidRequest(f"no section line named {reference!r}; see `lines list`.")


def cmd_view_section(session: Session, args: argparse.Namespace) -> int:
    document = session.document()
    branch = Session.branch(document)
    step_id = step_reference(session, document, args.step)
    request: dict[str, Any] = {
        "branchId": branch["id"], "stepId": step_id, "interpolation": args.interpolation,
    }
    if args.named is not None:
        line = _named_line(document, args.named)
        request["axis"] = "line"
        request["line"] = {"start": line["start"], "end": line["end"]}
    elif args.line is not None:
        x0, y0, x1, y1 = args.line
        request["axis"] = "line"
        request["line"] = {"start": [x0, y0], "end": [x1, y1]}
    else:
        request["axis"] = args.axis
        if args.at is not None:
            request["position"] = args.at
    payload = session.call("get_section", root=str(session.root), **request)
    destination = Path(args.output)
    _write_png(payload, destination)
    extent = payload["extent"]
    session.emit(
        {key: value for key, value in payload.items() if key != "image"} | {"path": str(destination)},
        [
            f"Wrote {destination} ({payload['width']}×{payload['height']} px)",
            f"Cut along {payload['axis']} at {format_number(payload['position'])} µm; "
            f"{payload.get('horizontalAxis', 'h')} {format_number(extent['horizontalMin'])}..{format_number(extent['horizontalMax'])}, "
            f"z {format_number(extent['verticalMin'])}..{format_number(extent['verticalMax'])} µm",
        ],
    )
    return EXIT_OK


def cmd_view_top(session: Session, args: argparse.Namespace) -> int:
    document = session.document()
    branch = Session.branch(document)
    step_id = step_reference(session, document, args.step)
    payload = session.call("get_top_view", root=str(session.root), branchId=branch["id"], stepId=step_id)
    destination = Path(args.output)
    _write_png(payload, destination)
    session.emit(
        {key: value for key, value in payload.items() if key != "image"} | {"path": str(destination)},
        [f"Wrote {destination} ({payload['width']}×{payload['height']} px)"],
    )
    return EXIT_OK


def cmd_view_mesh(session: Session, args: argparse.Namespace) -> int:
    document = session.document()
    branch = Session.branch(document)
    step_id = step_reference(session, document, args.step)
    result = session.call(
        "export_mesh", root=str(session.root), branchId=branch["id"], stepId=step_id,
        destination=str(Path(args.output).resolve()), interpolation=args.interpolation,
        materials=args.material or None,
    )
    session.emit(
        result,
        [f"Wrote {result['path']}"] + [f"  {name}: {count} triangles" for name, count in result["triangles"].items()],
    )
    return EXIT_OK


def cmd_materials_list(session: Session, args: argparse.Namespace) -> int:
    document = session.document()
    used: dict[str, int] = {}
    for step in Session.steps(document):
        for name in [step.get("outputMaterial"), *step.get("materialResponses", {})]:
            if name:
                used[name] = used.get(name, 0) + 1
    rows = [
        [material["name"], material["category"], material["color"], format_number(material["opacity"]), used.get(material["name"], 0)]
        for material in document["materials"]
    ]
    session.emit(document["materials"], lambda: table(("Name", "Category", "Color", "Opacity", "Used by"), rows))
    return EXIT_OK


def cmd_materials_add(session: Session, args: argparse.Namespace) -> int:
    document = session.document()
    by_name = {material["name"]: material for material in document["materials"]}
    current = by_name.get(args.name)
    material = {
        "id": current["id"] if current else new_id(),
        "name": args.name,
        "category": args.category or (current["category"] if current else "Other"),
        "color": args.color or (current["color"] if current else "#7c83a0"),
        "opacity": args.opacity if args.opacity is not None else (current["opacity"] if current else 1.0),
    }
    by_name[args.name] = material
    document["materials"] = list(by_name.values())
    saved = session.save(document)
    session.note(("Updated" if current else "Added") + f" material {args.name}")
    session.emit(saved["materials"], lambda: table(("Name", "Category", "Color", "Opacity"), [
        [m["name"], m["category"], m["color"], format_number(m["opacity"])] for m in saved["materials"]
    ]))
    return EXIT_OK


def cmd_materials_rm(session: Session, args: argparse.Namespace) -> int:
    document = session.document()
    names = {material["name"] for material in document["materials"]}
    missing = [name for name in args.name if name not in names]
    if missing:
        raise InvalidRequest(f"no material named {', '.join(missing)}")
    for step in Session.steps(document):
        for name in args.name:
            if step.get("outputMaterial") == name or name in step.get("materialResponses", {}):
                raise InvalidRequest(f"{name} is used by step {step['name']!r}; change that step first.")
    document["materials"] = [m for m in document["materials"] if m["name"] not in set(args.name)]
    saved = session.save(document)
    session.note(f"Removed {', '.join(args.name)}")
    session.emit(saved["materials"], lambda: [m["name"] for m in saved["materials"]])
    return EXIT_OK


def cmd_recipes_list(session: Session, args: argparse.Namespace) -> int:
    document = session.document()
    ordered = sorted(document["recipes"], key=lambda r: (r["processType"], r.get("group") or "", r["name"].lower()))
    rows = [
        [recipe["processType"].replace("_", " "), recipe.get("group") or "", recipe["name"], recipe.get("tool") or "",
         recipe.get("outputMaterial") or "",
         ", ".join(f"{key}={format_number(value)}" for key, value in recipe["parameters"].items())]
        for recipe in ordered
    ]
    session.emit(document["recipes"], lambda: table(("Type", "Group", "Recipe", "Tool", "Material", "Parameters"), rows))
    return EXIT_OK


def cmd_tools_list(session: Session, args: argparse.Namespace) -> int:
    document = session.document()
    used: dict[str, int] = {}
    for step in Session.steps(document):
        if step.get("tool"):
            used[step["tool"]] = used.get(step["tool"], 0) + 1
    ordered = sorted(document.get("tools", []), key=lambda t: (t.get("group") or "", t["name"].lower()))
    rows = [[tool.get("group") or "", tool["name"], tool.get("notes") or "", used.get(tool["name"], 0)] for tool in ordered]
    session.emit(document.get("tools", []), lambda: table(("Group", "Tool", "Notes", "Used by"), rows) if rows else ["(no tools)"])
    return EXIT_OK


def cmd_tools_add(session: Session, args: argparse.Namespace) -> int:
    document = session.document()
    tools = list(document.get("tools", []))
    current = next((tool for tool in tools if tool["name"].lower() == args.name.strip().lower()), None)
    tool = {
        "id": current["id"] if current else new_id(),
        "name": args.name.strip(),
        "group": args.group if args.group is not None else (current["group"] if current else ""),
        "notes": args.notes if args.notes is not None else (current["notes"] if current else ""),
    }
    if current:
        tools[tools.index(current)] = tool
    else:
        tools.append(tool)
    document["tools"] = tools
    saved = session.save(document)
    session.note(("Updated" if current else "Added") + f" tool {tool['name']}")
    session.emit(tool, lambda: [f"{t.get('group') or '-':<16} {t['name']}" for t in saved.get("tools", [])])
    return EXIT_OK


def cmd_tools_rm(session: Session, args: argparse.Namespace) -> int:
    document = session.document()
    tools = list(document.get("tools", []))
    names = {name.lower() for name in args.name}
    missing = [name for name in args.name if not any(t["name"].lower() == name.lower() for t in tools)]
    if missing:
        raise InvalidRequest(f"no tool named {', '.join(missing)}")
    document["tools"] = [tool for tool in tools if tool["name"].lower() not in names]
    saved = session.save(document)
    session.note(f"Removed {', '.join(args.name)}; steps that named the tool keep the name as text.")
    session.emit(saved.get("tools", []), lambda: [t["name"] for t in saved.get("tools", [])] or ["(no tools)"])
    return EXIT_OK


def cmd_recipes_export(session: Session, args: argparse.Namespace) -> int:
    result = session.call("export_recipes_xlsx", root=str(session.root), destination=str(Path(args.file).resolve()))
    session.emit(result, [f"Wrote {result['path']}"])
    return EXIT_OK


def cmd_recipes_import(session: Session, args: argparse.Namespace) -> int:
    result = session.call("import_recipes_xlsx", root=str(session.root), source=str(Path(args.file).resolve()))
    session.emit({"imported": result["imported"]}, [f"Imported {result['imported']} recipe(s)"])
    return EXIT_OK


def cmd_sketch_list(session: Session, args: argparse.Namespace) -> int:
    document = session.document()
    rows = [[sketch["id"], sketch["name"], len(sketch["shapes"])] for sketch in document.get("sketches", [])]
    session.emit(document.get("sketches", []), lambda: table(("Id", "Name", "Shapes"), rows))
    return EXIT_OK


def _sketch(document: Mapping[str, Any], sketch_id: str) -> dict[str, Any]:
    for sketch in document.get("sketches", []):
        if sketch["id"] == sketch_id:
            return sketch
    raise InvalidRequest(f"no sketch with id {sketch_id!r}; see `sketch list`.")


def cmd_sketch_show(session: Session, args: argparse.Namespace) -> int:
    sketch = _sketch(session.document(), args.id)

    def text() -> list[str]:
        lines = [f"{sketch['id']}: {sketch['name']}"]
        for index, shape in enumerate(sketch["shapes"], start=1):
            parameters = ", ".join(f"{key}={value}" for key, value in shape["parameters"].items())
            array = shape.get("array", [1, 1, 0, 0])
            repeat = f"  array {array[0]}×{array[1]} pitch {array[2]}×{array[3]}" if array[:2] != [1, 1] else ""
            lines.append(f"  {index}. {shape['operation']} {shape['kind']}: {parameters}{repeat}")
        return lines

    session.emit(sketch, text)
    return EXIT_OK


def cmd_sketch_export(session: Session, args: argparse.Namespace) -> int:
    sketch = _sketch(session.document(), args.id)
    destination = Path(args.file)
    destination.write_text(json.dumps({"name": sketch["name"], "shapes": sketch["shapes"]}, indent=2), encoding="utf-8")
    session.emit({"path": str(destination)}, [f"Wrote {destination}"])
    return EXIT_OK


def cmd_sketch_import(session: Session, args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    saved = session.call(
        "save_sketch", root=str(session.root), sketchId=args.id,
        sketch={"name": payload.get("name", args.id), "shapes": payload.get("shapes", [])},
    )
    session.note(f"Saved sketch {args.id}; steps that use it need a re-run.")
    session.emit(_sketch(saved, args.id), lambda: table(STEP_HEADER, step_rows(saved)))
    return EXIT_OK


def _line_rows(document: Mapping[str, Any]) -> list[list[Any]]:
    return [
        [index, line["name"], format_number(line["start"][0]), format_number(line["start"][1]),
         format_number(line["end"][0]), format_number(line["end"][1])]
        for index, line in enumerate(document["project"].get("sectionLines", []), start=1)
    ]


LINE_HEADER = ("#", "Name", "A x", "A y", "B x", "B y")


def cmd_lines_list(session: Session, args: argparse.Namespace) -> int:
    document = session.document()
    session.emit(
        document["project"].get("sectionLines", []),
        lambda: table(LINE_HEADER, _line_rows(document)) if _line_rows(document) else ["(no section lines)"],
    )
    return EXIT_OK


def cmd_lines_add(session: Session, args: argparse.Namespace) -> int:
    document = session.document()
    lines = list(document["project"].get("sectionLines", []))
    existing = next((line for line in lines if line["name"].lower() == args.name.strip().lower()), None)
    x0, y0, x1, y1 = args.coordinates
    if (x0, y0) == (x1, y1):
        raise InvalidRequest("A and B must be different points.")
    line = {"id": existing["id"] if existing else new_id(), "name": args.name.strip(), "start": [x0, y0], "end": [x1, y1]}
    if existing:
        lines[lines.index(existing)] = line
    else:
        lines.append(line)
    document["project"]["sectionLines"] = lines
    saved = session.save(document)
    session.note(("Updated" if existing else "Added") + f" section line {line['name']}")
    session.emit(line, lambda: table(LINE_HEADER, _line_rows(saved)))
    return EXIT_OK


def cmd_lines_rm(session: Session, args: argparse.Namespace) -> int:
    document = session.document()
    lines = list(document["project"].get("sectionLines", []))
    for reference in args.name:
        line = _named_line(document, reference)
        lines = [item for item in lines if item["id"] != line["id"]]
        document["project"]["sectionLines"] = lines
    saved = session.save(document)
    session.note(f"Removed {', '.join(args.name)}")
    session.emit(saved["project"].get("sectionLines", []), lambda: table(LINE_HEADER, _line_rows(saved)) or ["(no section lines)"])
    return EXIT_OK


def cmd_flow_dump(session: Session, args: argparse.Namespace) -> int:
    flow = flow_from_document(session.document())
    if args.file:
        destination = Path(args.file)
        write_flow(destination, flow)
        session.emit({"path": str(destination)}, [f"Wrote {destination}"])
    else:
        session.out.write(json.dumps(flow, indent=2, ensure_ascii=False) + "\n")
    return EXIT_OK


def cmd_flow_apply(session: Session, args: argparse.Namespace) -> int:
    flow = read_flow(Path(args.file))
    saved = apply_flow(session, flow)
    session.emit(Session.steps(saved), lambda: summarize_project(saved) + [""] + table(STEP_HEADER, step_rows(saved)))
    return EXIT_OK


def cmd_log(session: Session, args: argparse.Namespace) -> int:
    from ..worker.workspace import open_repository

    repository = open_repository(session.root)
    with repository.connect() as connection:
        rows = connection.execute(
            "SELECT created_at, level, step_id, message, elapsed_ms FROM process_logs "
            "ORDER BY id DESC LIMIT ?",
            (args.lines,),
        ).fetchall()
    entries = [dict(row) for row in reversed(rows)]
    session.emit(
        entries,
        lambda: [
            f"{entry['created_at'][:19].replace('T', ' ')}  {entry['level']:<5} {entry['message']}"
            + (f"  ({entry['elapsed_ms'] / 1000.0:.1f} s)" if entry.get("elapsed_ms") else "")
            for entry in entries
        ] or ["(no log entries)"],
    )
    return EXIT_OK


def cmd_rpc(session: Session, args: argparse.Namespace) -> int:
    if args.params is None:
        params: dict[str, Any] = {}
    elif args.params.startswith("@"):
        params = json.loads(Path(args.params[1:]).read_text(encoding="utf-8"))
    else:
        params = json.loads(args.params)
    if not isinstance(params, dict):
        raise InvalidRequest("the parameters must be a JSON object.")
    if "root" not in params and session._root is not None:
        params["root"] = str(session.root)
    result = session.call(args.method, **params)
    session.out.write(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return EXIT_OK


# -- the parser ----------------------------------------------------------------


def _step_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--name", help="the step's name")
    parser.add_argument("--tool", help="the tool it runs on (free text)")
    parser.add_argument("--material", help="the material a deposition adds")
    parser.add_argument("--set", action="append", metavar="KEY=VALUE",
                        help="a process parameter, e.g. target=0.04 or mode=conformal (repeatable)")
    parser.add_argument("--unset", action="append", metavar="KEY", help="drop a parameter (repeatable)")
    parser.add_argument("--mask", metavar="MASK", help="none, sketch:ID or gds:LAYER/DATATYPE")
    parser.add_argument("--keep", choices=("inside", "outside"), help="which side of the mask the step acts on")
    parser.add_argument("--rate", action="append", metavar="MATERIAL=UM_PER_MIN",
                        help="an etch rate for a material (repeatable)")
    parser.add_argument("--stop", action="append", metavar="MATERIAL", help="an etch stop layer (repeatable)")
    parser.add_argument("--no-response", action="append", metavar="MATERIAL",
                        help="forget a material's etch response (repeatable)")


def _view_step(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--step", help="a step number or name; 0 is the bare wafer; default: the last step")
    parser.add_argument("-o", "--output", required=True, help="the file to write")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="process-studio",
        description="Build and run Process Studio flows from the terminal.",
        epilog="Most commands work in the nearest workspace at or above the current "
               "directory; --root or PROCESS_STUDIO_ROOT names another one.",
    )
    parser.add_argument("--root", help="the workspace directory")
    parser.add_argument("--json", action="store_true", help="print results as JSON")
    parser.add_argument("-q", "--quiet", action="store_true", help="no progress or remarks on stderr")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    commands.required = True

    new = commands.add_parser("new", help="create a workspace")
    new.add_argument("directory")
    new.add_argument("--name", help="the project name; default: the directory name")
    new.add_argument("--kernel", help="slab or levelset; default: the build's default")
    new.set_defaults(handler=cmd_new, needs_root=False)

    commands.add_parser("kernels", help="the kernels this build offers").set_defaults(handler=cmd_kernels, needs_root=False)
    commands.add_parser("info", help="the project, its window and its steps").set_defaults(handler=cmd_info)
    commands.add_parser("status", help="each step and whether its result is current").set_defaults(handler=cmd_status)

    steps = commands.add_parser("steps", help="list and edit the process steps")
    step_commands = steps.add_subparsers(dest="steps_command", metavar="ACTION")
    step_commands.required = True
    step_commands.add_parser("list", help="the steps in order").set_defaults(handler=cmd_steps_list)
    show = step_commands.add_parser("show", help="everything about one step")
    show.add_argument("step")
    show.set_defaults(handler=cmd_steps_show)
    add = step_commands.add_parser("add", help="append or insert a step")
    add.add_argument("type", choices=PROCESS_TYPES)
    position = add.add_mutually_exclusive_group()
    position.add_argument("--after", metavar="STEP", help="insert after this step")
    position.add_argument("--before", metavar="STEP", help="insert before this step")
    _step_options(add)
    add.set_defaults(handler=cmd_steps_add)
    edit = step_commands.add_parser("set", help="change a step's settings")
    edit.add_argument("step")
    _step_options(edit)
    edit.set_defaults(handler=cmd_steps_set)
    remove = step_commands.add_parser("rm", help="delete steps and their stored results")
    remove.add_argument("step", nargs="+")
    remove.set_defaults(handler=cmd_steps_rm)
    duplicate = step_commands.add_parser("dup", help="copy a step in place after itself")
    duplicate.add_argument("step")
    duplicate.add_argument("--name", help="the copy's name; default: the original's plus 'copy'")
    duplicate.set_defaults(handler=cmd_steps_dup)
    move = step_commands.add_parser("mv", help="move a step to another position")
    move.add_argument("step")
    move.add_argument("position", help="the new 1-based position")
    move.set_defaults(handler=cmd_steps_mv)
    skip = step_commands.add_parser("skip", help="leave steps out of the run")
    skip.add_argument("step", nargs="+")
    skip.set_defaults(handler=lambda session, args: cmd_steps_enable(session, args, False))
    include = step_commands.add_parser("include", help="put skipped steps back in the run")
    include.add_argument("step", nargs="+")
    include.set_defaults(handler=lambda session, args: cmd_steps_enable(session, args, True))

    run = commands.add_parser("run", help="run the flow, reusing results that are still current")
    run.add_argument("--through", metavar="STEP", help="stop after this step")
    run.add_argument("--force", action="store_true", help="recompute every step")
    run.set_defaults(handler=cmd_run)

    window = commands.add_parser("window", help="show or change the project window and spacing")
    for axis in ("x", "y", "z"):
        window.add_argument(f"--{axis}", nargs=2, type=float, metavar=("MIN", "MAX"), help=f"the {axis} range in µm")
    window.add_argument("--spacing", type=float, metavar="NM", help="grid spacing (level set) or the z step of the slab kernel, in nm")
    window.add_argument("--spacing-xy", type=float, metavar="NM",
                        help="slab kernel only: the XY arc sagitta in nm; 0 makes it follow the z step again")
    window.add_argument("--yes", action="store_true", help="do not remark that results are discarded")
    window.set_defaults(handler=cmd_window)

    view = commands.add_parser("view", help="write a section, a top view or a mesh to a file")
    view_commands = view.add_subparsers(dest="view_command", metavar="KIND")
    view_commands.required = True
    section = view_commands.add_parser("section", help="a cross-section as PNG")
    _view_step(section)
    section.add_argument("--axis", choices=("x", "y"), default="y", help="cut along this axis (default y)")
    section.add_argument("--at", type=float, metavar="UM", help="where to cut, in µm; default: the middle")
    section.add_argument("--line", nargs=4, type=float, metavar=("X0", "Y0", "X1", "Y1"), help="cut along an arbitrary line")
    section.add_argument("--named", metavar="LINE", help="cut along a saved section line, by name or number from `lines list`")
    section.add_argument("--interpolation", type=int, default=1, help="display upsampling factor")
    section.set_defaults(handler=cmd_view_section)
    top = view_commands.add_parser("top", help="the top view as PNG")
    _view_step(top)
    top.set_defaults(handler=cmd_view_top)
    mesh = view_commands.add_parser("mesh", help="the 3D surfaces as .glb, .gltf, .obj, .stl or .ply")
    _view_step(mesh)
    mesh.add_argument("--material", action="append", help="only these materials (repeatable)")
    mesh.add_argument("--interpolation", type=int, default=1, help="surface upsampling factor (level set)")
    mesh.set_defaults(handler=cmd_view_mesh)

    materials = commands.add_parser("materials", help="the material library")
    material_commands = materials.add_subparsers(dest="materials_command", metavar="ACTION")
    material_commands.required = True
    material_commands.add_parser("list", help="every material and where it is used").set_defaults(handler=cmd_materials_list)
    material_add = material_commands.add_parser("add", help="add a material, or change one that exists")
    material_add.add_argument("name")
    material_add.add_argument("--category", help="Semiconductor, Dielectric, Metal, Mask or free text")
    material_add.add_argument("--color", help="#rrggbb")
    material_add.add_argument("--opacity", type=float)
    material_add.set_defaults(handler=cmd_materials_add)
    material_rm = material_commands.add_parser("rm", help="remove materials no step uses")
    material_rm.add_argument("name", nargs="+")
    material_rm.set_defaults(handler=cmd_materials_rm)

    recipes = commands.add_parser("recipes", help="the recipe library")
    recipe_commands = recipes.add_subparsers(dest="recipes_command", metavar="ACTION")
    recipe_commands.required = True
    recipe_commands.add_parser("list", help="every recipe").set_defaults(handler=cmd_recipes_list)
    recipe_export = recipe_commands.add_parser("export", help="write the library as .xlsx")
    recipe_export.add_argument("file")
    recipe_export.set_defaults(handler=cmd_recipes_export)
    recipe_import = recipe_commands.add_parser("import", help="read recipes from .xlsx")
    recipe_import.add_argument("file")
    recipe_import.set_defaults(handler=cmd_recipes_import)

    tools = commands.add_parser("tools", help="the tool library the Tool fields pick from")
    tool_commands = tools.add_subparsers(dest="tools_command", metavar="ACTION")
    tool_commands.required = True
    tool_commands.add_parser("list", help="every tool, grouped").set_defaults(handler=cmd_tools_list)
    tool_add = tool_commands.add_parser("add", help="add a tool, or change one that exists")
    tool_add.add_argument("name")
    tool_add.add_argument("--group", help="a path such as Etch/Dry; subgroups are separated by /")
    tool_add.add_argument("--notes")
    tool_add.set_defaults(handler=cmd_tools_add)
    tool_rm = tool_commands.add_parser("rm", help="remove tools from the library")
    tool_rm.add_argument("name", nargs="+")
    tool_rm.set_defaults(handler=cmd_tools_rm)

    sketch = commands.add_parser("sketch", help="the Quick Sketch masks")
    sketch_commands = sketch.add_subparsers(dest="sketch_command", metavar="ACTION")
    sketch_commands.required = True
    sketch_commands.add_parser("list", help="every sketch").set_defaults(handler=cmd_sketch_list)
    sketch_show = sketch_commands.add_parser("show", help="the shapes of one sketch")
    sketch_show.add_argument("id")
    sketch_show.set_defaults(handler=cmd_sketch_show)
    sketch_export = sketch_commands.add_parser("export", help="write a sketch as JSON")
    sketch_export.add_argument("id")
    sketch_export.add_argument("file")
    sketch_export.set_defaults(handler=cmd_sketch_export)
    sketch_import = sketch_commands.add_parser("import", help="read a sketch from JSON")
    sketch_import.add_argument("id")
    sketch_import.add_argument("file")
    sketch_import.set_defaults(handler=cmd_sketch_import)

    flow = commands.add_parser("flow", help="the whole flow as one file")
    flow_commands = flow.add_subparsers(dest="flow_command", metavar="ACTION")
    flow_commands.required = True
    dump = flow_commands.add_parser("dump", help="write the flow as .json or .yaml (stdout without a file)")
    dump.add_argument("file", nargs="?")
    dump.set_defaults(handler=cmd_flow_dump)
    apply = flow_commands.add_parser("apply", help="make the workspace match a flow file")
    apply.add_argument("file")
    apply.add_argument("--kernel", help="when --root names a directory that is not a workspace yet, create it on this kernel")
    apply.set_defaults(handler=cmd_flow_apply, creates=True)

    lines = commands.add_parser("lines", help="the saved AA–BB section lines")
    line_commands = lines.add_subparsers(dest="lines_command", metavar="ACTION")
    line_commands.required = True
    line_commands.add_parser("list", help="every saved line").set_defaults(handler=cmd_lines_list)
    line_add = line_commands.add_parser("add", help="save a line, or move one that has this name")
    line_add.add_argument("name")
    line_add.add_argument("coordinates", nargs=4, type=float, metavar=("X0", "Y0", "X1", "Y1"), help="A and B in µm")
    line_add.set_defaults(handler=cmd_lines_add)
    line_rm = line_commands.add_parser("rm", help="forget saved lines")
    line_rm.add_argument("name", nargs="+")
    line_rm.set_defaults(handler=cmd_lines_rm)

    log = commands.add_parser("log", help="what the worker recorded for this project")
    log.add_argument("-n", "--lines", type=int, default=30)
    log.set_defaults(handler=cmd_log)

    rpc = commands.add_parser("rpc", help="call a worker method directly")
    rpc.add_argument("method")
    rpc.add_argument("params", nargs="?", help="a JSON object, or @file.json")
    rpc.set_defaults(handler=cmd_rpc, needs_root=False)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    session = Session(None, json_output=args.json, quiet=args.quiet)
    try:
        if getattr(args, "needs_root", True):
            try:
                session._root = find_root(args.root)
            except WorkspaceError:
                # `flow apply --root DIR` on a directory that is not a
                # workspace creates it, so the file can stand up a project.
                if getattr(args, "creates", False) and args.root:
                    root = Path(args.root).expanduser().resolve()
                    kernel = args.kernel or read_flow(Path(args.file)).get("kernel")
                    session.call("create_workspace", root=str(root), name=root.name, kernel=kernel)
                    session.note(f"Created workspace {root}")
                    session._root = root
                else:
                    raise
        elif args.root:
            session._root = find_root(args.root)
        return int(args.handler(session, args))
    except Cancelled as error:
        session.err.write(f"stopped: {error}\n")
        return EXIT_INTERRUPTED
    except KeyboardInterrupt:
        session.err.write("interrupted\n")
        return EXIT_INTERRUPTED
    except InvalidRequest as error:
        session.err.write(f"error: {error}\n")
        return EXIT_USAGE
    except WorkspaceError as error:
        session.err.write(f"error: {error}\n")
        return EXIT_WORKSPACE
    except WorkerError as error:
        session.err.write(f"error: {error}\n")
        return EXIT_RUN
    except Exception as error:  # a kernel failure inside a step
        session.err.write(f"error: {type(error).__name__}: {error}\n")
        return EXIT_RUN


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
