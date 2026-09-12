"""The update check: what it asks GitHub and what it makes of the answer."""

from __future__ import annotations

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
