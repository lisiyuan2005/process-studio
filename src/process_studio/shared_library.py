"""The materials, tools and recipes, kept once for every project.

A library is not part of a project. Somebody's oxide is their oxide in
every workspace they open, and a recipe worked out once is worth having
the next time whatever it was first written for. Keeping a copy per
project meant editing the same material in five places, and the five
drifting apart.

So the live library is one file for the user, and a project keeps a copy
of it as well. The copy is what makes a workspace portable -- zip it,
send it, and the steps still name materials the person on the other end
can see -- and it is where the shared library learns anything it does not
already have when such a workspace is opened. What is already in the
library wins: opening someone else's project must not restyle your
materials.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from .models import MaterialDefinition, MaterialResponse, ProcessType, Recipe, ToolDefinition


def library_path() -> Path:
    """Where the shared library lives on this machine.

    ``PROCESS_STUDIO_LIBRARY`` names another file, which is what a test
    uses and what lets a group point at a library on a share.
    """
    named = os.environ.get("PROCESS_STUDIO_LIBRARY")
    if named:
        return Path(named).expanduser()
    return library_directory() / "library.sqlite3"


def library_directory() -> Path:
    """The per-user directory for things that are not part of a project."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        return Path(base) / "ProcessStudio" if base else Path.home() / "ProcessStudio"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ProcessStudio"
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "process-studio"


class SharedLibrary:
    """The user's materials, tools and recipes, in one SQLite file."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else library_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS materials (
                    id TEXT PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tools (
                    id TEXT PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recipes (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('id', ?)", (uuid4().hex,)
            )

    @property
    def identity(self) -> str:
        """What tells this library apart from anybody else's.

        A workspace records the library its copy was taken from. A copy
        taken from *this* library carries no news -- it is a mirror, and a
        stale one -- so it is not read back in; only a workspace that came
        from elsewhere has anything to teach. Without that, deleting a
        material would not stick: the next project still holding a copy of
        it would hand it straight back.
        """
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM meta WHERE key='id'").fetchone()
        return str(row[0]) if row else ""

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def is_empty(self) -> bool:
        with self.connect() as connection:
            for table in ("materials", "tools", "recipes"):
                if connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                    return False
        return True

    # -- materials ---------------------------------------------------------

    def save_material(self, material: MaterialDefinition) -> None:
        """Write a material by id, so a rename updates it rather than inserting.

        Looking the row up by name meant a renamed material was not found
        and was inserted again under its existing id, which the primary key
        refused on every autosave. The id is the identity; the name is a
        label that must merely be unique.
        """
        with self.connect() as connection:
            clash = connection.execute(
                "SELECT id FROM materials WHERE name=? AND id<>?",
                (material.name, material.id),
            ).fetchone()
            if clash is not None:
                raise ValueError(f"A material named {material.name!r} already exists.")
            connection.execute(
                """INSERT INTO materials(id, name, payload_json) VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                payload_json=excluded.payload_json""",
                (material.id, material.name, json.dumps(asdict(material))),
            )

    def load_materials(self) -> list[MaterialDefinition]:
        with self.connect() as connection:
            rows = connection.execute("SELECT payload_json FROM materials ORDER BY name")
            return [MaterialDefinition(**json.loads(row[0])) for row in rows]

    def remove_material(self, material_id: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM materials WHERE id=?", (material_id,))

    # -- tools -------------------------------------------------------------

    def save_tool(self, tool: ToolDefinition) -> None:
        """Write a tool by id; the name is a label that must merely be unique."""
        with self.connect() as connection:
            clash = connection.execute(
                "SELECT id FROM tools WHERE name=? AND id<>?", (tool.name, tool.id)
            ).fetchone()
            if clash is not None:
                raise ValueError(f"A tool named {tool.name!r} already exists.")
            connection.execute(
                """INSERT INTO tools(id, name, payload_json) VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                payload_json=excluded.payload_json""",
                (tool.id, tool.name, json.dumps(asdict(tool))),
            )

    def load_tools(self) -> list[ToolDefinition]:
        with self.connect() as connection:
            rows = connection.execute("SELECT payload_json FROM tools ORDER BY name")
            return [ToolDefinition(**json.loads(row[0])) for row in rows]

    def remove_tool(self, tool_id: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM tools WHERE id=?", (tool_id,))

    # -- recipes -----------------------------------------------------------

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
        with self.connect() as connection:
            rows = connection.execute("SELECT payload_json FROM recipes ORDER BY name")
            return [recipe_from_payload(json.loads(row[0])) for row in rows]

    def remove_recipe(self, recipe_id: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM recipes WHERE id=?", (recipe_id,))

    # -- taking in what a project brought ----------------------------------

    def adopt(
        self,
        materials: list[MaterialDefinition],
        tools: list[ToolDefinition],
        recipes: list[Recipe],
    ) -> list[str]:
        """Take in whatever the library does not have, by name.

        What is here already wins: opening a workspace from somebody else
        must not restyle your materials or rewrite your recipes. Returns
        what was taken in, for the log.
        """
        taken: list[str] = []
        have = {item.name for item in self.load_materials()}
        for material in materials:
            if material.name in have:
                continue
            self.save_material(material)
            have.add(material.name)
            taken.append(f"material {material.name}")
        have = {item.name for item in self.load_tools()}
        for tool in tools:
            if tool.name in have:
                continue
            self.save_tool(tool)
            have.add(tool.name)
            taken.append(f"tool {tool.name}")
        have = {item.name for item in self.load_recipes()}
        for recipe in recipes:
            if recipe.name in have:
                continue
            self.save_recipe(recipe)
            have.add(recipe.name)
            taken.append(f"recipe {recipe.name}")
        return taken


def recipe_from_payload(payload: dict) -> Recipe:
    """A recipe back out of the JSON a table row holds."""
    payload = dict(payload)
    payload["process_type"] = ProcessType(payload["process_type"])
    payload["material_responses"] = {
        name: MaterialResponse(**response)
        for name, response in payload["material_responses"].items()
    }
    return Recipe(**payload)
