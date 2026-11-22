"""`python -m gradetracker` (or GradeTracker.exe) opens the window; add a command to run the command line:

    GradeTracker.exe --data D:\\grades status
"""
import sys

VALUE_FLAGS = ("--data", "--view")


def wants_cli(argv) -> bool:
    """True when the arguments hold a command (a positional word) or ask for help/version."""
    skip = False
    for a in argv:
        if skip:
            skip = False
        elif a in VALUE_FLAGS:
            skip = True
        elif a in ("-h", "--help", "--version"):
            return True
        elif not a.startswith("-"):
            return True
    return False


def attach_console() -> None:
    """A windowed .exe starts with no stdout. Use the handles it was given (a redirect to a file) or the
    console it was started from, so command output shows up."""
    if sys.stdout is not None or sys.platform != "win32":
        return
    try:
        import ctypes
        import msvcrt
        k = ctypes.windll.kernel32
        k.GetStdHandle.restype = ctypes.c_void_p
        opened = {}
        for num, name in ((-11, "stdout"), (-12, "stderr")):
            h = k.GetStdHandle(num)
            if h and h != ctypes.c_void_p(-1).value and k.GetFileType(ctypes.c_void_p(h)) != 0:
                opened[name] = open(msvcrt.open_osfhandle(h, 0), "w", encoding="utf-8", errors="replace")
        if len(opened) < 2 and k.AttachConsole(-1):
            for name in ("stdout", "stderr"):
                opened.setdefault(name, open("CONOUT$", "w", encoding="utf-8", errors="replace"))
        for name, stream in opened.items():
            setattr(sys, name, stream)
    except Exception:
        pass


def run(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if wants_cli(argv):
        attach_console()
        from .cli import main
        code = main(argv)
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
        return code
    from .gui import main
    return main(argv)


if __name__ == "__main__":
    sys.exit(run())
