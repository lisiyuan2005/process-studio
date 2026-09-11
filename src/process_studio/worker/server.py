"""The long-lived request loop behind the desktop shell.

One process serves a whole session. Requests arrive on stdin and are answered
on stdout, but reading and executing are separate threads so that a cancel
message can reach a run that is already under way, and so that a view request
which a newer one has made pointless can be dropped before it is computed.

Ordering: requests execute one at a time, in arrival order, except that a
request the client has superseded or cancelled is answered at once and never
executed. Every line written to the output goes through one lock, so progress
events from the executor and answers from the reader never interleave.
"""

from __future__ import annotations

import json
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import IO, Any, Mapping

from .errors import Cancelled, InvalidRequest, WorkerError

#: Methods where only the newest pending request per key is worth running:
#: the client shows one view at a time, and a later request means it moved on.
COALESCED_METHODS = frozenset({"get_surfaces", "get_section", "get_top_view", "plan_grid"})


@dataclass
class Pending:
    id: Any
    request: Mapping[str, Any]
    cancel: threading.Event = field(default_factory=threading.Event)

    @property
    def method(self) -> str:
        return str(self.request.get("method"))

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
        self._in_flight: Pending | None = None
        self._closed = False

    # -- reader side --------------------------------------------------------

    def run(self) -> int:
        executor = threading.Thread(target=self._execute_forever, name="executor", daemon=True)
        executor.start()
        try:
            for raw_line in self.input:
                if raw_line.strip():
                    self._accept(raw_line)
        finally:
            with self._condition:
                self._closed = True
                # Whoever is still computing has nobody left to answer.
                if self._in_flight is not None:
                    self._in_flight.cancel.set()
                self._condition.notify_all()
            executor.join()
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
            self._condition.notify()

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
            if self._in_flight is not None and self._in_flight.id == request_id:
                self._in_flight.cancel.set()

    # -- executor side ------------------------------------------------------

    def _execute_forever(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._closed:
                    self._condition.wait()
                if not self._queue:
                    return
                pending = self._queue.popleft()
                self._in_flight = pending
            try:
                self._execute(pending)
            finally:
                with self._condition:
                    self._in_flight = None

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
