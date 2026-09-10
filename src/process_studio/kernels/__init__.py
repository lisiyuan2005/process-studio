"""The simulation kernels a project can be built on.

A project names its kernel when it is created and keeps it: the two kernels
represent geometry differently, so a stored result of one is not readable by
the other, and switching would silently change what every earlier step meant.
"""

from __future__ import annotations

from .base import Kernel, KernelInfo
from .levelset import LevelSetKernel

DEFAULT_KERNEL = "levelset"

_KERNELS: dict[str, Kernel] = {LevelSetKernel.info.id: LevelSetKernel()}

try:  # the slab kernel needs shapely and trimesh
    from .slab import SlabKernel
except ImportError:  # pragma: no cover - exercised only without the extra
    SlabKernel = None  # type: ignore[assignment]
else:
    _KERNELS[SlabKernel.info.id] = SlabKernel()


def available_kernels() -> list[Kernel]:
    """Every kernel this installation can run, in offering order."""
    return list(_KERNELS.values())


def kernel_ids() -> tuple[str, ...]:
    return tuple(_KERNELS)


def get_kernel(kernel_id: str | None) -> Kernel:
    """The kernel a project runs on; the level-set core when unnamed."""
    resolved = kernel_id or DEFAULT_KERNEL
    try:
        return _KERNELS[resolved]
    except KeyError:
        raise KeyError(
            f"unknown kernel {resolved!r}; this build has {', '.join(_KERNELS)}"
        ) from None


__all__ = [
    "DEFAULT_KERNEL",
    "Kernel",
    "KernelInfo",
    "LevelSetKernel",
    "SlabKernel",
    "available_kernels",
    "get_kernel",
    "kernel_ids",
]
