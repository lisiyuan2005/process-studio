"""All DeviceFlow errors derive from DeviceFlowError."""


class DeviceFlowError(Exception):
    """Base class for every error raised by deviceflow."""


class UnitError(DeviceFlowError, ValueError):
    """A length could not be parsed."""


class MaterialError(DeviceFlowError, ValueError):
    """Unknown, duplicate or otherwise invalid material."""


class MaskError(DeviceFlowError, ValueError):
    """Invalid or empty mask geometry."""


class GeometryError(DeviceFlowError):
    """Process geometry is invalid; never continue to meshing."""


class ProcessError(DeviceFlowError, ValueError):
    """A process operation was requested with invalid parameters."""


class MeshError(DeviceFlowError):
    """Mesh construction or validation failed."""
