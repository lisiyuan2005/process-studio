"""Checking GitHub for a newer release of the application.

The desktop asks the worker rather than fetching itself: the webview's
content policy keeps page code off the network, while the worker is a
plain process with the user's own connectivity. Only the repository's
public release feed is read, and only its own pages are ever opened.
"""

from __future__ import annotations

import json
import re
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


def fetch_latest_release() -> dict[str, Any]:
    request = urllib.request.Request(
        RELEASES_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"process-studio/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise WorkerError("No release has been published yet.") from error
        raise WorkerError(f"GitHub answered {error.code} to the release check.") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise WorkerError(f"Could not reach GitHub: {error}") from error


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
