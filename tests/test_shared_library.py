"""The materials, tools and recipes belong to the user, not to a project."""

import io

import pytest

from process_studio.models import MaterialDefinition, ProcessType, Recipe, ToolDefinition
from process_studio.shared_library import SharedLibrary, library_path
from process_studio.storage import ProjectRepository
from process_studio.worker.protocol import dispatch
from process_studio.worker.workspace import initialize_workspace, open_repository


def call(method: str, **params):
    return dispatch({"method": method, "params": params}, io.StringIO())


def test_the_library_is_where_the_environment_says(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PROCESS_STUDIO_LIBRARY", str(tmp_path / "shared" / "library.sqlite3"))
    assert library_path() == tmp_path / "shared" / "library.sqlite3"
    SharedLibrary()
    assert (tmp_path / "shared" / "library.sqlite3").is_file()


def test_a_material_saved_in_one_project_is_there_in_the_next(tmp_path) -> None:
    first = ProjectRepository(tmp_path / "one" / "project.sqlite3")
    first.save_material(MaterialDefinition("HfO2", category="Dielectric", color="#336699"))
    first.save_tool(ToolDefinition("Ion Mill"))
    first.save_recipe(Recipe("Argon Mill", ProcessType.ETCH, tool="Ion Mill"))

    second = ProjectRepository(tmp_path / "two" / "project.sqlite3")

    assert [material.name for material in second.load_materials()] == ["HfO2"]
    assert [tool.name for tool in second.load_tools()] == ["Ion Mill"]
    assert [recipe.name for recipe in second.load_recipes()] == ["Argon Mill"]


def test_editing_a_material_edits_it_everywhere(tmp_path) -> None:
    first = ProjectRepository(tmp_path / "one" / "project.sqlite3")
    first.save_material(MaterialDefinition("Si", color="#777777", id="material-si"))
    second = ProjectRepository(tmp_path / "two" / "project.sqlite3")

    first.save_material(MaterialDefinition("Si", color="#112233", id="material-si"))

    assert second.load_materials()[0].color == "#112233"


def test_a_project_keeps_a_copy_so_it_travels(tmp_path) -> None:
    """A zipped workspace has to name its materials on the other machine."""
    repository = ProjectRepository(tmp_path / "one" / "project.sqlite3")
    repository.save_material(MaterialDefinition("W", category="Metal"))

    with repository.connect() as connection:
        rows = connection.execute("SELECT name FROM materials").fetchall()

    assert [row["name"] for row in rows] == ["W"]


def test_a_workspace_from_elsewhere_teaches_the_library_what_is_new(tmp_path) -> None:
    somebody_else = tmp_path / "theirs" / "project.sqlite3"
    theirs = ProjectRepository(somebody_else, library=SharedLibrary(tmp_path / "theirs.sqlite3"))
    theirs.save_material(MaterialDefinition("Si", color="#ff0000", id="their-si"))
    theirs.save_material(MaterialDefinition("Ru", color="#00ff00", id="their-ru"))

    mine = SharedLibrary(tmp_path / "mine.sqlite3")
    mine.save_material(MaterialDefinition("Si", color="#7b68b8", id="my-si"))
    opened = ProjectRepository(somebody_else, library=mine)

    colors = {material.name: material.color for material in opened.load_materials()}
    # Their oxide came along; my silicon was not restyled by opening it.
    assert colors == {"Si": "#7b68b8", "Ru": "#00ff00"}


def test_a_new_workspace_does_not_bring_the_starter_library_back(tmp_path) -> None:
    initialize_workspace(tmp_path / "one", "First")
    first = open_repository(tmp_path / "one")
    seeded = len(first.load_recipes())
    assert seeded > 1
    first.remove_recipe(first.load_recipes()[0].id)

    initialize_workspace(tmp_path / "two", "Second")

    assert len(open_repository(tmp_path / "two").load_recipes()) == seeded - 1


def test_a_material_removed_in_one_project_is_removed_in_the_other(tmp_path) -> None:
    first = ProjectRepository(tmp_path / "one" / "project.sqlite3")
    first.save_material(MaterialDefinition("Si", id="material-si"))
    first.save_material(MaterialDefinition("Ge", id="material-ge"))
    second = ProjectRepository(tmp_path / "two" / "project.sqlite3")
    second.refresh_library_copy()

    first.remove_material("material-ge")

    assert [material.name for material in second.load_materials()] == ["Si"]
    # The copy this workspace travels with still has it: it is the record of
    # what the window was last shown, and it catches up when the workspace is
    # opened again, rather than handing the deleted material back.
    assert [material.name for material in second.own_materials()] == ["Ge", "Si"]
    reopened = ProjectRepository(tmp_path / "two" / "project.sqlite3")
    reopened.refresh_library_copy()
    assert [material.name for material in reopened.own_materials()] == ["Si"]
    assert [material.name for material in reopened.load_materials()] == ["Si"]


def test_two_materials_cannot_share_a_name(tmp_path) -> None:
    repository = ProjectRepository(tmp_path / "one" / "project.sqlite3")
    repository.save_material(MaterialDefinition("Si", id="material-si"))

    with pytest.raises(ValueError):
        repository.save_material(MaterialDefinition("Si", id="another-si"))


def test_a_window_that_never_saw_a_material_does_not_delete_it(tmp_path) -> None:
    """Two windows are open; one adds a material, the other saves its document."""
    call("create_workspace", root=str(tmp_path / "one"), name="First")
    call("create_workspace", root=str(tmp_path / "two"), name="Second")
    stale = call("open_workspace", root=str(tmp_path / "one"))

    other = open_repository(tmp_path / "two")
    other.save_material(MaterialDefinition("HfO2", category="Dielectric"))

    call("save_document", root=str(tmp_path / "one"), document=stale)

    assert "HfO2" in {material.name for material in other.load_materials()}


def test_deleting_a_material_in_a_window_that_had_it_does_delete_it(tmp_path) -> None:
    call("create_workspace", root=str(tmp_path / "one"), name="First")
    document = call("open_workspace", root=str(tmp_path / "one"))
    document["materials"] = [m for m in document["materials"] if m["name"] != "TiN"]

    call("save_document", root=str(tmp_path / "one"), document=document)

    assert "TiN" not in {
        material.name for material in open_repository(tmp_path / "one").load_materials()
    }


def test_a_library_that_cannot_be_written_falls_back_into_the_workspace(
    tmp_path, monkeypatch
) -> None:
    """A place the library cannot go is no reason not to open a project."""
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("")
    monkeypatch.setenv("PROCESS_STUDIO_LIBRARY", str(blocked / "library.sqlite3"))

    repository = ProjectRepository(tmp_path / "one" / "project.sqlite3")
    repository.save_material(MaterialDefinition("Si"))

    assert (tmp_path / "one" / "library.sqlite3").is_file()
    assert [material.name for material in repository.load_materials()] == ["Si"]
