"""The simulation kernel a project is built on.

There is one: the slab kernel, exact polygon slabs from the DeviceFlow
core. The level-set kernel lived here until 0.9.8 and was removed -- it is
on the ``archive/levelset`` branch -- so a project made with it is refused
with a message saying where to open it, never silently run on this one.

The registry is kept for the shape of the interface (a project names its
kernel and keeps it, and the desktop reads what the kernel offers) and
because a kernel whose dependencies a build left out has to be reported
rather than crash. ``PROCESS_STUDIO_KERNELS`` or a bundled ``enabled.txt``
beside this file still names what this worker offers.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from .base import Kernel, KernelInfo

#: Every kernel this source tree knows, in offering order, with the module
#: that provides it. Import failures mean the build left its dependencies out.
_KNOWN: tuple[tuple[str, str, str], ...] = (
    ("slab", "process_studio.kernels.slab", "SlabKernel"),
)

#: Kernels that were here and are not any more, and where to open a project
#: that names one. Without this the message would be "unknown kernel", which
#: reads as a corrupt file rather than as a version that no longer ships it.
_RETIRED = {
    "levelset": (
        "the level-set kernel was removed after 0.9.8; open this project in "
        "Process Studio 0.9.8 or earlier, or rebuild the flow on the slab kernel"
    ),
}

_KERNELS: dict[str, Kernel] = {}
_UNAVAILABLE: dict[str, str] = {}


def _configured_ids() -> set[str] | None:
    """Ids the build or the environment restricts this worker to, or None."""
    raw = os.environ.get("PROCESS_STUDIO_KERNELS")
    if raw is None:
        bundled = Path(__file__).with_name("enabled.txt")
        if not bundled.is_file():
            return None
        raw = bundled.read_text(encoding="utf-8")
    ids = {item.strip() for item in raw.replace("\n", ",").split(",") if item.strip()}
    return ids or None


def configure(enabled: Iterable[str] | None) -> None:
    """Rebuild the registry offering only ``enabled`` (None means every kernel)."""
    import importlib

    wanted = None if enabled is None else set(enabled)
    _KERNELS.clear()
    _UNAVAILABLE.clear()
    for kernel_id, module_name, class_name in _KNOWN:
        if wanted is not None and kernel_id not in wanted:
            _UNAVAILABLE[kernel_id] = "this build does not include it"
            continue
        try:
            module = importlib.import_module(module_name)
        except ImportError as error:
            _UNAVAILABLE[kernel_id] = f"its dependencies are not installed ({error})"
            continue
        _KERNELS[kernel_id] = getattr(module, class_name)()
    if not _KERNELS:
        raise RuntimeError(
            "no simulation kernel is available: "
            + "; ".join(f"{name}: {why}" for name, why in _UNAVAILABLE.items())
        )


def available_kernels() -> list[Kernel]:
    """Every kernel this worker can run, in offering order."""
    return list(_KERNELS.values())


def kernel_ids() -> tuple[str, ...]:
    return tuple(_KERNELS)


def default_kernel() -> str:
    """The kernel a workspace gets when none is named: the first offered."""
    return next(iter(_KERNELS))


def build_variant() -> str:
    """What this worker ships: ``full`` when it has every kernel it knows."""
    ids = tuple(_KERNELS)
    return "full" if len(ids) == len(_KNOWN) else "+".join(ids)


def get_kernel(kernel_id: str | None) -> Kernel:
    """The kernel a project runs on; the default when unnamed."""
    resolved = kernel_id or default_kernel()
    try:
        return _KERNELS[resolved]
    except KeyError:
        pass
    known = {kernel_id for kernel_id, _, _ in _KNOWN}
    offered = ", ".join(_KERNELS)
    if resolved in _RETIRED:
        raise KeyError(f"kernel {resolved!r} is not in this build: {_RETIRED[resolved]}.")
    if resolved in known:
        raise KeyError(
            f"kernel {resolved!r} is not in this build ({_UNAVAILABLE.get(resolved, 'unavailable')}); "
            f"this build offers {offered}. Open the project in a build that includes {resolved!r}."
        )
    raise KeyError(f"unknown kernel {resolved!r}; this build has {offered}")


configure(_configured_ids())

# Kept for callers that read it as a constant; prefer default_kernel().
DEFAULT_KERNEL = default_kernel()

__all__ = [
    "DEFAULT_KERNEL",
    "Kernel",
    "KernelInfo",
    "available_kernels",
    "build_variant",
    "configure",
    "default_kernel",
    "get_kernel",
    "kernel_ids",
]
