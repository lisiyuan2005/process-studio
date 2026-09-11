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
    assert described["numerics"]["solverOrders"] == [1, 2]
    assert described["limits"]["calibrated"] is False


def test_create_workspace_is_idempotent(tmp_path):
    root = tmp_path / "workspace"
    first = call("create_workspace", root=str(root), name="Test Project")
    second = call("create_workspace", root=str(root), name="Ignored Rename")
    assert first["project"]["id"] == second["project"]["id"]
    assert second["project"]["name"] == "Test Project"
    assert len(second["branches"][0]["steps"]) == len(first["branches"][0]["steps"])


def test_describe_publishes_the_kernels_a_project_can_be_built_on():
    described = call("describe")
    kernels = {kernel["id"]: kernel for kernel in described["kernels"]}
    assert described["defaultKernel"] == "levelset"
    assert kernels["levelset"]["spacingRole"] == "grid"
    assert kernels["slab"]["spacingRole"] == "conformal_resolution"
    # The slab kernel has no field, so there is no node ceiling to report.
    assert kernels["slab"]["maximumNodes"] is None
    assert kernels["slab"]["depositionModes"] == ["conformal", "planar"]


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
    document = call("open_workspace", root=str(workspace))
    document["project"]["kernel"] = "slab"
    with pytest.raises(InvalidRequest, match="cannot be moved"):
        call("save_document", root=str(workspace), document=document)
    unchanged = call("open_workspace", root=str(workspace))
    assert unchanged["project"]["kernel"] == "levelset"


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


@pytest.mark.parametrize("kernel", ["levelset", "slab"])
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


@pytest.fixture()
def slab_only_build():
    """A worker built with the slab kernel alone, restored afterwards."""
    from process_studio import kernels

    kernels.configure({"slab"})
    try:
        yield
    finally:
        kernels.configure(None)


def test_a_single_kernel_build_offers_only_that_kernel(slab_only_build):
    described = call("describe")
    assert [kernel["id"] for kernel in described["kernels"]] == ["slab"]
    assert described["defaultKernel"] == "slab"
    assert described["buildVariant"] == "slab"


def test_a_single_kernel_build_refuses_the_other_kernel(tmp_path, slab_only_build):
    root = tmp_path / "unnamed"
    created = call("create_workspace", root=str(root), name="Unnamed")
    # Unnamed means the default, which is the one kernel there is.
    assert created["project"]["kernel"] == "slab"
    with pytest.raises(InvalidRequest, match="not in this build"):
        call("create_workspace", root=str(tmp_path / "other"), name="Other", kernel="levelset")


def test_a_project_from_the_missing_kernel_is_refused_with_directions(tmp_path):
    from process_studio import kernels

    root = tmp_path / "levelset-project"
    call("create_workspace", root=str(root), name="Level set", kernel="levelset")
    call("run_flow", root=str(root))
    kernels.configure({"slab"})
    try:
        document = call("open_workspace", root=str(root))
        assert document["project"]["kernel"] == "levelset"
        with pytest.raises(WorkspaceError, match="includes 'levelset'"):
            call("run_flow", root=str(root))
        branch = document["branches"][0]
        with pytest.raises(WorkspaceError, match="includes 'levelset'"):
            call("get_top_view", root=str(root), branchId=branch["id"], stepId=branch["steps"][0]["id"])
    finally:
        kernels.configure(None)


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


def test_set_grid_applies_the_grid_and_discards_stale_results(workspace):
    call("run_flow", root=str(workspace))
    document = call("open_workspace", root=str(workspace))
    grid = dict(document["project"]["grid"])
    # 1.6/44 and 1.2/33 are the same spacing, which the kernel requires.
    grid.update({"nx": 45, "ny": 45, "nz": 34})
    updated = call("set_grid", root=str(workspace), grid=grid)
    assert updated["project"]["grid"]["nx"] == 45
    assert set(updated["stepStatuses"][updated["branches"][0]["id"]].values()) == {"dirty"}


def test_set_grid_rejects_unequal_spacing(workspace):
    document = call("open_workspace", root=str(workspace))
    grid = dict(document["project"]["grid"])
    grid["nx"] = 44
    with pytest.raises(InvalidRequest):
        call("set_grid", root=str(workspace), grid=grid)


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
    assert surfaces["interpolation"] == 2
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
    assert top["width"] == document["project"]["grid"]["nx"]


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


def test_a_newer_view_request_supersedes_a_queued_one(workspace):
    """Five quick clicks should cost one computation, not five."""
    import threading
    from process_studio.worker.server import Server

    document = call("open_workspace", root=str(workspace))
    branch = document["branches"][0]
    call("run_flow", root=str(workspace))
    read_end, write_end = os.pipe()
    input_stream = os.fdopen(read_end, "r")
    writer = os.fdopen(write_end, "w")
    output = io.StringIO()
    server = Server(input_stream, output)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    # A slow request first, so the view requests queue up behind it.
    writer.write(json.dumps({"id": "run", "method": "run_flow", "params": {"root": str(workspace), "force": True}}) + "\n")
    for index, step in enumerate(branch["steps"]):
        writer.write(json.dumps({
            "id": f"view-{index}", "method": "get_section",
            "params": {"root": str(workspace), "branchId": branch["id"], "stepId": step["id"], "axis": "y"},
        }) + "\n")
    writer.flush()
    # Closing the input tells the server its client is gone, which cancels
    # whatever is running; keep it open until the last answer has arrived.
    deadline = time.time() + 120
    while time.time() < deadline and '"id":"view-3"' not in output.getvalue().replace(" ", ""):
        time.sleep(0.02)
    writer.close()
    thread.join(timeout=120)
    assert not thread.is_alive()
    answers = {
        message["id"]: message
        for message in map(json.loads, output.getvalue().splitlines())
        if message["kind"] == "response"
    }
    assert answers["run"]["ok"]
    superseded = [key for key, answer in answers.items() if not answer["ok"] and answer["error"]["code"] == "Superseded"]
    assert sorted(superseded) == ["view-0", "view-1", "view-2"]
    assert answers["view-3"]["ok"] and "image" in answers["view-3"]["result"]


def test_a_run_can_be_cancelled_between_steps_and_keeps_what_ran(workspace, monkeypatch):
    import threading
    from process_studio.kernels.levelset import LevelSetKernel
    from process_studio.worker.server import Server

    # The first step holds until the cancel has been sent, so the outcome does
    # not depend on how fast the machine runs the starter flow: on a fast one
    # the whole flow would otherwise finish before the cancel arrived.
    cancel_sent = threading.Event()
    step_started = threading.Event()
    real_run_step = LevelSetKernel.run_step

    def held_run_step(self, state, step, **kwargs):
        step_started.set()
        assert cancel_sent.wait(timeout=60), "the test never sent its cancel"
        return real_run_step(self, state, step, **kwargs)

    monkeypatch.setattr(LevelSetKernel, "run_step", held_run_step)

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


def test_plan_grid_reports_the_lattice_and_its_cost(workspace):
    plan = call("plan_grid", root=str(workspace), targetSpacingNm=25.0)
    estimate = plan["estimate"]
    assert estimate["spacingNm"] == pytest.approx(25.0)
    # Every extent has to divide by one spacing, so the shape is searched, not rounded.
    assert plan["grid"]["nx"] == estimate["shape"][0]
    assert estimate["nodeCount"] == estimate["shape"][0] * estimate["shape"][1] * estimate["shape"][2]
    assert estimate["recommendedBytes"] > estimate["stateBytes"]
    assert plan["withinLimit"] is True
    assert plan["unchanged"] is False


def test_plan_grid_flags_a_grid_over_the_ceiling(workspace):
    plan = call("plan_grid", root=str(workspace), targetSpacingNm=2.0)
    assert plan["withinLimit"] is False
    assert plan["estimate"]["nodeCount"] > plan["maximumNodes"]


def test_plan_grid_rejects_a_spacing_outside_the_supported_range(workspace):
    for spacing in (0.05, 2000.0, "coarse"):
        with pytest.raises(InvalidRequest):
            call("plan_grid", root=str(workspace), targetSpacingNm=spacing)


def test_set_grid_by_target_spacing_applies_and_invalidates(workspace):
    call("run_flow", root=str(workspace))
    updated = call("set_grid", root=str(workspace), targetSpacingNm=25.0)
    grid = updated["project"]["grid"]
    assert grid["spacingUm"] == pytest.approx(0.025)
    assert grid["nx"] == 65 and grid["ny"] == 65 and grid["nz"] == 49
    assert set(updated["stepStatuses"][updated["branches"][0]["id"]].values()) == {"dirty"}


def test_set_grid_refuses_to_exceed_the_node_ceiling(workspace):
    with pytest.raises(InvalidRequest, match="ceiling"):
        call("set_grid", root=str(workspace), targetSpacingNm=2.0)
    unchanged = call("open_workspace", root=str(workspace))
    assert unchanged["project"]["grid"]["nx"] == 41


def test_describe_reports_the_node_ceiling_and_presets():
    numerics = call("describe")["numerics"]
    assert numerics["maximumNodes"] == 20_000_000
    assert numerics["spacingPresetsNm"] == [25.0, 12.5, 6.25]


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
