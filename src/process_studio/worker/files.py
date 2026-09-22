"""Files the desktop's File menu reads and writes.

The flow as a table (Excel, CSV) or as a flow file (JSON, YAML); the
material, tool and recipe libraries as workbooks, in and out; a copy of the
whole workspace under another directory (Save as); and a way to show a
workspace folder in the system's file manager.
"""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import IO, Any, Mapping

from openpyxl import Workbook, load_workbook
from openpyxl.utils import get_column_letter

from ..libraries import RecipeLibrary
from ..models import MaterialDefinition, ToolDefinition
from .errors import InvalidRequest, WorkspaceError
from .runner import build_document
from .workspace import load_project, open_repository

#: The columns a flow table can carry: an id the client asks for, the
#: heading it is written under, and the column width a workbook gives it.
#: A flow is exported for a reason -- a run sheet, a review, a tool list --
#: and which of these belong depends on the reason, so the client names the
#: ones it wants and gets them in this order.
FLOW_FIELDS: tuple[tuple[str, str, int], ...] = (
    ("index", "#", 4),
    ("name", "Step", 30),
    ("type", "Type", 12),
    ("tool", "Tool", 16),
    ("material", "Material", 12),
    ("mode", "Mode", 11),
    ("target", "Target (um)", 11),
    ("directional_fraction", "Directional fraction", 10),
    ("mask", "Mask", 18),
    ("keep", "Keep", 8),
    ("rates", "Rates (um/min)", 28),
    ("stops", "Stop layers", 16),
    ("other", "Other parameters (JSON)", 30),
    ("enabled", "Enabled", 8),
    ("status", "Status", 9),
    ("loop", "Loop", 14),
)
FLOW_FIELD_IDS = tuple(field for field, _heading, _width in FLOW_FIELDS)
FLOW_COLUMNS = [heading for _field, heading, _width in FLOW_FIELDS]
MATERIAL_COLUMNS = ["Material", "Category", "Color", "Opacity"]
TOOL_COLUMNS = ["Tool", "Group", "Notes"]
LIBRARY_KINDS = ("materials", "tools", "recipes")


def _cli():
    """The CLI's flow-file code, imported late: the CLI imports this worker."""
    from ..cli import flowfile
    from ..cli.session import Session

    return flowfile, Session


def _status_word(document: Mapping[str, Any], step_id: str) -> str:
    _, Session = _cli()
    return {"clean": "ready", "stale": "stale", "dirty": "not run"}.get(
        Session.status_of(document, step_id), "not run"
    )


def _chosen_fields(columns: Any) -> tuple[tuple[str, str, int], ...]:
    """The fields to write, in canonical order; None means all of them."""
    if columns is None:
        return FLOW_FIELDS
    if not isinstance(columns, (list, tuple)) or not all(isinstance(item, str) for item in columns):
        raise InvalidRequest("export_flow columns must be a list of column names.")
    wanted = set(columns)
    unknown = sorted(wanted - set(FLOW_FIELD_IDS))
    if unknown:
        raise InvalidRequest(
            f"export_flow does not have the column {unknown[0]!r}; it has "
            + ", ".join(FLOW_FIELD_IDS)
        )
    chosen = tuple(field for field in FLOW_FIELDS if field[0] in wanted)
    if not chosen:
        raise InvalidRequest("export_flow needs at least one column.")
    return chosen


def flow_rows(document: Mapping[str, Any], columns: Any = None) -> list[list[Any]]:
    """One row per step, holding the chosen columns in canonical order."""
    _, Session = _cli()
    fields = _chosen_fields(columns)
    rows = []
    for index, step in enumerate(Session.steps(document), start=1):
        parameters = dict(step.get("parameters", {}))
        sketch_id = parameters.pop("sketch_id", None)
        mode = parameters.pop("mode", None)
        target = parameters.pop("target", None)
        fraction = parameters.pop("directional_fraction", None)
        source = step.get("maskSource", "none")
        if source == "quick_sketch":
            mask = f"sketch:{sketch_id or 'default'}"
        elif source == "gds":
            mask = f"gds:{step.get('layer')}/{step.get('datatype') or 0}"
        else:
            mask = ""
        responses = step.get("materialResponses", {})
        rates = "; ".join(
            f"{name}={response['rateUmPerMin']}"
            for name, response in responses.items()
            if not response.get("stopLayer")
        )
        stops = "; ".join(name for name, response in responses.items() if response.get("stopLayer"))
        values = {
            "index": index,
            "name": step["name"],
            "type": step["processType"],
            "tool": step.get("tool", ""),
            "material": step.get("outputMaterial") or "",
            "mode": mode or "",
            "target": target,
            "directional_fraction": fraction,
            "mask": mask,
            "keep": step.get("keep", "inside") if mask else "",
            "rates": rates,
            "stops": stops,
            "other": json.dumps(parameters, ensure_ascii=False) if parameters else "",
            "enabled": bool(step.get("enabled", True)),
            "status": _status_word(document, step["id"]),
            "loop": _loop_word(step),
        }
        rows.append([values[field] for field, _heading, _width in fields])
    return rows


def _loop_word(step: Mapping[str, Any]) -> str:
    loop = step.get("loop")
    if not loop:
        return ""
    return f"{loop.get('name') or 'Loop'} {int(loop.get('iteration', 0)) + 1}/{int(loop.get('repeat', 1))}"


def export_flow(
    root: Path,
    destination: Path,
    fmt: str,
    project_id: str | None = None,
    columns: Any = None,
) -> dict[str, Any]:
    """Write the flow as a table (xlsx, csv) or a flow file (json, yaml).

    ``columns`` names the table columns to write (see ``FLOW_FIELDS``); the
    flow-file formats carry the whole flow and ignore it, since a flow file
    is what the workspace is rebuilt from.
    """
    flowfile, Session = _cli()
    repository = open_repository(root)
    document = build_document(root, repository, load_project(repository, project_id))
    fmt = fmt.lower()
    if fmt in ("json", "yaml", "yml"):
        flowfile.write_flow(
            destination.with_suffix(f".{fmt}") if destination.suffix == "" else destination,
            flowfile.flow_from_document(document),
        )
    elif fmt == "xlsx":
        fields = _chosen_fields(columns)
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Process flow"
        sheet.append([heading for _field, heading, _width in fields])
        for row in flow_rows(document, columns):
            sheet.append(row)
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for index, (_field, _heading, width) in enumerate(fields, start=1):
            sheet.column_dimensions[get_column_letter(index)].width = width
        workbook.save(destination)
    elif fmt == "csv":
        fields = _chosen_fields(columns)
        with destination.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow([heading for _field, heading, _width in fields])
            writer.writerows(flow_rows(document, columns))
    else:
        raise InvalidRequest("export_flow format must be xlsx, csv, json or yaml.")
    return {"path": str(destination), "steps": len(Session.steps(document))}


def import_flow(root: Path, source: Path, output: IO[str]) -> dict[str, Any]:
    """Make the workspace match a flow file, the way ``flow apply`` does."""
    if not source.is_file():
        raise InvalidRequest("The selected flow file does not exist.")
    flowfile, Session = _cli()
    session = Session(root, events=output)
    flowfile.apply_flow(session, flowfile.read_flow(source))
    repository = open_repository(root)
    return {"path": str(source), "document": build_document(root, repository, load_project(repository))}


def export_library(root: Path, kind: str, destination: Path) -> dict[str, Any]:
    repository = open_repository(root)
    if kind == "recipes":
        RecipeLibrary(repository.load_recipes()).export_excel(destination)
        count = len(repository.load_recipes())
    elif kind == "materials":
        count = _write_sheet(
            destination, "Materials", MATERIAL_COLUMNS,
            [[m.name, m.category, m.color, m.opacity] for m in repository.load_materials()],
        )
    elif kind == "tools":
        count = _write_sheet(
            destination, "Tools", TOOL_COLUMNS,
            [[t.name, t.group, t.notes] for t in repository.load_tools()],
        )
    else:
        raise InvalidRequest(f"export_library kind must be one of {LIBRARY_KINDS}.")
    return {"path": str(destination), "count": count}


def import_library(root: Path, kind: str, source: Path, project_id: str | None = None) -> dict[str, Any]:
    """Add the rows of a workbook to a library; a row with a known name replaces it."""
    if not source.is_file():
        raise InvalidRequest("The selected workbook does not exist.")
    repository = open_repository(root)
    if kind == "recipes":
        try:
            library = RecipeLibrary.import_excel(source)
        except Exception as error:  # openpyxl raises workbook-specific errors
            raise InvalidRequest(f"Cannot read the recipe workbook: {error}") from error
        for recipe in library.recipes.values():
            repository.save_recipe(recipe)
        count = len(library.recipes)
    elif kind == "materials":
        existing = {m.name: m for m in repository.load_materials()}
        count = 0
        for row in _read_rows(source, MATERIAL_COLUMNS):
            name = str(row.get("Material") or "").strip()
            if not name:
                continue
            current = existing.get(name)
            material = MaterialDefinition(
                name,
                str(row.get("Category") or (current.category if current else "Other")),
                str(row.get("Color") or (current.color if current else "#7c83a0")),
                float(row.get("Opacity") if row.get("Opacity") not in (None, "") else (current.opacity if current else 1.0)),
                id=current.id if current else MaterialDefinition(name).id,
            )
            repository.save_material(material)
            count += 1
    elif kind == "tools":
        existing = {t.name: t for t in repository.load_tools()}
        count = 0
        for row in _read_rows(source, TOOL_COLUMNS):
            name = str(row.get("Tool") or "").strip()
            if not name:
                continue
            current = existing.get(name)
            tool = ToolDefinition(
                name,
                str(row.get("Group") or (current.group if current else "")),
                str(row.get("Notes") or (current.notes if current else "")),
                id=current.id if current else ToolDefinition(name).id,
            )
            repository.save_tool(tool)
            count += 1
    else:
        raise InvalidRequest(f"import_library kind must be one of {LIBRARY_KINDS}.")
    return {"imported": count, "document": build_document(root, repository, load_project(repository, project_id))}


def _write_sheet(destination: Path, title: str, columns: list[str], rows: list[list[Any]]) -> int:
    if destination.suffix.lower() == ".csv":
        with destination.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            writer.writerows(rows)
        return len(rows)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = title
    sheet.append(columns)
    for row in rows:
        sheet.append(row)
    sheet.freeze_panes = "A2"
    workbook.save(destination)
    return len(rows)


def _read_rows(source: Path, columns: list[str]) -> list[dict[str, Any]]:
    """Rows of the first sheet (or a CSV) as dicts keyed by the header names."""
    if source.suffix.lower() == ".csv":
        with source.open("r", newline="", encoding="utf-8-sig") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    try:
        workbook = load_workbook(source, data_only=True)
    except Exception as error:
        raise InvalidRequest(f"Cannot read the workbook: {error}") from error
    sheet = workbook.active
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        return []
    headers = [str(cell) if cell is not None else "" for cell in rows[0]]
    if not set(columns[:1]) <= set(headers):
        raise InvalidRequest(f"The sheet needs a {columns[0]!r} column; it has {headers}.")
    return [dict(zip(headers, values, strict=False)) for values in rows[1:]]


def copy_workspace(root: Path, destination: Path) -> dict[str, Any]:
    """Save as: the workspace copied under ``destination``, then opened.

    The database records each snapshot's absolute path, so the copy's
    rows are pointed at the copy's own snapshot directory.
    """
    root = root.resolve()
    destination = destination.expanduser().resolve()
    if destination == root or root in destination.parents:
        raise InvalidRequest("Save the workspace outside the current workspace directory.")
    if destination.exists() and any(destination.iterdir()):
        raise InvalidRequest(f"{destination} is not empty; choose a new or empty directory.")
    repository = open_repository(root)  # fails early on a non-workspace
    old_snapshots = repository.snapshot_directory.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    for item in root.iterdir():
        if item.name.startswith("_profile"):
            continue
        target = destination / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)
    copied = open_repository(destination)
    new_snapshots = copied.snapshot_directory.resolve()
    with sqlite3.connect(copied.database_path) as connection:
        rows = connection.execute("SELECT id, path FROM snapshots").fetchall()
        for snapshot_id, path in rows:
            relative = Path(path).name
            connection.execute(
                "UPDATE snapshots SET path=? WHERE id=?", (str(new_snapshots / relative), snapshot_id)
            )
    del old_snapshots
    return {"root": str(destination), "document": build_document(destination, copied, load_project(copied))}


def reveal_path(path: Any) -> dict[str, Any]:
    """Show a directory (or a file's directory) in the system file manager."""
    if not isinstance(path, str) or not path:
        raise InvalidRequest("reveal_path requires a path.")
    target = Path(path).expanduser()
    if not target.exists():
        raise InvalidRequest(f"{target} does not exist.")
    folder = target if target.is_dir() else target.parent
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(folder))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
    except OSError as error:
        raise WorkspaceError(f"Could not open {folder}: {error}") from error
    return {"path": str(folder)}
