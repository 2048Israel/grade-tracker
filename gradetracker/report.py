"""The printable report: one self-contained HTML page with every course, the GPA and past terms.

Open it in a browser and print (or "Save as PDF"). No scripts, no external files.
"""
from __future__ import annotations

import datetime as dt
import html

from .engine import (GradeBook, Transcript, fgpa, fixed, fnum, fpct, graded, need_cell, pace, plain, project)

CSS = """
:root { --ink:#16202e; --muted:#5b6b80; --line:#d9e0ea; --soft:#f3f6fa; --good:#137a4b; --warn:#9a6700;
        --bad:#b42318; --accent:#2f6fd6; }
* { box-sizing: border-box; }
body { margin: 0; background: #fff; color: var(--ink); font: 14px/1.45 "Segoe UI", system-ui, sans-serif; }
main { max-width: 980px; margin: 0 auto; padding: 32px 24px 48px; }
h1 { font-size: 26px; margin: 0 0 4px; }
h2 { font-size: 19px; margin: 34px 0 6px; padding-top: 14px; border-top: 2px solid var(--ink); }
h3 { font-size: 15px; margin: 18px 0 6px; }
.sub { color: var(--muted); margin: 0 0 14px; }
table { width: 100%; border-collapse: collapse; margin: 6px 0 10px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { font-size: 11px; letter-spacing: .04em; text-transform: uppercase; color: var(--muted); font-weight: 600; }
td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
tr.total td { font-weight: 600; border-top: 2px solid var(--line); }
.pill { display: inline-block; min-width: 2.4em; text-align: center; padding: 1px 6px; border-radius: 10px;
        background: var(--soft); font-weight: 600; }
.good { color: var(--good); } .warn { color: var(--warn); } .bad { color: var(--bad); } .muted { color: var(--muted); }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; margin: 10px 0; }
.tile { border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; }
.tile b { display: block; font-size: 24px; }
.tile span { color: var(--muted); font-size: 12px; }
ul { margin: 4px 0 8px 18px; padding: 0; }
.course { break-inside: avoid-page; }
footer { margin-top: 40px; color: var(--muted); font-size: 12px; }
@media print { main { padding: 0; } h2 { break-before: auto; } a { color: inherit; } }
@media (max-width: 640px) { td, th { padding: 5px 4px; } }
"""


def e(s) -> str:
    return html.escape(str(s), quote=True)


def need_html(result, target: float) -> str:
    kind, v = result
    if kind == "locked":
        return '<span class="good">locked in</span>'
    if kind == "out":
        return f'<span class="bad">out of reach</span> <span class="muted">(max {e(fpct(v))})</span>'
    if kind == "done":
        cls = "good" if v >= target - 1e-9 else "bad"
        return f'<span class="{cls}">{e(fpct(v))}</span>'
    cls = "good" if v <= 85 else ("warn" if v <= 100 else "bad")
    return f'<span class="{cls}">{e(fpct(v))}</span>'


def course_section(course) -> str:
    s = course.summary()
    out = [f'<section class="course"><h2>{e(course.code)}'
           + (f' <span class="muted">— {e(course.title)}</span>' if course.title else "") + "</h2>"]
    ga = s["graded_avg"]
    meta = [f"{fnum(course.credits)} credits"]
    meta.append(f"graded work so far {fpct(ga)} ({s['graded_letter']})" if ga is not None else "nothing graded yet")
    out.append(f'<p class="sub">{e(" · ".join(meta))}</p>')
    out.append('<table><tr><th>Component</th><th class="n">Weight</th><th>Standing</th><th>Used in the math</th></tr>')
    for c in course.components:
        mn = f' <span class="muted">min {e(fnum(c.min))}%</span>' if c.min is not None else ""
        out.append(f"<tr><td>{e(c.label)}{mn}</td><td class='n'>{e(fnum(c.weight))}%</td>"
                   f"<td>{e(plain(c.standing_text()))}</td><td>{e(c.projection_text())}</td></tr>")
    total = sum(c.weight for c in course.components)
    out.append(f"<tr class='total'><td>Total</td><td class='n'>{e(fnum(total))}%</td><td></td><td></td></tr></table>")

    opens = " + ".join(s["open_components"])
    if opens:
        out.append(f"<h3>Average needed on {e(opens)}</h3>")
    else:
        out.append("<h3>Final result</h3>")
    out.append('<table><tr><th>Target</th><th>Needed</th></tr>')
    for letter, target in s["targets"]:
        out.append(f"<tr><td><span class='pill'>{e(letter)}</span> {e(fnum(target))}%</td>"
                   f"<td>{need_html(s['need'][letter], target)}</td></tr>")
    out.append("</table>")
    if opens:
        out.append(f'<p class="muted">{e(fnum(s["banked"], 1))} points banked or on pace, '
                   f'{e(fnum(s["open"], 1))}% still open.</p>')

    extra = []
    ex = course.exemption()
    if ex:
        extra.append(f"Exemption: {ex['state']}" + (f" ({'; '.join(ex['lost'])})" if ex["lost"] else ""))
    extra += [f"Requirement: {g}" for g in s["gates"]]
    extra += [f"Flag: {f}" for f in s["flags"]]
    extra += [f"Note: {n}" for n in course.raw.get("notes", [])]
    if extra:
        out.append("<ul>" + "".join(f"<li>{e(x)}</li>" for x in extra) + "</ul>")

    rows = []
    for c in course.components:
        for it in c.counted:
            if graded(it):
                rows.append(f"<tr><td>{e(it['name'])}</td><td>{e(c.short)}</td><td>{e(c.item_mark(it))}</td>"
                            f"<td class='muted'>{e(it.get('logged', ''))}</td></tr>")
    if rows:
        out.append("<h3>Marks</h3><table><tr><th>Item</th><th>Component</th><th>Mark</th><th>Logged</th></tr>"
                   + "".join(rows) + "</table>")
    out.append("</section>")
    return "\n".join(out)


def gpa_section(book: GradeBook, tr: Transcript) -> str:
    out = ['<section><h2>GPA</h2>']
    rows = pace(book)
    proj = project(tr, [(r["credits"], r["letter"]) for r in rows]) if rows else None
    tiles = [("Cumulative GPA", fgpa(tr.gpa), f"{fnum(tr.hours)} credit hours" if not tr.empty else "no transcript yet"),
             ("Quality points", fixed(tr.qp), "grade points × credits")]
    if proj:
        tiles.append((f"{book.term}, at pace", fgpa(proj["cumulative"]), f"term GPA {fgpa(proj['term_gpa'])}"))
    out.append('<div class="tiles">' + "".join(f'<div class="tile"><span>{e(a)}</span><b>{e(b)}</b><span>{e(c)}</span></div>'
                                               for a, b, c in tiles) + "</div>")
    for m in tr.mismatches():
        out.append(f'<p class="warn">Check: {e(m)}</p>')
    if rows:
        out.append(f"<h3>{e(book.term)} projection</h3>")
        out.append('<table><tr><th>Course</th><th class="n">Credits</th><th>Graded so far</th><th>Pace</th></tr>')
        for r in rows:
            ga = fpct(r["graded_avg"]) if r["graded_avg"] is not None else "nothing back"
            out.append(f"<tr><td>{e(r['code'])}</td><td class='n'>{e(fnum(r['credits']))}</td><td>{e(ga)}</td>"
                       f"<td><span class='pill'>{e(r['letter'])}</span>{' assumed' if r['assumed'] else ''}</td></tr>")
        out.append("</table>")
    out.append("</section>")
    return "\n".join(out)


def past_section(tr: Transcript) -> str:
    if tr.empty:
        return ""
    out = ['<section><h2>Past semesters</h2>',
           '<table><tr><th>Term</th><th>Courses</th><th class="n">Hours</th><th class="n">Term GPA</th>'
           '<th class="n">Cumulative</th></tr>']
    for t in tr.terms:
        courses = ", ".join(f"{c.code} {c.letter or '—'}" for c in t.courses)
        out.append(f"<tr><td>{e(t.name)}</td><td>{e(courses)}</td><td class='n'>{e(fnum(t.hours))}</td>"
                   f"<td class='n'>{e(t.gpa_text())}</td><td class='n'>{e(fgpa(t.cumulative))}</td></tr>")
    out.append(f"<tr class='total'><td>Overall</td><td></td><td class='n'>{e(fnum(tr.hours))}</td><td></td>"
               f"<td class='n'>{e(fgpa(tr.gpa))}</td></tr></table></section>")
    return "\n".join(out)


def build_html(book: GradeBook, tr: Transcript, when: dt.datetime = None) -> str:
    when = when or dt.datetime.now()
    title = f"Grade report — {book.term}"
    parts = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
             f"<meta name='viewport' content='width=device-width, initial-scale=1'><title>{e(title)}</title>"
             f"<style>{CSS}</style></head><body><main>",
             f"<h1>{e(title)}</h1>",
             f"<p class='sub'>{len(book.courses)} course{'s' if len(book.courses) != 1 else ''} · "
             f"made {e(when.strftime('%b'))} {when.day}, {when.year} with Grade Tracker</p>"]
    if book.courses:
        parts.append('<table><tr><th>Course</th><th>Graded so far</th>'
                     + "".join(f"<th>{e(l)}</th>" for l, _ in book.courses[0].targets()) + "</tr>")
        for c in book.courses:
            s = c.summary()
            ga = f"{fpct(s['graded_avg'])} ({s['graded_letter']})" if s["graded_avg"] is not None else "—"
            cells = "".join(f"<td>{need_html(s['need'].get(l, ('out', 0)), t)}</td>" for l, t in c.targets())
            parts.append(f"<tr><td><b>{e(c.code)}</b></td><td>{e(ga)}</td>{cells}</tr>")
        parts.append("</table><p class='muted'>Each target column is the average needed on what's left.</p>")
    parts += [course_section(c) for c in book.courses]
    parts.append(gpa_section(book, tr))
    parts.append(past_section(tr))
    parts.append("<footer>Projections assume coursework in progress continues at its current level. "
                 "Always check against your school's official records.</footer></main></body></html>")
    return "\n".join(p for p in parts if p)


__all__ = ["build_html", "need_cell"]
