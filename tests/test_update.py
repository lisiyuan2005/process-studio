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
    # `resources` is mirrored, so a previous version's worker cannot linger
    # there and be picked ahead of the one that just arrived; everything
    # else is merged, leaving whatever the user keeps beside the app.
    assert "/MIR" in script and "/XD" in script and "resources" in script
    # The script runs detached and with no console, where `timeout` fails at
    # once ("input redirection is not supported"). It used to be the whole
    # wait, so the copy started while the application was still running and
    # every file it needed was locked: nothing was replaced and the unpacked
    # build stayed behind as a new folder beside the old version.
    assert "timeout" not in script and "ping -n" in script
    # A failed copy has to look different from a successful one, or the old
    # version is restarted as if it had been updated.
    assert "if errorlevel 8 goto failed" in script and ":failed" in script
    # The log has to say which application was being written over.
    assert f'application: "{app}"' in script
    monkeypatch.setattr(update.sys, "platform", "darwin")
    bundle = tmp_path / "Process Studio.app"
    command, _ = update.write_updater(bundle, staged, [33])
    script = (tmp_path / ".process-studio-update.sh").read_text()
    assert command[0] == "/bin/bash" and "kill -0 33" in script and "ditto" in script and "open '" in script
    # The bundle is only thrown away once its replacement is in place.
    assert f"mv '{bundle}' '{bundle}.previous'" in script
    assert f"mv '{bundle}.previous' '{bundle}'" in script


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


def test_a_failed_update_leaves_a_folder_that_the_next_one_clears(tmp_path):
    # The visible symptom of a failed update: the unpacked build stays put
    # beside the application instead of replacing it. Starting another
    # update sweeps them, so they cannot pile up at a build apiece.
    app = tmp_path / "ProcessStudio"
    app.mkdir()
    wreck = tmp_path / f"{update.STAGING_PREFIX}abcd"
    (wreck / "unpacked").mkdir(parents=True)
    (wreck / "unpacked" / "ProcessStudio.exe").write_bytes(b"old attempt")
    keep = tmp_path / "my notes"
    keep.mkdir()
    update.clear_stale_staging(tmp_path)
    assert not wreck.exists() and keep.exists() and app.exists()


def test_the_update_check_verifies_against_the_system_trust_store():
    """A packaged worker has no CA bundle of its own.

    Its OpenSSL was built on a machine that is not the user's, so the path
    to a bundle compiled into it points at nothing and every check dies
    with "unable to get local issuer certificate". The operating system's
    store is the right place to look: it is what the machine's browser
    trusts, including the root a company's inspecting proxy needs.
    """
    import ssl

    context = update.verifier()
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    # The same context every time; building one reads a certificate store.
    assert update.verifier() is context


def test_the_bundle_is_the_fallback_when_the_system_store_is_unreachable(monkeypatch):
    import builtins
    import ssl

    real_import = builtins.__import__

    def without_truststore(name, *args, **kwargs):
        if name == "truststore":
            raise ImportError("no truststore here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_truststore)
    update.verifier.cache_clear()
    try:
        context = update.verifier()
        assert isinstance(context, ssl.SSLContext) and context.check_hostname
        assert context.get_ca_certs(), "the fallback has to carry authorities of its own"
    finally:
        monkeypatch.undo()
        update.verifier.cache_clear()


def test_a_certificate_failure_says_what_it_is_about():
    """The raw OpenSSL line tells a user nothing they can act on."""
    import ssl
    import urllib.error

    verify = urllib.error.URLError(
        ssl.SSLCertVerificationError(1, "unable to get local issuer certificate")
    )
    message = str(update._unreachable(verify))
    assert "certificate" in message and "not about the network" in message
    # Anything else keeps the plain wording.
    assert "Could not reach GitHub" in str(update._unreachable(urllib.error.URLError("refused")))
