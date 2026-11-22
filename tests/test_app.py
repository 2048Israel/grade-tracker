"""The data folder, the report, the command line and the window, end to end on a temporary folder."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from gradetracker import DATA, cli, engine as g
from gradetracker.report import build_html
from gradetracker.store import Library


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class Folder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "data"
        self.lib = Library(self.home)

    def tearDown(self):
        self.tmp.cleanup()


class Store(Folder):
    def test_empty_then_round_trip_with_backup(self):
        self.assertEqual(self.lib.load().courses, [])
        self.assertTrue(self.lib.load_transcript().empty)
        data = self.lib.load_data()
        data["term"] = "Fall 2026"
        self.lib.save(data)
        data["term"] = "Winter 2027"
        self.lib.save(data)
        self.assertEqual(self.lib.load_data()["term"], "Winter 2027")
        self.assertIn("Fall 2026", (self.home / "gradebook.json.bak").read_text(encoding="utf-8"))

    def test_broken_file_says_where(self):
        self.home.mkdir(parents=True)
        self.lib.data_path.write_text("{ nope", encoding="utf-8")
        with self.assertRaises(g.GradeError) as ctx:
            self.lib.load()
        self.assertIn("line 1", str(ctx.exception))


class CommandLine(Folder):
    def test_prompt_check_import_add_status_report(self):
        code, out, _ = run("prompt")
        self.assertEqual(code, 0)
        self.assertIn("Grade Tracker", out)
        course = str(DATA / "example-course.json")
        self.assertEqual(run("--data", str(self.home), "check", course)[0], 0)
        self.assertFalse(self.lib.data_path.exists())                  # check saves nothing
        self.assertEqual(run("--data", str(self.home), "import", course)[0], 0)
        self.assertEqual(run("--data", str(self.home), "import", str(DATA / "example-transcript.json"))[0], 0)
        code, out, _ = run("--data", str(self.home), "add", "DEMO 1010", "Quiz 4", "17/20")
        self.assertEqual(code, 0, out)
        self.assertIn("Quiz 4 17/20 (85%)", out)
        code, out, _ = run("--data", str(self.home), "status", "--json")
        self.assertEqual(json.loads(out)[0]["code"], "DEMO 1010")
        code, out, _ = run("--data", str(self.home), "gpa", "--json")
        self.assertEqual(json.loads(out)["standing"]["cumulative_gpa"], 3.66)
        report = Path(self.tmp.name) / "r.html"
        self.assertEqual(run("--data", str(self.home), "report", str(report))[0], 0)
        self.assertIn("DEMO 1010", report.read_text(encoding="utf-8"))
        shared = Path(self.tmp.name) / "share.json"
        self.assertEqual(run("--data", str(self.home), "export", "DEMO 1010", str(shared))[0], 0)
        self.assertNotIn('"mark"', shared.read_text(encoding="utf-8"))
        self.assertEqual(run("--data", str(self.home), "remove", "DEMO 1010")[0], 0)
        self.assertEqual(self.lib.load().courses, [])

    def test_errors_exit_2(self):
        bad = Path(self.tmp.name) / "bad.json"
        bad.write_text('{"code": "X 1", "components": [{"label": "A", "weight": 100, "type": "nope"}]}',
                       encoding="utf-8")
        code, _, err = run("--data", str(self.home), "import", str(bad))
        self.assertEqual(code, 2)
        self.assertIn("type 'nope'", err)
        self.assertEqual(run("--data", str(self.home), "add", "X 1", "A", "50")[0], 2)


class Report(unittest.TestCase):
    def test_escapes_and_covers_everything(self):
        book = g.GradeBook({"term": "Fall 2026", "courses": [{"code": "X 1", "key": "x1", "title": "<b>Bold</b>",
                                                              "components": [{"id": "f", "label": "Final", "weight": 100,
                                                                              "items": [{"name": "Final", "mark": 81}]}]}]})
        html = build_html(book, g.Transcript({}, book))
        self.assertIn("&lt;b&gt;Bold&lt;/b&gt;", html)
        self.assertNotIn("<b>Bold</b>", html)
        self.assertIn("Grade report — Fall 2026", html)
        self.assertNotIn("<script", html)


class Window(Folder):
    def test_every_view_and_dialog_opens(self):
        try:
            import tkinter as tk
            root = tk.Tk()
        except Exception as e:   # no display (e.g. a headless Linux box)
            self.skipTest(f"no Tk display: {e}")
        from gradetracker import gui
        try:
            app = gui.App(root, self.lib, calm=True)
            for view in ("marks", "gpa", "past"):
                app.show_view(view)
                root.update()
            dialog = gui.ImportDialog(app, "syllabus", text=(DATA / "example-course.json").read_text(encoding="utf-8"))
            root.update()
            self.assertIsNotNone(dialog.result)
            dialog.add()
            self.assertEqual([c.code for c in app.book.courses], ["DEMO 1010"])
            app.mark_var.set("88")
            app.save()
            root.update()
            self.assertEqual(len(app.book.data["log"]), 1)
            gui.SettingsDialog(app).destroy()
            for view in ("marks", "gpa", "past"):
                app.show_view(view)
                root.update()
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
