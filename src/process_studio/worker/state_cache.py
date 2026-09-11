"""Recently used states, kept in memory between requests.

Reading a stored state back is the slow part of a view on a fine grid: a
compressed six-material state at 6.25 nm takes seconds to inflate, and the
views of one step ask for the same state several times over. The process
now lives for the whole session, so the last few states can simply stay.

Entries are keyed by the snapshot file, whose name is unique per save, so a
re-run that writes a new file never collides with the old state. The budget
is in bytes because states differ by orders of magnitude between kernels.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

DEFAULT_BUDGET_BYTES = 1024 * 1024 * 1024


def _budget_from_environment() -> int:
    raw = os.environ.get("PROCESS_STUDIO_STATE_CACHE_MB")
    if not raw:
        return DEFAULT_BUDGET_BYTES
    try:
        return max(0, int(float(raw) * 1024 * 1024))
    except ValueError:
        return DEFAULT_BUDGET_BYTES


class StateCache:
    def __init__(self, budget_bytes: int) -> None:
        self.budget = int(budget_bytes)
        self._entries: OrderedDict[str, tuple[Any, int]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, path: Path | str) -> Any | None:
        key = str(path)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
            return entry[0]

    def put(self, path: Path | str, state: Any, size: int) -> None:
        key = str(path)
        size = max(0, int(size))
        with self._lock:
            if key in self._entries:
                self._bytes -= self._entries.pop(key)[1]
            if size > self.budget:
                # Larger than the whole budget: keeping it would evict
                # everything else for one entry that may never be reused.
                return
            self._entries[key] = (state, size)
            self._bytes += size
            while self._bytes > self.budget and self._entries:
                _, (_, evicted) = self._entries.popitem(last=False)
                self._bytes -= evicted

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._bytes = 0

    @property
    def bytes_used(self) -> int:
        return self._bytes

    def __len__(self) -> int:
        return len(self._entries)


STATE_CACHE = StateCache(_budget_from_environment())
