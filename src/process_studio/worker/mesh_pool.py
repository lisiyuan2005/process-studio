"""The 3D view's triangles, built on several cores.

One material's caps and walls never look at another material's mesh, so
the materials of a stack are as parallel as work gets. Threads do not get
it: shapely does release the interpreter lock, but a good half of the
builder is Python-level vertex welding, and on a 5x5 hole block four
threads came out *slower* than one (0.82x). Four processes on that same
block take the full mesh from 1.29 s to 0.42 s (3.0x) and the free surface
from 0.46 s to 0.27 s (1.7x, which is the slowest single material).

Children are started with ``spawn`` on every platform, never ``fork``: the
worker is full of threads (the RPC lanes, the warming threads), and a fork
of a threaded process inherits locks held by threads that do not exist on
the other side. The price is re-importing numpy, shapely and trimesh in
each child, about 0.9 s for a pool of four and some 2 s in a packaged
build, so

* the pool is kept between builds -- a warm round costs ~1 ms of overhead;
* no build ever waits for it: the first one starts the pool in the
  background and builds sequentially itself;
* four idle children hold ~420 MB, so a pool nothing has used for a few
  minutes is let go.

Nothing here is required. Every path falls back to the sequential builder,
because a machine that will not give us processes should still show the 3D
view, and once it has refused once we stop asking.
"""

from __future__ import annotations

import atexit
import contextlib
import multiprocessing
import os
import pickle
import sys
import tempfile
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Iterator

import numpy as np

#: A display mesh as the 3D view wants it: vertices, triangles, whether
#: each triangle lies against another material, and which one.
MeshArrays = tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]

#: More processes than this buy nothing: the parallelism is bounded by the
#: number of materials in the stack, which is a handful, and every child
#: costs ~105 MB and ~0.2 s to start.
MAX_WORKERS = 4

#: A pool nothing has asked for in this long is given back to the machine.
IDLE_SECONDS = 180.0
REAP_EVERY = 15.0


def worker_count() -> int:
    """How many children to build with; 1 means "do it here".

    ``PROCESS_STUDIO_MESH_WORKERS`` overrides it, and setting it to 1 turns
    the pool off entirely -- the escape hatch for a machine where starting
    processes is the problem rather than the cure.
    """
    asked = os.environ.get("PROCESS_STUDIO_MESH_WORKERS")
    if asked:
        try:
            return max(1, int(asked))
        except ValueError:
            pass
    return max(1, min(MAX_WORKERS, os.cpu_count() or 1))


_lock = threading.RLock()
_pool: ProcessPoolExecutor | None = None
_starting: threading.Thread | None = None
_reaper: threading.Thread | None = None
_in_flight = 0
_last_used = 0.0
_registered = False
#: why we stopped trying, once we have
refused: str | None = None


# -- the child side --------------------------------------------------------

#: the state this child last read and the figure it cut from it, kept so
#: the other materials of the same build do not each pay for them again --
#: the figure is 6.6 s on a real stack and says nothing about which
#: material is being built. Keyed by the file's identity rather than its
#: name: a temporary name can be handed out again once we have deleted it,
#: and that would serve the wrong state.
_held: tuple[tuple[str, int, int], Any, Any] | None = None


def _child_setup() -> None:
    # The worker answers JSON lines on stdout, and a child inherits it. We
    # print nothing, but a library that decided to would be read as a reply,
    # so the child's stdout goes where its tracebacks already go.
    if sys.stderr is not None:
        sys.stdout = sys.stderr


def _ready(_index: int) -> int:
    """Build a two-material block, so that "the pool is up" is the truth.

    Submitting one task per child is what makes a ``ProcessPoolExecutor``
    start them all. Importing the builder is not enough to make the first
    real material quick, though -- the triangulator and half of trimesh are
    only reached on the first mesh -- so this warms the whole path on a
    solid small enough (a hundred triangles) to cost nothing.
    """
    from deviceflow import Device
    from deviceflow._internal.mesh.builder import build_one_material, materials_in_order
    from deviceflow._internal.mesh.triangulate import DEFAULT_ENGINE, using

    device = Device("mesh-pool-warmup", (0.0, 0.0, 1.0, 1.0), verbose=False)
    # Names the built-in palette knows, so this needs no material table.
    device.material("Si")
    device.material("SiO2")
    device.deposit("Si", 0.1, mode="planar")
    device.deposit("SiO2", 0.1, mode="planar")
    state = device._state
    state.validate()
    materials = materials_in_order(state)
    with using(DEFAULT_ENGINE):
        for material in materials:
            display_arrays(
                build_one_material(state, material, manifold=False, materials=materials)
            )
    return 1


def _build_material(job: tuple[str, int, str, bool, bool]) -> tuple[str, ...]:
    global _held
    path, index, engine, buried, manifold = job

    from deviceflow._internal.mesh.builder import arrange, build_one_material

    stat = os.stat(path)
    key = (path, stat.st_size, stat.st_mtime_ns)
    held = _held
    if held is None or held[0] != key:
        with open(path, "rb") as handle:
            state = pickle.loads(handle.read())
        _held = held = (key, state, arrange(state))
    _key, state, figure = held

    from deviceflow._internal.mesh.triangulate import using

    material = figure.materials[index]
    with using(engine):
        mesh = build_one_material(
            state, material, manifold=manifold, buried=buried, arrangement=figure
        )
    return (material.name, *display_arrays(mesh))


def display_arrays(mesh: Any) -> MeshArrays:
    """One material's mesh in the form the 3D view reads.

    A material with no free surface has an empty mesh, whose arrays come
    back flat; the view wants (n, 3) either way. A face against another
    material is the same face in that material's mesh, so each face also
    says which material it lies against, by its place in the build order:
    the viewer leaves such faces out while that other material is shown --
    two copies of one face would fight for the same pixels -- and draws
    them once it is hidden.
    """
    return (
        np.ascontiguousarray(mesh.vertices, dtype=np.float32).reshape(-1, 3),
        np.ascontiguousarray(mesh.faces, dtype=np.uint32).reshape(-1, 3),
        np.ascontiguousarray(mesh.metadata["interface_faces"], dtype=np.uint8),
        np.ascontiguousarray(mesh.metadata["neighbour_faces"], dtype=np.int16),
    )


# -- the pool --------------------------------------------------------------


def start() -> ProcessPoolExecutor | None:
    """Bring the pool up, here and now. Returns None if it will not come.

    One task per child is submitted before returning, because an executor
    with nothing to do has no children at all: each submission starts one,
    unless a child has already gone idle by then. So this warms whatever
    children it starts and a real build brings out the rest -- what it
    guarantees is that the machine *will* give us processes, which is the
    thing a caller cannot afford to find out in the middle of a build.
    """
    global _pool, refused
    with _lock:
        if _pool is not None:
            return _pool
        if refused is not None:
            return None
        count = worker_count()
        if count < 2:
            refused = "asked for one worker"
            return None
    try:
        pool = ProcessPoolExecutor(
            max_workers=count,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=_child_setup,
        )
        list(pool.map(_ready, range(count)))
    except Exception as error:  # noqa: BLE001 - any refusal means "not here"
        _refuse(error)
        return None
    with _lock:
        # Another caller won the race, or the worker went away while we were
        # starting -- either way these children have nothing to do, and
        # leaving them running is the folder-in-use bug all over again.
        if _pool is not None or refused is not None:
            _end(pool)
            return _pool
        _pool = pool
        _touch()
        _watch()
        _end_with_the_interpreter()
        return pool


def _end_with_the_interpreter() -> None:
    """End the children before multiprocessing tears its own plumbing down.

    A command-line run starts the pool for the meshes it is about to warm
    and then exits, and a child still coming up would try to rebuild the
    call queue's semaphore after the file behind it was unlinked --
    FileNotFoundError, printed at the end of an otherwise clean run.
    ``atexit`` runs its handlers newest first, and multiprocessing's were
    registered when the first child was created, so registering here (and
    not at import) is what puts this one ahead of them.
    """
    global _registered
    if _registered:
        return
    _registered = True
    atexit.register(shutdown, final=True)


def prewarm() -> None:
    """Start the pool in the background, once."""
    global _starting
    with _lock:
        if _pool is not None or refused is not None:
            return
        if _starting is not None and _starting.is_alive():
            return
        _starting = threading.Thread(target=start, name="mesh-pool", daemon=True)
        _starting.start()


def shutdown(final: bool = False) -> None:
    """End the children now.

    An interpreter holds the directory it lives in open on Windows, so a
    child that outlives the worker is a folder the updater cannot replace
    and the user cannot delete. That is the bug this has to not reintroduce,
    which is why it does not wait for what the children are doing.

    ``final`` is the worker going away: no pool is started after it, which
    also settles a start that is in flight right now.
    """
    global _pool, refused
    with _lock:
        pool, _pool = _pool, None
        if final:
            refused = "the worker is shutting down"
    _end(pool)


def _end(pool: ProcessPoolExecutor | None) -> None:
    if pool is None:
        return
    # Read before the shutdown: it is what clears the record of them.
    # concurrent.futures has no public way to end a child that is mid-task,
    # and waiting for one is what we cannot do here.
    children = list((getattr(pool, "_processes", None) or {}).values())
    try:
        pool.shutdown(wait=False, cancel_futures=True)
    except Exception:  # noqa: BLE001
        pass
    for child in children:
        try:
            if child.is_alive():
                child.kill()
            child.join(1.0)
        except Exception:  # noqa: BLE001
            pass


def _refuse(error: BaseException) -> None:
    global refused
    with _lock:
        refused = f"{type(error).__name__}: {error}"
    shutdown()


def _touch() -> None:
    global _last_used
    _last_used = time.monotonic()


def _watch() -> None:
    global _reaper
    if _reaper is not None and _reaper.is_alive():
        return
    _reaper = threading.Thread(target=_reap_when_idle, name="mesh-pool-idle", daemon=True)
    _reaper.start()


def _reap_when_idle() -> None:
    while True:
        time.sleep(REAP_EVERY)
        with _lock:
            if _pool is None:
                return
            if _in_flight or time.monotonic() - _last_used < IDLE_SECONDS:
                continue
        shutdown()
        return


# -- what the kernel calls -------------------------------------------------


def build(
    state: Any, *, engine: str, buried: bool, manifold: bool = False
) -> dict[str, MeshArrays]:
    """One display mesh per material, in build order, however is quickest.

    On the pool when it is up and the handover is small enough; here
    otherwise, having asked for a pool for next time.
    """
    from deviceflow._internal.mesh.builder import materials_in_order

    names = [material.name for material in materials_in_order(state)]
    if len(names) > 1 and worker_count() > 1 and refused is None:
        pool = _lease()
        if pool is None:
            prewarm()
        else:
            try:
                with _handed_over(state) as path:
                    return _gather(pool, path, len(names), engine, buried, manifold)
            except Exception as error:  # noqa: BLE001
                _refuse(error)
            finally:
                _release()
    return build_here(state, engine=engine, buried=buried, manifold=manifold)


@contextlib.contextmanager
def _handed_over(state: Any) -> Iterator[str]:
    """The state on disk, for each child to read once.

    One task per material is what keeps the cores busy -- the materials are
    wildly uneven, 19.4 s against 0.4 s on the project this was measured on
    -- but a task carries its arguments to the child, so a state passed
    inline crosses the wire once per material: 159 MB for a 22.7 MB state
    and seven materials. A file is written once and read from the page
    cache, and measured exactly as fast (25.3 s against 25.9 s), so it is
    what lets any state through. That matters more than it sounds: the size
    ceiling this replaces silently turned the pool off on the first real
    project it met.
    """
    handle = tempfile.NamedTemporaryFile(
        prefix="process-studio-mesh-", suffix=".state", delete=False
    )
    try:
        handle.write(pickle.dumps(state, protocol=pickle.HIGHEST_PROTOCOL))
        handle.close()
        yield handle.name
    finally:
        handle.close()
        with contextlib.suppress(OSError):
            os.unlink(handle.name)


def _lease() -> ProcessPoolExecutor | None:
    global _in_flight
    with _lock:
        if _pool is None:
            return None
        _in_flight += 1
        _touch()
        return _pool


def _release() -> None:
    global _in_flight
    with _lock:
        _in_flight = max(0, _in_flight - 1)
        _touch()


def _gather(
    pool: ProcessPoolExecutor,
    path: str,
    count: int,
    engine: str,
    buried: bool,
    manifold: bool,
) -> dict[str, MeshArrays]:
    jobs = [(path, index, engine, buried, manifold) for index in range(count)]
    built: dict[str, MeshArrays] = {}
    for name, vertices, faces, interface, neighbour in pool.map(_build_material, jobs):
        built[name] = (vertices, faces, interface, neighbour)
    return built


def build_here(
    state: Any, *, engine: str, buried: bool, manifold: bool = False
) -> dict[str, MeshArrays]:
    """The same meshes, built in this process."""
    from deviceflow._internal.mesh.builder import build_material_meshes
    from deviceflow._internal.mesh.triangulate import using

    with using(engine):
        built = build_material_meshes(state, manifold=manifold, buried=buried)
    return {material.name: display_arrays(mesh) for material, mesh in built.items()}
