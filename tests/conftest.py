"""Test-wide setup.

The mesh pool is the one thing here that outlives a test: once a build has
warmed it, the next test's ``monkeypatch`` of the builder would be patching
this process while the work happened in a child. So it is off by default
and the tests that want it turn it on for themselves.
"""

import pytest

from process_studio.worker import mesh_pool


@pytest.fixture(autouse=True)
def mesh_pool_off(monkeypatch):
    monkeypatch.setenv("PROCESS_STUDIO_MESH_WORKERS", "1")
    monkeypatch.setattr(mesh_pool, "refused", None)
    yield
    mesh_pool.shutdown()
    mesh_pool.refused = None
