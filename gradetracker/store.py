"""Where your data lives: one folder with gradebook.json (this term's courses and marks) and
transcript.json (past terms). Nothing is sent anywhere.

The folder is %APPDATA%\\Grade Tracker on Windows (~/.grade-tracker elsewhere), or whatever
GRADE_TRACKER_HOME (or --data DIR) points at.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from .engine import GradeBook, GradeError, Transcript

LIBRARY_FORMAT = "grade-tracker-library"
COMPACT_LISTS = {"items", "log", "scale", "targets", "checks", "courses_in_term"}


def default_home() -> Path:
    env = os.environ.get("GRADE_TRACKER_HOME")
    if env:
        return Path(env).expanduser()
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "Grade Tracker"
    return Path.home() / ".grade-tracker"


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8-sig").replace("\r\n", "\n")


def write_text(path: Path, text: str):
    """Write atomically, keeping the previous version as NAME.bak."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    if path.exists():
        shutil.copyfile(path, path.with_name(path.name + ".bak"))
    os.replace(tmp, path)


def dump_json(obj, indent: int = 0, key: str = None) -> str:
    """Pretty JSON with one item / log row per line, so the files stay easy to read and diff."""
    pad = "  " * indent
    if isinstance(obj, dict) and obj:
        inner = ",\n".join(f"{pad}  {json.dumps(k, ensure_ascii=False)}: {dump_json(v, indent + 1, k)}"
                           for k, v in obj.items())
        return "{\n" + inner + "\n" + pad + "}"
    if isinstance(obj, list) and obj:
        if all(not isinstance(v, (dict, list)) for v in obj):
            return json.dumps(obj, ensure_ascii=False)
        if key in COMPACT_LISTS:
            inner = ",\n".join(f"{pad}  {json.dumps(v, ensure_ascii=False, separators=(', ', ': '))}" for v in obj)
        else:
            inner = ",\n".join(f"{pad}  {dump_json(v, indent + 1)}" for v in obj)
        return "[\n" + inner + "\n" + pad + "]"
    return json.dumps(obj, ensure_ascii=False)


def empty_library() -> dict:
    return {"format": LIBRARY_FORMAT, "version": 1, "term": "", "scale": None, "targets": [],
            "courses": [], "log": []}


class Library:
    """The data folder the window and the command line read and write."""

    def __init__(self, home=None):
        self.home = Path(home).expanduser() if home else default_home()
        self.data_path = self.home / "gradebook.json"
        self.transcript_path = self.home / "transcript.json"

    # ---- grade book
    def load_data(self) -> dict:
        if not self.data_path.exists():
            return empty_library()
        try:
            data = json.loads(read_text(self.data_path))
        except json.JSONDecodeError as e:
            raise GradeError(f"{self.data_path} is not valid JSON (line {e.lineno}, column {e.colno}): {e.msg}."
                             f" The previous version is in {self.data_path.name}.bak.") from None
        for k, v in empty_library().items():
            data.setdefault(k, v)
        return data

    def load(self) -> GradeBook:
        return GradeBook(self.load_data())

    def save(self, book_or_data):
        data = book_or_data.data if isinstance(book_or_data, GradeBook) else book_or_data
        write_text(self.data_path, dump_json(data) + "\n")

    def mtime(self):
        try:
            return self.data_path.stat().st_mtime
        except OSError:
            return None

    # ---- transcript
    def load_transcript_data(self) -> dict:
        if not self.transcript_path.exists():
            return {}
        try:
            return json.loads(read_text(self.transcript_path))
        except json.JSONDecodeError as e:
            raise GradeError(f"{self.transcript_path} is not valid JSON (line {e.lineno}, column {e.colno}):"
                             f" {e.msg}.") from None

    def load_transcript(self, book: GradeBook = None) -> Transcript:
        """Past terms, scored with the library's letter scale. An empty transcript if none is imported."""
        return Transcript(self.load_transcript_data(), book or self.load())

    def save_transcript(self, data: dict):
        write_text(self.transcript_path, dump_json(data) + "\n")

    def transcript_mtime(self):
        try:
            return self.transcript_path.stat().st_mtime
        except OSError:
            return None
