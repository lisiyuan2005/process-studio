"""The long-lived request loop behind the desktop shell.

One process serves a whole session. Requests arrive on stdin and are answered
on stdout, but reading and executing are separate threads so that a cancel
message can reach a run that is already under way, and so that a view request
which a newer one has made pointless can be dropped before it is computed.

Ordering: requests execute in arrival order on two lanes. Views (surfaces,
sections, the top view) run on their own lane, so a step that has finished
can be looked at while a run is still working on the ones after it; every
other request runs on the main lane, one at a time. A request the client has
superseded or cancelled is answered at once and never executed. Every line
written to the output goes through one lock, so progress events from a run
and answers from the other lane never interleave.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import IO, Any, Mapping

from . import mesh_pool
from .errors import Cancelled, InvalidRequest, WorkerError

#: Methods where only the newest pending request per key is worth running:
#: the client shows one view at a time, and a later request means it moved on.
COALESCED_METHODS = frozenset({"get_surfaces", "get_section", "get_top_view", "plan_grid"})

#: Methods that only read stored results, run beside whatever else is going on.
VIEW_METHODS = frozenset({"get_surfaces", "get_section", "get_top_view"})
LANES = ("main", "views")

#: How long to wait, once the input has closed, for work that is still
#: running to notice and put its answer down. Then the worker leaves
#: regardless: the executors are daemon threads, so the process ends with
#: the main thread. Waiting without a limit meant a worker outliving the
#: shell for as long as whatever it was doing took -- and a running
#: interpreter holds the directory it lives in open on Windows, so the
#: application's folder could not be deleted. Not every long computation
#: is interruptible: a display mesh has no cancellation check at all.
SHUTDOWN_GRACE_SECONDS = 2.0


@dataclass
class Pending:
    id: Any
    request: Mapping[str, Any]
    cancel: threading.Event = field(default_factory=threading.Event)

    @property
    def method(self) -> str:
        return str(self.request.get("method"))

    @property
    def lane(self) -> str:
        return "views" if self.method in VIEW_METHODS else "main"

    def coalescing_key(self) -> tuple[str, str] | None:
        if self.method not in COALESCED_METHODS:
            return None
        parameters = self.request.get("params") or {}
        root = parameters.get("root", "") if isinstance(parameters, Mapping) else ""
        return (self.method, str(root))


class LockedStream:
    """A text stream that admits one writer at a time."""

    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        with self._lock:
            return self._stream.write(text)

    def flush(self) -> None:
        with self._lock:
            self._stream.flush()


def write_message(stream: IO[str], message: Mapping[str, Any]) -> None:
    stream.write(json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n")
    stream.flush()


def _failure(request_id: Any, code: str, message: str) -> dict[str, Any]:
    return {
        "kind": "response",
        "id": request_id,
        "ok": False,
        "error": {"code": code, "message": message},
    }


class Server:
    def __init__(self, input_stream: IO[str], output_stream: IO[str]) -> None:
        self.input = input_stream
        self.output = LockedStream(output_stream)
        self._queue: deque[Pending] = deque()
        self._condition = threading.Condition()
        self._in_flight: dict[str, Pending | None] = {lane: None for lane in LANES}
        self._closed = False

    # -- reader side --------------------------------------------------------

    def run(self) -> int:
        executors = [
            threading.Thread(target=self._execute_forever, args=(lane,), name=f"executor-{lane}", daemon=True)
            for lane in LANES
        ]
        for executor in executors:
            executor.start()
        try:
            for raw_line in self.input:
                if raw_line.strip():
                    self._accept(raw_line)
        finally:
            with self._condition:
                self._closed = True
                # Whoever is still computing has nobody left to answer.
                for pending in self._in_flight.values():
                    if pending is not None:
                        pending.cancel.set()
                self._condition.notify_all()
            deadline = time.monotonic() + SHUTDOWN_GRACE_SECONDS
            for executor in executors:
                executor.join(max(0.0, deadline - time.monotonic()))
        # The mesh pool's children are interpreters of their own, and one of
        # them still running holds the installation folder open just as this
        # process would.
        mesh_pool.shutdown(final=True)
        return 0

    def _accept(self, raw_line: str) -> None:
        try:
            message = json.loads(raw_line)
            if not isinstance(message, dict):
                raise InvalidRequest("Each input line must contain a JSON object.")
        except (json.JSONDecodeError, InvalidRequest) as error:
            write_message(self.output, _failure(None, "InvalidRequest", str(error)))
            return
        kind = message.get("kind")
        if kind == "cancel":
            self._cancel(message.get("id"))
            return
        if kind not in (None, "request"):
            write_message(
                self.output,
                _failure(message.get("id"), "InvalidRequest", "Input kind must be 'request' or 'cancel'."),
            )
            return
        pending = Pending(message.get("id"), message)
        with self._condition:
            key = pending.coalescing_key()
            if key is not None:
                for older in [item for item in self._queue if item.coalescing_key() == key]:
                    self._queue.remove(older)
                    write_message(
                        self.output,
                        _failure(older.id, "Superseded", "A newer request replaced this one."),
                    )
            self._queue.append(pending)
            self._condition.notify_all()  # both lanes look; the one it is for takes it

    def _cancel(self, request_id: Any) -> None:
        with self._condition:
            for item in list(self._queue):
                if item.id == request_id:
                    self._queue.remove(item)
                    write_message(
                        self.output,
                        _failure(item.id, "Cancelled", "The request was cancelled before it ran."),
                    )
                    return
            for pending in self._in_flight.values():
                if pending is not None and pending.id == request_id:
                    pending.cancel.set()

    # -- executor side ------------------------------------------------------

    def _execute_forever(self, lane: str) -> None:
        while True:
            with self._condition:
                pending = self._next_for(lane)
                while pending is None and not self._closed:
                    self._condition.wait()
                    pending = self._next_for(lane)
                if pending is None:
                    return
                self._in_flight[lane] = pending
            try:
                self._execute(pending)
            finally:
                with self._condition:
                    self._in_flight[lane] = None

    def _next_for(self, lane: str) -> Pending | None:
        """The oldest queued request for this lane, taken off the queue."""
        for item in self._queue:
            if item.lane == lane:
                self._queue.remove(item)
                return item
        return None

    def _execute(self, pending: Pending) -> None:
        from .protocol import dispatch

        try:
            result = dispatch(pending.request, self.output, cancel=pending.cancel)
            write_message(
                self.output,
                {"kind": "response", "id": pending.id, "ok": True, "result": result},
            )
        except Cancelled as error:
            write_message(self.output, _failure(pending.id, "Cancelled", str(error)))
        except WorkerError as error:
            write_message(self.output, _failure(pending.id, type(error).__name__, str(error)))
        except (KeyError, TypeError, ValueError) as error:
            write_message(self.output, _failure(pending.id, "InvalidRequest", str(error)))
        except Exception as error:  # fail closed without leaking a traceback
            write_message(self.output, _failure(pending.id, "InternalError", str(error)))


def serve(input_stream: IO[str] = sys.stdin, output_stream: IO[str] = sys.stdout) -> int:
    return Server(input_stream, output_stream).run()
