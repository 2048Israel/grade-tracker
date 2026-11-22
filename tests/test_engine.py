"""The grading math, on small made-up courses whose answers can be checked by hand."""
import unittest

from gradetracker import engine as g


def course(components, **extra):
    raw = {"code": "TEST 1000", "key": "test1000", "title": "Test", "components": components, **extra}
    return g.GradeBook({"courses": [raw]}).courses[0]


def exam(cid, weight, mark=None, **kw):
    item = {"name": cid.title()}
    if mark is not None:
        item["mark"] = mark
    return {"id": cid, "label": cid.title(), "weight": weight, "role": "exam", "items": [item], **kw}


class Need(unittest.TestCase):
    def test_coursework_at_pace_and_exam_solved(self):
        c = course([{"id": "hw", "label": "Homework", "weight": 40,
                     "items": [{"name": "HW 1", "mark": 80}, {"name": "HW 2", "mark": 90}, {"name": "HW 3"}]},
                    exam("final", 60)])
        self.assertAlmostEqual(c.comp("hw").value(None), 85)        # open homework projected at 85
        self.assertAlmostEqual(c.banked(), 34)
        kind, v = c.need(80)
        self.assertEqual(kind, "need")
        self.assertAlmostEqual(v, (80 - 34) / 0.6, places=4)        # 76.67 on the final

    def test_locked_out_and_done(self):
        c = course([exam("mid", 50, 100), exam("final", 50)])
        self.assertEqual(c.need(50)[0], "locked")
        c = course([exam("mid", 50, 20), exam("final", 50)])
        kind, best = c.need(80)
        self.assertEqual(kind, "out")
        self.assertAlmostEqual(best, 60)
        c = course([exam("mid", 50, 80), exam("final", 50, 90)])
        self.assertEqual(c.need(80), ("done", 85))

    def test_targets_come_from_the_scale(self):
        us = [[93, "A", 4.0], [90, "A-", 3.7], [83, "B", 3.0], [70, "C", 2.0], [0, "F", 0]]
        c = course([exam("final", 100)], scale=us)
        self.assertEqual(c.targets(), [("B", 83), ("A-", 90), ("A", 93)])
        self.assertEqual(c.letter(91), "A-")
        self.assertEqual(c.fail_cap(), 69)

    def test_target_letters_from_settings(self):
        raw = {"code": "X 1", "key": "x1", "components": [exam("final", 100)]}
        book = g.GradeBook({"courses": [raw], "targets": ["B", "A"]})
        self.assertEqual(book.courses[0].targets(), [("B", 73), ("A", 85)])


class Components(unittest.TestCase):
    def test_best_n(self):
        c = course([{"id": "q", "label": "Quizzes", "weight": 100, "best": 2,
                     "items": [{"name": "Q1", "mark": 50}, {"name": "Q2", "mark": 100}, {"name": "Q3", "mark": 80}]}])
        self.assertAlmostEqual(c.comp("q").current(), 90)

    def test_capped_pool(self):
        c = course([{"id": "act", "label": "Activities", "weight": 100, "type": "capped", "cap": 10,
                     "items": [{"name": "A1", "max": 4, "mark": 4}, {"name": "A2", "max": 4, "mark": 4},
                               {"name": "A3", "max": 4, "mark": 3}, {"name": "A4", "max": 4}]}])
        self.assertAlmostEqual(c.comp("act").current(), 100)

    def test_points_count_by_size(self):
        c = course([{"id": "labs", "label": "Labs", "weight": 100, "type": "points",
                     "items": [{"name": "Lab 1", "max": 10, "mark": 5}, {"name": "Lab 2", "max": 30, "mark": 30}]}])
        self.assertAlmostEqual(c.comp("labs").current(), (50 * 10 + 100 * 30) / 40)

    def test_item_weights_override_points(self):
        c = course([{"id": "proj", "label": "Project", "weight": 35, "type": "points", "role": "exam",
                     "items": [{"name": "Phase 1", "max": 80, "weight": 5, "mark": 40},
                               {"name": "Phase 2", "max": 100, "weight": 15},
                               {"name": "Phase 3", "max": 120, "weight": 15}]},
                    exam("final", 65, 100)])
        proj = c.comp("proj")
        self.assertAlmostEqual(proj.open_weight(), 30)
        self.assertAlmostEqual(proj.value(1.0), (50 * 5 + 100 * 30) / 35)
        self.assertAlmostEqual(proj.item_share(proj.items[0]), 5)

    def test_attendance_free_misses(self):
        days = [{"name": f"Day {i}", "present": i <= 4} for i in range(1, 7)]
        c = course([{"id": "att", "label": "Attendance", "weight": 100, "type": "attendance", "free_misses": 1,
                     "items": days}])
        self.assertAlmostEqual(c.comp("att").current(), 100 * 5 / 6)
        self.assertEqual(c.comp("att").type, "presence")

    def test_new_attendance_day_is_created(self):
        book = g.GradeBook({"courses": [{"code": "X 1", "key": "x1", "components": [
            {"id": "att", "label": "Attendance", "weight": 10, "type": "attendance", "new_item": "Day {n}",
             "items": []}, exam("final", 90)]}]})
        g.log_mark(book, "X 1", "Day 3", "present")
        self.assertEqual([it["name"] for it in book.courses[0].comp("att").items], ["Day 3"])

    def test_higher_of(self):
        c = course([exam("mid", 30, 60), exam("final", 60),
                    {"id": "better", "label": "Better of", "weight": 10, "type": "higher_of", "of": ["mid", "final"]}])
        self.assertAlmostEqual(c.values(1.0)["better"], 100)
        self.assertAlmostEqual(c.values(0.5)["better"], 60)


class Rules(unittest.TestCase):
    def test_replace_lowest(self):
        c = course([exam("t1", 25, 50), exam("t2", 25, 90), exam("final", 50, 80)],
                   rules={"replace_lowest": {"tests": ["t1", "t2"], "with": "final"}})
        self.assertAlmostEqual(c.values(None)["t1"], 80)
        self.assertAlmostEqual(c.total(0.0), 25 * 0.8 + 25 * 0.9 + 50 * 0.8)

    def test_failed_gate_caps_the_course(self):
        c = course([exam("mid", 50, 100), exam("final", 50, 40)],
                   gates=[{"text": "50% on the final", "checks": [{"type": "min", "component": "final", "min": 50}]}])
        self.assertAlmostEqual(c.total(0.0), 49)
        self.assertIn("[x]", c.summary()["gates"][0])

    def test_average_gate(self):
        c = course([exam("mid", 50, 40), exam("final", 50, 55)],
                   gates=[{"checks": [{"type": "avg_min", "components": ["mid", "final"], "min": 50}]}])
        self.assertAlmostEqual(c.total(0.0), 47.5)   # below the cap anyway: unchanged
        c = course([exam("mid", 50, 40), exam("final", 50, 58)],
                   gates=[{"checks": [{"type": "avg_min", "components": ["mid", "final"], "min": 50}]}])
        self.assertAlmostEqual(c.total(0.0), 49)

    def test_minimum_shortfall_deducts(self):
        c = course([exam("mid", 50, 100), exam("final", 50, 30, min=40)], min_effect="deduct")
        self.assertAlmostEqual(c.total(0.0), 50 + 15 - 10)

    def test_exemption(self):
        c = course([exam("t1", 30, 95), exam("t2", 30), exam("final", 40)],
                   rules={"exemption": {"tests": ["t1", "t2"], "avg_above": 90, "each_at_least": 80}})
        ex = c.exemption()
        self.assertEqual(ex["state"], "open")
        self.assertAlmostEqual(ex["need"], 85)


class Marks(unittest.TestCase):
    def setUp(self):
        self.book = g.GradeBook({"courses": [{"code": "BIOL 1010", "key": "biol1010", "title": "Biology",
                                              "components": [
            {"id": "labs", "label": "Labs", "weight": 40, "type": "points",
             "items": [{"name": "Lab 1", "max": 2}, {"name": "Lab 2", "max": 2}]},
            {"id": "quiz", "label": "Quizzes", "weight": 20, "items": [{"name": "Quiz 1"}, {"name": "Quiz 2"}]},
            exam("final", 40)]}]})

    def test_parse_marks(self):
        g.log_mark(self.book, "BIOL 1010", "Quiz 1", "17/20")
        it = self.book.courses[0].find_item("Quiz 1")[1]
        self.assertEqual((it["mark"], it["raw"]), (85, "17/20"))
        g.log_mark(self.book, "biol", "Lab 1", "1.5/2")
        self.assertEqual(self.book.courses[0].find_item("Lab 1")[1]["mark"], 1.5)
        with self.assertRaises(g.GradeError):
            g.log_mark(self.book, "BIOL 1010", "Quiz 2", "0.5")      # 0.5% or 50%?
        with self.assertRaises(g.GradeError):
            g.log_mark(self.book, "BIOL 1010", "Lab 2", "3/4")       # out of 2, not 4

    def test_log_and_clear(self):
        change = g.log_mark(self.book, "1010", "Final", "missed")
        self.assertIn("BIOL 1010 Final missed", change.entry)
        self.assertEqual(len(self.book.data["log"]), 1)
        g.clear_mark(self.book, "BIOL 1010", "Final")
        self.assertEqual(self.book.data["log"], [])
        with self.assertRaises(g.GradeError):
            g.clear_mark(self.book, "BIOL 1010", "Final")

    def test_unknown_names(self):
        with self.assertRaises(g.GradeError):
            self.book.course("CHEM")
        with self.assertRaises(g.GradeError):
            self.book.courses[0].find_item("Lab 9")


class GPA(unittest.TestCase):
    def setUp(self):
        self.book = g.GradeBook({"courses": [{"code": "BIOL 2000", "key": "biol2000", "credits": 3,
                                              "components": [exam("final", 100, 86)]}]})
        self.data = {"default_credits": 3, "terms": [
            {"term": "Fall 2025", "courses": [{"code": "BIOL 1010", "letter": "A-"}, {"code": "MATH 1210", "letter": "B+"},
                                              {"code": "HIST 1100", "letter": "W"}],
             "term_gpa": 3.5, "cumulative_gpa": 3.5},
            {"term": "Winter 2026", "courses": [{"code": "BIOL 1020", "letter": "A", "credits": 6},
                                                {"code": "ART 1000", "letter": None}]}]}

    def test_recomputes_from_letters(self):
        tr = g.Transcript(self.data, self.book)
        self.assertEqual(tr.hours, 12)
        self.assertAlmostEqual(tr.qp, 3 * 3.7 + 3 * 3.3 + 6 * 4.0)
        self.assertEqual(g.fgpa(tr.gpa), "3.75")
        self.assertEqual(tr.mismatches(), [])
        self.assertEqual(tr.term("autumn 2025").name, "Fall 2025")

    def test_mismatch_is_reported(self):
        self.data["terms"][0]["term_gpa"] = 3.6
        msgs = g.Transcript(self.data, self.book).mismatches()
        self.assertEqual(len(msgs), 1)
        self.assertIn("Fall 2025 term GPA", msgs[0])

    def test_projection_and_what_if(self):
        tr = g.Transcript(self.data, self.book)
        self.assertEqual(g.pace(self.book)[0]["letter"], "A")
        at_pace = g.project(tr, [(3, "A")])
        self.assertAlmostEqual(at_pace["cumulative"], (tr.qp + 12) / 15)
        self.assertEqual(g.project(tr, [(3, "W")])["hours"], 12)
        self.assertEqual(g.what_if(self.book, ["B+"]), {"BIOL 2000": "B+"})
        with self.assertRaises(g.GradeError):
            g.what_if(self.book, ["Q"])

    def test_by_subject_groups_repeated_subjects(self):
        rows = g.Transcript(self.data, self.book).by_subject()
        self.assertEqual([r["subject"] for r in rows], ["BIOL", "Other"])
        self.assertEqual(len(rows[0]["courses"]), 2)

    def test_reports_run(self):
        tr = g.Transcript(self.data, self.book)
        self.assertIn("Cumulative GPA 3.75", g.gpa_report(tr))
        self.assertIn("Fall 2025", g.terms_report(tr))
        self.assertIn("no GPA effect", g.term_report(tr.terms[0]))
        self.assertIn("BIOL 2000", g.course_report(self.book.courses[0]))


if __name__ == "__main__":
    unittest.main()
