"""The update check: what it asks GitHub and what it makes of the answer."""

from __future__ import annotations

from pathlib import Path

import pytest

from process_studio import __version__
from process_studio.worker import update
from process_studio.worker.errors import InvalidRequest, WorkerError


def test_the_asset_follows_the_platform_and_the_edition():
    assert update.asset_name("slab", "win32") == "ProcessStudio-Slab-Windows.zip"
    assert update.asset_name("full", "darwin") == "ProcessStudio-macOS.zip"
    assert update.asset_name("levelset", "win32") == "ProcessStudio-LevelSet-Windows.zip"
    assert update.asset_name("slab", "linux") is None


def test_versions_compare_numerically():
    assert update.version_tuple("v0.10.0") > update.version_tuple("0.9.9")
    assert update.version_tuple("0.6.0-rc1") == (0, 6, 0)


def test_a_release_is_described_against_this_build(monkeypatch):
    monkeypatch.setattr(update, "asset_name", lambda *args: "ProcessStudio-Slab-Windows.zip")
    release = {
        "tag_name": "v9.0.0",
        "html_url": "https://github.com/lisiyuan2005/process-studio/releases/tag/v9.0.0",
        "published_at": "2026-09-12T00:00:00Z",
        "body": "notes",
        "assets": [
            {"name": "ProcessStudio-macOS.zip", "browser_download_url": "m", "size": 1},
            {"name": "ProcessStudio-Slab-Windows.zip", "browser_download_url": "w", "size": 123},
        ],
    }
    described = update.describe_release(release)
    assert described["currentVersion"] == __version__
    assert described["latestVersion"] == "9.0.0" and described["isNewer"]
    assert described["asset"] == {"name": "ProcessStudio-Slab-Windows.zip", "url": "w", "sizeBytes": 123}
    older = update.describe_release({"tag_name": "v0.0.1", "assets": []})
    assert not older["isNewer"] and older["asset"] is None


def test_check_update_reads_the_latest_release(monkeypatch):
    monkeypatch.setattr(update, "fetch_latest_release", lambda: {"tag_name": "v0.0.1", "assets": []})
    assert update.check_update()["latestVersion"] == "0.0.1"


def test_only_the_repository_pages_can_be_opened(monkeypatch):
    opened = []
    monkeypatch.setattr(update.webbrowser, "open", lambda url: opened.append(url) or True)
    update.open_url("https://github.com/lisiyuan2005/process-studio/releases/tag/v1")
    assert opened == ["https://github.com/lisiyuan2005/process-studio/releases/tag/v1"]
    with pytest.raises(InvalidRequest):
        update.open_url("https://example.com/")
    with pytest.raises(InvalidRequest):
        update.open_url(None)
    monkeypatch.setattr(update.webbrowser, "open", lambda url: False)
    with pytest.raises(WorkerError):
        update.open_url("https://github.com/lisiyuan2005/process-studio/")


def test_the_release_archive_is_unpacked_beside_the_application(tmp_path, monkeypatch):
    import zipfile

    monkeypatch.setattr(update.sys, "platform", "win32")
    archive = tmp_path / "release.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("ProcessStudio.exe", b"app")
        bundle.writestr("resources/worker/process-studio-worker.exe", b"worker")
    staged = update.stage_update(archive, tmp_path / "unpacked")
    assert (staged / "ProcessStudio.exe").read_bytes() == b"app"
    assert (staged / "resources" / "worker" / "process-studio-worker.exe").is_file()
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as bundle:
        bundle.writestr("../escape.exe", b"x")
    with pytest.raises(WorkerError, match="unsafe"):
        update.stage_update(bad, tmp_path / "unpacked2")


def test_the_updater_script_waits_copies_and_restarts(tmp_path, monkeypatch):
    app = tmp_path / "ProcessStudio"
    app.mkdir()
    staged = tmp_path / "work" / "unpacked"
    staged.mkdir(parents=True)
    monkeypatch.setattr(update.sys, "platform", "win32")
    command, log = update.write_updater(app, staged, [11, 22])
    script = (tmp_path / "process-studio-update.cmd").read_text()
    assert command[0] == "cmd.exe" and log.parent == tmp_path
    assert "PID eq 11" in script and "PID eq 22" in script and "robocopy" in script
    monkeypatch.setattr(update.sys, "platform", "darwin")
    bundle = tmp_path / "Process Studio.app"
    command, _ = update.write_updater(bundle, staged, [33])
    script = (tmp_path / ".process-studio-update.sh").read_text()
    assert command[0] == "/bin/bash" and "kill -0 33" in script and "ditto" in script and "open '" in script


def test_installing_needs_a_packaged_application_and_the_repository_url():
    with pytest.raises(WorkerError, match="source checkout"):
        update.install_update("https://github.com/lisiyuan2005/process-studio/releases/download/v1/x.zip")
    with pytest.raises(InvalidRequest):
        update.download("https://example.com/x.zip", Path("/nonexistent/x.zip"))
    assert update.application_root() is None


def test_application_root_takes_the_shells_override_first(monkeypatch, tmp_path):
    # The Windows shell running an embeddable Python is never sys.frozen, so
    # it hands the app root down itself instead.
    monkeypatch.setenv("PROCESS_STUDIO_APP_ROOT", str(tmp_path))
    assert update.application_root() == tmp_path
