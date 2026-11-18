"""The OpenVDB kernel: per-material level sets (see :mod:`.kernel`).

Importing this needs the compiled core (``_vdbcore``, built by
``scripts/build_vdbcore.py`` against OpenVDB), scipy and scikit-fmm. A
build without them reports the kernel as unavailable and offers the slab
kernel alone.
"""

from .kernel import OpenVDBKernel

__all__ = ["OpenVDBKernel"]
