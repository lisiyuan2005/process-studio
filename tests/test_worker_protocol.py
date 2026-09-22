"""RPC-level tests for the desktop worker."""

from __future__ import annotations

import base64
import io
import json
import os
import time

import numpy as np
import pytest
from pathlib import Path
from PIL import Image

from process_studio.models import ProjectDefinition
from process_studio.worker.errors import InvalidRequest, WorkspaceError
from process_studio.worker.protocol import dispatch, serve
from process_studio.worker.workspace import DigestCache, initialize_workspace, load_project, open_repository


def call(method: str, **params):
    sink = io.StringIO()
    return dispatch({"method": method, "params": params}, sink)


def call_with_events(method: str, **params):
    sink = io.StringIO()
    result = dispatch({"method": method, "params": params}, sink)
    events = [json.loads(line)["event"] for line in sink.getvalue().splitlines()]
    return result, events


@pytest.fixture()
def workspace(tmp_path):
    root = tmp_path / "workspace"
    call("create_workspace", root=str(root), name="Test Project")
    return root


def test_describe_reports_capabilities_without_hard_coding_them():
    described = call("describe")
    assert described["protocolVersion"] == 2
    assert "etch" in described["processTypes"]
    assert described["maskSources"] == ["none", "quick_sketch", "gds"]
    # The desktop's CLI panel prefixes its commands with this.
    assert described["cli"]["command"] and described["cli"]["packaged"] is False
    assert described["limits"]["calibrated"] is False


def test_create_workspace_is_idempotent(tmp_path):
    root = tmp_path / "workspace"
    first = call("create_workspace", root=str(root), name="Test Project")
    second = call("create_workspace", root=str(root), name="Ignored Rename")
    assert first["project"]["id"] == second["project"]["id"]
    assert second["project"]["name"] == "Test Project"
    assert len(second["branches"][0]["steps"]) == len(first["branches"][0]["steps"])


def test_describe_publishes_the_kernel_a_project_is_built_on():
    described = call("describe")
    kernels = {kernel["id"]: kernel for kernel in described["kernels"]}
    assert described["defaultKernel"] == "slab"
    assert list(kernels) == ["slab"]
    assert kernels["slab"]["spacingRole"] == "conformal_resolution"
    # The kernel has no field, so there is no node ceiling to report.
    assert kernels["slab"]["maximumNodes"] is None
    assert kernels["slab"]["depositionModes"] == ["conformal", "planar"]
    # It hands over its own triangles; nothing else has to be installed.
    assert described["rendering"]["surfaces"] is True


def test_a_workspace_keeps_the_kernel_it_was_created_with(tmp_path):
    root = tmp_path / "slab-workspace"
    created = call("create_workspace", root=str(root), name="Slab", kernel="slab")
    assert created["project"]["kernel"] == "slab"
    assert created["project"]["resolutionUm"] == pytest.approx(0.01)
    reopened = call("open_workspace", root=str(root))
    assert reopened["project"]["kernel"] == "slab"


def test_an_unknown_kernel_is_rejected_rather_than_defaulted(tmp_path):
    with pytest.raises(InvalidRequest, match="unknown kernel"):
        call("create_workspace", root=str(tmp_path / "ws"), name="Nope", kernel="quantum")


def test_saving_a_document_cannot_move_a_project_to_another_kernel(workspace):
    """A stored result of one kernel is not a result of another."""
    document = call("open_workspace", root=str(workspace))
    document["project"]["kernel"] = "levelset"
    with pytest.raises(InvalidRequest, match="cannot be moved"):
        call("save_document", root=str(workspace), document=document)
    unchanged = call("open_workspace", root=str(workspace))
    assert unchanged["project"]["kernel"] == "slab"


def test_a_slab_project_runs_its_flow_and_draws_every_view(tmp_path):
    root = tmp_path / "slab-run"
    document = call("create_workspace", root=str(root), name="Slab", kernel="slab")
    branch = document["branches"][0]
    result = call("run_flow", root=str(root), branchId=branch["id"])
    assert len(result["executedStepIds"]) == len(branch["steps"])
    assert result["materials"] == ["Si", "Al2O3"]
    last = branch["steps"][-1]["id"]
    surfaces = call("get_surfaces", root=str(root), branchId=branch["id"], stepId=last)
    assert [surface["material"] for surface in surfaces["surfaces"]] == ["Si", "Al2O3"]
    assert surfaces["exact"] is True
    section = call("get_section", root=str(root), branchId=branch["id"], stepId=last, axis="y")
    assert base64.b64decode(section["image"])[:4] == b"\x89PNG"
    top = call("get_top_view", root=str(root), branchId=branch["id"], stepId=last)
    assert base64.b64decode(top["image"])[:4] == b"\x89PNG"


@pytest.mark.parametrize("kernel", ["slab"])
def test_a_section_can_run_along_any_line(tmp_path, kernel):
    """The AA–BB cut: a diagonal through the trench shows the trench."""
    root = tmp_path / f"line-{kernel}"
    document = call("create_workspace", root=str(root), name="Line", kernel=kernel)
    branch = document["branches"][0]
    call("run_flow", root=str(root), throughStepId=branch["steps"][1]["id"])
    etched = branch["steps"][1]["id"]
    diagonal = call(
        "get_section", root=str(root), branchId=branch["id"], stepId=etched,
        line={"start": [-0.6, -0.6], "end": [0.6, 0.6]},
    )
    assert diagonal["axis"] == "line"
    assert diagonal["horizontalAxis"] == "s"
    assert diagonal["extent"]["horizontalMax"] == pytest.approx(1.2 * 2**0.5)
    assert diagonal["line"] == {"start": [-0.6, -0.6], "end": [0.6, 0.6]}
    image = Image.open(io.BytesIO(base64.b64decode(diagonal["image"]))).convert("RGB")
    width, height = image.size
    # The trench is a 0.22 um circle at the origin, so the middle of the cut
    # is open just under the wafer surface (z = -0.02, the frame runs from
    # z = 0.4 at the top row down to -0.8) while the ends are still silicon.
    surface_row = int(round((0.4 + 0.02) / 1.2 * (height - 1)))
    background = image.getpixel((width // 2, surface_row))
    silicon = image.getpixel((2, surface_row))
    assert background != silicon
    with pytest.raises(InvalidRequest):
        call(
            "get_section", root=str(root), branchId=branch["id"], stepId=etched,
            line={"start": [0.0, 0.0], "end": [0.0, 0.0]},
        )


def test_a_project_built_on_the_retired_kernel_is_refused_with_directions(tmp_path):
    """What the level-set projects made before it was removed now do.

    The flow is still readable -- the document opens, so the steps can be
    looked at and copied out -- but nothing will run on this kernel, and
    the message says where such a project can still be opened.
    """
    root = tmp_path / "old-project"
    call("create_workspace", root=str(root), name="Old", kernel="slab")
    repository = open_repository(root)
    with repository.connect() as connection:
        connection.execute("UPDATE projects SET kernel='levelset'")

    document = call("open_workspace", root=str(root))
    assert document["project"]["kernel"] == "levelset"
    branch = document["branches"][0]
    with pytest.raises(WorkspaceError, match="removed after 0.9.8"):
        call("run_flow", root=str(root))
    with pytest.raises(WorkspaceError, match="removed after 0.9.8"):
        call("get_top_view", root=str(root), branchId=branch["id"], stepId=branch["steps"][0]["id"])


def test_an_unknown_kernel_in_a_stored_project_says_so(tmp_path):
    root = tmp_path / "strange"
    call("create_workspace", root=str(root), name="Strange", kernel="slab")
    repository = open_repository(root)
    with repository.connect() as connection:
        connection.execute("UPDATE projects SET kernel='quantum'")
    with pytest.raises(WorkspaceError, match="unknown kernel"):
        call("run_flow", root=str(root))


def test_a_slab_project_sets_a_resolution_rather_than_a_grid(tmp_path):
    root = tmp_path / "slab-resolution"
    call("create_workspace", root=str(root), name="Slab", kernel="slab")
    call("run_flow", root=str(root))
    plan = call("plan_grid", root=str(root), targetSpacingNm=2.0)
    assert plan["spacingRole"] == "conformal_resolution"
    assert plan["estimate"] == {"spacingNm": 2.0, "spacingXyNm": 2.0}
    # The XY arc sagitta can be set apart from the z step, and unset again.
    plan = call("plan_grid", root=str(root), targetSpacingNm=2.0, targetSpacingXyNm=10.0)
    assert plan["estimate"] == {"spacingNm": 2.0, "spacingXyNm": 10.0}
    split = call("set_grid", root=str(root), targetSpacingNm=2.0, targetSpacingXyNm=10.0)
    assert split["project"]["resolutionUm"] == pytest.approx(0.002)
    assert split["project"]["resolutionXyUm"] == pytest.approx(0.01)
    assert call("plan_grid", root=str(root), targetSpacingNm=2.0, targetSpacingXyNm=10.0)["unchanged"] is True
    assert call("plan_grid", root=str(root), targetSpacingNm=2.0)["unchanged"] is False
    joined = call("set_grid", root=str(root), targetSpacingNm=2.0)
    assert joined["project"]["resolutionXyUm"] is None
    # 2 nm over this window is far past the level-set node ceiling; without a
    # field there is nothing for that ceiling to apply to.
    assert plan["withinLimit"] is True
    updated = call("set_grid", root=str(root), targetSpacingNm=2.0)
    assert updated["project"]["resolutionUm"] == pytest.approx(0.002)
    assert set(updated["stepStatuses"][updated["branches"][0]["id"]].values()) == {"dirty"}


def test_open_workspace_without_a_database_is_rejected(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(WorkspaceError):
        call("open_workspace", root=str(empty))


def test_unknown_method_is_rejected():
    with pytest.raises(InvalidRequest):
        call("teleport")


def test_every_step_starts_dirty_and_turns_clean_after_a_run(workspace):
    document = call("open_workspace", root=str(workspace))
    branch = document["branches"][0]
    assert set(document["stepStatuses"][branch["id"]].values()) == {"dirty"}

    result, events = call_with_events("run_flow", root=str(workspace))
    assert len(result["executedStepIds"]) == len(branch["steps"])
    assert result["cachedStepIds"] == []
    assert set(result["stepStatuses"].values()) == {"clean"}
    assert any(event["message"].startswith("Running") for event in events)


def test_a_second_run_reuses_every_snapshot(workspace):
    call("run_flow", root=str(workspace))
    result = call("run_flow", root=str(workspace))
    assert result["executedStepIds"] == []
    assert len(result["cachedStepIds"]) == 4


def test_force_recomputes_even_when_the_digest_matches(workspace):
    call("run_flow", root=str(workspace))
    result = call("run_flow", root=str(workspace), force=True)
    assert result["cachedStepIds"] == []
    assert len(result["executedStepIds"]) == 4


def test_editing_a_step_invalidates_it_and_everything_after(workspace):
    call("run_flow", root=str(workspace))
    document = call("open_workspace", root=str(workspace))
    branch = document["branches"][0]
    branch["steps"][1]["parameters"]["target"] = 0.2
    saved = call("save_document", root=str(workspace), document=document)
    statuses = saved["stepStatuses"][branch["id"]]
    assert statuses[branch["steps"][0]["id"]] == "clean"
    # Their stored results are no longer current, but they are still results.
    assert [statuses[step["id"]] for step in branch["steps"][1:]] == ["stale"] * 3

    result = call("run_flow", root=str(workspace))
    assert result["cachedStepIds"] == [branch["steps"][0]["id"]]
    assert len(result["executedStepIds"]) == 3


def test_a_step_that_never_ran_is_reported_separately_from_a_stale_one(workspace):
    """The views need the difference: stale has something to show, dirty does not."""
    document = call("open_workspace", root=str(workspace))
    branch = document["branches"][0]
    second = branch["steps"][1]["id"]
    call("run_flow", root=str(workspace), throughStepId=second)
    document = call("open_workspace", root=str(workspace))
    document["branches"][0]["steps"][0]["parameters"]["target"] = 0.11
    saved = call("save_document", root=str(workspace), document=document)
    statuses = saved["stepStatuses"][branch["id"]]
    assert [statuses[step["id"]] for step in branch["steps"]] == [
        "stale",
        "stale",
        "dirty",
        "dirty",
    ]


def test_renaming_a_step_does_not_invalidate_its_result(workspace):
    call("run_flow", root=str(workspace))
    document = call("open_workspace", root=str(workspace))
    branch = document["branches"][0]
    branch["steps"][0]["name"] = "Litho (renamed)"
    saved = call("save_document", root=str(workspace), document=document)
    assert set(saved["stepStatuses"][branch["id"]].values()) == {"clean"}


def test_deleting_a_step_drops_its_snapshot_and_digest(workspace):
    call("run_flow", root=str(workspace))
    document = call("open_workspace", root=str(workspace))
    branch = document["branches"][0]
    removed = branch["steps"].pop(1)
    saved = call("save_document", root=str(workspace), document=document)
    remaining = saved["branches"][0]
    assert all(step["id"] != removed["id"] for step in remaining["steps"])

    repository = open_repository(workspace)
    assert removed["id"] not in DigestCache(repository).load(branch["id"])
    with repository.connect() as connection:
        rows = connection.execute(
            "SELECT step_id FROM branch_snapshots WHERE branch_id=?", (branch["id"],)
        ).fetchall()
    assert all(row["step_id"] != removed["id"] for row in rows)


def test_save_document_refuses_a_grid_change(workspace):
    document = call("open_workspace", root=str(workspace))
    document["project"]["grid"]["nx"] = 45
    with pytest.raises(InvalidRequest):
        call("save_document", root=str(workspace), document=document)


def test_recipe_and_material_deletions_reach_the_database(workspace):
    document = call("open_workspace", root=str(workspace))
    document["recipes"] = [
        recipe for recipe in document["recipes"] if recipe["id"] != "recipe-boe"
    ]
    document["materials"] = [
        material for material in document["materials"] if material["name"] != "SiN"
    ]
    saved = call("save_document", root=str(workspace), document=document)
    assert all(recipe["id"] != "recipe-boe" for recipe in saved["recipes"])
    assert all(material["name"] != "SiN" for material in saved["materials"])


def test_material_color_edits_survive_a_round_trip(workspace):
    document = call("open_workspace", root=str(workspace))
    document["materials"][0]["color"] = "#123456"
    document["materials"][0]["opacity"] = 0.5
    saved = call("save_document", root=str(workspace), document=document)
    stored = next(item for item in saved["materials"] if item["id"] == document["materials"][0]["id"])
    assert stored["color"] == "#123456"
    assert stored["opacity"] == 0.5


def test_views_render_the_stored_result(workspace):
    result = call("run_flow", root=str(workspace))
    branch_id = result["branchId"]
    last_step = result["executedStepIds"][-1]

    surfaces = call(
        "get_surfaces",
        root=str(workspace),
        branchId=branch_id,
        stepId=last_step,
        interpolation=2,
    )
    # The geometry is exact, so sampling is not something a surface has:
    # what comes back is the kernel's own triangles, once.
    assert surfaces["interpolation"] == 1 and surfaces["exact"] is True
    assert surfaces["surfaces"], "the finished stack should have at least one surface"
    surface = surfaces["surfaces"][0]
    positions = np.frombuffer(base64.b64decode(surface["positions"]), dtype=np.float32)
    indices = np.frombuffer(base64.b64decode(surface["indices"]), dtype=np.uint32)
    assert positions.size == surface["vertexCount"] * 3
    assert indices.size == surface["triangleCount"] * 3
    assert indices.max() < surface["vertexCount"]
    assert surface["color"].startswith("#")

    section = call(
        "get_section", root=str(workspace), branchId=branch_id, stepId=last_step, interpolation=2
    )
    image = Image.open(io.BytesIO(base64.b64decode(section["image"])))
    assert image.size == (section["width"], section["height"])
    assert section["axis"] == "y"

    top = call("get_top_view", root=str(workspace), branchId=branch_id, stepId=last_step)
    assert Image.open(io.BytesIO(base64.b64decode(top["image"]))).size == (top["width"], top["height"])


def test_section_axis_and_interpolation_are_validated(workspace):
    result = call("run_flow", root=str(workspace))
    common = {
        "root": str(workspace),
        "branchId": result["branchId"],
        "stepId": result["executedStepIds"][-1],
    }
    with pytest.raises(InvalidRequest):
        call("get_section", **common, axis="z")
    with pytest.raises(InvalidRequest):
        call("get_section", **common, interpolation=9)


def test_views_before_the_first_step_show_the_bare_wafer(workspace):
    document = call("open_workspace", root=str(workspace))
    branch_id = document["branches"][0]["id"]
    top = call("get_top_view", root=str(workspace), branchId=branch_id, stepId="")
    # The bare wafer: a picture of the whole window, all one material.
    window = document["project"]["grid"]
    assert top["width"] > 0 and top["height"] > 0
    assert top["extent"]["horizontalMin"] == pytest.approx(window["xMin"])
    assert top["extent"]["verticalMax"] == pytest.approx(window["yMax"])


def test_a_step_without_a_result_reports_a_useful_error(workspace):
    document = call("open_workspace", root=str(workspace))
    branch = document["branches"][0]
    with pytest.raises(InvalidRequest, match="Run the flow first"):
        call(
            "get_top_view",
            root=str(workspace),
            branchId=branch["id"],
            stepId=branch["steps"][0]["id"],
        )


def test_run_to_a_step_stops_there(workspace):
    document = call("open_workspace", root=str(workspace))
    branch = document["branches"][0]
    second = branch["steps"][1]["id"]
    result = call("run_flow", root=str(workspace), throughStepId=second)
    assert result["executedStepIds"] == [branch["steps"][0]["id"], second]
    statuses = result["stepStatuses"]
    assert [statuses[step["id"]] for step in branch["steps"]] == ["clean", "clean", "dirty", "dirty"]


def test_run_to_an_unknown_step_is_rejected(workspace):
    with pytest.raises(InvalidRequest):
        call("run_flow", root=str(workspace), throughStepId="not-a-step")


def test_sketch_edits_invalidate_the_steps_that_use_them(workspace):
    call("run_flow", root=str(workspace))
    document = call("save_sketch", root=str(workspace), sketchId="default", sketch={
        "name": "default",
        "shapes": [
            {"kind": "circle", "operation": "merge", "parameters": {"center": [0.0, 0.0], "radius": 0.3}, "array": [1, 1, 0.0, 0.0]}
        ],
    })
    branch = document["branches"][0]
    statuses = document["stepStatuses"][branch["id"]]
    assert statuses[branch["steps"][0]["id"]] == "stale"
    assert document["sketches"][0]["shapes"][0]["parameters"]["radius"] == 0.3


def test_recipes_round_trip_through_excel(workspace, tmp_path):
    destination = tmp_path / "recipes.xlsx"
    exported = call("export_recipes_xlsx", root=str(workspace), destination=str(destination))
    assert destination.is_file() and exported["path"] == str(destination)
    imported = call("import_recipes_xlsx", root=str(workspace), source=str(destination))
    assert imported["imported"] >= 1


def test_export_requires_an_xlsx_name(workspace, tmp_path):
    with pytest.raises(InvalidRequest):
        call("export_recipes_xlsx", root=str(workspace), destination=str(tmp_path / "recipes.csv"))


def test_import_gds_stores_the_layout_and_reports_layers(workspace, tmp_path):
    gdstk = pytest.importorskip("gdstk")
    library = gdstk.Library()
    cell = library.new_cell("TOP")
    cell.add(gdstk.rectangle((-0.2, -0.2), (0.2, 0.2), layer=3, datatype=1))
    source = tmp_path / "layout.gds"
    library.write_gds(source)

    result = call("import_gds", root=str(workspace), source=str(source))
    assert result["layers"] == [{"layer": 3, "datatype": 1}]
    assert (workspace / "layouts").is_dir()
    assert result["document"]["project"]["gdsPath"].endswith("layout.gds")
    assert call("gds_layers", root=str(workspace))["layers"] == result["layers"]


def test_import_gds_rejects_a_non_gds_file(workspace, tmp_path):
    source = tmp_path / "layout.txt"
    source.write_text("not a layout", encoding="utf-8")
    with pytest.raises(InvalidRequest):
        call("import_gds", root=str(workspace), source=str(source))


def test_serve_answers_line_by_line_and_reports_errors(tmp_path):
    root = tmp_path / "workspace"
    initialize_workspace(root, "Served")
    requests = "\n".join(
        [
            json.dumps({"kind": "request", "id": "a", "method": "ping"}),
            "",
            json.dumps({"kind": "request", "id": "b", "method": "nope"}),
            json.dumps({"kind": "request", "id": "c", "method": "open_workspace", "params": {"root": str(root)}}),
            "{not json",
        ]
    )
    output = io.StringIO()
    assert serve(io.StringIO(requests), output) == 0
    answers = {
        json.loads(line)["id"]: json.loads(line) for line in output.getvalue().splitlines()
    }
    # Requests are answered in order; a line that is not even JSON is refused
    # by the reader as soon as it arrives, ahead of whatever is still queued.
    executed = [answer["id"] for answer in map(json.loads, output.getvalue().splitlines()) if answer["id"]]
    assert executed == ["a", "b", "c"]
    assert [answers[key]["ok"] for key in ("a", "b", "c", None)] == [True, False, True, False]
    assert answers["b"]["error"]["code"] == "InvalidRequest"


def _server_over_pipe():
    """A server reading from a pipe we write to, with its output captured."""
    import threading
    from process_studio.worker.server import Server

    read_end, write_end = os.pipe()
    input_stream = os.fdopen(read_end, "r")
    writer = os.fdopen(write_end, "w")
    output = io.StringIO()
    server = Server(input_stream, output)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return writer, output, thread


def _responses(output):
    return {
        message["id"]: message
        for message in map(json.loads, output.getvalue().splitlines())
        if message["kind"] == "response"
    }


def _wait_for(output, request_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if request_id in _responses(output):
            return True
        time.sleep(0.02)
    return False


def test_a_newer_view_request_supersedes_a_queued_one(workspace, monkeypatch):
    """Five quick clicks should cost one computation, not five: while one
    view is being drawn, the ones queued behind it collapse to the newest."""
    import threading

    document = call("open_workspace", root=str(workspace))
    branch = document["branches"][0]
    call("run_flow", root=str(workspace))
    # The first view holds until told to go on, so the rest queue behind it.
    release = threading.Event()
    from process_studio.worker import protocol

    real_dispatch = protocol.dispatch

    def held_dispatch(request, output, cancel=None):
        result = real_dispatch(request, output, cancel=cancel)
        if request.get("id") == "view-0":
            release.wait(timeout=60)
        return result

    monkeypatch.setattr(protocol, "dispatch", held_dispatch)
    writer, output, thread = _server_over_pipe()
    for index, step in enumerate(branch["steps"]):
        writer.write(json.dumps({
            "id": f"view-{index}", "method": "get_section",
            "params": {"root": str(workspace), "branchId": branch["id"], "stepId": step["id"], "axis": "y"},
        }) + "\n")
        writer.flush()
        time.sleep(0.05)  # let the reader take each line before the next arrives
    release.set()
    assert _wait_for(output, "view-3")
    writer.close()
    thread.join(timeout=120)
    assert not thread.is_alive()
    answers = _responses(output)
    superseded = [key for key, answer in answers.items() if not answer["ok"] and answer["error"]["code"] == "Superseded"]
    assert sorted(superseded) == ["view-1", "view-2"]
    assert answers["view-0"]["ok"] and answers["view-3"]["ok"] and "image" in answers["view-3"]["result"]


def test_a_finished_step_can_be_viewed_while_a_run_is_under_way(workspace, monkeypatch):
    """A run holds the main lane for as long as it takes; the views have a
    lane of their own, so the steps that are done can be looked at meanwhile."""
    import threading
    from process_studio.kernels.slab import SlabKernel

    document = call("open_workspace", root=str(workspace))
    branch = document["branches"][0]
    call("run_flow", root=str(workspace))  # every step stored
    # The re-run's first step holds until the view has been answered.
    release = threading.Event()
    step_started = threading.Event()
    real_run_step = SlabKernel.run_step

    def held_run_step(self, state, step, **kwargs):
        step_started.set()
        assert release.wait(timeout=120), "the view never arrived"
        return real_run_step(self, state, step, **kwargs)

    monkeypatch.setattr(SlabKernel, "run_step", held_run_step)
    writer, output, thread = _server_over_pipe()
    writer.write(json.dumps({"id": "run", "method": "run_flow", "params": {"root": str(workspace), "force": True}}) + "\n")
    writer.flush()
    assert step_started.wait(timeout=60)
    last = branch["steps"][-1]["id"]
    writer.write(json.dumps({
        "id": "view", "method": "get_section",
        "params": {"root": str(workspace), "branchId": branch["id"], "stepId": last, "axis": "y"},
    }) + "\n")
    writer.flush()
    # The view is answered while the run is still on its first step.
    assert _wait_for(output, "view", timeout=60)
    assert "run" not in _responses(output)
    release.set()
    assert _wait_for(output, "run")
    writer.close()
    thread.join(timeout=120)
    answers = _responses(output)
    assert answers["view"]["ok"] and "image" in answers["view"]["result"]
    assert answers["run"]["ok"]


def test_a_run_can_be_cancelled_between_steps_and_keeps_what_ran(workspace, monkeypatch):
    import threading
    from process_studio.kernels.slab import SlabKernel
    from process_studio.worker.server import Server

    # The first step holds until the cancel has been sent, so the outcome does
    # not depend on how fast the machine runs the starter flow: on a fast one
    # the whole flow would otherwise finish before the cancel arrived.
    cancel_sent = threading.Event()
    step_started = threading.Event()
    real_run_step = SlabKernel.run_step

    def held_run_step(self, state, step, **kwargs):
        step_started.set()
        assert cancel_sent.wait(timeout=60), "the test never sent its cancel"
        return real_run_step(self, state, step, **kwargs)

    monkeypatch.setattr(SlabKernel, "run_step", held_run_step)

    document = call("open_workspace", root=str(workspace))
    branch = document["branches"][0]
    read_end, write_end = os.pipe()
    input_stream = os.fdopen(read_end, "r")
    writer = os.fdopen(write_end, "w")
    output = io.StringIO()
    server = Server(input_stream, output)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    writer.write(json.dumps({"id": "run", "method": "run_flow", "params": {"root": str(workspace)}}) + "\n")
    writer.flush()
    assert step_started.wait(timeout=60)
    writer.write(json.dumps({"kind": "cancel", "id": "run"}) + "\n")
    writer.flush()
    cancel_sent.set()
    writer.close()
    thread.join(timeout=120)
    answers = [m for m in map(json.loads, output.getvalue().splitlines()) if m["kind"] == "response"]
    assert answers[0]["ok"] is False
    assert answers[0]["error"]["code"] == "Cancelled"
    statuses = call("open_workspace", root=str(workspace))["stepStatuses"][branch["id"]]
    ran = [step["id"] for step in branch["steps"] if statuses[step["id"]] == "clean"]
    # The step that was running when the cancel arrived finished and stayed
    # stored; the ones after it never started.
    assert ran == [branch["steps"][0]["id"]]


def test_a_material_can_be_added_and_then_renamed(workspace):
    """Adding a material then renaming it used to fail every autosave."""
    document = call("open_workspace", root=str(workspace))
    document["materials"].append(
        {"id": "material-new", "name": "New material", "category": "Other", "color": "#7c83a0", "opacity": 1.0}
    )
    saved = call("save_document", root=str(workspace), document=document)
    names = {material["id"]: material["name"] for material in saved["materials"]}
    assert names["material-new"] == "New material"
    for material in saved["materials"]:
        if material["id"] == "material-new":
            material["name"] = "HfO2"
    renamed = call("save_document", root=str(workspace), document=saved)
    names = {material["id"]: material["name"] for material in renamed["materials"]}
    assert names["material-new"] == "HfO2"
    assert "New material" not in names.values()
    # Two materials cannot share a name; the second one is refused, by name.
    for material in renamed["materials"]:
        if material["id"] == "material-new":
            material["name"] = "Si"
    with pytest.raises(ValueError, match="named 'Si' already exists"):
        call("save_document", root=str(workspace), document=renamed)


def test_a_sketch_can_be_previewed_before_it_is_saved(workspace):
    """The editor's fill is the kernel's own CSG over the project window."""
    preview = call(
        "preview_mask", root=str(workspace),
        sketch={"name": "draft", "shapes": [
            {"kind": "rectangle", "operation": "merge", "parameters": {"center": [0, 0], "size": [0.8, 0.8]}, "array": [1, 1, 0, 0]},
            {"kind": "circle", "operation": "subtract", "parameters": {"center": [0, 0], "radius": 0.2}, "array": [1, 1, 0, 0]},
        ]},
    )
    image = Image.open(io.BytesIO(base64.b64decode(preview["image"]))).convert("RGBA")
    width, height = image.size
    assert preview["extent"]["horizontalMin"] == pytest.approx(-0.8)
    # The square is exposed, its centre is carved out, the corners are not.
    assert image.getpixel((int(width * 0.7), int(height * 0.5)))[3] > 0
    assert image.getpixel((width // 2, height // 2))[3] == 0
    assert image.getpixel((2, 2))[3] == 0
    square = 0.8 * 0.8 - 3.14159 * 0.2**2
    assert preview["exposedFraction"] == pytest.approx(square / (1.6 * 1.6), abs=0.01)
    flipped = call(
        "preview_mask", root=str(workspace), keep="outside",
        sketch={"name": "draft", "shapes": [
            {"kind": "circle", "operation": "merge", "parameters": {"center": [0, 0], "radius": 0.2}, "array": [1, 1, 0, 0]},
        ]},
    )
    assert flipped["exposedFraction"] == pytest.approx(1 - 3.14159 * 0.04 / 2.56, abs=0.01)
    with pytest.raises(InvalidRequest, match="three points"):
        call(
            "preview_mask", root=str(workspace),
            sketch={"name": "bad", "shapes": [
                {"kind": "polygon", "operation": "merge", "parameters": {"points": [[0, 0], [1, 1]]}, "array": [1, 1, 0, 0]},
            ]},
        )


def test_a_film_taller_than_the_window_is_refused_with_the_unit(workspace):
    document = call("open_workspace", root=str(workspace))
    branch = document["branches"][0]
    branch["steps"][3]["parameters"]["target"] = 50
    call("save_document", root=str(workspace), document=document)
    with pytest.raises(ValueError, match="micrometres: 50 nm is 0.05"):
        call("run_flow", root=str(workspace))


def test_the_project_window_can_be_resized(workspace):
    """A taller window, for a thicker stack; the stored results go with it."""
    call("run_flow", root=str(workspace))
    bounds = {"xMin": -1.0, "xMax": 1.0, "yMin": -1.0, "yMax": 1.0, "zMin": -1.0, "zMax": 2.0}
    plan = call("plan_grid", root=str(workspace), targetSpacingNm=25.0, bounds=bounds)
    assert plan["grid"]["zMax"] == pytest.approx(2.0)
    assert plan["grid"]["nz"] == 121 and plan["grid"]["nx"] == 81
    assert plan["unchanged"] is False
    updated = call("set_grid", root=str(workspace), targetSpacingNm=25.0, bounds=bounds)
    grid = updated["project"]["grid"]
    assert (grid["zMin"], grid["zMax"]) == (-1.0, 2.0)
    assert grid["spacingUm"] == pytest.approx(0.025)
    assert set(updated["stepStatuses"][updated["branches"][0]["id"]].values()) == {"dirty"}
    # A 1.5 µm film now fits where the 1.2 µm window refused it.
    document = call("open_workspace", root=str(workspace))
    document["branches"][0]["steps"][3]["parameters"]["target"] = 1.5
    call("save_document", root=str(workspace), document=document)
    call("run_flow", root=str(workspace))


def test_a_window_that_misses_the_wafer_surface_is_refused(workspace):
    with pytest.raises(InvalidRequest, match="wafer surface is z = 0"):
        call(
            "plan_grid", root=str(workspace), targetSpacingNm=25.0,
            bounds={"xMin": -1, "xMax": 1, "yMin": -1, "yMax": 1, "zMin": 0.5, "zMax": 2.0},
        )
    with pytest.raises(InvalidRequest, match="micrometres"):
        call(
            "plan_grid", root=str(workspace), targetSpacingNm=25.0,
            bounds={"xMin": -500, "xMax": 500, "yMin": -1, "yMax": 1, "zMin": -1, "zMax": 1},
        )


def test_a_slab_window_can_be_resized_too(tmp_path):
    root = tmp_path / "slab-window"
    call("create_workspace", root=str(root), name="Slab", kernel="slab")
    updated = call(
        "set_grid", root=str(root), targetSpacingNm=10.0,
        bounds={"xMin": -2, "xMax": 2, "yMin": -2, "yMax": 2, "zMin": -0.5, "zMax": 3.0},
    )
    grid = updated["project"]["grid"]
    assert (grid["xMin"], grid["zMax"]) == (-2.0, 3.0)
    assert updated["project"]["resolutionUm"] == pytest.approx(0.01)
    # The substrate is as deep as the window: 0.5 µm now, its top still at 0.
    result = call("run_flow", root=str(root))
    assert result["materials"] == ["Si", "Al2O3"]


def test_project_id_cannot_be_swapped(workspace):
    document = call("open_workspace", root=str(workspace))
    document["project"]["id"] = "someone-elses-project"
    with pytest.raises(InvalidRequest):
        call("save_document", root=str(workspace), document=document)


def test_load_project_prefers_the_default_project(workspace):
    repository = open_repository(workspace)
    assert load_project(repository).id == "default-project"


def test_a_library_recipe_can_be_deleted_without_changing_existing_steps(workspace):
    document = call("open_workspace", root=str(workspace))
    etch = document["branches"][0]["steps"][1]
    original_definition = {
        key: etch[key]
        for key in ("processType", "tool", "outputMaterial", "parameters", "materialResponses")
    }
    document["recipes"] = [
        recipe for recipe in document["recipes"] if recipe["id"] != "recipe-si-trench"
    ]
    saved = call("save_document", root=str(workspace), document=document)
    saved_etch = saved["branches"][0]["steps"][1]
    assert all(recipe["id"] != "recipe-si-trench" for recipe in saved["recipes"])
    assert {
        key: saved_etch[key]
        for key in ("processType", "tool", "outputMaterial", "parameters", "materialResponses")
    } == original_definition


def test_opening_a_legacy_workspace_detaches_steps_from_library(workspace):
    repository = open_repository(workspace)
    stored = repository.load_branch("default-main")
    legacy = stored.steps[1]
    legacy.recipe_id = "recipe-si-trench"
    legacy.overrides = {"target": 0.27, "sketch_id": "default"}
    legacy.process_type = None
    legacy.tool = ""
    legacy.output_material = None
    legacy.parameters = {}
    legacy.material_responses = {}
    repository.save_branch("default-project", stored)

    document = call("open_workspace", root=str(workspace))
    assert all("recipeId" not in step for step in document["branches"][0]["steps"])
    migrated = repository.load_branch("default-main")
    assert all(step.recipe_id is None and step.process_type is not None for step in migrated.steps)
    assert migrated.steps[1].parameters["target"] == 0.27


def test_running_to_a_step_keeps_later_valid_results(workspace):
    call("run_flow", root=str(workspace))
    document = call("open_workspace", root=str(workspace))
    branch = document["branches"][0]
    # Nothing changed, so a partial run must not invalidate the tail.
    result = call("run_flow", root=str(workspace), throughStepId=branch["steps"][1]["id"])
    assert result["executedStepIds"] == []
    assert set(result["stepStatuses"].values()) == {"clean"}


def test_plan_grid_reports_the_resolution_and_whether_it_would_change_anything(workspace):
    """There is no lattice: the number is the resolution, and it costs nothing."""
    plan = call("plan_grid", root=str(workspace), targetSpacingNm=25.0)

    assert plan["kernel"] == "slab"
    assert plan["spacingRole"] == "conformal_resolution"
    assert plan["estimate"]["spacingNm"] == pytest.approx(25.0)
    assert plan["estimate"]["spacingXyNm"] == pytest.approx(25.0)
    assert plan["maximumNodes"] is None and plan["withinLimit"] is True
    # Applying discards every stored result, so "it is already this" matters.
    assert plan["unchanged"] is False
    assert call("plan_grid", root=str(workspace), targetSpacingNm=10.0)["unchanged"] is True


def test_plan_grid_rejects_a_spacing_outside_the_supported_range(workspace):
    for spacing in (0.05, 2000.0, "coarse"):
        with pytest.raises(InvalidRequest):
            call("plan_grid", root=str(workspace), targetSpacingNm=spacing)


def test_set_grid_applies_a_resolution_and_discards_what_was_computed_at_the_old_one(workspace):
    call("run_flow", root=str(workspace))

    updated = call("set_grid", root=str(workspace), targetSpacingNm=25.0)

    assert updated["project"]["resolutionUm"] == pytest.approx(0.025)
    assert set(updated["stepStatuses"][updated["branches"][0]["id"]].values()) == {"dirty"}


def test_set_grid_needs_a_resolution_to_set(workspace):
    document = call("open_workspace", root=str(workspace))
    with pytest.raises(InvalidRequest, match="no grid to set"):
        call("set_grid", root=str(workspace), grid=document["project"]["grid"])


def test_a_window_of_any_shape_is_allowed(workspace):
    """The window is bounds. Nothing has to divide anything any more.

    A 200 nm substrate under a 1.6 um window is an awkward set of spans for
    a lattice and a perfectly ordinary wafer, so it has to be accepted.
    """
    bounds = {"xMin": -0.8, "xMax": 0.8, "yMin": -0.8, "yMax": 0.8, "zMin": -0.2, "zMax": 0.4}
    updated = call("set_grid", root=str(workspace), targetSpacingNm=10.0, bounds=bounds)

    grid = updated["project"]["grid"]
    assert (grid["zMin"], grid["zMax"]) == (-0.2, 0.4)
    # The substrate is as thick as the window is deep: 200 nm.
    document = call("open_workspace", root=str(workspace))
    assert document["project"]["grid"]["zMin"] == -0.2


def test_the_views_can_be_written_to_files(tmp_path, workspace):
    """A mesh of the 3D surfaces in a few formats, and a picture as PNG."""
    document = call("open_workspace", root=str(workspace))
    etch = document["branches"][0]["steps"][1]["id"]
    call("run_flow", root=str(workspace), branchId="default-main", throughStepId=etch)
    for suffix in ("glb", "obj", "stl"):
        destination = tmp_path / f"etch.{suffix}"
        result = call(
            "export_mesh", root=str(workspace), branchId="default-main", stepId=etch,
            destination=str(destination),
        )
        assert Path(result["path"]) == destination and destination.stat().st_size > 0
        assert result["triangles"]["Si"] > 0
    with pytest.raises(InvalidRequest):
        call("export_mesh", root=str(workspace), branchId="default-main", stepId=etch,
             destination=str(tmp_path / "etch.txt"))

    section = call("get_section", root=str(workspace), branchId="default-main", stepId=etch, axis="y")
    picture = tmp_path / "pictures" / "cut.png"
    saved = call("save_image", destination=str(picture), image=section["image"])
    assert picture.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and saved["bytes"] == picture.stat().st_size
    with pytest.raises(InvalidRequest):
        call("save_image", destination=str(tmp_path / "x.png"), image="bm90IGEgcG5n")


def test_named_section_lines_live_in_the_project(workspace):
    """The AA–BB lines a user saves come back with the document, are checked,
    and stay out of the way of everything else."""
    document = call("open_workspace", root=str(workspace))
    assert document["project"]["sectionLines"] == []
    document["project"]["sectionLines"] = [
        {"id": "diag", "name": "Diagonal", "start": [-0.5, -0.5], "end": [0.5, 0.5]},
        {"name": "Across", "start": [-0.8, 0.1], "end": [0.8, 0.1]},
    ]
    saved = call("save_document", root=str(workspace), document=document)
    lines = saved["project"]["sectionLines"]
    assert [line["name"] for line in lines] == ["Diagonal", "Across"]
    assert lines[0]["id"] == "diag" and lines[1]["id"]
    assert call("open_workspace", root=str(workspace))["project"]["sectionLines"] == lines
    document["project"]["sectionLines"] = [{"name": "Nowhere", "start": [0, 0], "end": [0, 0]}]
    with pytest.raises(InvalidRequest):
        call("save_document", root=str(workspace), document=document)


def test_recipes_have_groups_and_tools_have_a_library(workspace, tmp_path):
    """Recipes sit in groups below their type; tools are a grouped library the
    document carries and the save writes back, and both survive Excel."""
    document = call("open_workspace", root=str(workspace))
    groups = {recipe["name"]: recipe["group"] for recipe in document["recipes"]}
    assert groups["Conformal Al2O3"] == "ALD" and groups["BOE Oxide Etch"] == "Wet"
    assert {tool["name"]: tool["group"] for tool in document["tools"]}["ICP-RIE"] == "Etch/Dry"

    # A subgroup path is trimmed; a new tool joins, a renamed one keeps its id.
    trench = next(recipe for recipe in document["recipes"] if recipe["name"] == "Si Directional Trench Etch")
    trench["group"] = " ALD / Oxides "
    document["tools"].append({"name": "Sputter-2", "group": "Deposition/PVD", "notes": "Ar only"})
    ald = next(tool for tool in document["tools"] if tool["name"] == "ALD")
    ald["name"] = "ALD-1"
    saved = call("save_document", root=str(workspace), document=document)
    assert next(r for r in saved["recipes"] if r["id"] == trench["id"])["group"] == "ALD/Oxides"
    tools = {tool["name"]: tool for tool in saved["tools"]}
    assert tools["Sputter-2"]["group"] == "Deposition/PVD" and tools["Sputter-2"]["id"]
    assert tools["ALD-1"]["id"] == ald["id"] and "ALD" not in tools
    # Dropping a tool from the document removes it.
    saved["tools"] = [tool for tool in saved["tools"] if tool["name"] != "Ash"]
    assert "Ash" not in {tool["name"] for tool in call("save_document", root=str(workspace), document=saved)["tools"]}
    # Two tools cannot share a name.
    saved["tools"].append({"name": "ICP-RIE", "group": ""})
    with pytest.raises(InvalidRequest):
        call("save_document", root=str(workspace), document=saved)

    workbook = tmp_path / "recipes.xlsx"
    call("export_recipes_xlsx", root=str(workspace), destination=str(workbook))
    fresh = tmp_path / "fresh"
    call("create_workspace", root=str(fresh), name="Fresh")
    imported = call("import_recipes_xlsx", root=str(fresh), source=str(workbook))["document"]
    assert {recipe["name"]: recipe["group"] for recipe in imported["recipes"]}["Si Directional Trench Etch"] == "ALD/Oxides"


def test_run_cli_runs_a_command_inside_the_worker(tmp_path):
    root = tmp_path / "console"
    made = call("run_cli", argv=["new", str(root), "--kernel", "slab"])
    assert made["exitCode"] == 0 and "document" not in made
    flow = json.dumps({"name": "Pasted", "kernel": "slab", "steps": [{"name": "Polish", "type": "cmp", "parameters": {"target_z": 0.0}}]})
    applied = call("run_cli", root=str(root), argv=["flow", "apply", "-"], stdin=flow)
    assert applied["exitCode"] == 0, applied["stderr"]
    assert [step["name"] for step in applied["document"]["branches"][0]["steps"]] == ["Polish"]
    ran, events = call_with_events("run_cli", root=str(root), argv=["run"])
    assert ran["exitCode"] == 0 and "Ran 1 step(s)" in ran["stdout"]
    # The run's progress went out on the request's own stream, like a run_flow.
    assert any(event.get("kind") == "progress" and event.get("stepId") for event in events)
    assert list(ran["document"]["stepStatuses"].values())[0] == {
        ran["document"]["branches"][0]["steps"][0]["id"]: "clean"
    }
    failed = call("run_cli", root=str(root), argv=["steps", "bogus"])
    assert failed["exitCode"] == 2 and "invalid choice" in failed["stderr"]
    with pytest.raises(InvalidRequest):
        call("run_cli", root=str(root), argv=[])


def _top_view_colors(view) -> set[str]:
    """Every colour the rendered top view actually shows."""
    picture = Image.open(io.BytesIO(base64.b64decode(view["image"]))).convert("RGB")
    pixels = np.asarray(picture).reshape(-1, 3)
    return {"#%02x%02x%02x" % tuple(pixel) for pixel in np.unique(pixels, axis=0)}


def test_the_top_view_can_look_through_a_material(workspace):
    """Hiding the last deposit is how you see the hole it is standing in."""
    root = str(workspace)
    document = call("open_workspace", root=root)
    branch = document["branches"][0]
    call("run_flow", root=root, branchId=branch["id"])
    last = branch["steps"][-1]["id"]
    colors = {material["name"]: material["color"] for material in document["materials"]}

    plain = call("get_top_view", root=root, branchId=branch["id"], stepId=last)
    through = call(
        "get_top_view", root=root, branchId=branch["id"], stepId=last, hidden=["Al2O3"]
    )

    assert colors["Al2O3"] in _top_view_colors(plain)
    assert colors["Al2O3"] not in _top_view_colors(through)
    # What was under it is what is seen instead, not a hole in the picture.
    assert colors["Si"] in _top_view_colors(through)
    with pytest.raises(InvalidRequest):
        call("get_top_view", root=root, branchId=branch["id"], stepId=last, hidden="Al2O3")


def test_the_top_view_marks_a_step_inside_one_material(workspace):
    """A trench in silicon is the same colour as the silicon around it."""
    root = str(workspace)
    document = call("open_workspace", root=root)
    branch = document["branches"][0]
    call("run_flow", root=root, branchId=branch["id"])
    last = branch["steps"][-1]["id"]
    silicon = next(m["color"] for m in document["materials"] if m["name"] == "Si")
    through = {"root": root, "branchId": branch["id"], "stepId": last, "hidden": ["Al2O3"]}

    plain = _top_view_colors(call("get_top_view", **through, steps=False))
    marked = _top_view_colors(call("get_top_view", **through))

    assert plain == {silicon}
    # The rim of the trench, in the same silicon at a darker shade.
    assert marked - plain == {_shaded(silicon)}


def _shaded(color: str) -> str:
    """A material's colour as a step inside it is drawn: darker, same hue."""
    channels = [int(color[index : index + 2], 16) for index in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(channel * 0.45) for channel in channels)


def test_the_slab_top_view_marks_a_step_inside_one_material(tmp_path):
    root = str(tmp_path / "slab")
    document = call("create_workspace", root=root, name="Steps", kernel="slab")
    branch = document["branches"][0]
    call("run_flow", root=root, branchId=branch["id"])
    last = branch["steps"][-1]["id"]
    silicon = next(m["color"] for m in document["materials"] if m["name"] == "Si")
    through = {"root": root, "branchId": branch["id"], "stepId": last, "hidden": ["Al2O3"]}

    plain = _top_view_colors(call("get_top_view", **through, steps=False))
    marked = _top_view_colors(call("get_top_view", **through))

    assert plain == {silicon}
    assert marked - plain == {_shaded(silicon)}


def test_the_slab_top_view_can_look_through_a_material(tmp_path):
    root = str(tmp_path / "slab")
    document = call("create_workspace", root=root, name="Through", kernel="slab")
    branch = document["branches"][0]
    call("run_flow", root=root, branchId=branch["id"])
    last = branch["steps"][-1]["id"]
    colors = {material["name"]: material["color"] for material in document["materials"]}

    plain = call("get_top_view", root=root, branchId=branch["id"], stepId=last)
    through = call(
        "get_top_view", root=root, branchId=branch["id"], stepId=last, hidden=["Al2O3"]
    )

    assert colors["Al2O3"] in _top_view_colors(plain)
    assert colors["Al2O3"] not in _top_view_colors(through)
    assert colors["Si"] in _top_view_colors(through)


def _ran_workspace(tmp_path, name="Splits"):
    root = str(tmp_path / "splits")
    document = call("create_workspace", root=root, name=name, kernel="slab")
    branch = document["branches"][0]
    call("run_flow", root=root, branchId=branch["id"])
    return root, call("open_workspace", root=root)


def test_a_branch_forks_a_flow_at_a_step_and_inherits_its_results(tmp_path):
    """A split is the same flow up to a step, and something else after it."""
    root, document = _ran_workspace(tmp_path)
    main = document["branches"][0]
    fork_at = main["steps"][1]

    forked = call(
        "create_branch", root=root, branchId=main["id"], stepId=fork_at["id"], name="thick oxide"
    )

    branch = next(item for item in forked["branches"] if item["id"] == forked["branchId"])
    assert [step["name"] for step in branch["steps"]] == [
        step["name"] for step in main["steps"][:2]
    ]
    # The steps up to the fork are the same steps, so their results stand:
    # only what is added after it has to run.
    assert set(forked["stepStatuses"][branch["id"]].values()) == {"clean"}
    # And the fork is where the work now happens.
    assert forked["project"]["activeBranchId"] == branch["id"]
    assert set(forked["stepStatuses"][main["id"]].values()) == {"clean"}


def test_deleting_a_branch_keeps_what_the_others_are_still_using(tmp_path):
    root, document = _ran_workspace(tmp_path)
    main = document["branches"][0]
    forked = call(
        "create_branch", root=root, branchId=main["id"], stepId=main["steps"][1]["id"], name="split"
    )

    after = call("delete_branch", root=root, branchId=forked["branchId"])

    assert [branch["id"] for branch in after["branches"]] == [main["id"]]
    # The snapshots the fork was sharing are the ones main is standing on.
    assert set(after["stepStatuses"][main["id"]].values()) == {"clean"}
    assert after["project"]["activeBranchId"] == main["id"]


def test_a_branch_needs_a_name_of_its_own_and_a_step_to_fork_at(tmp_path):
    root, document = _ran_workspace(tmp_path)
    main = document["branches"][0]
    fork = {"root": root, "branchId": main["id"], "stepId": main["steps"][0]["id"]}

    with pytest.raises(InvalidRequest, match="needs a name"):
        call("create_branch", **fork, name="  ")
    with pytest.raises(InvalidRequest, match="already has a branch"):
        call("create_branch", **fork, name=main["name"])
    with pytest.raises(InvalidRequest, match="not on the branch"):
        call("create_branch", root=root, branchId=main["id"], stepId="nowhere", name="x")

    made = call("create_branch", **fork, name="split")
    with pytest.raises(InvalidRequest, match="already has a branch"):
        call("rename_branch", root=root, branchId=made["branchId"], name=main["name"])
    # Its own name back onto itself is not a clash.
    assert call("rename_branch", root=root, branchId=made["branchId"], name="split")


def test_the_last_branch_and_a_forked_from_branch_are_kept(tmp_path):
    root, document = _ran_workspace(tmp_path)
    main = document["branches"][0]
    with pytest.raises(InvalidRequest, match="at least one branch"):
        call("delete_branch", root=root, branchId=main["id"])

    made = call("create_branch", root=root, branchId=main["id"], stepId=main["steps"][1]["id"], name="split")
    call("create_branch", root=root, branchId=made["branchId"], stepId=main["steps"][0]["id"], name="split of a split")

    with pytest.raises(InvalidRequest, match="delete those first"):
        call("delete_branch", root=root, branchId=made["branchId"])


def test_results_of_both_fidelities_are_kept_side_by_side(tmp_path):
    root = str(tmp_path / "slab")
    document = call("create_workspace", root=root, name="Both", kernel="slab")
    branch = document["branches"][0]
    assert document["project"]["fidelity"] == "simplified"
    first = branch["steps"][0]["id"]
    ran = call("run_flow", root=root, branchId=branch["id"], throughStepId=first)
    assert ran["stepStatuses"][first] == "clean"
    # Switching the film model shows the other mode's (absent) results...
    document["project"]["fidelity"] = "detailed"
    switched = call("save_document", root=root, document=document)
    assert switched["project"]["fidelity"] == "detailed"
    assert switched["stepStatuses"][branch["id"]][first] == "dirty"
    with pytest.raises(InvalidRequest, match="current fidelity"):
        call("get_section", root=root, branchId=branch["id"], stepId=first, axis="y")
    ran = call("run_flow", root=root, branchId=branch["id"], throughStepId=first)
    assert ran["executedStepIds"] == [first]
    # ...and switching back finds the simplified result still there, unrun.
    document["project"]["fidelity"] = "simplified"
    back = call("save_document", root=root, document=document)
    assert back["stepStatuses"][branch["id"]][first] == "clean"
    assert call("get_section", root=root, branchId=branch["id"], stepId=first, axis="y")["image"]
    ran = call("run_flow", root=root, branchId=branch["id"], throughStepId=first)
    assert ran["cachedStepIds"] == [first]
    with pytest.raises(InvalidRequest, match="fidelity"):
        document["project"]["fidelity"] = "rough"
        call("save_document", root=root, document=document)


def test_a_digest_does_not_tell_a_whole_float_from_an_int():
    from process_studio.models import ProcessStep, ProcessType, Recipe
    from process_studio.worker.workspace import step_digest

    def digest(fraction):
        step = ProcessStep("Etch", process_type=ProcessType.ETCH, parameters={"target": 0.3, "directional_fraction": fraction})
        recipe = Recipe("r", ProcessType.ETCH, parameters=dict(step.parameters))
        return step_digest("genesis", step, recipe, None, {"nx": 10, "spacingUm": 0.01})

    assert digest(1) == digest(1.0)
    assert digest(1) != digest(0)


def test_the_file_menu_methods_export_import_and_copy(tmp_path):
    root = str(tmp_path / "files")
    document = call("create_workspace", root=root, name="Files", kernel="slab")
    for fmt in ("xlsx", "csv", "json", "yaml"):
        exported = call("export_flow", root=root, destination=str(tmp_path / f"flow.{fmt}"), format=fmt)
        assert Path(exported["path"]).is_file() and exported["steps"] == len(document["branches"][0]["steps"])
    header = (tmp_path / "flow.csv").read_text(encoding="utf-8-sig").splitlines()[0]
    assert header.startswith("#,Step,Type,Tool,Material")
    for kind in ("materials", "tools", "recipes"):
        out = call("export_library", root=root, kind=kind, destination=str(tmp_path / f"{kind}.xlsx"))
        assert out["count"] > 0
    # A material row with a known name replaces it; an unknown one is added.
    (tmp_path / "more.csv").write_text("Material,Category,Color,Opacity\nSi,Semiconductor,#123456,1\nNewStuff,Metal,#abcdef,0.5\n")
    imported = call("import_library", root=root, kind="materials", source=str(tmp_path / "more.csv"))
    names = {m["name"]: m for m in imported["document"]["materials"]}
    assert imported["imported"] == 2 and names["Si"]["color"] == "#123456" and names["NewStuff"]["opacity"] == 0.5
    # Save as copies everything, snapshots included, and the copy is a workspace of its own.
    first = document["branches"][0]["steps"][0]["id"]
    call("run_flow", root=root, branchId=document["branches"][0]["id"], throughStepId=first)
    copied = call("copy_workspace", root=root, destination=str(tmp_path / "copy"))
    assert copied["document"]["stepStatuses"][document["branches"][0]["id"]][first] == "clean"
    assert call("get_section", root=copied["root"], branchId=document["branches"][0]["id"], stepId=first, axis="y")["image"]
    applied = call("import_flow", root=copied["root"], source=str(tmp_path / "flow.json"))
    assert len(applied["document"]["branches"][0]["steps"]) == len(document["branches"][0]["steps"])
    with pytest.raises(InvalidRequest):
        call("copy_workspace", root=root, destination=root)
    with pytest.raises(InvalidRequest):
        call("export_library", root=root, kind="sketches", destination=str(tmp_path / "x.xlsx"))


def test_the_protocol_and_the_mesh_code_agree_on_the_triangulators():
    """protocol.py spells the list out rather than importing it, because a
    level-set-only package has no shapely for deviceflow's mesh code."""
    from deviceflow._internal.mesh.triangulate import ENGINES

    from process_studio.worker.protocol import MESH_ENGINES

    assert MESH_ENGINES == ENGINES


def test_changing_the_layout_makes_the_steps_that_cut_from_it_stale(tmp_path):
    """A step masked from the layout has to notice when the layout changes.

    Importing a GDS copies it into the workspace under a new name, and a
    layout can also be edited where it lies. Neither moved the digest,
    which records the layer and datatype but said nothing about the file,
    so the flow went on reporting every step up to date and refused to run
    them again.
    """
    from process_studio.models import ProcessStep, ProcessType, Recipe
    from process_studio.worker.workspace import layout_fingerprint, step_digest

    gds = tmp_path / "layout.gds"
    gds.write_bytes(b"first")

    def digest(source: str, path) -> str:
        step = ProcessStep(
            "Etch", process_type=ProcessType.ETCH, mask_source=source, layer=1, datatype=0,
            parameters={"target": 0.3},
        )
        recipe = Recipe("r", ProcessType.ETCH, parameters=dict(step.parameters))
        return step_digest(
            "genesis", step, recipe, None, {"nx": 10, "spacingUm": 0.01},
            layout_fingerprint(None if path is None else str(path)),
        )

    before = digest("gds", gds)
    assert digest("gds", gds) == before  # nothing changed, nothing to redo

    gds.write_bytes(b"second, and longer")
    assert digest("gds", gds) != before, "an edited layout is a different layout"

    other = tmp_path / "1700000000000-layout.gds"
    other.write_bytes(b"second, and longer")
    assert digest("gds", other) != digest("gds", gds), "re-importing copies it under a new name"

    # A step that does not read the layout is unaffected by it.
    assert digest("none", gds) == digest("none", other)
    assert digest("none", gds) == digest("none", None)


def test_a_layout_that_is_gone_is_not_the_layout_that_ran(tmp_path):
    from process_studio.worker.workspace import layout_fingerprint

    gds = tmp_path / "layout.gds"
    gds.write_bytes(b"x")
    present = layout_fingerprint(str(gds))
    gds.unlink()
    assert layout_fingerprint(str(gds)) != present
    assert layout_fingerprint(None) is None


def test_a_step_can_be_run_again_although_it_says_it_is_up_to_date(tmp_path):
    """Not everything a step depends on is something the digest can see.

    Running one step again reuses everything before it and recomputes it
    and everything after, because each step starts from the state the one
    before left.
    """
    root = str(tmp_path / "redo")
    document = call("create_workspace", root=root, name="Redo", kernel="slab")
    steps = document["branches"][0]["steps"]
    assert len(steps) >= 3, "the starter flow is what this runs"

    first = call("run_flow", root=root)
    assert first["executedStepIds"] == [step["id"] for step in steps]

    # Nothing changed: everything is reused.
    again = call("run_flow", root=root)
    assert again["executedStepIds"] == []
    assert again["cachedStepIds"] == [step["id"] for step in steps]

    # The middle step again: the one before it is still cached, it and the
    # one after it run.
    target = steps[1]["id"]
    redone = call("run_flow", root=root, fromStepId=target)
    assert redone["cachedStepIds"] == [steps[0]["id"]]
    assert redone["executedStepIds"] == [step["id"] for step in steps[1:]]

    # And it is up to date again afterwards.
    settled = call("run_flow", root=root)
    assert settled["executedStepIds"] == []


def test_running_a_step_again_leaves_the_rest_of_the_flow_alone(tmp_path):
    root = str(tmp_path / "redo-last")
    document = call("create_workspace", root=root, name="Redo last", kernel="slab")
    steps = document["branches"][0]["steps"]
    call("run_flow", root=root)

    last = steps[-1]["id"]
    redone = call("run_flow", root=root, fromStepId=last)
    assert redone["executedStepIds"] == [last]
    assert redone["cachedStepIds"] == [step["id"] for step in steps[:-1]]

    # An id that is not in the flow changes nothing.
    unknown = call("run_flow", root=root, fromStepId="no-such-step")
    assert unknown["executedStepIds"] == []


def test_a_workspace_carries_its_layout_when_it_moves(tmp_path):
    """The layout path is stored relative to the workspace.

    A workspace written with an absolute path stops finding its layout the
    moment it is copied anywhere -- another machine, another user's home,
    a zip and back -- and every step that cuts from that layout then
    cannot run. Importing a layout copies the file into the workspace, so
    where it sits in the workspace is what gets stored.
    """
    import shutil

    from process_studio.storage import ProjectRepository

    here = tmp_path / "here"
    (here / "layouts").mkdir(parents=True)
    (here / "layouts" / "wafer.gds").write_bytes(b"not really a gds")
    repository = ProjectRepository(here / "process_studio.sqlite3")
    project = ProjectDefinition(
        id="p", name="p", grid={}, gds_path=str(here / "layouts" / "wafer.gds")
    )
    repository.save_project(project)

    with repository.connect() as connection:
        stored = connection.execute("SELECT gds_path FROM projects").fetchone()[0]
    assert stored == "layouts/wafer.gds", "stored where it sits, not where it was"

    there = tmp_path / "there"
    shutil.copytree(here, there)
    moved = ProjectRepository(there / "process_studio.sqlite3")
    assert moved.load_project("p").gds_path == str(there / "layouts" / "wafer.gds")

    # ...and the layout still counts as the same layout, so the steps that
    # cut from it are not all stale for having been copied.
    from process_studio.worker.workspace import layout_fingerprint

    assert layout_fingerprint(
        repository.load_project("p").gds_path, repository
    ) == layout_fingerprint(moved.load_project("p").gds_path, moved)


def test_an_old_absolute_layout_path_is_adopted(tmp_path):
    """What the workspaces written before that look like: a path from the
    machine they were made on, Windows separators and all. The file is in
    the workspace's ``layouts``, which is where importing put it."""
    from process_studio.storage import ProjectRepository

    root = tmp_path / "workspace"
    (root / "layouts").mkdir(parents=True)
    (root / "layouts" / "1789494000968-3D DRAM.gds").write_bytes(b"gds")
    repository = ProjectRepository(root / "process_studio.sqlite3")
    repository.save_project(ProjectDefinition(id="p", name="p", grid={}))
    with repository.connect() as connection:
        connection.execute(
            "UPDATE projects SET gds_path = ?",
            (r"\\?\C:\Users\someone\Project\layouts\1789494000968-3D DRAM.gds",),
        )

    found = repository.load_project("p").gds_path
    assert found == str(root / "layouts" / "1789494000968-3D DRAM.gds")
    # Reading it did not rewrite the row; saving the project does that.
    repository.save_project(repository.load_project("p"))
    with repository.connect() as connection:
        assert connection.execute("SELECT gds_path FROM projects").fetchone()[0] == (
            "layouts/1789494000968-3D DRAM.gds"
        )

    # One that names a file this workspace does not have is left alone, so
    # the error the user sees still names the path they chose.
    with repository.connect() as connection:
        connection.execute("UPDATE projects SET gds_path = ?", (r"C:\elsewhere\other.gds",))
    assert repository.load_project("p").gds_path == r"C:\elsewhere\other.gds"
