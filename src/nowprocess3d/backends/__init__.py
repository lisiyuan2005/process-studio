"""后端注册表。"""


def get_backend(name: str):
    if name == "viennaps":
        from .viennaps_backend import ViennaPSBackend
        return ViennaPSBackend
    if name == "openvdb":
        from .openvdb_backend import OpenVDBBackend
        return OpenVDBBackend
    raise ValueError(f"未知后端 {name!r}，可用：viennaps、openvdb")
