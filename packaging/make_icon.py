"""Draw the app icon with juice.app_icon and save it as packaging/icon.ico (PNG-in-ICO, 16-256 px).

    python packaging/make_icon.py
"""
import struct
import sys
import tempfile
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from gradetracker import juice as j  # noqa: E402
from gradetracker.gui import NEON  # noqa: E402

SIZES = (256, 64, 48, 32, 16)


def main() -> int:
    root = tk.Tk()
    root.withdraw()
    pngs = []
    with tempfile.TemporaryDirectory() as tmp:
        for size in SIZES:
            path = Path(tmp) / f"{size}.png"
            j.app_icon(size, NEON).write(str(path), format="png")
            pngs.append((size, path.read_bytes()))
    root.destroy()
    header = struct.pack("<HHH", 0, 1, len(pngs))
    offset = len(header) + 16 * len(pngs)
    entries, blobs = b"", b""
    for size, data in pngs:
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    out = ROOT / "packaging" / "icon.ico"
    out.write_bytes(header + entries + blobs)
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
