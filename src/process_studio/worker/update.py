"""Checking GitHub for a newer release of the application.

The desktop asks the worker rather than fetching itself: the webview's
content policy keeps page code off the network, while the worker is a
plain process with the user's own connectivity. Only the repository's
public release feed is read, and only its own pages are ever opened.
"""

from __future__ import annotations

import functools
import json
import re
import ssl
import sys
import urllib.error
import urllib.request
import webbrowser
from typing import Any, Mapping

from .. import __version__
from ..kernels import build_variant
from .errors import InvalidRequest, WorkerError

#: The repository whose releases carry the packaged builds.
REPOSITORY = "lisiyuan2005/process-studio"
RELEASES_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
ALLOWED_URL_PREFIXES = (
    f"https://github.com/{REPOSITORY}/",
    f"https://objects.githubusercontent.com/",
)
TIMEOUT_SECONDS = 12.0


def version_tuple(text: str) -> tuple[int, ...]:
    """Numbers of a version string such as "v0.6.1" or "0.6.1-rc2", for ordering."""
    numbers = re.findall(r"\d+", text.split("-", 1)[0])
    return tuple(int(n) for n in numbers) or (0,)


def asset_name(variant: str | None = None, platform: str | None = None) -> str | None:
    """The release asset that matches this build, or None when there is none.

    The workflow names the Windows and macOS zips by edition; other
    platforms have no packaged build.
    """
    variant = variant or build_variant()
    platform = platform or sys.platform
    edition = {"full": "", "slab": "Slab-", "levelset": "LevelSet-"}.get(variant)
    if edition is None:
        return None
    if platform.startswith("win"):
        return f"ProcessStudio-{edition}Windows.zip"
    if platform == "darwin":
        return f"ProcessStudio-{edition}macOS.zip"
    return None


def describe_release(release: Mapping[str, Any], *, current: str = __version__) -> dict[str, Any]:
    """What the desktop shows for a release document from the GitHub API."""
    tag = str(release.get("tag_name") or "")
    latest = tag[1:] if tag.startswith("v") else tag
    wanted = asset_name()
    asset = None
    for item in release.get("assets") or []:
        if wanted and item.get("name") == wanted:
            asset = {
                "name": str(item["name"]),
                "url": str(item.get("browser_download_url") or ""),
                "sizeBytes": int(item.get("size") or 0),
            }
            break
    return {
        "currentVersion": current,
        "latestVersion": latest,
        "isNewer": version_tuple(latest) > version_tuple(current),
        "releaseUrl": str(release.get("html_url") or f"https://github.com/{REPOSITORY}/releases"),
        "publishedAt": release.get("published_at"),
        "notes": str(release.get("body") or ""),
        "asset": asset,
    }


@functools.lru_cache(maxsize=1)
def verifier() -> ssl.SSLContext:
    """Where to look for the authorities GitHub's certificate chain ends at.

    A packaged worker carries its own Python, and its OpenSSL was built on
    a machine that is not this one: the path to a CA bundle compiled into
    it points at nothing here, so every https request dies with "unable to
    get local issuer certificate". Nothing is wrong with the network and
    nothing is wrong with GitHub; the interpreter simply has no idea who
    to trust.

    Asking the operating system is the best answer: it is the same set of
    authorities the machine's browser trusts, so a root that the user's IT
    put there for an inspecting proxy is included, which no bundle we ship
    could know about. Where that is unavailable, the bundle we ship is the
    fallback, and the interpreter's own idea is the last resort.
    """
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # noqa: BLE001 - any failure here falls through to a bundle
        pass
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 - and then to whatever Python was built with
        return ssl.create_default_context()


def _unreachable(error: Exception) -> WorkerError:
    """The message for a request that never got an answer."""
    if isinstance(error, urllib.error.URLError):
        error = error.reason if isinstance(error.reason, Exception) else error
    if isinstance(error, ssl.SSLCertVerificationError):
        return WorkerError(
            "Could not verify GitHub's certificate, so the check was refused: "
            f"{error}. This is about which certificate authorities this "
            "machine trusts, not about the network. On a company network "
            "that inspects https, the root certificate your IT installs has "
            "to be in the system's own store; the update check reads that "
            "store. Downloading the release from the project's page in a "
            "browser always works."
        )
    return WorkerError(f"Could not reach GitHub: {error}")


def fetch_latest_release() -> dict[str, Any]:
    request = urllib.request.Request(
        RELEASES_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"process-studio/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(
            request, timeout=TIMEOUT_SECONDS, context=verifier()
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise WorkerError("No release has been published yet.") from error
        raise WorkerError(f"GitHub answered {error.code} to the release check.") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise _unreachable(error) from error


def check_update() -> dict[str, Any]:
    return describe_release(fetch_latest_release())


def open_url(url: Any) -> dict[str, Any]:
    """Open one of the repository's own pages in the user's browser."""
    if not isinstance(url, str) or not url.startswith(ALLOWED_URL_PREFIXES):
        raise InvalidRequest("open_url only opens pages of the application's own repository.")
    opened = webbrowser.open(url)
    if not opened:
        raise WorkerError("No browser could be opened; copy the address instead.")
    return {"opened": True}


# -- installing an update ------------------------------------------------------
#
# The application cannot overwrite itself while it runs, and on Windows the
# worker's own executable is locked too. So the worker downloads the release
# zip, unpacks it beside the application, writes a small updater script and
# starts it detached; the shell then quits, the script waits for both
# processes to be gone, copies the unpacked files over the application and
# starts it again. In a source checkout there is nothing to install over.

import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Callable


def application_root() -> Path | None:
    """The directory (Windows) or .app bundle (macOS) a packaged worker belongs to."""
    # A Windows build running its worker as "python.exe -m process_studio.worker"
    # (an embeddable Python beside the app, not a PyInstaller executable) is
    # never frozen, so the shell hands the app root down directly instead.
    override = os.environ.get("PROCESS_STUDIO_APP_ROOT")
    if override:
        return Path(override)
    if not getattr(sys, "frozen", False):
        return None
    executable = Path(sys.executable).resolve()
    if sys.platform.startswith("win"):
        # <app>/resources/worker/process-studio-worker.exe
        return executable.parents[2]
    if sys.platform == "darwin":
        # <name>.app/Contents/Resources/worker/process-studio-worker
        for parent in executable.parents:
            if parent.suffix == ".app":
                return parent
    return None


def download(url: str, destination: Path, report: Callable[[int, int], None] | None = None) -> None:
    """Fetch ``url`` to ``destination``, reporting (received, total) bytes as it goes."""
    if not url.startswith(ALLOWED_URL_PREFIXES):
        raise InvalidRequest("Only the application's own release files are downloaded.")
    request = urllib.request.Request(url, headers={"User-Agent": f"process-studio/{__version__}"})
    try:
        with urllib.request.urlopen(
            request, timeout=TIMEOUT_SECONDS, context=verifier()
        ) as response, destination.open("wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            received = 0
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                received += len(chunk)
                if report:
                    report(received, total)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise WorkerError(f"The download failed: {_unreachable(error)}") from error


def stage_update(archive: Path, staging: Path) -> Path:
    """Unpack the release zip; returns the directory that replaces the application."""
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    with zipfile.ZipFile(archive) as bundle:
        root = staging.resolve()
        for member in bundle.namelist():
            # No member may escape the staging directory.
            target = (staging / member).resolve()
            if target != root and root not in target.parents:
                raise WorkerError(f"The archive holds an unsafe path: {member}")
        bundle.extractall(staging)
    if sys.platform == "darwin":
        apps = [p for p in staging.iterdir() if p.suffix == ".app"]
        if len(apps) != 1:
            raise WorkerError("The macOS archive does not hold exactly one application bundle.")
        # A zip carries no executable bits reliably; restore them where they matter.
        for path in apps[0].rglob("*"):
            if path.is_file() and ("MacOS" in path.parts or "worker" in path.parts):
                path.chmod(path.stat().st_mode | 0o111)
        return apps[0]
    if not any(staging.glob("*.exe")):
        raise WorkerError("The Windows archive holds no application executable.")
    return staging


def write_updater(app: Path, staged: Path, wait_for: list[int]) -> tuple[list[str], Path]:
    """The script that swaps the application once it has quit; returns its command and log.

    On Windows it is a batch file, which needs no policy to run: it polls
    the task list until both processes are gone, mirrors the unpacked
    files over the application with robocopy and starts it again. On macOS
    a shell script does the same with ditto and open.
    """
    log = app.parent / "process-studio-update.log"
    if sys.platform.startswith("win"):
        script = app.parent / "process-studio-update.cmd"
        exe = next(app.glob("*.exe"), app / "ProcessStudio.exe")
        # The updater is started detached and with no console, so `timeout`
        # is not available to it: without a console it fails at once with
        # "input redirection is not supported", which turned every wait
        # into a busy loop spawning tasklist as fast as it could. `ping`
        # needs no console. Each wait is bounded so a process that never
        # exits leaves a line in the log instead of hanging for ever.
        waits = "\n".join(
            "\n".join([
                f"for /L %%i in (1,1,120) do (",
                f'  tasklist /FI "PID eq {pid}" /NH 2>NUL | find "{pid}" >NUL || goto gone{index}',
                f"  ping -n 2 127.0.0.1 >NUL",
                f")",
                f'echo   process {pid} is still running after two minutes; copying anyway >> "{log}"',
                f":gone{index}",
            ])
            for index, pid in enumerate(wait_for)
        )
        script.write_text(
            "\n".join([
                "@echo off",
                f'echo. >> "{log}"',
                f'echo ==== %DATE% %TIME% >> "{log}"',
                f'echo   application: "{app}" >> "{log}"',
                f'echo   new version: "{staged}" >> "{log}"',
                waits,
                "ping -n 3 127.0.0.1 >NUL",
                # `resources` is mirrored rather than merged: it belongs
                # wholly to the application and its layout changes between
                # versions (0.9.0 replaced resources\worker's PyInstaller
                # executable with an embeddable Python under
                # resources\python). A plain /E copy leaves the old worker
                # in place, and the shell prefers it, so the update would
                # quietly keep running the previous version's worker.
                # Everything outside `resources` is still merged, so
                # anything the user keeps beside the application survives.
                f'if exist "{staged}\\resources" (',
                f'  robocopy "{staged}\\resources" "{app}\\resources"'
                f' /MIR /R:5 /W:2 /NFL /NDL /NJH /NJS >> "{log}"',
                # robocopy reports what it did in the exit code: under 8 is
                # success (files copied, or nothing needed copying), 8 and
                # above is a real failure. Without this check a failed copy
                # looked exactly like a successful one — the application was
                # left untouched and the unpacked new version deleted.
                "  if errorlevel 8 goto failed",
                ")",
                f'robocopy "{staged}" "{app}" /E /XD "{staged}\\resources"'
                f' /R:5 /W:2 /NFL /NDL /NJH /NJS >> "{log}"',
                "if errorlevel 8 goto failed",
                f'echo   updated >> "{log}"',
                f'rmdir /S /Q "{staged.parent}"',
                f'start "" "{exe}"',
                'del "%~f0"',
                "exit /b 0",
                ":failed",
                f'echo   FAILED: could not write into "{app}". >> "{log}"',
                f'echo   The new version is unpacked in "{staged}" >> "{log}"',
                f'echo   and can be copied over the application by hand. >> "{log}"',
                f'start "" "{staged}"',
                "exit /b 1",
            ]),
            encoding="utf-8",
        )
        return ["cmd.exe", "/C", str(script)], log
    script = app.parent / ".process-studio-update.sh"
    waits = "\n".join(f"while kill -0 {pid} 2>/dev/null; do sleep 0.5; done" for pid in wait_for)
    # The bundle is moved aside rather than deleted, and only thrown away
    # once the new one is in place: if the copy fails — the application
    # sits somewhere this user cannot write — putting the old one back is
    # the difference between an update that did not happen and a machine
    # with no application at all.
    previous = f"{app}.previous"
    script.write_text(
        "\n".join([
            "#!/bin/bash",
            f"exec >> '{log}' 2>&1",
            "echo",
            "echo \"==== $(date)\"",
            f"echo \"  application: {app}\"",
            f"echo \"  new version: {staged}\"",
            waits,
            "sleep 1",
            f"rm -rf '{previous}'",
            f"mv '{app}' '{previous}' || true",
            f"if ditto '{staged}' '{app}'; then",
            f"  rm -rf '{previous}' '{staged.parent}'",
            "  echo '  updated'",
            "else",
            f"  echo '  FAILED: could not write {app}'",
            f"  echo '  the new version is unpacked in {staged}'",
            f"  rm -rf '{app}'",
            f"  mv '{previous}' '{app}'",
            f"  open -R '{staged}'",
            "fi",
            f"open '{app}'",
            f"rm -f '{script}'",
        ]),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return ["/bin/bash", str(script)], log


#: The staging directories an update unpacks into, beside the application.
STAGING_PREFIX = "process-studio-update-"


def clear_stale_staging(beside: Path) -> None:
    """Remove staging directories an earlier update left behind.

    A staging directory is deleted the moment its copy succeeds, so any
    that survives is the wreckage of an update that failed. They are the
    "a new folder appeared next to the application" the user sees, and
    each holds a full unpacked build, so they are worth several hundred
    megabytes apiece. One that cannot be removed (a file still open, a
    permission) is left alone: the update itself is what matters.
    """
    for path in beside.glob(f"{STAGING_PREFIX}*"):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)


def install_update(url: Any, report: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Download the release for this build, unpack it and hand over to the updater.

    Returns once the updater is running; the caller then quits the
    application, and the updater replaces it and starts it again.
    """
    if not isinstance(url, str) or not url:
        raise InvalidRequest("install_update requires the download url of the release asset.")
    app = application_root()
    if app is None:
        raise WorkerError(
            "This is a source checkout, not a packaged application; update it with git and pip."
        )
    say = report or (lambda _message: None)
    clear_stale_staging(app.parent)
    work = Path(tempfile.mkdtemp(prefix="process-studio-update-", dir=str(app.parent)))
    archive = work / "release.zip"

    def progress(received: int, total: int) -> None:
        if total:
            say(f"Downloading {received / 1_048_576:.0f} of {total / 1_048_576:.0f} MB")
        else:
            say(f"Downloading {received / 1_048_576:.0f} MB")

    say("Downloading the update")
    download(url, archive, progress)
    say("Unpacking")
    staged = stage_update(archive, work / "unpacked")
    archive.unlink(missing_ok=True)
    command, log = write_updater(app, staged, [os.getpid(), os.getppid()])
    say("Handing over to the updater; the application restarts by itself")
    quiet: dict[str, Any] = {
        "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
    }
    if sys.platform.startswith("win"):
        quiet["creationflags"] = 0x00000008 | 0x00000200  # detached, its own process group
    else:
        quiet["start_new_session"] = True
    subprocess.Popen(command, **quiet)
    return {"staged": str(staged), "log": str(log), "restart": True}
