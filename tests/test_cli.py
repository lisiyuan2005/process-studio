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
