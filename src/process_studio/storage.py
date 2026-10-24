"""Embedded SQLite persistence and reference-counted snapshot files."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import (
    FIDELITIES,
    RETIRED_FIDELITIES,
    known_fidelity,
    result_key,
    result_keys,
    FlowBranch,
    MaterialDefinition,
    MaterialResponse,
    ProcessStep,
    ProcessType,
    ProjectDefinition,
    Recipe,
    ToolDefinition,
)
from .shared_library import SharedLibrary, recipe_from_payload


#: Process-log lines a workspace keeps; older ones are dropped when it is tidied.
KEPT_LOG_LINES = 5000

#: A file in the results folder that no stored result owns is only removed
#: once it is this old: a run may be writing it right now.
ORPHAN_AGE_SECONDS = 600


def snapshot_files(path: Path) -> list[Path]:
    """A stored result's file and every file kept beside it under its name
    (the 3D meshes built from it, one per triangulator and face set)."""
    return [path, *path.parent.glob(f"{path.name}.*")]


class ProjectRepository:
    """One workspace on disk, plus the shared library it draws its names from.

    Materials, tools and recipes are the user's, not the project's: the same
    oxide and the same etch recipe belong in every workspace they open. So
    those three live in one shared library (see ``shared_library``) and the
    project's own tables are kept as a copy of it, which is what makes a
    workspace portable -- zip it, send it, and the steps still name
    materials the person on the other end can see. Opening a workspace
    hands the library whatever it has never seen, without letting it
    overwrite anything already there.
    """

    def __init__(self, database_path: str | Path, library: SharedLibrary | None = None) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot_directory = self.database_path.with_suffix("").with_name(
            self.database_path.stem + "_snapshots"
        )
        self.snapshot_directory.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self.library = library if library is not None else self._shared_library()
        self.adopted = self._learn_from_this_workspace()

    @property
    def workspace(self) -> Path:
        """The directory this workspace's files live in."""
        return self.database_path.parent

    def as_stored(self, resolved: str | Path | None) -> str | None:
        """A path as it goes into the database: relative to this workspace.

        An absolute path names the machine it was written on. A workspace
        carrying one stops finding the file the moment it is copied
        anywhere -- another machine, another user's home, a zip and back --
        and everything that needed the file then quietly does not work.
        Every file a project refers to is inside the workspace (importing a
        layout copies it into ``layouts``, a snapshot is written into the
        snapshot directory), so what is stored is where it sits in the
        workspace. A path that really does point outside is kept as it is.
        """
        if resolved is None or resolved == "":
            return None if resolved is None else ""
        path = Path(resolved)
        try:
            return path.relative_to(self.workspace).as_posix()
        except ValueError:
            return str(path)

    def on_disk(self, stored: str | None, adopt_in: Path | None = None) -> str | None:
        """Where a stored path actually is, now, on this machine.

        ``adopt_in`` is where to look for a file named by a path written
        before they were stored relative -- absolute, from another machine,
        Windows separators and all. Everything a project refers to lives in
        one known directory per kind, so the name is enough to find it
        again; a name that is not there is left alone, so the error the
        user sees still carries the path they chose.
        """
        if not stored:
            return stored
        # "Absolute" has to mean absolute on the machine it was written on,
        # not on this one: to a POSIX Path, ``\\?\C:\Users\...`` is a
        # relative file whose name happens to contain backslashes.
        absolute_somewhere = (
            Path(stored).is_absolute()
            or stored.startswith(("/", "\\"))
            or (len(stored) > 1 and stored[1] == ":")
        )
        if not absolute_somewhere:
            # Written as a POSIX relative path; a Windows one is read too.
            return str(self.workspace.joinpath(*stored.replace("\\", "/").split("/")))
        if Path(stored).exists():
            return stored
        if adopt_in is not None:
            name = stored.replace("\\", "/").rsplit("/", 1)[-1]
            moved = adopt_in / name
            if moved.is_file():
                return str(moved)
        return stored

    @property
    def layouts_directory(self) -> Path:
        """Where importing a layout puts it."""
        return self.workspace / "layouts"

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    grid_json TEXT NOT NULL,
                    gds_path TEXT,
                    active_branch_id TEXT,
                    kernel TEXT NOT NULL DEFAULT 'slab',
                    resolution_um REAL,
                    resolution_xy_um REAL,
                    section_lines_json TEXT,
                    fidelity TEXT NOT NULL DEFAULT 'simplified',
                    experiment_json TEXT
                );
                CREATE TABLE IF NOT EXISTS materials (
                    id TEXT PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recipes (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tools (
                    id TEXT PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS branches (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    parent_branch_id TEXT,
                    parent_step_id TEXT,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS branch_steps (
                    branch_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(branch_id, step_id),
                    FOREIGN KEY(branch_id) REFERENCES branches(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS branch_snapshots (
                    branch_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    PRIMARY KEY(branch_id, step_id),
                    FOREIGN KEY(branch_id) REFERENCES branches(id) ON DELETE CASCADE,
                    FOREIGN KEY(snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS process_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    branch_id TEXT,
                    step_id TEXT,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    elapsed_ms REAL,
                    created_at TEXT NOT NULL
                );
                """
            )
            # Databases written before the kernel was recorded have neither
            # column. Those projects were made on the level-set kernel, so the
            # added column says so and opening one is refused with a message
            # naming the version that can still read it.
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(projects)").fetchall()
            }
            if "kernel" not in columns:
                connection.execute(
                    "ALTER TABLE projects ADD COLUMN kernel TEXT NOT NULL DEFAULT 'levelset'"
                )
            if "resolution_um" not in columns:
                connection.execute("ALTER TABLE projects ADD COLUMN resolution_um REAL")
            if "resolution_xy_um" not in columns:
                connection.execute("ALTER TABLE projects ADD COLUMN resolution_xy_um REAL")
            if "section_lines_json" not in columns:
                connection.execute("ALTER TABLE projects ADD COLUMN section_lines_json TEXT")
            if "fidelity" not in columns:
                connection.execute(
                    "ALTER TABLE projects ADD COLUMN fidelity TEXT NOT NULL DEFAULT 'detailed'"
                )
            if "experiment_json" not in columns:
                connection.execute("ALTER TABLE projects ADD COLUMN experiment_json TEXT")

    def save_project(self, project: ProjectDefinition) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO projects(
                    id, name, grid_json, gds_path, active_branch_id, kernel, resolution_um,
                    resolution_xy_um, section_lines_json, fidelity, experiment_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                -- kernel is deliberately absent from the update list: a
                -- project keeps the kernel it was created with.
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                grid_json=excluded.grid_json, gds_path=excluded.gds_path,
                active_branch_id=excluded.active_branch_id,
                resolution_um=excluded.resolution_um,
                resolution_xy_um=excluded.resolution_xy_um,
                section_lines_json=excluded.section_lines_json,
                fidelity=excluded.fidelity,
                experiment_json=excluded.experiment_json""",
                (
                    project.id,
                    project.name,
                    json.dumps(project.grid),
                    self.as_stored(project.gds_path),
                    project.active_branch_id,
                    project.kernel,
                    project.resolution_um,
                    project.resolution_xy_um,
                    json.dumps(project.section_lines),
                    project.fidelity,
                    None if project.experiment_steps is None else json.dumps(project.experiment_steps),
                ),
            )

    def load_project(self, project_id: str) -> ProjectDefinition:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        if row is None:
            raise KeyError(project_id)
        return ProjectDefinition(
            id=row["id"],
            name=row["name"],
            grid=json.loads(row["grid_json"]),
            gds_path=self.on_disk(row["gds_path"], self.layouts_directory),
            active_branch_id=row["active_branch_id"],
            kernel=row["kernel"] or "levelset",
            resolution_um=row["resolution_um"],
            resolution_xy_um=row["resolution_xy_um"],
            section_lines=json.loads(row["section_lines_json"]) if row["section_lines_json"] else [],
            fidelity=known_fidelity(row["fidelity"], "detailed"),
            experiment_steps=json.loads(row["experiment_json"]) if row["experiment_json"] else None,
        )

    def list_projects(self) -> list[ProjectDefinition]:
        with self.connect() as connection:
            ids = [row[0] for row in connection.execute("SELECT id FROM projects ORDER BY name")]
        return [self.load_project(project_id) for project_id in ids]

    # -- the shared library, and this workspace's copy of it ---------------

    def save_material(self, material: MaterialDefinition) -> None:
        """Write a material to the library and to this workspace's copy.

        By id, so a rename updates it rather than inserting. Looking the row
        up by name meant a renamed material was not found and was inserted
        again under its existing id, which the primary key refused on every
        autosave. The id is the identity; the name is a label that must
        merely be unique.
        """
        self.library.save_material(material)
        self._store("materials", material.id, material.name, asdict(material))

    def load_materials(self) -> list[MaterialDefinition]:
        return self.library.load_materials()

    def remove_material(self, material_id: str) -> None:
        self.library.remove_material(material_id)
        self._drop("materials", "id", material_id)

    def save_tool(self, tool: ToolDefinition) -> None:
        """Write a tool; the name is a label that must merely be unique."""
        self.library.save_tool(tool)
        self._store("tools", tool.id, tool.name, asdict(tool))

    def load_tools(self) -> list[ToolDefinition]:
        return self.library.load_tools()

    def remove_tool(self, tool_id: str) -> None:
        self.library.remove_tool(tool_id)
        self._drop("tools", "id", tool_id)

    def save_recipe(self, recipe: Recipe) -> None:
        payload = asdict(recipe)
        payload["process_type"] = recipe.process_type.value
        self.library.save_recipe(recipe)
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM recipes WHERE name=? AND id<>?", (recipe.name, recipe.id)
            )
        self._store("recipes", recipe.id, recipe.name, payload)

    def load_recipes(self) -> list[Recipe]:
        return self.library.load_recipes()

    def remove_recipe(self, recipe_id: str) -> None:
        self.library.remove_recipe(recipe_id)
        self._drop("recipes", "id", recipe_id)

    def _store(self, table: str, row_id: str, name: str, payload: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                f"""INSERT INTO {table}(id, name, payload_json) VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                payload_json=excluded.payload_json""",
                (row_id, name, json.dumps(payload)),
            )

    def _drop(self, table: str, column: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute(f"DELETE FROM {table} WHERE {column}=?", (value,))

    def own_materials(self) -> list[MaterialDefinition]:
        """This workspace's own copy of the library.

        The copy is what a zipped workspace travels with, and where the
        library gets back anything it lost without the user deleting it
        (see :meth:`_learn_from_this_workspace`). It catches up on open and
        on the saves this workspace makes.
        """
        with self.connect() as connection:
            rows = connection.execute("SELECT payload_json FROM materials ORDER BY name")
            return [MaterialDefinition(**json.loads(row[0])) for row in rows]

    def own_tools(self) -> list[ToolDefinition]:
        with self.connect() as connection:
            rows = connection.execute("SELECT payload_json FROM tools ORDER BY name")
            return [ToolDefinition(**json.loads(row[0])) for row in rows]

    def own_recipes(self) -> list[Recipe]:
        with self.connect() as connection:
            rows = connection.execute("SELECT payload_json FROM recipes ORDER BY name")
            return [recipe_from_payload(json.loads(row[0])) for row in rows]

    def _shared_library(self) -> SharedLibrary:
        """The user's library, or one in this workspace if there is nowhere else.

        A home directory that cannot be written to is rare, and the answer
        to it is not to refuse to open the workspace: a library beside the
        project is what every project had before they were shared.
        """
        try:
            return SharedLibrary()
        except (OSError, sqlite3.Error):
            return SharedLibrary(self.workspace / "library.sqlite3")

    def _learn_from_this_workspace(self) -> list[str]:
        """Hand the library whatever this workspace arrived with.

        A workspace from elsewhere teaches the library every name it does
        not have. A copy of this same library is a mirror of it, so from it
        comes back only what the library is missing and the user did not
        delete: a deletion is recorded, and anything else that took an
        entry out of the library is not the user's wish.
        """
        if self._setting("library") == self.library.identity:
            # A copy of this same library has nothing new -- except what the
            # library lost without the user deleting it, which comes back.
            return self.library.adopt(
                self.own_materials(), self.own_tools(), self.own_recipes(), skip_deleted=True
            )
        taken = self.library.adopt(self.own_materials(), self.own_tools(), self.own_recipes())
        self._remember("library", self.library.identity)
        return taken

    def _setting(self, key: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else None

    def _remember(self, key: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, value),
            )

    def refresh_library_copy(self) -> None:
        """Leave this workspace holding a copy of the library, so it travels.

        A workspace that is zipped and sent has to carry the names its steps
        use; the library on the other machine is somebody else's. This is
        called when a window is handed the document -- opening the
        workspace -- after the library has taken back from the copy what it
        had lost.
        """
        materials = self.library.load_materials()
        tools = self.library.load_tools()
        recipes = self.library.load_recipes()
        with self.connect() as connection:
            for table, items in (
                ("materials", materials),
                ("tools", tools),
                ("recipes", recipes),
            ):
                keep = {item.id for item in items}
                stored = [row[0] for row in connection.execute(f"SELECT id FROM {table}")]
                for row_id in stored:
                    if row_id not in keep:
                        connection.execute(f"DELETE FROM {table} WHERE id=?", (row_id,))
        for material in materials:
            self._store("materials", material.id, material.name, asdict(material))
        for tool in tools:
            self._store("tools", tool.id, tool.name, asdict(tool))
        for recipe in recipes:
            payload = asdict(recipe)
            payload["process_type"] = recipe.process_type.value
            self._store("recipes", recipe.id, recipe.name, payload)

    def save_branch(self, project_id: str, branch: FlowBranch) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO branches(id, project_id, name, parent_branch_id, parent_step_id)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                parent_branch_id=excluded.parent_branch_id,
                parent_step_id=excluded.parent_step_id""",
                (
                    branch.id,
                    project_id,
                    branch.name,
                    branch.parent_branch_id,
                    branch.parent_step_id,
                ),
            )
            connection.execute("DELETE FROM branch_steps WHERE branch_id = ?", (branch.id,))
            for position, step in enumerate(branch.steps):
                connection.execute(
                    "INSERT INTO branch_steps VALUES (?, ?, ?, ?)",
                    (branch.id, step.id, position, json.dumps(asdict(step))),
                )

    def load_branch(self, branch_id: str) -> FlowBranch:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM branches WHERE id = ?", (branch_id,)
            ).fetchone()
            if row is None:
                raise KeyError(branch_id)
            step_rows = connection.execute(
                "SELECT payload_json FROM branch_steps WHERE branch_id = ? ORDER BY position",
                (branch_id,),
            )
            steps = [ProcessStep(**json.loads(step[0])) for step in step_rows]
        return FlowBranch(
            id=row["id"],
            name=row["name"],
            steps=steps,
            parent_branch_id=row["parent_branch_id"],
            parent_step_id=row["parent_step_id"],
        )

    def list_branches(self, project_id: str) -> list[FlowBranch]:
        with self.connect() as connection:
            ids = [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM branches WHERE project_id=? ORDER BY name",
                    (project_id,),
                )
            ]
        return [self.load_branch(branch_id) for branch_id in ids]

    def create_branch(
        self,
        project_id: str,
        source_branch_id: str,
        through_step_id: str,
        name: str,
    ) -> FlowBranch:
        source = self.load_branch(source_branch_id)
        step_ids = [step.id for step in source.steps]
        if through_step_id not in step_ids:
            raise KeyError(through_step_id)
        end = step_ids.index(through_step_id) + 1
        branch = FlowBranch(
            name=name,
            steps=[ProcessStep(**asdict(step)) for step in source.steps[:end]],
            parent_branch_id=source_branch_id,
            parent_step_id=through_step_id,
        )
        self.save_branch(project_id, branch)
        with self.connect() as connection:
            source_links = connection.execute(
                """SELECT step_id, snapshot_id FROM branch_snapshots
                WHERE branch_id = ?""",
                (source_branch_id,),
            ).fetchall()
            # Every fidelity's result comes across, not only the detailed
            # one: the steps up to the fork are the same steps, so whatever
            # was computed for them holds on this branch too. The files are
            # shared rather than copied -- a snapshot is deleted when the
            # last branch that links to it lets go.
            allowed = {key for step in branch.steps for key in result_keys(step.id)}
            for link in source_links:
                if link["step_id"] in allowed:
                    connection.execute(
                        "INSERT OR REPLACE INTO branch_snapshots VALUES (?, ?, ?)",
                        (branch.id, link["step_id"], link["snapshot_id"]),
                    )
        return branch

    def rename_branch(self, branch_id: str, name: str) -> FlowBranch:
        """Give a branch another name; the name is a label, the id is what runs."""
        branch = self.load_branch(branch_id)
        with self.connect() as connection:
            connection.execute("UPDATE branches SET name=? WHERE id=?", (name, branch_id))
        branch.name = name
        return branch

    def delete_branch(self, branch_id: str) -> None:
        """Forget a branch and the results only it was holding on to.

        The steps and the links go with the row (both cascade); a snapshot
        file goes only when nothing else points at it, because a branch
        shares the results of everything before the fork with the branch it
        was forked from.
        """
        with self.connect() as connection:
            snapshot_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT snapshot_id FROM branch_snapshots WHERE branch_id=?", (branch_id,)
                )
            ]
            connection.execute("DELETE FROM branches WHERE id=?", (branch_id,))
        for snapshot_id in snapshot_ids:
            self._delete_snapshot_if_unreferenced(snapshot_id)

    def save_snapshot(
        self,
        project_id: str,
        branch_id: str,
        step_id: str,
        state: Any,
        *,
        suffix: str = ".npz",
    ) -> str:
        """Store one step's result. The kernel owns the file format."""
        snapshot_id = uuid4().hex
        path = self.snapshot_directory / f"{snapshot_id}{suffix}"
        state.save(path)
        with self.connect() as connection:
            old = connection.execute(
                "SELECT snapshot_id FROM branch_snapshots WHERE branch_id=? AND step_id=?",
                (branch_id, step_id),
            ).fetchone()
            connection.execute(
                "INSERT INTO snapshots VALUES (?, ?, ?, ?)",
                (
                    snapshot_id,
                    project_id,
                    self.as_stored(path),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            connection.execute(
                "INSERT OR REPLACE INTO branch_snapshots VALUES (?, ?, ?)",
                (branch_id, step_id, snapshot_id),
            )
        if old is not None:
            self._delete_snapshot_if_unreferenced(old["snapshot_id"])
        return snapshot_id

    def snapshot_path(self, branch_id: str, step_id: str) -> Path:
        """Where a step's stored result lives, whatever wrote it."""
        with self.connect() as connection:
            row = connection.execute(
                """SELECT snapshots.path FROM snapshots JOIN branch_snapshots
                ON snapshots.id = branch_snapshots.snapshot_id
                WHERE branch_snapshots.branch_id=? AND branch_snapshots.step_id=?""",
                (branch_id, step_id),
            ).fetchone()
        if row is None:
            raise KeyError((branch_id, step_id))
        return Path(self.on_disk(row["path"], self.snapshot_directory) or row["path"])

    def delete_project_snapshots(self, project_id: str) -> int:
        """Remove every cached state for a project while preserving its flow."""
        with self.connect() as connection:
            snapshot_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM snapshots WHERE project_id=?", (project_id,)
                )
            ]
            if snapshot_ids:
                placeholders = ",".join("?" for _ in snapshot_ids)
                connection.execute(
                    f"DELETE FROM branch_snapshots WHERE snapshot_id IN ({placeholders})",
                    snapshot_ids,
                )
        for snapshot_id in snapshot_ids:
            self._delete_snapshot_if_unreferenced(snapshot_id)
        return len(snapshot_ids)

    def delete_step_and_dependents(self, branch_id: str, step_id: str) -> list[str]:
        branch = self.load_branch(branch_id)
        ids = [step.id for step in branch.steps]
        if step_id not in ids:
            raise KeyError(step_id)
        removed = ids[ids.index(step_id) :]
        # Results of every fidelity go with the step.
        stored_keys = [key for step_id_ in removed for key in result_keys(step_id_)]
        snapshot_ids: list[str] = []
        with self.connect() as connection:
            placeholders = ",".join("?" for _ in stored_keys)
            params = [branch_id, *stored_keys]
            snapshot_ids = [
                row[0]
                for row in connection.execute(
                    f"SELECT snapshot_id FROM branch_snapshots WHERE branch_id=? "
                    f"AND step_id IN ({placeholders})",
                    params,
                )
            ]
            connection.execute(
                f"DELETE FROM branch_snapshots WHERE branch_id=? "
                f"AND step_id IN ({placeholders})",
                params,
            )
            connection.execute(
                f"DELETE FROM branch_steps WHERE branch_id=? "
                f"AND step_id IN ({','.join('?' for _ in removed)})",
                [branch_id, *removed],
            )
        for snapshot_id in snapshot_ids:
            self._delete_snapshot_if_unreferenced(snapshot_id)
        return removed

    def _delete_snapshot_if_unreferenced(self, snapshot_id: str) -> None:
        with self.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM branch_snapshots WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()[0]
            if count:
                return
            row = connection.execute(
                "SELECT path FROM snapshots WHERE id=?", (snapshot_id,)
            ).fetchone()
            connection.execute("DELETE FROM snapshots WHERE id=?", (snapshot_id,))
        if row is not None:
            found = self.on_disk(row["path"], self.snapshot_directory)
            if found:
                for path in snapshot_files(Path(found)):
                    path.unlink(missing_ok=True)

    # -- what the workspace holds on disk, and tidying it -------------------------

    def storage(self, project: ProjectDefinition) -> dict[str, Any]:
        """How much each kind of thing in the workspace takes, in bytes."""
        return self._survey(project)["sizes"]

    def tidy(
        self,
        project: ProjectDefinition,
        *,
        meshes: bool = False,
        other_fidelity: bool = False,
        layouts: bool = True,
        compact: bool = False,
    ) -> dict[str, Any]:
        """Remove what nothing needs, and on request what can be rebuilt.

        Always: results of steps that are no longer on their branch, of film
        models there no longer are, files no stored result owns (a 3D mesh
        left behind by a result that was replaced, a file from a run that
        was cut short), and process-log lines past the newest
        :data:`KEPT_LOG_LINES`. With ``layouts``, GDS files in the
        workspace's layouts folder no project uses (every import keeps a
        copy). With ``meshes``, every 3D mesh kept beside a result; they are
        built again when a step's 3D view is opened. With
        ``other_fidelity``, the results of the film model the project is not
        set to. ``compact`` gives the database's free pages back to the disk.
        """
        before = self._survey(project)
        with self.connect() as connection:
            wanted_steps = {
                row["branch_id"]: set()
                for row in connection.execute("SELECT id AS branch_id FROM branches")
            }
            for row in connection.execute("SELECT branch_id, step_id FROM branch_steps"):
                wanted_steps.setdefault(row["branch_id"], set()).add(row["step_id"])
            current = known_fidelity(project.fidelity)
            kept_suffixes = {result_key("", fidelity) for fidelity in FIDELITIES}
            if other_fidelity:
                kept_suffixes = {result_key("", current)}
            dropped: list[tuple[str, str]] = []
            for row in connection.execute("SELECT branch_id, step_id FROM branch_snapshots"):
                step, _, suffix = row["step_id"].partition("~")
                key_suffix = f"~{suffix}" if suffix else ""
                if step not in wanted_steps.get(row["branch_id"], set()) or key_suffix not in kept_suffixes:
                    dropped.append((row["branch_id"], row["step_id"]))
            for branch_id, key in dropped:
                connection.execute(
                    "DELETE FROM branch_snapshots WHERE branch_id=? AND step_id=?", (branch_id, key)
                )
            self._forget_digests(connection, dropped)
            unreferenced = [
                row["id"]
                for row in connection.execute(
                    """SELECT id FROM snapshots WHERE id NOT IN
                    (SELECT snapshot_id FROM branch_snapshots)"""
                )
            ]
            connection.execute(
                """DELETE FROM process_logs WHERE id NOT IN
                (SELECT id FROM process_logs ORDER BY id DESC LIMIT ?)""",
                (KEPT_LOG_LINES,),
            )
        for snapshot_id in unreferenced:
            self._delete_snapshot_if_unreferenced(snapshot_id)

        # files no stored result owns; a young one may belong to a run
        # that is writing it right now
        owned = self._owned_snapshot_names()
        cutoff = time.time() - ORPHAN_AGE_SECONDS
        for path in self.snapshot_directory.iterdir():
            if not path.is_file():
                continue
            base = path.name.split(".", 1)[0]
            if base not in owned:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            elif meshes and ".mesh-" in path.name:
                path.unlink(missing_ok=True)

        if layouts:
            for path in self._unused_layouts():
                path.unlink(missing_ok=True)
        if compact:
            with self.connect() as connection:
                connection.execute("VACUUM")
        after = self._survey(project)
        return {
            "before": before["sizes"],
            "after": after["sizes"],
            "freed": max(0, before["sizes"]["total"] - after["sizes"]["total"]),
            "droppedResults": len(dropped),
        }

    def _forget_digests(self, connection: sqlite3.Connection, keys: list[tuple[str, str]]) -> None:
        """The worker's record of what produced a result goes with the result."""
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='worker_step_digests'"
        ).fetchone()
        if exists:
            connection.executemany(
                "DELETE FROM worker_step_digests WHERE branch_id=? AND step_id=?", keys
            )

    def _owned_snapshot_names(self) -> set[str]:
        with self.connect() as connection:
            rows = connection.execute("SELECT id, path FROM snapshots").fetchall()
        names = set()
        for row in rows:
            names.add(row["id"])
            names.add(Path(str(row["path"]).replace("\\", "/")).name.split(".", 1)[0])
        return names

    def _layout_paths_in_use(self) -> set[Path]:
        with self.connect() as connection:
            rows = connection.execute("SELECT gds_path FROM projects").fetchall()
        used = set()
        for row in rows:
            found = self.on_disk(row["gds_path"], self.layouts_directory)
            if found:
                used.add(Path(found).resolve())
        return used

    def _unused_layouts(self) -> list[Path]:
        if not self.layouts_directory.is_dir():
            return []
        used = self._layout_paths_in_use()
        return [
            path
            for path in self.layouts_directory.iterdir()
            if path.is_file() and path.resolve() not in used
        ]

    def _survey(self, project: ProjectDefinition) -> dict[str, Any]:
        """Bytes per kind: results of this film model and of the other one,
        3D meshes, files nothing owns, layouts in use and not, the database."""
        current = known_fidelity(project.fidelity)
        with self.connect() as connection:
            links = connection.execute(
                """SELECT branch_snapshots.step_id AS key, snapshots.id AS id, snapshots.path AS path
                FROM branch_snapshots JOIN snapshots ON snapshots.id = branch_snapshots.snapshot_id"""
            ).fetchall()
        fidelity_of: dict[str, str] = {}
        for row in links:
            _, _, suffix = row["key"].partition("~")
            fidelity = suffix or "detailed"
            # a result shared by two keys counts once, as the current model's if either is
            if fidelity_of.get(row["id"]) != current:
                fidelity_of[row["id"]] = fidelity
        owned = self._owned_snapshot_names()
        sizes = {
            "results": 0,
            "otherFidelity": 0,
            "retired": 0,
            "meshes": 0,
            "leftovers": 0,
            "layouts": 0,
            "unusedLayouts": 0,
            "database": 0,
        }
        counts = {"results": 0, "otherFidelity": 0, "meshes": 0, "leftovers": 0, "unusedLayouts": 0}
        for path in self.snapshot_directory.iterdir() if self.snapshot_directory.is_dir() else []:
            if not path.is_file():
                continue
            size = path.stat().st_size
            base = path.name.split(".", 1)[0]
            if base not in owned:
                sizes["leftovers"] += size
                counts["leftovers"] += 1
            elif ".mesh-" in path.name:
                sizes["meshes"] += size
                counts["meshes"] += 1
            else:
                fidelity = fidelity_of.get(base)
                if fidelity == current:
                    sizes["results"] += size
                    counts["results"] += 1
                elif fidelity is None:
                    # a result no step links to any more
                    sizes["leftovers"] += size
                    counts["leftovers"] += 1
                elif fidelity in RETIRED_FIDELITIES:
                    sizes["retired"] += size
                else:
                    sizes["otherFidelity"] += size
                    counts["otherFidelity"] += 1
        unused = set(self._unused_layouts())
        if self.layouts_directory.is_dir():
            for path in self.layouts_directory.iterdir():
                if path.is_file():
                    key = "unusedLayouts" if path in unused else "layouts"
                    sizes[key] += path.stat().st_size
                    if path in unused:
                        counts["unusedLayouts"] += 1
        for suffix in ("", "-wal", "-shm"):
            database = Path(f"{self.database_path}{suffix}")
            if database.is_file():
                sizes["database"] += database.stat().st_size
        sizes["total"] = sum(value for value in sizes.values())
        return {"sizes": {**sizes, "counts": counts, "fidelity": current}}

    def log(
        self,
        project_id: str,
        message: str,
        *,
        branch_id: str | None = None,
        step_id: str | None = None,
        level: str = "INFO",
        elapsed_ms: float | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO process_logs(project_id, branch_id, step_id, level,
                message, elapsed_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    project_id,
                    branch_id,
                    step_id,
                    level,
                    message,
                    elapsed_ms,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
