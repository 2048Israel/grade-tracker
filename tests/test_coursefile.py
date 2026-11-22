"""Reading what an AI writes: the forgiving parser, the strict checker, and adding it to the library."""
import json
import re
import unittest

from gradetracker import DATA, coursefile as cf, engine as g, prompt


def a_course(**over):
    c = {"format": "grade-tracker-course", "version": 1, "code": "BIOL 1010", "title": "Biology", "term": "Fall 2026",
         "components": [
             {"id": "labs", "label": "Labs", "weight": 40, "count": 4, "item_name": "Lab"},
             {"id": "final", "label": "Final exam", "weight": 60, "role": "exam", "items": [{"name": "Final"}]}]}
    c.update(over)
    return c


class Extract(unittest.TestCase):
    def test_fenced_reply_with_chatter(self):
        text = "Sure! Here it is:\n```json\n" + json.dumps(a_course()) + "\n```\nAnything else?"
        self.assertEqual(cf.extract_json(text)["code"], "BIOL 1010")

    def test_unfenced_with_chatter(self):
        text = "Here you go: " + json.dumps(a_course()) + " Hope that helps."
        self.assertEqual(cf.extract_json(text)["code"], "BIOL 1010")

    def test_trailing_commas_smart_quotes_and_comments(self):
        text = '{\n  “code”: "X 1",\n  // a comment\n  "components": [1, 2,],\n}'
        self.assertEqual(cf.extract_json(text), {"code": "X 1", "components": [1, 2]})

    def test_bad_json_names_the_place(self):
        with self.assertRaises(g.GradeError) as ctx:
            cf.extract_json('{"code": "X 1", "components": [')
        self.assertIn("line 1", str(ctx.exception))
        with self.assertRaises(g.GradeError):
            cf.extract_json("   ")


class CheckCourse(unittest.TestCase):
    def check(self, **over):
        return cf.check_course(a_course(**over))

    def test_count_makes_items_and_key(self):
        c, errors, warnings = self.check()
        self.assertEqual((errors, warnings), ([], []))
        self.assertEqual([it["name"] for it in c["components"][0]["items"]], ["Lab 1", "Lab 2", "Lab 3", "Lab 4"])
        self.assertEqual(c["key"], "biol1010")
        self.assertNotIn("format", c)

    def test_conveniences(self):
        c, errors, _ = self.check(components=[
            {"label": "Quizzes", "weight": "20%", "count": 5, "drop_lowest": 1},
            {"label": "Final", "weight": 80, "must_pass": 50}])
        self.assertEqual(errors, [])
        quiz, final = c["components"]
        self.assertEqual((quiz["id"], quiz["weight"], quiz["best"]), ("quizzes", 20, 4))
        self.assertEqual(quiz["items"][0]["name"], "Quiz 1")
        self.assertEqual(final["items"], [{"name": "Final"}])
        self.assertEqual(c["gates"][0]["checks"], [{"type": "min", "component": "final", "min": 50}])

    def test_errors_name_the_problem(self):
        _, errors, _ = self.check(components=[{"label": "Labs", "weight": 50, "type": "lab"},
                                              {"label": "Pool", "weight": 50, "type": "capped", "items": ["A"]}])
        self.assertTrue(any("type 'lab'" in e for e in errors))
        self.assertTrue(any("cap" in e for e in errors))
        _, errors, _ = self.check(gates=[{"checks": [{"type": "min", "component": "exam", "min": 50}]}])
        self.assertTrue(any("'exam'" in e and "isn't a component id" in e for e in errors))
        _, errors, _ = self.check(components=[{"label": "Labs", "weight": 100, "type": "points", "items": ["Lab 1"]}])
        self.assertTrue(any("need \"max\"" in e for e in errors))
        _, errors, _ = self.check(components=[{"label": "Mid", "weight": 50}, {"label": "Better", "weight": 50,
                                                                               "type": "higher_of", "of": ["mid", "x"]}])
        self.assertTrue(any("\"of\" names x" in e for e in errors))
        _, errors, _ = cf.check_course({"components": []})
        self.assertTrue(errors)

    def test_weights_not_100_is_a_warning(self):
        c, errors, warnings = self.check(components=[{"label": "Midterm", "weight": 45},
                                                     {"label": "Final", "weight": 60}])
        self.assertEqual(errors, [])
        self.assertIn("105%", warnings[0])

    def test_duplicate_item_names_are_renamed(self):
        c, errors, warnings = self.check(components=[{"label": "Labs", "weight": 50, "items": ["Report"]},
                                                     {"label": "Project", "weight": 50, "items": ["Report"]}])
        self.assertEqual(errors, [])
        self.assertEqual(c["components"][1]["items"][0]["name"], "Project Report")
        self.assertTrue(warnings)

    def test_scale_rows(self):
        errors = []
        self.assertEqual(cf.check_scale([[80, "B"], [90, "A", 4], [0, "F", 0]], "x", errors),
                         [[90, "A", 4], [80, "B"], [0, "F", 0]])
        self.assertEqual(errors, [])
        cf.check_scale([[90, "A"], [80, "B"]], "x", errors)
        self.assertTrue(any("start at 0" in e for e in errors))


class Import(unittest.TestCase):
    def test_several_courses_share_scale_and_term(self):
        scale = [[90, "A", 4.0], [80, "B", 3.0], [0, "F", 0]]
        text = json.dumps({"format": "grade-tracker-courses", "term": "Fall 2026", "scale": scale,
                           "courses": [a_course(term=None), a_course(code="CHEM 1010", term=None)]})
        imp = cf.read_import(text)
        self.assertTrue(imp.ok, imp.errors)
        self.assertEqual([c["code"] for c in imp.courses], ["BIOL 1010", "CHEM 1010"])
        self.assertEqual(imp.term, "Fall 2026")
        lib, _, notes, errors = cf.plan(imp, {"courses": []}, {})
        self.assertEqual(errors, [])
        self.assertEqual(lib["scale"], scale)        # an empty library takes the file's scale
        self.assertEqual(lib["term"], "Fall 2026")
        self.assertEqual(len(notes), 2)

    def test_reimport_keeps_marks(self):
        lib, _, _, _ = cf.plan(cf.read_import(json.dumps(a_course())), {"courses": [], "log": []}, {})
        book = g.GradeBook(lib)
        g.log_mark(book, "BIOL 1010", "Lab 2", "90")
        g.log_mark(book, "BIOL 1010", "Final", "70")
        fixed = a_course()
        fixed["components"][0]["weight"] = 30
        fixed["components"].append({"id": "quiz", "label": "Quiz", "weight": 10})
        lib2, _, notes, errors = cf.plan(cf.read_import(json.dumps(fixed)), book.data, {})
        self.assertEqual(errors, [])
        self.assertIn("keeping its 2 marks", notes[0])
        c = g.GradeBook(lib2).courses[0]
        self.assertEqual(c.find_item("Lab 2")[1]["mark"], 90)
        self.assertEqual(c.comp("labs").weight, 30)

    def test_transcript_with_unknown_letter_and_mismatch(self):
        t = {"format": "grade-tracker-transcript", "terms": [
            {"term": "Fall 2025", "courses": [{"code": "BIOL 1010", "letter": "a-"}, {"code": "X 1", "letter": "P"}],
             "term_gpa": 3.9}]}
        imp = cf.read_import(json.dumps(t))
        self.assertEqual(imp.kind, "transcript")
        self.assertEqual(imp.transcript["terms"][0]["courses"][0]["letter"], "A-")
        _, _, notes, errors = cf.plan(imp, {"courses": []}, {})
        self.assertEqual(errors, [])
        self.assertTrue(any("term GPA" in n for n in notes))
        t["terms"][0]["courses"][0]["letter"] = "Z"
        _, _, _, errors = cf.plan(cf.read_import(json.dumps(t)), {"courses": []}, {})
        self.assertTrue(any("'Z'" in e for e in errors))

    def test_blank_and_remove(self):
        lib, _, _, _ = cf.plan(cf.read_import(json.dumps(a_course())), {"courses": [], "log": []}, {})
        book = g.GradeBook(lib)
        g.log_mark(book, "BIOL 1010", "Lab 1", "88")
        blank = cf.blank_course(book.courses[0].raw)
        self.assertFalse(any(g.graded(it) for c in blank["components"] for it in c["items"]))
        self.assertEqual(blank["format"], cf.FORMAT_COURSE)
        self.assertTrue(cf.read_import(json.dumps(blank)).ok)
        gone = cf.remove_course(book.data, "BIOL 1010")
        self.assertEqual((gone["courses"], gone["log"]), ([], []))


class PromptsAndExamples(unittest.TestCase):
    """The prompts are the format's documentation: every example in them must import cleanly."""

    def blocks(self, kind):
        return [b for b in re.findall(r"```json\n(.*?)```", prompt(kind), re.S) if b.strip().startswith("{")]

    def test_syllabus_prompt_examples_import(self):
        complete = [b for b in self.blocks("syllabus") if "..." not in b and "{course}" not in b]
        self.assertTrue(complete)
        for block in complete:
            imp = cf.read_import(block)
            self.assertTrue(imp.ok, imp.errors)
            self.assertEqual(imp.warnings, [])

    def test_transcript_prompt_example_imports(self):
        imp = cf.read_import(self.blocks("transcript")[0])
        self.assertTrue(imp.ok, imp.errors)
        _, _, notes, errors = cf.plan(imp, {"courses": []}, {})
        self.assertEqual(errors, [])
        self.assertFalse([n for n in notes if n.startswith("Check")], notes)   # its GPAs add up

    def test_prompt_documents_every_type_and_check(self):
        text = prompt("syllabus")
        for name in ("average", "points", "capped", "attendance", "higher_of"):
            self.assertIn(f"`{name}`", text)
        for name in ("min", "avg_min", "submitted"):
            self.assertIn(f'"type": "{name}"', text)

    def test_bundled_examples_import(self):
        for name in ("example-course.json", "example-transcript.json"):
            imp = cf.read_import((DATA / name).read_text(encoding="utf-8"))
            self.assertTrue(imp.ok, (name, imp.errors))
            _, _, notes, errors = cf.plan(imp, {"courses": []}, {})
            self.assertEqual(errors, [], name)
            self.assertFalse([n for n in notes if n.startswith("Check")], (name, notes))


if __name__ == "__main__":
    unittest.main()
