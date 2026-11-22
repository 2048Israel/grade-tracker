"""Updating the app from its GitHub releases.

The only network traffic the app makes: one request to GitHub's public API for the latest release
(no data about you or your grades is sent), and, when you click Update, downloading the new .exe.

Updating the packaged .exe in place works because Windows lets a running program's file be renamed
(just not overwritten): the new .exe is downloaded next to it, checked against the size and
SHA-256 GitHub publishes, the running one is renamed to NAME.exe.old, the new one takes its name,
and the app restarts. The .old file is deleted the next time the app starts. Your data folder is
never touched. Running from source, the app can only point you at the release page.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__

REPO = "2048Israel/grade-tracker"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases/latest"
ASSET = "GradeTracker.exe"
TIMEOUT = 15


class UpdateError(Exception):
    """Something the user should hear about: no connection, a bad download, a folder we can't write."""


def parse_version(text) -> tuple:
    """'v0.1.10' -> (0, 1, 10). Anything unparseable sorts lowest."""
    m = re.match(r"\s*v?(\d+)\.(\d+)(?:\.(\d+))?", str(text or ""))
    return tuple(int(x or 0) for x in m.groups()) if m else (0, 0, 0)


class Release:
    def __init__(self, data: dict):
        self.tag = data.get("tag_name", "")
        self.version = self.tag.lstrip("vV")
        self.notes = (data.get("body") or "").strip()
        self.page = data.get("html_url") or RELEASES_URL
        asset = next((a for a in data.get("assets", []) if a.get("name") == ASSET), None)
        self.url = asset.get("browser_download_url") if asset else None
        self.size = int(asset.get("size") or 0) if asset else 0
        digest = (asset or {}).get("digest") or ""
        self.sha256 = digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else None

    def is_newer(self, than: str = __version__) -> bool:
        return parse_version(self.tag) > parse_version(than)


def _request(url: str, accept: str):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": f"GradeTracker/{__version__}"})
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def latest() -> Release:
    """The newest published release. Raises UpdateError when GitHub can't be reached."""
    try:
        with _request(API_URL, "application/vnd.github+json") as r:
            return Release(json.load(r))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise UpdateError(f"Couldn't reach GitHub to check for updates ({getattr(e, 'reason', e)}).") from None


def can_self_update() -> bool:
    """True for the packaged Windows .exe; running from source, updates come through git or the release page."""
    return bool(getattr(sys, "frozen", False)) and sys.platform == "win32"


def app_path() -> Path:
    return Path(sys.executable).resolve()


def download(release: Release, progress=None) -> Path:
    """Download the release's .exe next to the running one and verify it. progress(done, total) is called as it goes."""
    if not release.url:
        raise UpdateError(f"Release {release.tag} has no {ASSET} to download.")
    target = app_path().with_name(app_path().name + ".download")
    digest, done = hashlib.sha256(), 0
    try:
        with _request(release.url, "application/octet-stream") as r, open(target, "wb") as out:
            total = int(r.headers.get("Content-Length") or release.size or 0)
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    except PermissionError:
        raise UpdateError(f"Can't write in {target.parent}. Download the new version from {release.page}"
                          " instead.") from None
    except (urllib.error.URLError, OSError) as e:
        target.unlink(missing_ok=True)
        raise UpdateError(f"The download failed ({getattr(e, 'reason', e)}). Try again in a moment.") from None
    if (release.size and done != release.size) or (release.sha256 and digest.hexdigest() != release.sha256):
        target.unlink(missing_ok=True)
        raise UpdateError("The download didn't match what GitHub published, so it wasn't installed. Try again.")
    return target


def install(new_file: Path) -> Path:
    """Swap the downloaded .exe in for the running one. Returns the path to start."""
    exe = app_path()
    old = exe.with_name(exe.name + ".old")
    if old.exists():
        try:
            old.unlink()
        except OSError:
            old = exe.with_name(f"{exe.name}.{int(time.time())}.old")
    try:
        os.rename(exe, old)
    except OSError as e:
        raise UpdateError(f"Couldn't replace {exe.name} ({e.strerror}). Close other copies of the app and try"
                          " again.") from None
    try:
        os.rename(new_file, exe)
    except OSError as e:
        os.rename(old, exe)   # put the running version back
        raise UpdateError(f"Couldn't put the new version in place ({e.strerror}).") from None
    return exe


def restart(exe: Path, args) -> None:
    """Start the given .exe as a fresh, independent app (the caller then quits)."""
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")   # a new instance, not a child of this one
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(exe), *args], env=env, close_fds=True, creationflags=flags, cwd=str(exe.parent))


def just_updated(max_age: float = 600) -> bool:
    """True when this .exe replaced an older one moments ago: the replaced copy is still beside it, and
    this file was written (downloaded) in the last few minutes. Checked before cleanup() deletes it."""
    if not can_self_update():
        return False
    exe = app_path()
    try:
        fresh = time.time() - exe.stat().st_mtime < max_age
    except OSError:
        return False
    return fresh and any(exe.parent.glob(exe.name + ".*old"))


def cleanup() -> None:
    """Delete what a previous update left behind (the replaced .exe, a half-finished download)."""
    if not can_self_update():
        return
    exe = app_path()
    for leftover in list(exe.parent.glob(exe.name + ".*old")) + [exe.with_name(exe.name + ".download")]:
        try:
            leftover.unlink(missing_ok=True)
        except OSError:
            pass   # still closing: next time
