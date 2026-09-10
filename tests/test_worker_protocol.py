"""RPC-level tests for the desktop worker."""

from __future__ import annotations

import base64
import io
import json

import numpy as np
import pytest
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
    assert [statuses[step["id"]] for step in branch["steps"][1:]] == ["dirty"] * 3

    result = call("run_flow", root=str(workspace))
    assert result["cachedStepIds"] == [branch["steps"][0]["id"]]
    assert len(result["executedStepIds"]) == 3


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
    assert statuses[branch["steps"][0]["id"]] == "dirty"
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
    answers = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [answer["id"] for answer in answers] == ["a", "b", "c", None]
    assert [answer["ok"] for answer in answers] == [True, False, True, False]
    assert answers[1]["error"]["code"] == "InvalidRequest"


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
