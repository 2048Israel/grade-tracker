"""Entry point for the packaged app (and a double-click launcher when running from source).

With no arguments it opens the window; with a command it runs the command line,
e.g. `GradeTracker.exe status` (output appears only when run from a console).
"""
import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gradetracker.__main__ import run  # noqa: E402

sys.exit(run())
