"""DeviceFlow: semiconductor device geometry from masks and process operations."""

__version__ = "0.2.0"

from .device import Device
from .exceptions import (
    DeviceFlowError,
    GeometryError,
    MaskError,
    MaterialError,
    MeshError,
    ProcessError,
    UnitError,
)
from .mask import Mask
from .material import Material
from .reports import GeometryReport, MaterialMeshReport, MeshReport
from .view import CrossSection, TopView

__all__ = [
    "__version__",
    "Device",
    "Layout",
    "Mask",
    "Material",
    "CrossSection",
    "TopView",
    "GeometryReport",
    "MeshReport",
    "MaterialMeshReport",
    "DeviceFlowError",
    "GeometryError",
    "MaskError",
    "MaterialError",
    "MeshError",
    "ProcessError",
    "UnitError",
]


def __getattr__(name: str):
    if name == "Layout":
        try:
            from .layout import Layout
        except ModuleNotFoundError as exc:
            if exc.name == "gdstk":
                raise ModuleNotFoundError(
                    "GDS import requires the optional dependency: pip install 'deviceflow[gds]'"
                ) from exc
            raise
        globals()[name] = Layout
        return Layout
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
