"""Test-wide setup.

The mesh pool is the one thing here that outlives a test: once a build has
warmed it, the next test's ``monkeypatch`` of the builder would be patching
this process while the work happened in a child. So it is off by default
and the tests that want it turn it on for themselves.

The shared material and recipe library is the other: it is one file per
user, so a test run would otherwise read and write the library of whoever
is running the tests. Every test gets its own empty one.
"""

import pytest

from process_studio.worker import mesh_pool


@pytest.fixture(autouse=True)
def private_library(tmp_path_factory, monkeypatch):
    directory = tmp_path_factory.mktemp("library")
    monkeypatch.setenv("PROCESS_STUDIO_LIBRARY", str(directory / "library.sqlite3"))


@pytest.fixture(autouse=True)
def mesh_pool_off(monkeypatch):
    monkeypatch.setenv("PROCESS_STUDIO_MESH_WORKERS", "1")
    monkeypatch.setattr(mesh_pool, "refused", None)
    yield
    mesh_pool.shutdown()
    mesh_pool.refused = None
