"""Embedded SQLite persistence and reference-counted snapshot files."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .kernel.material_state import MaterialState
from .models import (
    FlowBranch,
    MaterialDefinition,
    MaterialResponse,
    ProcessStep,
    ProcessType,
    ProjectDefinition,
    Recipe,
)


class ProjectRepository:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot_directory = self.database_path.with_suffix("").with_name(
            self.database_path.stem + "_snapshots"
        )
        self.snapshot_directory.mkdir(parents=True, exist_ok=True)
        self._initialize()

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
                    active_branch_id TEXT
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

    def save_project(self, project: ProjectDefinition) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO projects(id, name, grid_json, gds_path, active_branch_id)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                grid_json=excluded.grid_json, gds_path=excluded.gds_path,
                active_branch_id=excluded.active_branch_id""",
                (
                    project.id,
                    project.name,
                    json.dumps(project.grid),
                    project.gds_path,
                    project.active_branch_id,
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
            gds_path=row["gds_path"],
            active_branch_id=row["active_branch_id"],
        )

    def list_projects(self) -> list[ProjectDefinition]:
        with self.connect() as connection:
            ids = [row[0] for row in connection.execute("SELECT id FROM projects ORDER BY name")]
        return [self.load_project(project_id) for project_id in ids]

    def save_material(self, material: MaterialDefinition) -> None:
        payload = asdict(material)
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM materials WHERE name=?", (material.name,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO materials(id, name, payload_json) VALUES (?, ?, ?)",
                    (material.id, material.name, json.dumps(payload)),
                )
            else:
                payload["id"] = existing["id"]
                connection.execute(
                    "UPDATE materials SET payload_json=? WHERE name=?",
                    (json.dumps(payload), material.name),
                )

    def load_materials(self) -> list[MaterialDefinition]:
        with self.connect() as connection:
            rows = connection.execute("SELECT payload_json FROM materials ORDER BY name")
            return [MaterialDefinition(**json.loads(row[0])) for row in rows]

    def save_recipe(self, recipe: Recipe) -> None:
        payload = asdict(recipe)
        payload["process_type"] = recipe.process_type.value
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM recipes WHERE name=? AND id<>?", (recipe.name, recipe.id)
            )
            connection.execute(
                """INSERT INTO recipes(id, name, payload_json) VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                payload_json=excluded.payload_json""",
                (recipe.id, recipe.name, json.dumps(payload)),
            )

    def load_recipes(self) -> list[Recipe]:
        recipes = []
        with self.connect() as connection:
            rows = connection.execute("SELECT payload_json FROM recipes ORDER BY name")
            for row in rows:
                payload = json.loads(row[0])
                payload["process_type"] = ProcessType(payload["process_type"])
                payload["material_responses"] = {
                    name: MaterialResponse(**response)
                    for name, response in payload["material_responses"].items()
                }
                recipes.append(Recipe(**payload))
        return recipes

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

    def load_latest_snapshot(self, branch_id: str) -> MaterialState | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT snapshots.path FROM branch_snapshots
                JOIN branch_steps ON branch_steps.branch_id=branch_snapshots.branch_id
                    AND branch_steps.step_id=branch_snapshots.step_id
                JOIN snapshots ON snapshots.id=branch_snapshots.snapshot_id
                WHERE branch_snapshots.branch_id=?
                ORDER BY branch_steps.position DESC LIMIT 1""",
                (branch_id,),
            ).fetchone()
        return None if row is None else MaterialState.load(row["path"])

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
            allowed = {step.id for step in branch.steps}
            for link in source_links:
                if link["step_id"] in allowed:
                    connection.execute(
                        "INSERT OR REPLACE INTO branch_snapshots VALUES (?, ?, ?)",
                        (branch.id, link["step_id"], link["snapshot_id"]),
                    )
        return branch

    def save_snapshot(
        self,
        project_id: str,
        branch_id: str,
        step_id: str,
        state: MaterialState,
    ) -> str:
        snapshot_id = uuid4().hex
        path = self.snapshot_directory / f"{snapshot_id}.npz"
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
                    str(path),
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

    def load_snapshot(self, branch_id: str, step_id: str) -> MaterialState:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT snapshots.path FROM snapshots JOIN branch_snapshots
                ON snapshots.id = branch_snapshots.snapshot_id
                WHERE branch_snapshots.branch_id=? AND branch_snapshots.step_id=?""",
                (branch_id, step_id),
            ).fetchone()
        if row is None:
            raise KeyError((branch_id, step_id))
        return MaterialState.load(row["path"])

    def delete_step_and_dependents(self, branch_id: str, step_id: str) -> list[str]:
        branch = self.load_branch(branch_id)
        ids = [step.id for step in branch.steps]
        if step_id not in ids:
            raise KeyError(step_id)
        removed = ids[ids.index(step_id) :]
        snapshot_ids: list[str] = []
        with self.connect() as connection:
            placeholders = ",".join("?" for _ in removed)
            params = [branch_id, *removed]
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
                f"AND step_id IN ({placeholders})",
                params,
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
            Path(row["path"]).unlink(missing_ok=True)

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
