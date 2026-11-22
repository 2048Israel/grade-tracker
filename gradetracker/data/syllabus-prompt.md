You are converting a course syllabus into a course file for **Grade Tracker**, a desktop app that tracks a student's marks and works out what they need on the remaining work for each letter grade. The app reads the JSON you write, so follow this format exactly.

The syllabus may come as a PDF or other file, as pasted text, or as screenshots or photos of its pages. Read every page or image you're given before you answer; the grading breakdown is often split across pages.

## What to reply with

Reply with **one ```json code block and nothing else**. If I gave you one syllabus, the JSON is one course object. If I gave you several, it is:

```json
{"format": "grade-tracker-courses", "courses": [ {course}, {course} ]}
```

Rules:

- Use only what the syllabus says. Never invent a weight, a count or a rule. If something is unclear or missing (e.g. "quizzes: 15%" but no count), make your best reading and say so in `"notes"`.
- Weights are percentages of the final grade and should add up to 100. Bonus marks are allowed but say so in a note.
- Leave out any field you don't need. Don't include marks: the student logs those in the app.
- Dates are written like `"Oct 5"`.

## The course object

```json
{
  "format": "grade-tracker-course",
  "version": 1,
  "code": "BIOL 1010",
  "title": "Introduction to Biology",
  "term": "Fall 2026",
  "credits": 3,
  "instructor": "Dr. Example",
  "scale": [[90, "A+", 4.3], [85, "A", 4.0], [80, "A-", 3.7], [77, "B+", 3.3], [73, "B", 3.0], [70, "B-", 2.7], [67, "C+", 2.3], [63, "C", 2.0], [60, "C-", 1.7], [50, "D", 1.0], [0, "F", 0.0]],
  "components": [ ... ],
  "gates": [ ... ],
  "rules": { ... },
  "notes": ["Anything the student should know that the fields can't hold."]
}
```

- `code` (required): the course code as printed, e.g. `"BIOL 1010"`, `"MATH-201"`. `title`: the course name.
- `credits`: credit hours or units (default 3). `term`: e.g. `"Fall 2026"`.
- `scale`: only if the syllabus gives the letter-grade cutoffs. Each row is `[lowest percent, letter, grade points]`, highest first, ending with a row that starts at 0. Grade points are the GPA value of the letter (often 4.0 for A; 4.3 for A+ at some schools). If the syllabus gives cutoffs but no grade points, use the usual values for that school if you know them, otherwise leave the points out: `[90, "A+"]`.

## Components

One component per line of the syllabus's grading breakdown. Each has:

- `id`: short lowercase name you make up, unique in the course (`"labs"`, `"midterm"`, `"final"`). Gates and rules refer to these.
- `label`: what the syllabus calls it, e.g. `"Labs (best 8 of 10)"`.
- `weight`: its percent of the final grade (a number, no % sign).
- `type`: how its items combine (default `"average"`), one of the types below.
- `role`: `"exam"` for tests and exams, `"coursework"` for everything else (the default). The app works out the average the student needs *on the exams still to come*; coursework still in progress is projected at its current level.
- `items`: the individual pieces, e.g. `[{"name": "Lab 1", "due": "Sep 12"}, {"name": "Lab 2"}]`. Give every item a distinct name across the whole course. A single exam is one item: `[{"name": "Midterm", "due": "Oct 20"}]`. Instead of listing many identical items you can write `"count": 10, "item_name": "Quiz"`, which makes Quiz 1 ... Quiz 10.
- Optional: `best` (only the best N items count), `drop_lowest` (N lowest items are dropped), `min` (a minimum percent on this component, see gates), `must_pass` (a percent the student must reach on this component or fail the course), `assume` (percent to project before any marks are in, e.g. `100` for participation), `note`.

### Types

| type | use it for | extra fields |
|---|---|---|
| `average` | items marked as percentages, each counting equally (assignments, quizzes, exams). Give an item `"weight"` if items count differently (Phase 1 is 5% and Phase 2 is 15% of the course: weights 5 and 15). | |
| `points` | items marked in points where bigger items count more (a 50-pt lab counts five times a 10-pt one) | every item needs `"max"` (what it's out of) |
| `capped` | earn points from a pool until a cap: "earn 30 points from any of the 10 activities" | `"cap"`: the points that earn full marks; items may have `"max"` |
| `attendance` | participation or attendance counted by days present | `"free_misses"`: days you can miss without penalty. Items can be left out; the student adds days as they go |
| `higher_of` | "this 10% goes to whichever is higher, your midterm or your final" | `"of"`: [the component ids it compares]. No items |

## Gates (pass requirements)

For rules like "you must get at least 50% on the final exam to pass the course":

```json
"gates": [
  {"text": "At least 50% on the final exam or you fail the course",
   "checks": [{"type": "min", "component": "final", "min": 50}]}
]
```

Check types:

- `{"type": "min", "component": "final", "min": 50}`: that component must reach `min` percent.
- `{"type": "avg_min", "components": ["midterm", "final"], "min": 50}`: the average of those components must reach `min`.
- `{"type": "submitted", "components": ["assignments"]}`: every item in those components must be handed in.

A failed `min` or `avg_min` caps the course at a failing grade. A simple single-component requirement can also be written on the component as `"must_pass": 50`.

If the syllabus says falling short of a component minimum costs marks instead of failing the course ("each percent below 40% on the final is deducted from your total"), give those components `"min"` and add `"min_effect": "deduct"` to the course.

## Rules

Only if the syllabus has them:

- Final replaces the lowest test if it's higher: `"rules": {"replace_lowest": {"tests": ["test1", "test2", "test3"], "with": "final"}}` (add `"max_misses": 2` if it only applies with at most 2 missed classes).
- Exemption from the final for a high test average: `"rules": {"exemption": {"tests": ["test1", "test2"], "avg_above": 90, "each_at_least": 80, "grade": "A+"}}`.

## A complete example

```json
{
  "format": "grade-tracker-course",
  "version": 1,
  "code": "PSYC 1000",
  "title": "Introduction to Psychology",
  "term": "Fall 2026",
  "credits": 3,
  "components": [
    {"id": "quizzes", "label": "Weekly quizzes (best 8 of 10)", "weight": 20, "count": 10, "item_name": "Quiz", "best": 8},
    {"id": "labs", "label": "Lab reports", "weight": 15, "type": "points",
     "items": [{"name": "Lab 1", "max": 10, "due": "Sep 18"}, {"name": "Lab 2", "max": 10, "due": "Oct 2"}, {"name": "Lab 3", "max": 20, "due": "Oct 30"}]},
    {"id": "participation", "label": "Participation", "weight": 5, "type": "attendance", "free_misses": 2},
    {"id": "midterm", "label": "Midterm (Oct 21)", "weight": 25, "role": "exam", "items": [{"name": "Midterm", "due": "Oct 21"}]},
    {"id": "final", "label": "Final exam (cumulative)", "weight": 35, "role": "exam", "items": [{"name": "Final"}], "must_pass": 50}
  ],
  "notes": ["Late labs lose 10% per day (not tracked by the app)."]
}
```

Now convert the syllabus I've attached or pasted.
