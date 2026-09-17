"""Building the 3D view's materials on several cores.

One material's mesh never looks at another's, so the stack's materials are
parallel work; a process pool takes a 5x5 hole block's full mesh from
1.29 s to 0.42 s. What matters here is not the speed but that nothing
depends on it: the pool is an accelerator, the meshes must come out
identical either way, and every refusal a machine can hand back -- no
processes, a child that dies, a state too big to hand over -- has to end
in the same mesh built here.
"""

from __future__ import annotations

import pathlib

import numpy as np
import pytest
from shapely.geometry import box

from deviceflow import Device
from deviceflow._internal.geometry import polygons as P
from process_studio.worker import mesh_pool


@pytest.fixture
def cores(monkeypatch):
    """Ask for a real pool, and make sure it is gone afterwards."""
    monkeypatch.setenv("PROCESS_STUDIO_MESH_WORKERS", "2")
    monkeypatch.setattr(mesh_pool, "refused", None)
    yield 2
    mesh_pool.shutdown()
    mesh_pool.refused = None


def _stack():
    """A block of three materials, one of them sealed inside the others."""
    device = Device("pool", (-0.4, -0.4, 0.4, 0.4), conformal_resolution=0.02, verbose=False)
    silicon, oxide, nitride = (device.material(n) for n in ("Si", "SiO2", "SiN"))
    window = P.as_multipolygon(box(-0.4, -0.4, 0.4, 0.4))
    inner = P.as_multipolygon(box(-0.2, -0.2, 0.2, 0.2))
    state = device._state
    state.add_slab(0.0, 0.1, {silicon: window})
    state.add_slab(0.1, 0.2, {silicon: P.as_multipolygon(window.difference(inner)), oxide: inner})
    state.add_slab(0.2, 0.3, {nitride: window})
    state.harmonize()
    state.validate()
    return state


def _same(left, right) -> None:
    assert list(left) == list(right), "the same materials, in the same order"
    for name in left:
        for mine, theirs in zip(left[name], right[name]):
            assert np.array_equal(mine, theirs), name


class _Wedged:
    """A pool that must not be used."""

    def map(self, *_args, **_kwargs):  # pragma: no cover - the point is not to
        raise AssertionError("this build should not have gone to the pool")


def test_the_warmup_task_runs_clean():
    """What every child does before the pool counts as up.

    It builds a real two-material solid, because importing the builder is
    not what makes the first mesh quick. If it raises, starting the pool
    looks like a machine that will not give us processes, and the whole
    thing quietly turns itself off -- so it is worth its own test.
    """
    assert mesh_pool._ready(0) == 1


def test_a_pool_build_is_the_mesh_we_would_have_built(cores):
    state = _stack()
    assert mesh_pool.start() is not None, mesh_pool.refused
    _same(mesh_pool.build(state, engine="ears", buried=True), mesh_pool.build_here(state, engine="ears", buried=True))
    _same(mesh_pool.build(state, engine="ears", buried=False), mesh_pool.build_here(state, engine="ears", buried=False))


def test_shutdown_ends_the_children(cores):
    """A child outliving the worker holds the installation folder open."""
    pool = mesh_pool.start()
    assert pool is not None, mesh_pool.refused
    children = list(pool._processes.values())
    assert len(children) == cores and all(child.is_alive() for child in children)

    mesh_pool.shutdown()
    for child in children:
        child.join(5.0)
        assert not child.is_alive()
    assert mesh_pool._pool is None


def test_one_worker_means_building_it_here(monkeypatch):
    monkeypatch.setenv("PROCESS_STUDIO_MESH_WORKERS", "1")
    monkeypatch.setattr(mesh_pool, "refused", None)
    monkeypatch.setattr(mesh_pool, "_pool", _Wedged())
    assert mesh_pool.build(_stack(), engine="ears", buried=False)

    # ...and asking for one outright is answered with "build it yourself".
    monkeypatch.setattr(mesh_pool, "_pool", None)
    assert mesh_pool.start() is None
    assert mesh_pool.refused == "asked for one worker"


def test_the_first_build_starts_the_pool_but_does_not_wait_for_it(monkeypatch):
    """Nobody pays the 0.9 s of starting one; they pay it for the next build."""
    asked: list[bool] = []
    monkeypatch.setenv("PROCESS_STUDIO_MESH_WORKERS", "4")
    monkeypatch.setattr(mesh_pool, "refused", None)
    monkeypatch.setattr(mesh_pool, "_pool", None)
    monkeypatch.setattr(mesh_pool, "prewarm", lambda: asked.append(True))

    _same(mesh_pool.build(_stack(), engine="ears", buried=False), mesh_pool.build_here(_stack(), engine="ears", buried=False))
    assert asked == [True]


def test_any_state_goes_over_however_big(cores):
    """The first real project met was 22.7 MB, and a size ceiling would
    have turned the pool off on exactly the build that needed it."""
    assert mesh_pool.start() is not None, mesh_pool.refused
    state = _stack()
    with mesh_pool._handed_over(state) as path:
        handed = pathlib.Path(path)
        assert handed.is_file() and handed.stat().st_size > 0
    assert not handed.exists(), "the handover file is not left behind"


def test_the_handover_file_goes_even_if_the_build_does_not(cores):
    with pytest.raises(RuntimeError):
        with mesh_pool._handed_over(_stack()) as path:
            handed = pathlib.Path(path)
            raise RuntimeError("the build failed")
    assert not handed.exists()


def test_a_pool_that_breaks_is_not_asked_again(monkeypatch):
    """The mesh still comes out, and the next build does not try the pool."""
    monkeypatch.setenv("PROCESS_STUDIO_MESH_WORKERS", "4")
    monkeypatch.setattr(mesh_pool, "refused", None)
    monkeypatch.setattr(mesh_pool, "_pool", object())

    def broken(*_args, **_kwargs):
        raise OSError("the machine said no")

    monkeypatch.setattr(mesh_pool, "_gather", broken)
    assert mesh_pool.build(_stack(), engine="ears", buried=False)
    assert mesh_pool.refused and "the machine said no" in mesh_pool.refused
    assert mesh_pool._pool is None
    monkeypatch.setattr(mesh_pool, "_pool", _Wedged())
    assert mesh_pool.build(_stack(), engine="ears", buried=False)


def test_an_idle_pool_is_given_back(monkeypatch):
    """Four children hold about 420 MB; nobody should pay that for nothing."""
    monkeypatch.setattr(mesh_pool, "REAP_EVERY", 0.01)
    monkeypatch.setattr(mesh_pool, "IDLE_SECONDS", 0.0)
    ended: list[bool] = []
    monkeypatch.setattr(mesh_pool, "_pool", object())
    monkeypatch.setattr(mesh_pool, "shutdown", lambda: ended.append(True))
    mesh_pool._reap_when_idle()
    assert ended == [True]


def test_a_pool_in_use_is_not_reaped(monkeypatch):
    monkeypatch.setattr(mesh_pool, "REAP_EVERY", 0.01)
    monkeypatch.setattr(mesh_pool, "IDLE_SECONDS", 0.0)
    monkeypatch.setattr(mesh_pool, "_pool", object())
    monkeypatch.setattr(mesh_pool, "_in_flight", 1)
    ended: list[bool] = []

    def stop():
        ended.append(True)
        mesh_pool._pool = None

    monkeypatch.setattr(mesh_pool, "shutdown", stop)
    # The loop only leaves when the pool is gone, so drop it from under it
    # after a few rounds of finding a build in flight.
    import threading

    threading.Timer(0.1, lambda: setattr(mesh_pool, "_pool", None)).start()
    mesh_pool._reap_when_idle()
    assert ended == []


def test_the_worker_going_away_stops_the_pool_coming_back(cores):
    """A pool started just as the worker exits is children nobody ends."""
    assert mesh_pool.start() is not None, mesh_pool.refused
    mesh_pool.shutdown(final=True)
    assert mesh_pool._pool is None
    assert mesh_pool.start() is None
    # The mesh still comes out; it is only slower.
    assert mesh_pool.build(_stack(), engine="ears", buried=False)
