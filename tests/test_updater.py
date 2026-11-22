"""Updating: reading GitHub's answer, verifying the download, swapping the .exe. No network: GitHub is faked."""
import hashlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gradetracker import __version__, updater

ROOT = Path(__file__).resolve().parent.parent
PAYLOAD = b"MZ fake exe " * 1000


def release_json(tag="v9.9.9", payload=PAYLOAD, digest=True):
    asset = {"name": "GradeTracker.exe", "size": len(payload),
             "browser_download_url": "https://github.com/x/y/releases/download/v9.9.9/GradeTracker.exe"}
    if digest:
        asset["digest"] = "sha256:" + hashlib.sha256(payload).hexdigest()
    return {"tag_name": tag, "body": "What's new", "html_url": "https://github.com/x/y/releases/tag/" + tag,
            "assets": [{"name": "notes.txt"}, asset]}


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}


def fake_urlopen(payload=PAYLOAD, release=None):
    def urlopen(req, timeout=None):
        if "api.github.com" in req.full_url:
            return FakeResponse(json.dumps(release or release_json(payload=payload)).encode())
        return FakeResponse(payload)
    return urlopen


class Versions(unittest.TestCase):
    def test_parse_and_compare(self):
        self.assertEqual(updater.parse_version("v0.1.10"), (0, 1, 10))
        self.assertEqual(updater.parse_version("1.2"), (1, 2, 0))
        self.assertEqual(updater.parse_version("nonsense"), (0, 0, 0))
        self.assertTrue(updater.Release(release_json("v0.1.10")).is_newer("0.1.9"))
        self.assertFalse(updater.Release(release_json("v" + __version__)).is_newer())

    def test_exe_resource_matches_the_package_version(self):
        """A release whose .exe reports an older version would offer the same update forever."""
        text = (ROOT / "packaging" / "version_info.txt").read_text(encoding="utf-8")
        self.assertIn(f"'ProductVersion', '{__version__}'", text)
        nums = tuple(int(x) for x in __version__.split("."))
        self.assertIn(f"prodvers=({nums[0]}, {nums[1]}, {nums[2]}, 0)", text)


class GitHub(unittest.TestCase):
    def test_latest_reads_the_asset(self):
        with mock.patch("urllib.request.urlopen", fake_urlopen()):
            r = updater.latest()
        self.assertEqual((r.tag, r.version, r.size), ("v9.9.9", "9.9.9", len(PAYLOAD)))
        self.assertEqual(r.sha256, hashlib.sha256(PAYLOAD).hexdigest())
        self.assertTrue(r.url.endswith("GradeTracker.exe"))

    def test_no_connection_is_a_friendly_error(self):
        def down(req, timeout=None):
            raise updater.urllib.error.URLError("no internet")
        with mock.patch("urllib.request.urlopen", down):
            with self.assertRaises(updater.UpdateError) as ctx:
                updater.latest()
        self.assertIn("Couldn't reach GitHub", str(ctx.exception))


class Swap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.exe = Path(self.tmp.name) / "GradeTracker.exe"
        self.exe.write_bytes(b"old version")
        self.patches = [mock.patch.object(sys, "executable", str(self.exe)),
                        mock.patch.object(sys, "frozen", True, create=True)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_download_verify_install_cleanup(self):
        release = updater.Release(release_json())
        seen = []
        with mock.patch("urllib.request.urlopen", fake_urlopen()):
            new = updater.download(release, progress=lambda d, t: seen.append((d, t)))
        self.assertEqual(new.read_bytes(), PAYLOAD)
        self.assertEqual(seen[-1], (len(PAYLOAD), len(PAYLOAD)))
        self.assertEqual(updater.install(new), self.exe.resolve())
        self.assertEqual(self.exe.read_bytes(), PAYLOAD)
        old = self.exe.with_name("GradeTracker.exe.old")
        self.assertEqual(old.read_bytes(), b"old version")
        if sys.platform == "win32":
            self.assertTrue(updater.just_updated())      # the next start says "Updated to ..."
            updater.cleanup()
            self.assertFalse(old.exists())
            self.assertFalse(updater.just_updated())     # ...but only that once

    def test_a_corrupt_download_is_rejected(self):
        release = updater.Release(release_json())
        with mock.patch("urllib.request.urlopen", fake_urlopen(payload=PAYLOAD[:-1] + b"X")):
            with self.assertRaises(updater.UpdateError):
                updater.download(release)
        self.assertEqual(self.exe.read_bytes(), b"old version")
        self.assertEqual(sorted(p.name for p in self.exe.parent.iterdir()), ["GradeTracker.exe"])

    def test_restart_starts_a_fresh_instance(self):
        with mock.patch("subprocess.Popen") as popen:
            updater.restart(self.exe, ["--data", "D:/x"])
        args, kwargs = popen.call_args
        self.assertEqual(args[0], [str(self.exe), "--data", "D:/x"])
        self.assertEqual(kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")


class Workflow(unittest.TestCase):
    def test_release_build_checks_the_tag(self):
        text = (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
        self.assertTrue(re.search(r"__version__", text), "the release job must compare the tag with __version__")


if __name__ == "__main__":
    unittest.main()
