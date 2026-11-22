"""Command line: everything the window does, for scripts and AI agents.

    python -m gradetracker prompt                    # the text to give your AI with a syllabus
    python -m gradetracker check reply.json          # is the AI's file usable? (nothing is saved)
    python -m gradetracker import reply.json         # add the course(s) or transcript it holds
    python -m gradetracker add "BIOL 1010" "Lab 3" 17/20
    python -m gradetracker status                    # every course: standing and what it needs
    python -m gradetracker gpa --what-if A- "BIOL 1010=B+"
    python -m gradetracker report grades.html        # the printable report
    GradeTracker.exe update                          # install the latest release
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, coursefile as cf, prompt, updater
from .engine import (GradeError, clear_mark, course_report, flag_item, fnum, gpa_json, gpa_report, graded,
                     item_hint, log_mark, past_course_json, past_course_report, term_json, term_report,
                     terms_report, what_if)
from .report import build_html
from .store import Library


def read_source(path: str) -> str:
    return sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8-sig")


def show_import(imp: cf.Import, notes, errors) -> None:
    if imp.kind == "transcript" and imp.transcript:
        print("\n".join(cf.describe_transcript(imp.transcript)))
    for c in imp.courses:
        print("\n".join(cf.describe_course(c)))
        print()
    for w in imp.warnings:
        print(f"warning: {w}")
    for n in notes:
        print(n)
    for err in errors:
        print(f"error: {err}", file=sys.stderr)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="gradetracker", description="Grade Tracker from the command line.")
    ap.add_argument("--data", help="data folder (default: %%APPDATA%%\\Grade Tracker, or GRADE_TRACKER_HOME)")
    ap.add_argument("--version", action="version", version=f"Grade Tracker {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("prompt", help="print the text to give your AI")
    pr.add_argument("kind", nargs="?", default="syllabus", choices=("syllabus", "transcript"))
    for name, hlp in (("check", "check an AI-made file without saving anything"),
                      ("import", "add the course(s) or transcript in an AI-made file")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("file", help="the file, or - to read from stdin")
        p.add_argument("--use-scale", action="store_true", help="make the file's letter scale the app's scale")
    rm = sub.add_parser("remove", help="remove a course and its marks")
    rm.add_argument("course")
    ex = sub.add_parser("export", help="write a course file without marks, to share")
    ex.add_argument("course")
    ex.add_argument("out")

    a = sub.add_parser("add", help="log a mark: add COURSE ITEM MARK")
    a.add_argument("course")
    a.add_argument("item")
    a.add_argument("mark", help="85 · 85%% · 17/20 · 1.5 (points) · missed · present")
    a.add_argument("--date", help="log date, e.g. 'Oct 19' (default: today)")
    a.add_argument("--note", help="appended to the log row")
    c = sub.add_parser("clear", help="remove a mark logged by mistake")
    c.add_argument("course")
    c.add_argument("item")
    f = sub.add_parser("flag", help="put a warning on an ungraded item; omit TEXT to remove it")
    f.add_argument("course")
    f.add_argument("item")
    f.add_argument("text", nargs="?")

    for name, hlp in (("status", "full standing per course"), ("need", "what each course needs per target")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("course", nargs="?")
        p.add_argument("--json", action="store_true")
    i = sub.add_parser("items", help="list a course's items")
    i.add_argument("course")
    gp = sub.add_parser("gpa", help="cumulative GPA, by subject, and this term's projection")
    gp.add_argument("--what-if", nargs="+", action="extend", metavar="LETTER|COURSE=LETTER")
    gp.add_argument("--json", action="store_true")
    ts = sub.add_parser("terms", help="every past term")
    ts.add_argument("--json", action="store_true")
    tm = sub.add_parser("term", help="one past term, or one past course")
    tm.add_argument("term")
    tm.add_argument("course", nargs="?")
    tm.add_argument("--json", action="store_true")
    rp = sub.add_parser("report", help="write the printable HTML report")
    rp.add_argument("out")
    sub.add_parser("where", help="print the data folder")
    up = sub.add_parser("update", help="install the latest version of the .exe from GitHub")
    up.add_argument("--check", action="store_true", help="only say whether a newer version exists")
    up.add_argument("--restart", action="store_true", help="open the new version when it's installed")

    args = ap.parse_args(argv)
    lib = Library(args.data)
    try:
        if args.cmd == "prompt":
            print(prompt(args.kind))
            return 0
        if args.cmd == "where":
            print(lib.home)
            return 0
        if args.cmd == "update":
            updater.cleanup()
            release = updater.latest()
            if not release.is_newer():
                print(f"Grade Tracker {__version__} is the latest version.")
                return 0
            print(f"Grade Tracker {release.version} is available (you have {__version__}).")
            if args.check:
                print(release.page)
                return 0
            if not updater.can_self_update():
                print(f"Running from source: update with `git pull`, or download it from {release.page}")
                return 0
            exe = updater.install(updater.download(release))
            print(f"Updated {exe.name} to {release.version}. Your data is untouched.")
            if args.restart:
                updater.restart(exe, ["--data", args.data] if args.data else [])
            return 0
        if args.cmd in ("check", "import"):
            imp = cf.read_import(read_source(args.file))
            new_lib, new_tr, notes, errors = cf.plan(imp, lib.load_data(), lib.load_transcript_data(),
                                                     adopt_scale=True if args.use_scale else None)
            if args.cmd == "check":
                notes = [n.replace("added.", "would be added.").replace("replaced", "would replace") for n in notes]
            show_import(imp, notes, errors)
            if errors:
                return 2
            if args.cmd == "import":
                lib.save(new_lib)
                if imp.kind == "transcript":
                    lib.save_transcript(new_tr)
                print(f"Saved to {lib.home}")
            return 0
        book = lib.load()
        if args.cmd == "remove":
            course = book.course(args.course)
            lib.save(cf.remove_course(book.data, course.code))
            print(f"Removed {course.code}.")
            return 0
        if args.cmd == "export":
            course = book.course(args.course)
            Path(args.out).write_text(json.dumps(cf.blank_course(course.raw), ensure_ascii=False, indent=2) + "\n",
                                      encoding="utf-8")
            print(f"Wrote {args.out}")
            return 0
        if args.cmd in ("gpa", "terms", "term"):
            tr = lib.load_transcript(book)
            if args.cmd == "gpa":
                picks = what_if(book, args.what_if)
                out = gpa_json(tr, picks) if args.json else gpa_report(tr, picks)
            elif args.cmd == "terms":
                out = [term_json(t, courses=False) for t in tr.terms] if args.json else terms_report(tr)
            else:
                term, course = tr.resolve(args.term, args.course)
                if args.json:
                    out = past_course_json(course) if course else term_json(term)
                else:
                    out = past_course_report(course) if course else term_report(term)
            print(json.dumps(out, ensure_ascii=False, indent=2) if args.json else out)
            return 0
        if args.cmd == "report":
            Path(args.out).write_text(build_html(book, lib.load_transcript(book)), encoding="utf-8")
            print(f"Wrote {args.out}")
            return 0
        if args.cmd in ("status", "need"):
            courses = [book.course(args.course)] if args.course else book.courses
            if not courses:
                print("No courses yet. Run `prompt`, give the text and your syllabus to your AI, then `import` its reply.")
                return 0
            if args.json:
                out = [c.summary() for c in courses]
                print(json.dumps(out if args.course is None else out[0], ensure_ascii=False, indent=2))
            else:
                print("\n\n".join(course_report(c, brief=args.cmd == "need") for c in courses))
            return 0
        if args.cmd == "items":
            course = book.course(args.course)
            for comp in course.components:
                if comp.type == "better":
                    continue
                print(f"{comp.short} ({fnum(comp.weight)}%)")
                for it in comp.items:
                    mark = comp.item_mark(it) if graded(it) else (f"flag: {it['flag']}" if it.get("flag") else "—")
                    print(f"    {it['name']:<18} {mark}{' (not counted)' if it.get('exclude') else ''}")
                if comp.raw.get("new_item"):
                    print(f"    (new days: '{comp.raw['new_item'].replace('{n}', 'N')}')")
                print(f"    mark format: {item_hint(comp, comp.items[0] if comp.items else {})}")
            return 0
        if args.cmd == "add":
            change = log_mark(book, args.course, args.item, args.mark, args.date, args.note)
        elif args.cmd == "clear":
            change = clear_mark(book, args.course, args.item)
        else:
            change = flag_item(book, args.course, args.item, args.text)
        lib.save(book)
        print(change.entry or change.message)
        print()
        print(course_report(change.course))
        return 0
    except (GradeError, updater.UpdateError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
