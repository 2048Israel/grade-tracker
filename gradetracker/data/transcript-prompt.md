You are converting a student's unofficial transcript into a transcript file for **Grade Tracker**, a desktop app that recalculates every term GPA and the cumulative GPA from the letter grades. The app reads the JSON you write, so follow this format exactly.

The transcript may come as a PDF, as pasted text, or as screenshots or photos. Read every page or image you're given before you answer.

## What to reply with

Reply with **one ```json code block and nothing else**.

Rules:

- Copy every term and every course, oldest term first. Copy letters exactly as printed (`"A-"`, `"B+"`, `"W"`, `"P"`).
- Copy the term GPA and cumulative GPA the transcript shows for each term, and the overall standing. The app recomputes them from the letters and warns the student if they don't match, which catches misread letters, so copy them carefully and don't calculate them yourself.
- A course in progress or with no grade yet gets `"letter": null`.
- Leave out any field the transcript doesn't show.

## The format

```json
{
  "format": "grade-tracker-transcript",
  "version": 1,
  "school": "Example University",
  "default_credits": 3,
  "scale": [[90, "A+", 4.3], [85, "A", 4.0], [80, "A-", 3.7], [77, "B+", 3.3], [73, "B", 3.0], [70, "B-", 2.7], [67, "C+", 2.3], [63, "C", 2.0], [60, "C-", 1.7], [50, "D", 1.0], [0, "F", 0.0]],
  "terms": [
    {
      "term": "Fall 2025",
      "courses": [
        {"code": "BIOL 1010", "title": "Introduction to Biology", "credits": 3, "letter": "A-"},
        {"code": "MATH 1210", "title": "Calculus I", "credits": 3, "letter": "B+"},
        {"code": "HIST 1100", "title": "World History", "credits": 3, "letter": "W"}
      ],
      "term_gpa": 3.5,
      "cumulative_gpa": 3.5
    }
  ],
  "standing": {"credit_hours": 6, "quality_points": 21.0, "cumulative_gpa": 3.5, "as_of": "Dec 2025"}
}
```

- `term`: the term's name as printed ("Fall 2025", "Sep–Dec 2025", "2025-26 Semester 1").
- `credits`: the course's credit hours or units. If every course has the same value you can set `default_credits` once and leave `credits` off.
- `scale`: the school's letter scale, if the transcript (or its legend) shows the grade points per letter. Each row is `[lowest percent, letter, grade points]`, highest first, ending at 0. If only grade points are shown and no percent cutoffs, give your best-known cutoffs for that school; the percents only matter for current courses, the grade points are what the GPA uses.
- `standing`: the totals at the bottom of the transcript: credit hours that count toward the GPA, quality points (if shown), cumulative GPA, and the date.

Now convert the transcript I've attached or pasted.
