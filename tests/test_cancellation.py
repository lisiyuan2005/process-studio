"""Stopping a run that is already inside a step.

The runner has always checked between steps, which is no help while one
step is running: a conformal deposition walks hundreds of z samples and a
run on a real structure spends nearly all its time inside such a call, so
Stop appeared to do nothing until the step happened to finish. The
geometry now asks the caller's check as it goes.
"""

from __future__ import annotations

import threading

import pytest

from deviceflow import Device
from deviceflow.cancellation import Cancelled, cancelling, check_cancelled
from process_studio.defaults import default_grid
from process_studio.kernels import get_kernel
from process_studio.models import ProcessStep, ProcessType, ProjectDefinition
from process_studio.worker.errors import Cancelled as WorkerCancelled
from process_studio.worker.serialize import grid_dict


def _device(resolution: float = 0.004) -> Device:
    device = Device("cancel probe", (-1, -1, 1, 1), conformal_resolution=resolution, verbose=False)
    device.material("Si")
    device.material("SiO2")
    device.deposit("Si", 0.4, mode="planar")
    return device


def test_no_check_installed_means_nothing_can_stop_a_step():
    # The library outside a worker, and every test that does not ask.
    check_cancelled()
    device = _device()
    device.deposit("SiO2", 0.1, mode="conformal")
    assert any(m.name == "SiO2" for m in device.materials)


def test_a_deposition_gives_up_part_way_when_the_check_says_so():
    calls = {"n": 0}

    def after_three() -> bool:
        calls["n"] += 1
        return calls["n"] >= 3

    device = _device()
    with pytest.raises(Cancelled):
        with cancelling(after_three):
            device.deposit("SiO2", 0.2, mode="conformal")
    # It stopped at its own third check, not after walking every sample.
    assert calls["n"] == 3


def test_the_check_is_only_installed_for_the_call_it_wraps():
    device = _device()
    with pytest.raises(Cancelled):
        with cancelling(lambda: True):
            device.deposit("SiO2", 0.1, mode="conformal")
    # Outside the block the hook is gone, so the same call runs to the end.
    device.deposit("SiO2", 0.1, mode="conformal")
    assert any(m.name == "SiO2" for m in device.materials)


def test_the_slab_kernel_reports_a_stopped_step_as_cancelled():
    kernel = get_kernel("slab")
    project = ProjectDefinition(
        "cancel probe", grid_dict(default_grid()), kernel="slab", resolution_um=0.004
    )
    state = kernel.initial_state(project)
    base = ProcessStep(
        "Substrate", process_type=ProcessType.DEPOSIT, output_material="Si",
        parameters={"target": 0.4, "mode": "planar"},
    )
    state = kernel.run_step(
        state, base, project=project, recipes={}, sketches={}, logger=lambda _m: None
    )
    step = ProcessStep(
        "Liner", process_type=ProcessType.DEPOSIT, output_material="SiO2",
        parameters={"target": 0.2, "mode": "conformal"},
    )
    stop = threading.Event()
    stop.set()
    # The kernel translates the geometry's own Cancelled into the error the
    # worker answers a withdrawn request with, naming the step that stopped.
    with pytest.raises(WorkerCancelled, match="Liner"):
        kernel.run_step(
            state, step, project=project, recipes={}, sketches={},
            logger=lambda _m: None, should_cancel=stop.is_set,
        )
    # Nothing was written into the state handed in: the step's device is a
    # working copy, so the run resumes from the step before it.
    assert "SiO2" not in kernel.state_materials(state)
