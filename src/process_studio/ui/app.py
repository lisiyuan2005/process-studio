"""Single-user desktop application for process-flow modeling."""

from __future__ import annotations

import argparse
import json
import queue
import threading
from pathlib import Path
from tkinter import (
    BOTH,
    END,
    LEFT,
    RIGHT,
    TOP,
    X,
    Y,
    BooleanVar,
    DoubleVar,
    StringVar,
    Tk,
    Toplevel,
    colorchooser,
    filedialog,
    messagebox,
    simpledialog,
)
from tkinter import ttk

import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.colors import ListedColormap
from matplotlib.figure import Figure

from process_studio.defaults import (
    default_branch,
    default_grid,
    default_materials,
    default_recipes,
)
from process_studio.engine import ProcessEngine
from process_studio.kernel.grid import UniformGrid3D
from process_studio.kernel.material_state import MaterialState
from process_studio.layout.gds import available_gds_layers
from process_studio.layout.quick_sketch import QuickSketch, SketchShape
from process_studio.libraries import MaterialLibrary, RecipeLibrary
from process_studio.models import (
    FlowBranch,
    MaterialDefinition,
    ProcessStep,
    ProcessType,
    ProjectDefinition,
    Recipe,
)
from process_studio.simulation_settings import estimate_grid, grid_for_target_spacing
from process_studio.storage import ProjectRepository
from process_studio.visualization import (
    downsampled_material_voxels,
    line_section_labels,
    top_view_labels,
)


class ProcessStudioApp:
    def __init__(self, root: Tk, workspace: Path) -> None:
        self.root = root
        self.workspace = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.repository = ProjectRepository(workspace / "process_studio.sqlite3")
        self.material_library = self._default_materials()
        self.recipe_library = self._default_recipes()
        for material in self.material_library.materials.values():
            self.repository.save_material(material)
        for recipe in self.recipe_library.recipes.values():
            self.repository.save_recipe(recipe)

        # Treat the embedded database as the source of truth.  This lets a
        # generated/demo workspace bring its own materials and recipes while a
        # blank workspace still receives the small built-in starter library.
        self.material_library = MaterialLibrary(self.repository.load_materials())
        self.recipe_library = RecipeLibrary(self.repository.load_recipes())

        try:
            self.project = self.repository.load_project("default-project")
            self.grid = UniformGrid3D(**self.project.grid)
            self.branch = self.repository.load_branch(
                self.project.active_branch_id or "default-main"
            )
        except KeyError:
            projects = self.repository.list_projects()
            if projects:
                self.project = projects[0]
                self.grid = UniformGrid3D(**self.project.grid)
                self.branch = self.repository.load_branch(
                    self.project.active_branch_id or self.repository.list_branches(self.project.id)[0].id
                )
            else:
                self.grid = default_grid()
                self.project = ProjectDefinition(
                    "3D Integration Demo", dict(self.grid.__dict__), id="default-project"
                )
                self.branch = self._default_branch()
                self.project.active_branch_id = self.branch.id
                self.repository.save_project(self.project)
                self.repository.save_branch(self.project.id, self.branch)

        self.initial_state = MaterialState(self.grid)
        self.initial_state.add_material("Si", self.grid.substrate())
        self.current_state = (
            self.repository.load_latest_snapshot(self.branch.id)
            or self.initial_state.clone()
        )
        sketch_path = self.workspace / "default-sketch.json"
        if sketch_path.exists():
            self.sketch = QuickSketch.load(sketch_path)
        else:
            self.sketch = QuickSketch(
                "default",
                [
                    SketchShape(
                        "rectangle",
                        parameters={"center": (0.0, 0.0), "size": (0.5, 0.4)},
                    )
                ],
            )
            self.sketch.save(sketch_path)
        self.sketches = {
            path.stem: QuickSketch.load(path)
            for path in self.workspace.glob("*.json")
            if path.name.endswith(".json")
        }
        self.sketches["default"] = self.sketch
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.engine = ProcessEngine(
            self.recipe_library.recipes,
            sketches=self.sketches,
            repository=self.repository,
            logger=self.log_queue.put,
        )
        self.running = False
        self.section_line = ((-0.7, 0.0), (0.7, 0.0))
        self.draw_mode = StringVar(value="section")
        self.sketch_operation = StringVar(value="merge")
        self._press_point: tuple[float, float] | None = None
        self._poly_points: list[tuple[float, float]] = []
        self.material_visible: dict[str, BooleanVar] = {}
        self.detail_vars: dict[str, StringVar] = {}

        self._build_window()
        self.refresh_all()
        self.root.after(100, self._poll_logs)

    @staticmethod
    def _default_materials() -> MaterialLibrary:
        return MaterialLibrary(default_materials())

    @staticmethod
    def _default_recipes() -> RecipeLibrary:
        return RecipeLibrary(default_recipes())

    def _recipe_named(self, name: str) -> Recipe:
        return next(recipe for recipe in self.recipe_library.recipes.values() if recipe.name == name)

    def _default_branch(self) -> FlowBranch:
        return default_branch()

    def _build_window(self) -> None:
        self.root.title("Process Studio — 3D Integration Process Modeling")
        self.root.geometry("1500x900")
        self.root.minsize(1100, 700)
        self._build_menu()

        vertical = ttk.Panedwindow(self.root, orient="vertical")
        vertical.pack(fill=BOTH, expand=True)
        main = ttk.Panedwindow(vertical, orient="horizontal")
        log_frame = ttk.LabelFrame(vertical, text="Process Log")
        vertical.add(main, weight=5)
        vertical.add(log_frame, weight=1)

        left = ttk.Frame(main, padding=6)
        center = ttk.Frame(main, padding=4)
        right = ttk.Frame(main, padding=6)
        main.add(left, weight=1)
        main.add(center, weight=4)
        main.add(right, weight=1)

        self._build_flow_panel(left)
        self._build_view_panel(center)
        self._build_details_panel(right)

        self.log_text = __import__("tkinter").Text(log_frame, height=8, wrap="word")
        self.log_text.pack(fill=BOTH, expand=True)
        self.log_text.configure(state="disabled")

    def _build_menu(self) -> None:
        import tkinter as tk

        menu = tk.Menu(self.root)
        project_menu = tk.Menu(menu, tearoff=False)
        project_menu.add_command(label="Save Project", command=self.save_project)
        project_menu.add_command(label="Simulation Settings…", command=self.edit_simulation_settings)
        project_menu.add_command(label="Import GDS…", command=self.import_gds)
        project_menu.add_separator()
        project_menu.add_command(label="Exit", command=self.root.destroy)
        library_menu = tk.Menu(menu, tearoff=False)
        library_menu.add_command(label="Material Library…", command=self.edit_materials)
        library_menu.add_command(label="Recipe Library…", command=self.edit_recipes)
        library_menu.add_command(label="Import Recipes from Excel…", command=self.import_recipes)
        library_menu.add_command(label="Export Recipes to Excel…", command=self.export_recipes)
        menu.add_cascade(label="Project", menu=project_menu)
        menu.add_cascade(label="Libraries", menu=library_menu)
        self.root.configure(menu=menu)

    def _build_flow_panel(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Process Flow", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        self.branch_var = StringVar(value=self.branch.name)
        self.branch_combo = ttk.Combobox(
            parent,
            textvariable=self.branch_var,
            values=[branch.name for branch in self.repository.list_branches(self.project.id)],
            state="readonly",
        )
        self.branch_combo.pack(fill=X, pady=(5, 6))
        self.branch_combo.bind("<<ComboboxSelected>>", self._switch_branch)
        self.flow_tree = ttk.Treeview(parent, columns=("type",), show="tree headings", selectmode="browse")
        self.flow_tree.heading("#0", text="Step")
        self.flow_tree.heading("type", text="Type")
        self.flow_tree.column("#0", width=150)
        self.flow_tree.column("type", width=80, anchor="center")
        self.flow_tree.pack(fill=BOTH, expand=True)
        self.flow_tree.bind("<<TreeviewSelect>>", self._on_step_selected)
        buttons = ttk.Frame(parent)
        buttons.pack(fill=X, pady=6)
        ttk.Button(buttons, text="+ Step", command=self.add_step).pack(side=LEFT, expand=True, fill=X)
        ttk.Button(buttons, text="Delete", command=self.delete_step).pack(side=LEFT, expand=True, fill=X)
        ttk.Button(parent, text="Run to Selected", command=self.run_to_selected).pack(fill=X, pady=2)
        ttk.Button(parent, text="Run All", command=self.run_all).pack(fill=X, pady=2)
        ttk.Button(parent, text="Branch Here", command=self.branch_here).pack(fill=X, pady=2)

    def _build_view_panel(self, parent: ttk.Frame) -> None:
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill=X)
        ttk.Label(toolbar, text="View / Draw:").pack(side=LEFT)
        for label, value in [
            ("Rectangle", "rectangle"),
            ("Circle", "circle"),
            ("Polygon", "polygon"),
            ("Path", "path"),
            ("AA/BB Line", "section"),
        ]:
            ttk.Radiobutton(toolbar, text=label, value=value, variable=self.draw_mode).pack(side=LEFT)
        ttk.Combobox(
            toolbar,
            textvariable=self.sketch_operation,
            values=["merge", "subtract", "intersect"],
            state="readonly",
            width=10,
        ).pack(side=RIGHT)
        ttk.Button(toolbar, text="Sketch Data…", command=self.edit_sketch).pack(side=RIGHT, padx=4)

        self.visibility_frame = ttk.Frame(parent)
        self.visibility_frame.pack(fill=X, pady=3)
        self.view_tabs = ttk.Notebook(parent)
        self.view_tabs.pack(fill=BOTH, expand=True)

        self.fig_3d = Figure(figsize=(7, 6), dpi=100)
        self.ax_3d = self.fig_3d.add_subplot(111, projection="3d")
        self.canvas_3d = self._canvas_tab("3D", self.fig_3d)

        self.fig_top = Figure(figsize=(7, 6), dpi=100)
        self.ax_top = self.fig_top.add_subplot(111)
        self.canvas_top = self._canvas_tab("Top View", self.fig_top)
        self.canvas_top.mpl_connect("button_press_event", self._top_press)
        self.canvas_top.mpl_connect("button_release_event", self._top_release)

        self.fig_section = Figure(figsize=(7, 6), dpi=100)
        self.ax_section = self.fig_section.add_subplot(111)
        self.canvas_section = self._canvas_tab("AA/BB Section", self.fig_section)

    def _canvas_tab(self, title: str, figure: Figure) -> FigureCanvasTkAgg:
        frame = ttk.Frame(self.view_tabs)
        self.view_tabs.add(frame, text=title)
        canvas = FigureCanvasTkAgg(figure, master=frame)
        canvas.get_tk_widget().pack(fill=BOTH, expand=True)
        NavigationToolbar2Tk(canvas, frame, pack_toolbar=True).pack(fill=X)
        return canvas

    def _build_details_panel(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Process Details", font=("Segoe UI", 12, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        fields = [
            ("Step Name", "name"),
            ("Recipe", "recipe"),
            ("Type", "type"),
            ("Tool", "tool"),
            ("Target (µm)", "target"),
            ("Time (min)", "time_min"),
            ("Temperature (°C)", "temperature_c"),
            ("Material", "material"),
            ("Rate (µm/min)", "rate"),
            ("Directional Fraction", "directional_fraction"),
            ("CMP Target Z (µm)", "target_z"),
            ("CMP Materials", "materials"),
            ("Mask Source", "mask_source"),
            ("Sketch ID", "sketch_id"),
            ("Layer", "layer"),
            ("Datatype", "datatype"),
            ("Keep", "keep"),
            ("Extra Overrides (JSON)", "extra_overrides"),
        ]
        for row, (label, key) in enumerate(fields, start=1):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
            variable = StringVar()
            self.detail_vars[key] = variable
            if key == "mask_source":
                widget = ttk.Combobox(parent, textvariable=variable, values=["none", "quick_sketch", "gds"], state="readonly")
            elif key == "keep":
                widget = ttk.Combobox(parent, textvariable=variable, values=["inside", "outside"], state="readonly")
            else:
                widget = ttk.Entry(parent, textvariable=variable)
                if key in {"recipe", "type", "tool"}:
                    widget.configure(state="readonly")
            widget.grid(row=row, column=1, sticky="ew", padx=(6, 0), pady=3)
        parent.columnconfigure(1, weight=1)
        ttk.Button(parent, text="Apply Step Overrides", command=self.apply_step_details).grid(row=len(fields) + 1, column=0, columnspan=2, sticky="ew", pady=(10, 3))
        ttk.Label(parent, text="Every recipe field may be overridden in the selected step.", wraplength=260, foreground="#59636e").grid(row=len(fields) + 2, column=0, columnspan=2, sticky="w", pady=4)

    def refresh_all(self) -> None:
        self._refresh_flow()
        self._refresh_visibility()
        self._draw_3d()
        self._draw_top()
        self._draw_section()

    def _refresh_flow(self) -> None:
        selected = self.flow_tree.selection()
        selected_id = selected[0] if selected else None
        self.flow_tree.delete(*self.flow_tree.get_children())
        for index, step in enumerate(self.branch.steps, start=1):
            recipe = self.recipe_library.recipes.get(step.recipe_id)
            process_type = recipe.process_type.value if recipe else "missing"
            self.flow_tree.insert("", END, iid=step.id, text=f"{index}. {step.name}", values=(process_type,))
        if selected_id and self.flow_tree.exists(selected_id):
            self.flow_tree.selection_set(selected_id)

    def _refresh_visibility(self) -> None:
        for child in self.visibility_frame.winfo_children():
            child.destroy()
        for name in self.current_state.priority:
            variable = self.material_visible.setdefault(name, BooleanVar(value=True))
            ttk.Checkbutton(
                self.visibility_frame,
                text=name,
                variable=variable,
                command=lambda: (self._draw_3d(), self._draw_top(), self._draw_section()),
            ).pack(side=LEFT, padx=4)

    def _material_colors(self) -> list[str]:
        return [
            self.material_library.materials.get(name, MaterialDefinition(name)).color
            for name in self.current_state.priority
        ]

    def _draw_3d(self) -> None:
        self.ax_3d.clear()
        voxels, _ = downsampled_material_voxels(self.current_state)
        drawn = False
        for name in self.current_state.priority:
            if not self.material_visible.get(name, BooleanVar(value=True)).get():
                continue
            filled = voxels[name]
            if not filled.any():
                continue
            definition = self.material_library.materials.get(name, MaterialDefinition(name))
            self.ax_3d.voxels(
                filled,
                facecolors=definition.color,
                edgecolors=(0.08, 0.09, 0.12, 0.12),
                linewidth=0.1,
                alpha=definition.opacity,
            )
            drawn = True
        if not drawn:
            self.ax_3d.text2D(0.4, 0.5, "No visible material", transform=self.ax_3d.transAxes)
        self.ax_3d.set_title("3D Structure — drag to rotate, scroll to zoom")
        self.ax_3d.set_xlabel("x")
        self.ax_3d.set_ylabel("y")
        self.ax_3d.set_zlabel("z")
        self.ax_3d.view_init(elev=27, azim=-55)
        self.ax_3d.set_box_aspect((1.0, 1.0, 0.7))
        self.fig_3d.tight_layout()
        self.canvas_3d.draw_idle()

    def _visible_labels(self, labels: np.ndarray) -> np.ndarray:
        result = labels.copy()
        for index, name in enumerate(self.current_state.priority):
            variable = self.material_visible.get(name)
            if variable is not None and not variable.get():
                result[result == index] = -1
        return result

    def _draw_top(self) -> None:
        self.ax_top.clear()
        labels = self._visible_labels(top_view_labels(self.current_state))
        colors = ["#f5f7fa", *self._material_colors()]
        self.ax_top.imshow(
            labels + 1,
            origin="lower",
            extent=(self.grid.x_min, self.grid.x_max, self.grid.y_min, self.grid.y_max),
            cmap=ListedColormap(colors),
            vmin=0,
            vmax=max(1, len(colors) - 1),
            interpolation="nearest",
        )
        mask = self.sketch.render(*self.grid.mesh_xy)
        if mask.any() and not mask.all():
            self.ax_top.contour(self.grid.x, self.grid.y, mask, levels=[0.5], colors=["#e33f4f"], linewidths=1.4)
        (x0, y0), (x1, y1) = self.section_line
        self.ax_top.plot([x0, x1], [y0, y1], color="#111820", linewidth=1.5)
        self.ax_top.text(x0, y0, "A", fontweight="bold")
        self.ax_top.text(x1, y1, "A", fontweight="bold")
        if self._poly_points:
            points = np.asarray(self._poly_points)
            self.ax_top.plot(points[:, 0], points[:, 1], "o--", color="#d73b49")
        self.ax_top.set_title("Top View — draw masks or an AA/BB section line")
        self.ax_top.set_xlabel("x (µm)")
        self.ax_top.set_ylabel("y (µm)")
        self.ax_top.set_aspect("equal")
        self.fig_top.tight_layout()
        self.canvas_top.draw_idle()

    def _draw_section(self) -> None:
        self.ax_section.clear()
        distance, labels = line_section_labels(self.current_state, *self.section_line)
        labels = self._visible_labels(labels)
        colors = ["#f5f7fa", *self._material_colors()]
        self.ax_section.imshow(
            labels + 1,
            origin="lower",
            extent=(distance[0], distance[-1], self.grid.z_min, self.grid.z_max),
            cmap=ListedColormap(colors),
            vmin=0,
            vmax=max(1, len(colors) - 1),
            interpolation="nearest",
            aspect="auto",
        )
        self.ax_section.set_title("AA/BB Section")
        self.ax_section.set_xlabel("distance along line (µm)")
        self.ax_section.set_ylabel("z (µm)")
        self.fig_section.tight_layout()
        self.canvas_section.draw_idle()

    def _top_press(self, event) -> None:
        if event.inaxes is not self.ax_top or event.xdata is None or event.ydata is None:
            return
        point = (float(event.xdata), float(event.ydata))
        if self.draw_mode.get() in {"polygon", "path"}:
            self._poly_points.append(point)
            if event.dblclick:
                self._finish_poly_shape()
            else:
                self._draw_top()
        else:
            self._press_point = point

    def _top_release(self, event) -> None:
        if self.draw_mode.get() in {"polygon", "path"}:
            return
        if self._press_point is None or event.xdata is None or event.ydata is None:
            return
        start = self._press_point
        end = (float(event.xdata), float(event.ydata))
        self._press_point = None
        mode = self.draw_mode.get()
        if mode == "section":
            if np.hypot(end[0] - start[0], end[1] - start[1]) > self.grid.dx:
                self.section_line = (start, end)
        elif mode == "rectangle":
            size = (abs(end[0] - start[0]), abs(end[1] - start[1]))
            if min(size) > self.grid.dx:
                self.sketch.add(
                    SketchShape(
                        "rectangle",
                        self.sketch_operation.get(),
                        {"center": ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2), "size": size},
                    )
                )
                self._save_sketch()
        elif mode == "circle":
            radius = float(np.hypot(end[0] - start[0], end[1] - start[1]))
            if radius > self.grid.dx:
                self.sketch.add(
                    SketchShape("circle", self.sketch_operation.get(), {"center": start, "radius": radius})
                )
                self._save_sketch()
        self._draw_top()
        self._draw_section()

    def _finish_poly_shape(self) -> None:
        mode = self.draw_mode.get()
        minimum = 3 if mode == "polygon" else 2
        if len(self._poly_points) < minimum:
            return
        parameters: dict[str, object] = {"points": list(self._poly_points)}
        if mode == "path":
            width = simpledialog.askfloat("Path width", "Width (µm):", initialvalue=0.08, minvalue=self.grid.dx)
            if width is None:
                self._poly_points.clear()
                return
            parameters["width"] = width
        self.sketch.add(SketchShape(mode, self.sketch_operation.get(), parameters))
        self._poly_points.clear()
        self._save_sketch()
        self._draw_top()

    def _save_sketch(self) -> None:
        self.sketch.save(self.workspace / "default-sketch.json")

    def _selected_step(self) -> ProcessStep | None:
        selection = self.flow_tree.selection()
        if not selection:
            return None
        return next((step for step in self.branch.steps if step.id == selection[0]), None)

    def _on_step_selected(self, _event=None) -> None:
        step = self._selected_step()
        if step is None:
            return
        recipe = self.recipe_library.recipes[step.recipe_id]
        parameters = recipe.resolved_parameters(step.overrides)
        values = {
            "name": step.name,
            "recipe": recipe.name,
            "type": recipe.process_type.value,
            "tool": recipe.tool,
            "target": parameters.get("target", ""),
            "time_min": parameters.get("time_min", ""),
            "temperature_c": parameters.get("temperature_c", ""),
            "material": parameters.get("material", recipe.output_material or ""),
            "rate": parameters.get("rate", ""),
            "directional_fraction": parameters.get("directional_fraction", ""),
            "target_z": parameters.get("target_z", ""),
            "materials": parameters.get("materials", ""),
            "mask_source": step.mask_source,
            "sketch_id": parameters.get("sketch_id", "default"),
            "layer": "" if step.layer is None else step.layer,
            "datatype": "" if step.datatype is None else step.datatype,
            "keep": step.keep,
        }
        visible_override_keys = {
            "target",
            "time_min",
            "temperature_c",
            "material",
            "rate",
            "directional_fraction",
            "target_z",
            "materials",
            "sketch_id",
        }
        values["extra_overrides"] = json.dumps(
            {
                key: value
                for key, value in step.overrides.items()
                if key not in visible_override_keys
            },
            ensure_ascii=False,
        )
        for key, value in values.items():
            self.detail_vars[key].set(str(value))

    def apply_step_details(self) -> None:
        step = self._selected_step()
        if step is None:
            return
        step.name = self.detail_vars["name"].get().strip() or step.name
        try:
            extra = json.loads(self.detail_vars["extra_overrides"].get().strip() or "{}")
            if not isinstance(extra, dict):
                raise ValueError("must be a JSON object")
        except (json.JSONDecodeError, ValueError) as error:
            messagebox.showerror("Invalid overrides", str(error))
            return
        step.overrides = extra
        for key in [
            "target",
            "time_min",
            "temperature_c",
            "rate",
            "directional_fraction",
            "target_z",
        ]:
            text = self.detail_vars[key].get().strip()
            if text:
                try:
                    step.overrides[key] = float(text)
                except ValueError:
                    messagebox.showerror("Invalid value", f"{key} must be numeric")
                    return
        for key in ["material", "materials", "sketch_id"]:
            value = self.detail_vars[key].get().strip()
            if value:
                step.overrides[key] = value
        step.mask_source = self.detail_vars["mask_source"].get() or "none"
        step.keep = self.detail_vars["keep"].get() or "inside"
        step.layer = self._optional_int(self.detail_vars["layer"].get())
        step.datatype = self._optional_int(self.detail_vars["datatype"].get())
        self.save_project()
        self._refresh_flow()

    @staticmethod
    def _optional_int(value: str) -> int | None:
        return None if not value.strip() else int(value)

    def add_step(self) -> None:
        recipes = list(self.recipe_library.recipes.values())
        names = [recipe.name for recipe in recipes]
        name = simpledialog.askstring("Add process step", "Recipe name:\n" + "\n".join(names), initialvalue=names[0])
        if not name:
            return
        recipe = next((item for item in recipes if item.name == name), None)
        if recipe is None:
            messagebox.showerror("Recipe not found", name)
            return
        step = ProcessStep(recipe.name, recipe.id)
        selected = self._selected_step()
        if selected:
            self.branch.steps.insert(self.branch.steps.index(selected) + 1, step)
        else:
            self.branch.steps.append(step)
        self.save_project()
        self._refresh_flow()
        self.flow_tree.selection_set(step.id)

    def delete_step(self) -> None:
        step = self._selected_step()
        if step is None:
            return
        index = self.branch.steps.index(step)
        removed = self.branch.steps[index:]
        if not messagebox.askyesno("Delete step", f"Delete {step.name} and {len(removed) - 1} dependent step(s) and snapshots?"):
            return
        try:
            self.repository.delete_step_and_dependents(self.branch.id, step.id)
        except KeyError:
            pass
        self.branch.steps = self.branch.steps[:index]
        self.save_project()
        self._refresh_flow()

    def run_all(self) -> None:
        self._start_run(None)

    def run_to_selected(self) -> None:
        step = self._selected_step()
        self._start_run(step.id if step else None)

    def _start_run(self, through_step_id: str | None) -> None:
        if self.running:
            return
        self.running = True
        self._append_log("Starting process simulation…")

        def worker() -> None:
            try:
                state = self.engine.run_branch(
                    self.initial_state,
                    self.project,
                    self.branch,
                    through_step_id=through_step_id,
                )
                self.current_state = state
                self.log_queue.put("__RUN_COMPLETE__")
            except Exception as error:  # UI boundary: surface errors in the log.
                self.log_queue.put(f"ERROR {type(error).__name__}: {error}")
                self.log_queue.put("__RUN_COMPLETE__")

        threading.Thread(target=worker, daemon=True).start()

    def _poll_logs(self) -> None:
        try:
            while True:
                message = self.log_queue.get_nowait()
                if message == "__RUN_COMPLETE__":
                    self.running = False
                    self.refresh_all()
                    self._append_log("Simulation ready")
                else:
                    self._append_log(message)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_logs)

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert(END, message + "\n")
        self.log_text.see(END)
        self.log_text.configure(state="disabled")

    def branch_here(self) -> None:
        step = self._selected_step()
        if step is None:
            return
        name = simpledialog.askstring("New branch", "Branch name:", initialvalue="variant")
        if not name:
            return
        self.branch = self.repository.create_branch(self.project.id, self.branch.id, step.id, name)
        self.project.active_branch_id = self.branch.id
        self.branch_var.set(name)
        self.branch_combo.configure(
            values=[branch.name for branch in self.repository.list_branches(self.project.id)]
        )
        self.save_project()
        self._refresh_flow()

    def _switch_branch(self, _event=None) -> None:
        name = self.branch_var.get()
        branch = next(
            (
                candidate
                for candidate in self.repository.list_branches(self.project.id)
                if candidate.name == name
            ),
            None,
        )
        if branch is None or branch.id == self.branch.id:
            return
        self.branch = branch
        self.project.active_branch_id = branch.id
        self.current_state = (
            self.repository.load_latest_snapshot(branch.id) or self.initial_state.clone()
        )
        self.repository.save_project(self.project)
        self.refresh_all()

    def save_project(self) -> None:
        self.repository.save_project(self.project)
        self.repository.save_branch(self.project.id, self.branch)
        self._append_log("Project saved")

    def edit_simulation_settings(self) -> None:
        if self.running:
            messagebox.showinfo("Simulation running", "Wait for the current run to finish first.")
            return
        window = Toplevel(self.root)
        window.title("Simulation Settings")
        window.resizable(False, False)
        body = ttk.Frame(window, padding=14)
        body.pack(fill=BOTH, expand=True)

        preset_values = {
            "Draft — 25 nm": 25.0,
            "Standard — 12.5 nm": 12.5,
            "Accurate — 6.25 nm": 6.25,
            "Custom": None,
        }
        current_nm = self.grid.dx * 1000.0
        preset_var = StringVar(value="Custom")
        for label, value in preset_values.items():
            if value is not None and np.isclose(value, current_nm):
                preset_var.set(label)
                break
        spacing_var = StringVar(value=f"{current_nm:g}")
        summary_var = StringVar()

        ttk.Label(body, text="Spatial calculation grid", font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10)
        )
        ttk.Label(body, text="Preset").grid(row=1, column=0, sticky="w", pady=4)
        preset = ttk.Combobox(
            body, textvariable=preset_var, values=list(preset_values), state="readonly", width=25
        )
        preset.grid(row=1, column=1, sticky="ew", padx=(12, 0), pady=4)
        ttk.Label(body, text="Target spacing (nm)").grid(row=2, column=0, sticky="w", pady=4)
        spacing_entry = ttk.Entry(body, textvariable=spacing_var, width=18)
        spacing_entry.grid(row=2, column=1, sticky="ew", padx=(12, 0), pady=4)
        ttk.Label(
            body,
            textvariable=summary_var,
            justify="left",
            foreground="#46515B",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 8))
        ttk.Label(
            body,
            text=(
                "This changes the actual 3D solver grid, not image DPI.\n"
                "Existing snapshots are invalid at another spacing and will be cleared."
            ),
            justify="left",
            wraplength=430,
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(0, 12))

        candidate: list[UniformGrid3D | None] = [None]

        def readable_bytes(value: int) -> str:
            return f"{value / 1024**3:.2f} GB"

        def refresh_estimate(*_args) -> None:
            try:
                proposed = grid_for_target_spacing(self.grid, float(spacing_var.get()))
                estimate = estimate_grid(proposed, len(self.material_library.materials))
                candidate[0] = proposed
                summary_var.set(
                    f"Actual spacing: {estimate.spacing_nm:g} nm\n"
                    f"Grid: {estimate.shape[0]} × {estimate.shape[1]} × {estimate.shape[2]} "
                    f"= {estimate.node_count:,} nodes\n"
                    f"Saved material fields: about {readable_bytes(estimate.state_bytes)}\n"
                    f"Recommended free RAM: at least {readable_bytes(estimate.recommended_bytes)}"
                )
            except (TypeError, ValueError) as error:
                candidate[0] = None
                summary_var.set(f"Invalid setting: {error}")

        def choose_preset(_event=None) -> None:
            value = preset_values[preset_var.get()]
            if value is not None:
                spacing_var.set(f"{value:g}")
            refresh_estimate()

        def mark_custom(*_args) -> None:
            value = preset_values.get(preset_var.get())
            try:
                entered = float(spacing_var.get())
            except ValueError:
                entered = None
            if value is not None and entered is not None and not np.isclose(value, entered):
                preset_var.set("Custom")
            refresh_estimate()

        def apply() -> None:
            proposed = candidate[0]
            if proposed is None:
                messagebox.showerror("Invalid grid", "Enter a valid spatial spacing.", parent=window)
                return
            estimate = estimate_grid(proposed, len(self.material_library.materials))
            if estimate.node_count > 20_000_000:
                messagebox.showerror(
                    "Grid too large",
                    f"This grid needs {estimate.node_count:,} nodes. The desktop safety limit is "
                    "20,000,000 nodes. Enlarge the spacing or reduce the project bounds.",
                    parent=window,
                )
                return
            if proposed == self.grid:
                window.destroy()
                return
            if not messagebox.askyesno(
                "Change calculation grid",
                "Changing spatial precision requires a full rerun. Clear all saved snapshots "
                "for this project and apply the new grid?",
                parent=window,
            ):
                return
            removed = self.repository.delete_project_snapshots(self.project.id)
            self.grid = proposed
            self.project.grid = dict(proposed.__dict__)
            self.initial_state = MaterialState(proposed)
            self.initial_state.add_material("Si", proposed.substrate())
            self.current_state = self.initial_state.clone()
            self.repository.save_project(self.project)
            self._append_log(
                f"GRID spacing={proposed.dx*1000:g} nm, "
                f"shape={proposed.nx}x{proposed.ny}x{proposed.nz}; "
                f"cleared {removed} incompatible snapshots"
            )
            window.destroy()
            self.refresh_all()

        preset.bind("<<ComboboxSelected>>", choose_preset)
        spacing_var.trace_add("write", mark_custom)
        buttons = ttk.Frame(body)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="Cancel", command=window.destroy).pack(side=RIGHT)
        ttk.Button(buttons, text="Apply", command=apply).pack(side=RIGHT, padx=(0, 8))
        refresh_estimate()

    def import_gds(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("GDSII", "*.gds"), ("All files", "*.*")])
        if not path:
            return
        try:
            layers = available_gds_layers(path)
        except Exception as error:
            messagebox.showerror("GDS import failed", str(error))
            return
        self.project.gds_path = path
        self.save_project()
        messagebox.showinfo("GDS imported", "Available layer/datatypes:\n" + "\n".join(f"{layer}/{datatype}" for layer, datatype in layers))

    def edit_sketch(self) -> None:
        window = Toplevel(self.root)
        window.title("Quick Sketch Data")
        text = __import__("tkinter").Text(window, width=90, height=28)
        text.pack(fill=BOTH, expand=True, padx=8, pady=8)
        payload = {"name": self.sketch.name, "shapes": [shape.__dict__ for shape in self.sketch.shapes]}
        text.insert("1.0", json.dumps(payload, indent=2))

        def apply() -> None:
            try:
                data = json.loads(text.get("1.0", END))
                self.sketch = QuickSketch(data["name"], [SketchShape(**shape) for shape in data["shapes"]])
                self.engine.sketches["default"] = self.sketch
                self.sketches["default"] = self.sketch
                self._save_sketch()
                self._draw_top()
                window.destroy()
            except Exception as error:
                messagebox.showerror("Invalid sketch", str(error), parent=window)

        ttk.Button(window, text="Apply", command=apply).pack(pady=(0, 8))

    def edit_materials(self) -> None:
        window = Toplevel(self.root)
        window.title("Material Library")
        tree = ttk.Treeview(window, columns=("category", "color", "opacity"), show="headings")
        for key, title in [("category", "Category"), ("color", "Color"), ("opacity", "Opacity")]:
            tree.heading(key, text=title)
        tree.pack(fill=BOTH, expand=True, padx=8, pady=8)
        for material in self.material_library.materials.values():
            tree.insert("", END, iid=material.name, values=(material.category, material.color, material.opacity))

        def add() -> None:
            name = simpledialog.askstring("Material", "Material name:", parent=window)
            if not name:
                return
            color = colorchooser.askcolor(parent=window)[1] or "#7c83a0"
            material = MaterialDefinition(name, color=color)
            self.material_library.add(material)
            self.repository.save_material(material)
            tree.insert("", END, iid=name, values=(material.category, material.color, material.opacity))

        ttk.Button(window, text="Add Material", command=add).pack(pady=(0, 8))

    def edit_recipes(self) -> None:
        window = Toplevel(self.root)
        window.title("Recipe Library")
        tree = ttk.Treeview(window, columns=("type", "tool", "material"), show="tree headings")
        tree.heading("#0", text="Recipe")
        tree.heading("type", text="Type")
        tree.heading("tool", text="Tool")
        tree.heading("material", text="Material")
        tree.pack(fill=BOTH, expand=True, padx=8, pady=8)
        for recipe in self.recipe_library.recipes.values():
            tree.insert("", END, iid=recipe.id, text=recipe.name, values=(recipe.process_type.value, recipe.tool, recipe.output_material or ""))

        def add() -> None:
            name = simpledialog.askstring("Recipe", "Recipe name:", parent=window)
            if not name:
                return
            kind = simpledialog.askstring("Recipe", "Type: deposit / etch / cmp / no_geometry", initialvalue="deposit", parent=window)
            if not kind:
                return
            tool = simpledialog.askstring("Recipe", "Tool:", parent=window) or ""
            material = simpledialog.askstring("Recipe", "Material (optional):", parent=window) or None
            recipe = Recipe(name, ProcessType(kind), tool=tool, output_material=material, parameters={"target": 0.05})
            self.recipe_library.add(recipe)
            self.engine.recipes[recipe.id] = recipe
            self.repository.save_recipe(recipe)
            tree.insert("", END, iid=recipe.id, text=recipe.name, values=(recipe.process_type.value, recipe.tool, recipe.output_material or ""))

        ttk.Button(window, text="Add Recipe", command=add).pack(pady=(0, 8))

    def import_recipes(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("Excel", "*.xlsx")])
        if not path:
            return
        imported = RecipeLibrary.import_excel(path)
        for recipe in imported.recipes.values():
            self.recipe_library.add(recipe)
            self.engine.recipes[recipe.id] = recipe
            self.repository.save_recipe(recipe)
        self._append_log(f"Imported {len(imported.recipes)} recipes")

    def export_recipes(self) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx")])
        if path:
            self.recipe_library.export_excel(path)
            self._append_log(f"Recipes exported to {path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path.cwd() / "process-studio-workspace")
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    root = Tk()
    app = ProcessStudioApp(root, args.workspace)
    if args.smoke_test:
        root.update_idletasks()
        root.update()
        if args.screenshot:
            import time

            from PIL import ImageGrab

            root.deiconify()
            root.lift()
            root.attributes("-topmost", True)
            root.update()
            time.sleep(1.0)
            root.update()
            left = root.winfo_rootx()
            top = root.winfo_rooty()
            right = left + root.winfo_width()
            bottom = top + root.winfo_height()
            ImageGrab.grab((left, top, right, bottom)).save(args.screenshot)
            root.attributes("-topmost", False)
        root.destroy()
        return
    root.mainloop()


if __name__ == "__main__":
    main()
