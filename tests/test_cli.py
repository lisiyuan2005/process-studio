"""The command line: the same workspace the desktop opens, driven from a shell."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from PIL import Image

from process_studio.cli import EXIT_OK, EXIT_RUN, EXIT_USAGE, EXIT_WORKSPACE, main

pytest.importorskip("shapely")


def run(*argv: str, root: Path | None = None) -> tuple[int, str, str]:
    """Run the CLI in-process; returns (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    arguments = list(argv)
    if root is not None:
        arguments = ["--root", str(root), *arguments]
    from process_studio import cli

    original = cli.Session.__init__

    def patched(self, root_, **kwargs):
        kwargs["out"], kwargs["err"] = out, err
        original(self, root_, **kwargs)

    cli.Session.__init__ = patched  # type: ignore[method-assign]
    try:
        code = main(arguments)
    finally:
        cli.Session.__init__ = original  # type: ignore[method-assign]
    return code, out.getvalue(), err.getvalue()


def as_json(*argv: str, root: Path | None = None):
    code, out, err = run("--json", *argv, root=root)
    assert code == EXIT_OK, err
    return json.loads(out)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "demo"
    code, out, err = run("new", str(root), "--kernel", "slab", "--name", "CLI demo")
    assert code == EXIT_OK, err
    assert "Kernel     slab" in out
    return root


def test_new_creates_a_workspace_the_worker_can_open(workspace: Path):
    assert (workspace / "process_studio.sqlite3").is_file()
    document = as_json("info", root=workspace)
    assert document["project"]["name"] == "CLI demo"
    assert document["project"]["kernel"] == "slab"


def test_the_workspace_is_found_from_inside_it(workspace: Path, monkeypatch):
    monkeypatch.chdir(workspace)
    code, out, _ = run("steps", "list")
    assert code == EXIT_OK
    assert "Trench Etch" in out
    monkeypatch.chdir(workspace.parent)
    code, _, err = run("steps", "list")
    assert code == EXIT_WORKSPACE
    assert "No workspace here" in err


def test_steps_are_edited_the_way_the_desktop_edits_them(workspace: Path):
    steps = as_json(
        "steps", "add", "deposit", "--name", "TiN liner", "--material", "TiN",
        "--set", "target=0.02", "--set", "mode=conformal", root=workspace,
    )
    assert [step["name"] for step in steps][-1] == "TiN liner"
    assert steps[-1]["parameters"] == {"target": 0.02, "mode": "conformal"}

    steps = as_json("steps", "set", "2", "--set", "target=0.25", "--rate", "Si=0.1", "--stop", "SiO2", root=workspace)
    assert steps[1]["parameters"]["target"] == 0.25
    assert steps[1]["materialResponses"]["Si"] == {"material": "Si", "rateUmPerMin": 0.1, "stopLayer": False}
    assert steps[1]["materialResponses"]["SiO2"]["stopLayer"] is True

    steps = as_json("steps", "dup", "TiN liner", root=workspace)
    assert [step["name"] for step in steps][-2:] == ["TiN liner", "TiN liner copy"]
    assert steps[-1]["id"] != steps[-2]["id"]

    steps = as_json("steps", "mv", "6", "1", root=workspace)
    assert steps[0]["name"] == "TiN liner copy"

    steps = as_json("steps", "rm", "1", root=workspace)
    assert "TiN liner copy" not in [step["name"] for step in steps]

    steps = as_json("steps", "skip", "3", root=workspace)
    assert steps[2]["enabled"] is False
    steps = as_json("steps", "include", "3", root=workspace)
    assert steps[2]["enabled"] is True

    steps = as_json("steps", "add", "etch", "--before", "1", "--mask", "gds:5/1", "--keep", "outside", root=workspace)
    assert steps[0]["maskSource"] == "gds" and steps[0]["layer"] == 5 and steps[0]["datatype"] == 1
    assert steps[0]["keep"] == "outside"

    code, _, err = run("steps", "show", "99", root=workspace)
    assert code == EXIT_USAGE
    assert "does not exist" in err


def test_run_reports_progress_and_the_views_write_files(workspace: Path, tmp_path: Path):
    code, out, err = run("run", root=workspace)
    assert code == EXIT_OK, err
    assert "Running Trench Etch" in err
    assert "Ran 4 step(s)" in out
    statuses = as_json("status", root=workspace)
    assert set(statuses["stepStatuses"]["default-main"].values()) == {"clean"}

    section = tmp_path / "out" / "cut.png"
    code, out, err = run("view", "section", "--axis", "x", "--at", "0.1", "-o", str(section), root=workspace)
    assert code == EXIT_OK, err
    with Image.open(section) as image:
        assert image.size[0] > 100
    top = tmp_path / "top.png"
    assert run("view", "top", "--step", "2", "-o", str(top), root=workspace)[0] == EXIT_OK
    assert top.stat().st_size > 0
    wafer = tmp_path / "wafer.png"
    assert run("view", "top", "--step", "0", "-o", str(wafer), root=workspace)[0] == EXIT_OK
    mesh = tmp_path / "mesh.glb"
    result = as_json("view", "mesh", "-o", str(mesh), root=workspace)
    assert set(result["triangles"]) == {"Si", "Al2O3"}
    assert mesh.stat().st_size > 0

    # The second run reuses every stored result.
    result = as_json("run", root=workspace)
    assert result["executedStepIds"] == [] and len(result["cachedStepIds"]) == 4


def test_run_through_a_step_and_a_failing_step_exit_code(workspace: Path):
    result = as_json("run", "--through", "2", root=workspace)
    assert len(result["executedStepIds"]) == 2
    as_json("steps", "set", "4", "--set", "target=50", root=workspace)
    code, _, err = run("run", root=workspace)
    assert code == EXIT_RUN
    assert "taller than the whole project window" in err


def test_window_changes_discard_results(workspace: Path):
    as_json("run", root=workspace)
    result = as_json("window", "--z", "-1", "2.5", "--spacing", "20", root=workspace)
    assert result["grid"]["zMax"] == 2.5 and result["resolutionUm"] == pytest.approx(0.02)
    statuses = as_json("status", root=workspace)
    assert set(statuses["stepStatuses"]["default-main"].values()) == {"dirty"}
    code, out, _ = run("window", root=workspace)
    assert code == EXIT_OK and "z -1..2.5" in out


def test_materials_and_sketches(workspace: Path, tmp_path: Path):
    materials = as_json("materials", "add", "Ru", "--category", "Metal", "--color", "#aabbcc", root=workspace)
    assert any(material["name"] == "Ru" and material["color"] == "#aabbcc" for material in materials)
    code, _, err = run("materials", "rm", "Si", root=workspace)
    assert code == EXIT_USAGE and "used by step" in err
    assert "Ru" not in [m["name"] for m in as_json("materials", "rm", "Ru", root=workspace)]

    exported = tmp_path / "default.json"
    assert run("sketch", "export", "default", str(exported), root=workspace)[0] == EXIT_OK
    payload = json.loads(exported.read_text())
    payload["shapes"][0]["parameters"]["radius"] = 0.3
    exported.write_text(json.dumps(payload))
    sketch = as_json("sketch", "import", "default", str(exported), root=workspace)
    assert sketch["shapes"][0]["parameters"]["radius"] == 0.3
    code, out, _ = run("sketch", "show", "default", root=workspace)
    assert code == EXIT_OK and "circle" in out


def test_a_flow_file_round_trips_and_keeps_results_by_position(workspace: Path, tmp_path: Path):
    as_json("run", root=workspace)
    flow_path = tmp_path / "flow.json"
    assert run("flow", "dump", str(flow_path), root=workspace)[0] == EXIT_OK
    flow = json.loads(flow_path.read_text())
    assert flow["kernel"] == "slab" and flow["resolution_nm"] == pytest.approx(10.0)
    assert [step["type"] for step in flow["steps"]] == ["no_geometry", "etch", "no_geometry", "deposit"]
    assert flow["steps"][1]["mask"] == "sketch:default"

    # Change the last step and add one: only those need to run.
    flow["steps"][3]["parameters"]["target"] = 0.05
    flow["steps"].append({"name": "W fill", "type": "deposit", "material": "W", "parameters": {"target": 0.1}})
    flow_path.write_text(json.dumps(flow))
    steps = as_json("flow", "apply", str(flow_path), root=workspace)
    assert [step["name"] for step in steps][-1] == "W fill"
    statuses = as_json("status", root=workspace)["stepStatuses"]["default-main"]
    by_name = {step["name"]: statuses[step["id"]] for step in steps}
    assert by_name["Trench Etch"] == "clean"
    assert by_name["Conformal Al2O3"] == "stale"
    assert by_name["W fill"] == "dirty"

    # A file can stand up a workspace on its own.
    fresh = tmp_path / "fresh"
    code, out, err = run("flow", "apply", str(flow_path), root=fresh)
    assert code == EXIT_OK, err
    assert "Created workspace" in err
    assert as_json("info", root=fresh)["project"]["kernel"] == "slab"

    flow["kernel"] = "levelset"
    flow_path.write_text(json.dumps(flow))
    code, _, err = run("flow", "apply", str(flow_path), root=workspace)
    assert code == EXIT_USAGE and "kernel" in err


def test_rpc_and_log_are_there_for_scripts(workspace: Path):
    code, out, _ = run("rpc", "list_sketches", root=workspace)
    assert code == EXIT_OK and json.loads(out) == {"sketches": ["default"]}
    code, out, _ = run("rpc", "describe")
    assert code == EXIT_OK and "kernels" in json.loads(out)
    as_json("run", "--through", "1", root=workspace)
    code, out, _ = run("log", "-n", "3", root=workspace)
    assert code == EXIT_OK and "Lithography completed" in out
    code, _, _ = run("kernels")
    assert code == EXIT_OK


def test_section_lines_can_be_saved_and_cut_along(workspace: Path, tmp_path: Path):
    line = as_json("lines", "add", "Diagonal", "-0.6", "-0.6", "0.6", "0.6", root=workspace)
    assert line["name"] == "Diagonal" and line["end"] == [0.6, 0.6]
    rows = as_json("lines", "list", root=workspace)
    assert [row["name"] for row in rows] == ["Diagonal"]
    as_json("run", "--through", "2", root=workspace)
    picture = tmp_path / "diag.png"
    assert run("view", "section", "--step", "2", "--named", "Diagonal", "-o", str(picture), root=workspace)[0] == EXIT_OK
    assert picture.stat().st_size > 0
    code, _, err = run("view", "section", "--named", "Missing", "-o", str(tmp_path / "x.png"), root=workspace)
    assert code == EXIT_USAGE and "no section line" in err
    assert as_json("lines", "rm", "Diagonal", root=workspace) == []


def test_tools_are_a_grouped_library(workspace: Path):
    tools = as_json("tools", "list", root=workspace)
    assert {tool["name"] for tool in tools} >= {"ICP-RIE", "ALD", "Stepper"}
    tool = as_json("tools", "add", "Sputter-2", "--group", "Deposition/PVD", root=workspace)
    assert tool["group"] == "Deposition/PVD"
    moved = as_json("tools", "add", "Sputter-2", "--group", "Deposition", root=workspace)
    assert moved["id"] == tool["id"] and moved["group"] == "Deposition"
    assert "Sputter-2" not in [t["name"] for t in as_json("tools", "rm", "Sputter-2", root=workspace)]
    code, _, err = run("tools", "rm", "Nope", root=workspace)
    assert code == EXIT_USAGE and "no tool" in err
    code, out, _ = run("recipes", "list", root=workspace)
    assert code == EXIT_OK and "ALD" in out


def test_the_3d_nand_example_flow_applies(tmp_path: Path):
    """The shipped 3D NAND flow stands up a workspace as written, so the
    example cannot drift from what the CLI accepts."""
    example = Path(__file__).resolve().parent.parent / "examples" / "3d-nand" / "flow.json"
    root = tmp_path / "nand"
    code, _out, err = run("flow", "apply", str(example), root=root)
    assert code == EXIT_OK, err
    info = as_json("info", root=root)
    assert info["project"]["kernel"] == "slab"
    assert info["project"]["resolutionUm"] == pytest.approx(0.004)
    steps = as_json("steps", "list", root=root)
    assert len(steps) == 34
    assert [step["processType"] for step in steps[:2]] == ["deposit", "deposit"]
    names = [step["name"] for step in steps]
    assert "SiN removal (hot H3PO4)" in names and names[-1] == "Final CMP"
    assert {sketch["id"] for sketch in as_json("sketch", "list", root=root)} >= {"holes", "slits", "wlc", "stair1"}
    assert {m["name"] for m in as_json("materials", "list", root=root)} >= {"Poly-Si", "SiN-trap", "SiN", "W"}


def test_a_flow_can_be_applied_from_stdin(workspace: Path):
    out, err = io.StringIO(), io.StringIO()
    flow = json.dumps({"name": "Pasted", "kernel": "slab", "steps": [{"name": "Polish", "type": "cmp", "parameters": {"target_z": 0.0}}]})
    code = main(["--root", str(workspace), "flow", "apply", "-"], out=out, err=err, stdin=flow)
    assert code == EXIT_OK, err.getvalue()
    assert "Polish" in out.getvalue()
    # argparse's own messages reach the hosted streams, not the process's.
    code = main(["--root", str(workspace), "steps", "bogus"], out=out, err=err)
    assert code == EXIT_USAGE
    assert "invalid choice: 'bogus'" in err.getvalue()


def test_the_fidelity_is_a_project_setting_and_travels_in_flow_files(workspace: Path, tmp_path: Path):
    # A new project is simplified; the other mode is a deliberate switch.
    assert as_json("fidelity", root=workspace) == {"fidelity": "simplified"}
    shown = as_json("fidelity", "detailed", root=workspace)
    assert shown["fidelity"] == "detailed"
    code, out, _ = run("info", root=workspace)
    assert code == EXIT_OK and "Fidelity   detailed" in out
    flow = as_json("flow", "dump", root=workspace)
    assert flow["fidelity"] == "detailed"
    flow["fidelity"] = "simplified"
    (tmp_path / "flow.json").write_text(json.dumps(flow))
    as_json("flow", "apply", str(tmp_path / "flow.json"), root=workspace)
    assert as_json("fidelity", root=workspace) == {"fidelity": "simplified"}


def test_a_tool_carries_the_recipes_loaded_on_it(tmp_path, workspace):
    """The machine's own recipe book, and the step that records one of them."""
    code, _, err = run("tools", "add", "Savannah ALD", "--group", "Deposition/ALD",
                       "--recipe", "Siva_HZO_300C", "--recipe", "Al2O3_200C", root=workspace)
    assert code == EXIT_OK, err
    listed = run("tools", "list", root=workspace)[1]
    assert "Siva_HZO_300C; Al2O3_200C" in listed

    # Editing other fields leaves the recipe book alone.
    run("tools", "add", "Savannah ALD", "--notes", "the small chamber", root=workspace)
    tools = {tool["name"]: tool for tool in as_json("tools", "list", root=workspace)}
    assert tools["Savannah ALD"]["recipes"] == ["Siva_HZO_300C", "Al2O3_200C"]
    assert tools["Savannah ALD"]["notes"] == "the small chamber"

    # A step says which one it ran, in its experiment values.
    run("steps", "add", "deposit", "--name", "HZO", "--tool", "Savannah ALD",
        "--set-experiment", "tool_recipe=Siva_HZO_300C", root=workspace)
    assert "Siva_HZO_300C" in run("steps", "show", "HZO", root=workspace)[1]


def test_the_experiment_settings_are_edited_and_shown_separately(tmp_path, workspace):
    """What the tool was set to, beside what the kernel was asked to build."""
    code, out, err = run("steps", "add", "deposit", "--name", "HZO",
                         "--tool", "Savannah ALD", "--material", "SiO2", root=workspace)
    assert code == EXIT_OK, err
    shown = run("steps", "show", "HZO", root=workspace)[1]
    # An ALD tool means cycles, as the recipe is written in the fab.
    assert "cycles = 100" in shown and "rate_per_cycle" in shown
    assert "the same as the parameters above" in shown

    code, _, err = run("steps", "set", "HZO", "--set-experiment", "time_min=42",
                       "--set-experiment", "tool_recipe=Siva_HZO_300C", root=workspace)
    assert code == EXIT_OK, err
    shown = run("steps", "show", "HZO", root=workspace)[1]
    assert "time_min = 42" in shown and "Siva_HZO_300C" in shown
    # The set starts as a copy of the parameters, so only the differences
    # have to be typed.
    assert shown.count("cycles = 100") == 2

    # It survives a flow file.
    flow = tmp_path / "flow.json"
    assert run("flow", "dump", str(flow), root=workspace)[0] == EXIT_OK
    assert "Siva_HZO_300C" in flow.read_text()
    again = tmp_path / "again"
    assert run("flow", "apply", str(flow), root=again)[0] == EXIT_OK
    assert "Siva_HZO_300C" in run("steps", "show", "HZO", root=again)[1]

    # And it can be given up.
    assert run("steps", "set", "HZO", "--same-experiment", root=workspace)[0] == EXIT_OK
    assert "the same as the parameters above" in run("steps", "show", "HZO", root=workspace)[1]


def test_loops_repeat_a_block_and_fold_back_into_the_flow_file(workspace: Path, tmp_path: Path):
    existing = as_json("steps", "list", root=workspace)
    if existing:
        as_json("steps", "rm", *[str(number) for number in range(1, len(existing) + 1)], root=workspace)
    as_json("steps", "add", "deposit", "--name", "Oxide", "--material", "SiO2", "--set", "target=0.02", root=workspace)
    as_json("steps", "add", "deposit", "--name", "Nitride", "--material", "SiN", "--set", "target=0.03", root=workspace)
    as_json("steps", "add", "cmp", "--name", "Polish", root=workspace)
    # The two films become a pair repeated three times: the pair itself is
    # iteration 1, and two copies follow it, before the polish.
    steps = as_json("steps", "loop", "Oxide", "Nitride", "--repeat", "3", "--name", "ON pair", root=workspace)
    assert [step["name"] for step in steps] == ["Oxide", "Nitride"] * 3 + ["Polish"]
    assert [step["loop"]["iteration"] for step in steps[:6]] == [0, 0, 1, 1, 2, 2]
    assert len({step["loop"]["id"] for step in steps[:6]}) == 1
    assert steps[6]["loop"] is None
    code, out, _ = run("steps", "list", root=workspace)
    assert code == EXIT_OK and "ON pair 2/3" in out

    # The file writes the loop once; applying it unrolls it again.
    flow = as_json("flow", "dump", root=workspace)
    assert [entry.get("loop") or entry["name"] for entry in flow["steps"]] == ["ON pair", "Polish"]
    assert flow["steps"][0]["repeat"] == 3
    assert [spec["name"] for spec in flow["steps"][0]["steps"]] == ["Oxide", "Nitride"]
    flow["steps"][0]["repeat"] = 2
    (tmp_path / "flow.yaml").write_text(__import__("yaml").safe_dump(flow, sort_keys=False))
    applied = as_json("flow", "apply", str(tmp_path / "flow.yaml"), root=workspace)
    assert [step["name"] for step in applied] == ["Oxide", "Nitride"] * 2 + ["Polish"]
    # Steps keep their identity by position, so the first pair is untouched.
    assert [step["id"] for step in applied[:4]] == [step["id"] for step in steps[:4]]

    # A loop within a loop in the file unrolls into its parent.
    nested = {"steps": [{"loop": "Outer", "repeat": 2, "steps": [
        {"name": "A", "type": "cmp"},
        {"loop": "Inner", "repeat": 2, "steps": [{"name": "B", "type": "cmp"}]},
    ]}]}
    (tmp_path / "nested.json").write_text(json.dumps(nested))
    unrolled = as_json("flow", "apply", str(tmp_path / "nested.json"), root=workspace)
    assert [step["name"] for step in unrolled] == ["A", "B", "B"] * 2
    assert all(step["loop"]["name"] == "Outer" for step in unrolled)

    # Taking a loop apart leaves plain steps; a copy never joins a loop.
    plain = as_json("steps", "unloop", "1", root=workspace)
    assert all(step["loop"] is None for step in plain)
    code, _, err = run("steps", "loop", "1", "3", root=workspace)
    assert code == EXIT_USAGE and "next to each other" in err
