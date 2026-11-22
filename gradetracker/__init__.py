"""Grade Tracker: turn a syllabus into a grade tracker that says what you need on the rest."""
from pathlib import Path

__version__ = "0.1.2"
DATA = Path(__file__).resolve().parent / "data"


def prompt(kind: str = "syllabus") -> str:
    """The text to give your AI: kind is "syllabus" or "transcript"."""
    name = {"syllabus": "syllabus-prompt.md", "course": "syllabus-prompt.md", "transcript": "transcript-prompt.md"}[kind]
    return (DATA / name).read_text(encoding="utf-8")
