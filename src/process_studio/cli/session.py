"""What every command shares: the workspace, the worker, and the terminal.

The CLI is a second client of the worker, beside the desktop shell. It goes
through the same ``dispatch`` the shell uses, edits the same document shape,
and saves it the same way, so a workspace looks the same whichever client
touched it last.
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable, IO, Mapping, Sequence

from ..worker.errors import InvalidRequest, WorkspaceError
from ..worker.protocol import dispatch

DATABASE = "process_studio.sqlite3"
ROOT_VARIABLE = "PROCESS_STUDIO_ROOT"

STATUS_WORDS = {"clean": "ready", "stale": "stale", "dirty": "not run"}


def is_workspace(path: Path) -> bool:
    return (path / DATABASE).is_file()


def find_root(explicit: str | None) -> Path:
    """The workspace a command works in.

    ``--root`` wins; then ``PROCESS_STUDIO_ROOT``; then the nearest directory
    at or above the current one that holds a workspace database, the way git
    finds its repository.
    """
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not is_workspace(path):
            raise WorkspaceError(f"{path} is not a Process Studio workspace: no {DATABASE} there.")
        return path
    variable = os.environ.get(ROOT_VARIABLE)
    if variable:
        return find_root(variable)
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        if is_workspace(candidate):
            return candidate
    raise WorkspaceError(
        "No workspace here. Run inside one, pass --root, or set "
        f"{ROOT_VARIABLE}; `process-studio new DIR` creates one."
    )


class EventPrinter(io.TextIOBase):
    """Receives the worker's progress lines and shows them on the terminal.

    ``dispatch`` writes one JSON line per event to the stream it is given.
    Here that stream is the terminal's stderr, so a run reports each step as
    it happens while stdout stays free for the result.
    """

    def __init__(self, stream: IO[str], quiet: bool) -> None:
        super().__init__()
        self.stream = stream
        self.quiet = quiet

    def write(self, text: str) -> int:  # type: ignore[override]
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self.stream.write(line + "\n")
                continue
            event = message.get("event", {}) if isinstance(message, Mapping) else {}
            said = event.get("message")
            if not said or self.quiet:
                continue
            if event.get("kind") == "progress" and event.get("total"):
                self.stream.write(f"[{event.get('completed', 0)}/{event['total']}] {said}\n")
            else:
                self.stream.write(f"  {said}\n")
        self.stream.flush()
        return len(text)

    def flush(self) -> None:
        self.stream.flush()


class Session:
    """One command's view of a workspace and of where its output goes."""

    def __init__(
        self,
        root: Path | None,
        *,
        json_output: bool = False,
        quiet: bool = False,
        out: IO[str] | None = None,
        err: IO[str] | None = None,
        events: IO[str] | None = None,
        cancel: threading.Event | None = None,
        stdin: str | None = None,
    ) -> None:
        self._root = root
        self.json_output = json_output
        self.quiet = quiet
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        #: Where the worker's progress events go. The terminal prints them
        #: on stderr; the desktop's console hands its own event stream in, so
        #: a run started there reports to the shell like one it started itself.
        self.events = events
        self.cancel = cancel
        #: Text standing in for a file argument of ``-`` (a pasted flow file).
        self.stdin = stdin

    @property
    def root(self) -> Path:
        if self._root is None:
            raise WorkspaceError("this command needs a workspace.")
        return self._root

    # -- the worker ---------------------------------------------------------

    def call(self, method: str, **params: Any) -> Any:
        sink = self.events if self.events is not None else EventPrinter(self.err, self.quiet)
        return dispatch({"method": method, "params": params}, sink, cancel=self.cancel)

    def document(self) -> dict[str, Any]:
        return self.call("open_workspace", root=str(self.root))

    def save(self, document: Mapping[str, Any]) -> dict[str, Any]:
        return self.call("save_document", root=str(self.root), document=document)

    # -- the document -------------------------------------------------------

    @staticmethod
    def branch(document: Mapping[str, Any]) -> dict[str, Any]:
        active = document["project"].get("activeBranchId")
        for branch in document["branches"]:
            if branch["id"] == active:
                return branch
        return document["branches"][0]

    @classmethod
    def steps(cls, document: Mapping[str, Any]) -> list[dict[str, Any]]:
        return cls.branch(document)["steps"]

    @classmethod
    def status_of(cls, document: Mapping[str, Any], step_id: str) -> str:
        branch = cls.branch(document)
        return document.get("stepStatuses", {}).get(branch["id"], {}).get(step_id, "dirty")

    @classmethod
    def step_index(cls, document: Mapping[str, Any], reference: str) -> int:
        """The 0-based index of a step named by its 1-based number or its name."""
        steps = cls.steps(document)
        if reference.strip().isdigit():
            number = int(reference)
            if not 1 <= number <= len(steps):
                raise InvalidRequest(
                    f"step {number} does not exist; the flow has {len(steps)} step(s)."
                )
            return number - 1
        wanted = reference.strip().lower()
        matches = [index for index, step in enumerate(steps) if step["name"].lower() == wanted]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise InvalidRequest(f"no step is named {reference!r}; use its number from `steps list`.")
        raise InvalidRequest(
            f"{len(matches)} steps are named {reference!r}; use the number from `steps list`."
        )

    # -- the terminal -------------------------------------------------------

    def emit(self, payload: Any, text: Callable[[], str | Sequence[str]] | str | Sequence[str]) -> None:
        """Print the machine-readable payload or the human-readable text."""
        if self.json_output:
            self.out.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
            return
        rendered = text() if callable(text) else text
        if isinstance(rendered, str):
            rendered = [rendered]
        for line in rendered:
            self.out.write(line + "\n")

    def note(self, message: str) -> None:
        """A remark for the person, never part of the JSON result."""
        if not self.quiet:
            self.err.write(message + "\n")


def table(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    """Rows of text aligned in columns, the way `ls -l` or `docker ps` do."""
    cells = [[str(value) for value in row] for row in rows]
    widths = [len(name) for name in header]
    for row in cells:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    lines = ["  ".join(name.ljust(widths[index]) for index, name in enumerate(header)).rstrip()]
    for row in cells:
        lines.append("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)).rstrip())
    return lines


def step_rows(document: Mapping[str, Any]) -> list[list[str]]:
    rows = []
    for index, step in enumerate(Session.steps(document), start=1):
        rows.append(
            [
                index,
                step["name"],
                step["processType"].replace("_", " "),
                step.get("outputMaterial") or "",
                describe_mask(step),
                describe_loop(step),
                "yes" if step.get("enabled", True) else "skip",
                STATUS_WORDS.get(Session.status_of(document, step["id"]), "?"),
            ]
        )
    return rows


STEP_HEADER = ("#", "Name", "Type", "Material", "Mask", "Loop", "Run", "Status")


def describe_loop(step: Mapping[str, Any]) -> str:
    """"NAME 2/4" for a step inside a repeated block, "" for one on its own."""
    loop = step.get("loop")
    if not loop:
        return ""
    return f"{loop.get('name') or 'Loop'} {int(loop.get('iteration', 0)) + 1}/{int(loop.get('repeat', 1))}"


def describe_mask(step: Mapping[str, Any]) -> str:
    source = step.get("maskSource", "none")
    if source == "quick_sketch":
        sketch = step.get("parameters", {}).get("sketch_id", "default")
        text = f"sketch:{sketch}"
    elif source == "gds":
        layer = step.get("layer")
        datatype = step.get("datatype")
        text = f"gds:{layer if layer is not None else '?'}/{datatype if datatype is not None else 0}"
    else:
        return "none"
    return text + (" (outside)" if step.get("keep") == "outside" else "")


def format_number(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)
