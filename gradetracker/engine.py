"""The grading engine: course math, mark parsing and GPA. Standard library only, Python 3.9+.

A course is a list of weighted components (labs, quizzes, midterm, final...), each holding items
(Lab 1, Lab 2...). The window, the command line and the printable report all run this code.

How the "need" numbers work: coursework still in progress (assignments, labs, quizzes) is
projected at its current level; anything with no marks yet and an "assume" value is projected
at that value. The remaining exams (components with role "exam") are then solved for the single
average score that lands the course on each target letter, with every rule applied: best-N
drops, capped points, higher-of components, lowest-test replacement, point-for-point minimum
deductions, and pass requirements (a failed requirement caps the course at a failing grade).

Inside a component, items count equally, or by their points when the component is "points"
(a 215-pt assignment counts 215/1,109 of the component). An item's own "weight" overrides both:
three project phases worth 80/100/120 pts that weigh 5/15/15% of the course count by weight,
and their points only set each phase's percentage.

How the GPA numbers work: a letter's grade points come from the letter scale (e.g. A+ 4.3 ...
F 0); a course's quality points are points x credits. GPA = quality points / credit hours,
rounded half-up to 2 places as on most transcripts. A W (or another no-GPA mark), or a course
with no grade recorded yet, carries no hours at all. The term projection puts each current
course at the letter of its graded work so far, or at a what-if letter, and adds that term to
the transcript's hours and quality points.
"""
from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal

# A common 4.3 letter scale: used until a course file or transcript brings the school's own.
DEFAULT_SCALE = [
    [90, "A+", 4.3], [85, "A", 4.0], [80, "A-", 3.7],
    [77, "B+", 3.3], [73, "B", 3.0], [70, "B-", 2.7],
    [67, "C+", 2.3], [63, "C", 2.0], [60, "C-", 1.7],
    [50, "D", 1.0], [0, "F", 0.0],
]
DEFAULT_CREDITS = 3.0
# Marks that carry no credit hours in a GPA. A null letter means no grade is recorded yet.
NO_GPA = {"W", "WD", "P", "CR", "AU", "IP", "INC"}

# Friendlier names accepted in course files, mapped onto the engine's own.
TYPE_ALIASES = {"attendance": "presence", "participation": "presence", "higher_of": "better",
                "better_of": "better", "best_of": "better", "pool": "capped"}
ROLE_ALIASES = {"exam": "solve", "test": "solve", "coursework": "project", "work": "project"}
TYPES = ("average", "points", "capped", "presence", "better")


class GradeError(Exception):
    """A problem the user can fix: unknown course or item, bad mark, bad file."""


# --------------------------------------------------------------------------- formatting

def fnum(v: float, dp: int = 2, sep: bool = False) -> str:
    """Round half-up to dp places and drop trailing zeros: 80.490 -> '80.49', 96.0 -> '96'."""
    q = Decimal(repr(float(v))).quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_UP)
    if q == 0:
        q = abs(q)
    s = f"{q:,.{dp}f}" if sep else f"{q:.{dp}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def fixed(v: float, dp: int = 2) -> str:
    """Round half-up and keep the zeros: 1.125 -> '1.13', 1 -> '1.00'."""
    return f"{Decimal(repr(float(v))).quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_UP):.{dp}f}"


def fpct(v: float, dp: int = 1) -> str:
    return fnum(v, dp) + "%"


def spaced(raw: str) -> str:
    return re.sub(r"\s*/\s*", " / ", raw.strip())


def num(v: float):
    """Store whole numbers as ints so the files stay readable."""
    v = round(float(v), 4)
    return int(v) if v.is_integer() else v


def norm(s: str) -> str:
    """Matching key: 'Lab 05' -> 'lab5', 'Quiz #3' -> 'quiz3', 'A01' -> 'a1'."""
    toks = re.findall(r"[a-z]+|\d+", str(s).lower())
    return "".join(str(int(t)) if t.isdigit() else t for t in toks)


def item_number(name: str):
    found = re.findall(r"\d+", name)
    return int(found[-1]) if found else None


class _Blank(dict):
    def __missing__(self, key):
        return ""


def fill(template: str, fields: dict) -> str:
    """'{name}[ ({due})]' -> 'Lab 1 (Sep 27)'; a [...] segment drops out when its field is empty."""
    def optional(m):
        inner = m.group(1)
        keys = re.findall(r"\{(\w+)", inner)
        return inner if all(fields.get(k) not in (None, "") for k in keys) else ""
    return re.sub(r"\[([^\[\]]*)\]", optional, template).format_map(_Blank(fields))


def plain(md: str) -> str:
    """Status text with icons -> console text."""
    for icon, tag in (("✅", "[ok]"), ("⚠️", "[!]"), ("❌", "[x]"), ("⬜", "[ ]"), ("🟢", "[ok]"),
                      ("🟡", "[~]"), ("🔴", "[x]"), ("❓", "[?]"), ("→", "->"), ("≥", ">="), ("≤", "<=")):
        md = md.replace(icon, tag)
    return md.replace("**", "").replace("*", "")


def lower(s: str) -> str:
    """'Hands-on' -> 'hands-on', 'Better Test' -> 'better test'; keeps 'iClicker', 'MATLAB'."""
    return " ".join(w if any(ch.isupper() for ch in w[1:]) else w.lower() for w in s.split(" "))


def short_date(d: dt.date) -> str:
    return f"{d:%b} {d.day}"


# --------------------------------------------------------------------------- marks

MISSED_WORDS = {"missed", "miss", "m", "skip", "skipped", "absent", "a", "x", "no", "none", "zero"}
PRESENT_WORDS = {"present", "p", "yes", "y", "here", "attended", "ok"}
FRACTION_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*$")
NUMBER_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*(%?)\s*$")
MARK_KEYS = ("mark", "missed", "present", "raw")


def graded(it: dict) -> bool:
    return "mark" in it or bool(it.get("missed")) or "present" in it


def parse_mark(comp: "Component", item: dict, text: str) -> dict:
    """Turn what the user typed into the fields stored on the item."""
    t = text.strip()
    low = t.lower()
    if not t:
        raise GradeError("Enter a mark.")
    if comp.unit == "presence":
        if low in PRESENT_WORDS:
            return {"present": True}
        if low in MISSED_WORDS:
            return {"present": False}
        if FRACTION_RE.match(t) or NUMBER_RE.match(t):
            return {"present": True, "raw": t.replace(" ", "").rstrip("%")}
        raise GradeError(f"'{t}': write present, missed, or the score shown (e.g. 3.5/10).")
    if low in MISSED_WORDS:
        return {"missed": True}
    fm, nm = FRACTION_RE.match(t), NUMBER_RE.match(t)
    if comp.unit == "pts":
        mx = item.get("max")
        if fm:
            got, out = float(fm[1]), float(fm[2])
            if mx and abs(out - mx) > 1e-9:
                raise GradeError(f"{item['name']} is out of {fnum(mx)}, not {fnum(out)}.")
            res = {"mark": num(got)}
            if not mx:
                res["max"] = num(out)
                mx = out
        elif nm and nm[2] == "%":
            if not mx:
                raise GradeError(f"{item['name']} has no known maximum; give points, e.g. 4 or 4/4.")
            res = {"mark": num(mx * float(nm[1]) / 100)}
        elif nm:
            res = {"mark": num(float(nm[1]))}
        else:
            raise GradeError(f"'{t}': write points, e.g. 1.5 or 1.5/2.")
        if res["mark"] < 0 or (mx and res["mark"] > mx + 1e-9):
            raise GradeError(f"{fnum(res['mark'])} is outside 0–{fnum(mx)} for {item['name']}.")
        return res
    if fm:
        got, out = float(fm[1]), float(fm[2])
        if out <= 0:
            raise GradeError(f"'{t}': the denominator must be positive.")
        res = {"mark": num(100 * got / out), "raw": f"{fnum(got)}/{fnum(out)}"}
    elif nm:
        v = float(nm[1])
        if nm[2] != "%" and 0 < v <= 1:
            raise GradeError(f"'{t}' is ambiguous for a percentage: write {fnum(v * 100)}% or {t}/1.")
        res = {"mark": num(v)}
    else:
        raise GradeError(f"'{t}': write a percentage (85 or 85%), a score (17/20), or missed.")
    if not 0 <= res["mark"] <= 150:
        raise GradeError(f"{fnum(res['mark'])}% is out of range.")
    return res


# --------------------------------------------------------------------------- model

class Component:
    def __init__(self, raw: dict, course: "Course"):
        self.raw, self.course = raw, course
        self.id = raw["id"]
        kind = str(raw.get("type", "average")).lower()
        kind = TYPE_ALIASES.get(kind, kind)
        self.by_points = raw.get("by_points", False)
        unit = raw.get("unit", "pct")
        if kind == "points":            # points-based items that count by their size
            kind, self.by_points, unit = "average", True, "pts"
        self.type = kind
        role = str(raw.get("role", "project")).lower()
        self.role = ROLE_ALIASES.get(role, role)
        self.weight = float(raw["weight"])
        self.items = raw["items"] if "items" in raw else []  # never add keys: saves must round-trip
        self.label = raw.get("label", raw.get("short", self.id))
        self.short = raw.get("short") or re.sub(r"\s*\(.*?\)", "", self.label).replace("*", "")
        self.min = raw.get("min")
        self.best = raw.get("best")
        self.unit = {"capped": "pts", "presence": "presence"}.get(self.type, unit)

    # ---- items
    @property
    def counted(self):
        return [it for it in self.items if not it.get("exclude")]

    def graded_items(self):
        return [it for it in self.counted if graded(it)]

    def open_items(self):
        return [it for it in self.counted if not graded(it)]

    def fields(self, it: dict) -> dict:
        n = item_number(it["name"])
        return {**it, "n": n if n is not None else "", "nn": f"{n:02d}" if n is not None else ""}

    def item_pct(self, it: dict):
        if it.get("missed"):
            return 0.0
        if self.unit == "pts":
            return 100 * it["mark"] / it["max"] if it.get("max") else None
        return float(it["mark"])

    def wt(self, it: dict) -> float:
        """An item's share of the component: its own "weight", else its points (by_points), else 1."""
        if "weight" in it:
            return float(it["weight"])
        return float(it.get("max", 1)) if self.by_points else 1.0

    @property
    def own_weights(self) -> bool:
        """True when items carry their own weights (phases 5/15/15%, not 80/100/120 pts)."""
        return any("weight" in it for it in self.counted)

    def item_share(self, it: dict) -> float:
        """The item's weight in the course, in percent."""
        return self.weight * self.wt(it) / sum(self.wt(x) for x in self.counted)

    def earned(self) -> float:
        return sum(0 if it.get("missed") else it["mark"] for it in self.graded_items())

    # ---- standing
    def current(self):
        """Standing on graded work only (pct), or None. Free misses are free."""
        if self.type == "presence":
            p, m = self.presence_counts()
            free = min(m, int(self.raw.get("free_misses", 0)))
            return 100 * (p + free) / (p + m) if p + m else None
        if self.type == "capped":
            g = self.graded_items()
            return 100 * min(self.earned(), self.raw["cap"]) / self.raw["cap"] if g else None
        if self.type == "better":
            known = [v for v in (self.course.comp(r).current() for r in self.raw["of"]) if v is not None]
            return max(known) if known else None
        g = self.graded_items()
        if self.best:
            drops = len(self.counted) - self.best
            misses = [it for it in g if it.get("missed")][:drops]
            g = [it for it in g if not any(it is m for m in misses)]
        vals = [(self.item_pct(it), self.wt(it)) for it in g]
        vals = [(p, w) for p, w in vals if p is not None]
        if not vals:
            return None
        if self.best and len(vals) > self.best:
            vals = sorted(vals, key=lambda pw: -pw[0])[: self.best]
        return sum(p * w for p, w in vals) / sum(w for _, w in vals)

    def gauge(self):
        """Value used for the 'graded work' average (None if nothing is back)."""
        if self.type == "capped":
            return self.value(None) if self.graded_items() else None
        if self.type == "average" and not self.graded_items():
            return None
        return self.current()

    def presence_counts(self):
        p = sum(1 for it in self.items if it.get("present") is True)
        m = sum(1 for it in self.items if it.get("present") is False)
        return p, m

    def assumed(self) -> bool:
        return self.type in ("average", "capped") and not self.graded_items() and "assume" in self.raw

    def is_open(self) -> bool:
        """True when the solver's score x decides (part of) this component."""
        if self.type == "better":
            return any(self.course.comp(r).is_open() for r in self.raw["of"])
        if self.type == "presence" or not self.open_items():
            return False
        return self.role == "solve" or (not self.graded_items() and "assume" not in self.raw)

    def open_weight(self) -> float:
        if not self.is_open():
            return 0.0
        if self.type == "better":
            return self.weight
        total = sum(self.wt(it) for it in self.counted)
        return self.weight * sum(self.wt(it) for it in self.open_items()) / total

    def value(self, x):
        """Projected component percentage. x (0-1) fills open items; None = no fill."""
        if self.type == "presence":
            cur = self.current()
            return cur if cur is not None else float(self.raw.get("assume", 100))
        if self.type == "better":
            raise AssertionError("Higher-of components are evaluated by the course")
        g, ung = self.graded_items(), self.open_items()
        filler = None
        if ung:
            if self.is_open():
                filler = None if x is None else 100 * x
            elif g:
                filler = "rate"
            else:
                return float(self.raw["assume"])
        if self.type == "capped":
            cap, pts = self.raw["cap"], self.earned()
            if filler == "rate":
                pts += self.earned() / len(g) * len(ung)
            elif filler is not None:
                default = self.raw.get("item_max", cap / max(len(self.counted), 1))
                pts += sum(filler / 100 * it.get("max", default) for it in ung)
            elif not g:
                return None
            return 100 * min(pts, cap) / cap
        cur = self.current()
        if filler == "rate":
            filler = cur
        if filler is None:
            return cur
        vals = [(self.item_pct(it), self.wt(it)) for it in g] + [(filler, self.wt(it)) for it in ung]
        if self.best and len(vals) > self.best:
            vals = sorted(vals, key=lambda pw: -pw[0])[: self.best]
        return sum(p * w for p, w in vals) / sum(w for _, w in vals)

    # ---- display
    def fpts(self, v: float) -> str:
        dp = self.raw.get("decimals", 0)
        return f"{v:.{dp}f}" if dp else fnum(v, 2, sep=True)

    def item_mark(self, it: dict) -> str:
        """'80.49%', '9.95 / 10 — 99.5%', '85 / 85 — 100%', '4 pts', '3.5 / 10'."""
        if self.unit == "presence":
            if it.get("raw"):
                return spaced(it["raw"])
            return "present" if it.get("present") else "missed"
        if it.get("missed"):
            return "missed"
        if self.unit == "pts":
            if it.get("max"):
                return f"{self.fpts(it['mark'])} / {fnum(it['max'], 2, True)} — {fpct(self.item_pct(it))}"
            return f"{self.fpts(it['mark'])} pts"
        if it.get("raw"):
            return f"{spaced(it['raw'])} — {fnum(it['mark'])}%"
        return f"{fnum(it['mark'])}%"

    def item_short(self, it: dict) -> str:
        """Compact mark for one-liners: '85%', '9.5/10 (95%)', '160/160', '4 pts', 'present'."""
        if self.unit == "presence":
            return it["raw"] if it.get("raw") else ("present" if it.get("present") else "missed")
        if it.get("missed"):
            return "missed"
        if self.unit == "pts":
            return f"{fnum(it['mark'])}/{fnum(it['max'])}" if it.get("max") else f"{fnum(it['mark'])} pts"
        return f"{it['raw']} ({fnum(it['mark'])}%)" if it.get("raw") else f"{fnum(it['mark'])}%"

    def open_label(self) -> str:
        """'Final', or the open items of a partly graded pool: 'Phase 2 + Phase 3'."""
        if len(self.counted) > 1 and self.graded_items() and self.type != "better":
            return " + ".join(it["name"] for it in self.open_items())
        return self.short

    def standing_text(self) -> str:
        """Short standing: '89.7% avg (3/10)', '7.0/16 pts', '4 days, 0 missed'."""
        g = self.graded_items()
        if self.type == "presence":
            p, m = self.presence_counts()
            free = self.raw.get("free_misses")
            return f"{p + m} days, {m} missed" + (f" ({free} free)" if free else "")
        if self.type == "better":
            cur = self.current()
            return fpct(cur) if cur is not None else "—"
        if self.type == "capped":
            return f"{self.fpts(self.earned())}/{fnum(self.raw['cap'])} pts ({len(g)}/{len(self.counted)})"
        if not g:
            return "—"
        if len(self.counted) == 1:
            return self.item_mark(g[0])
        if self.by_points:
            got = sum(0 if it.get("missed") else it["mark"] for it in g)
            weighted = " weighted" if self.own_weights else ""
            return f"{self.fpts(got)}/{fnum(sum(it['max'] for it in g), 2, True)} ({fpct(self.current())}{weighted})"
        if len(g) == 1:
            return f"{g[0]['name']} {self.item_mark(g[0])}"
        return f"{fpct(self.current())} avg ({len(g)}/{len(self.counted)})"

    def projection_text(self) -> str:
        """What the course math uses for this component right now: '86.5%', '100% assumed', 'open'."""
        if self.type == "better":
            return "open" if self.is_open() else ""
        if self.is_open():
            return "partly open" if self.graded_items() else "open"
        v = self.value(None)
        return (fpct(v) + (" assumed" if self.assumed() else "")) if v is not None else ""


def scale_rows(raw) -> list:
    """[[floor, letter, points?], ...] -> [(floor, letter, points or None)], highest floor first."""
    rows = []
    for s in raw:
        rows.append((float(s[0]), str(s[1]), float(s[2]) if len(s) > 2 and s[2] is not None else None))
    return sorted(rows, key=lambda r: -r[0])


class Course:
    def __init__(self, raw: dict, book: "GradeBook"):
        self.raw, self.book = raw, book
        self.code, self.key, self.title = raw["code"], raw["key"], raw.get("title", "")
        self.credits = float(raw.get("credits", DEFAULT_CREDITS))
        self.components = [Component(c, self) for c in raw["components"]]
        self.rules = raw.get("rules", {})
        self.gates = raw.get("gates", [])
        self.scale = scale_rows(raw["scale"]) if raw.get("scale") else book.scale

    def comp(self, cid: str) -> Component:
        for c in self.components:
            if c.id == cid:
                return c
        raise KeyError(f"{self.code} has no component '{cid}'")

    # ---- letters
    def letter(self, pct: float) -> str:
        for floor, letter, _ in self.scale:
            if pct >= floor - 1e-9:
                return letter
        return self.scale[-1][1]

    def targets(self) -> list:
        """[(letter, floor)] the window shows needs for, lowest first (e.g. A- 80, A 85, A+ 90)."""
        floors = {letter: floor for floor, letter, _ in self.scale}
        out = []
        for t in self.book.target_spec:
            if isinstance(t, (list, tuple)):
                out.append((str(t[0]), float(t[1])))
            elif t in floors:
                out.append((t, floors[t]))
        if not out:
            out = [(letter, floor) for floor, letter, _ in self.scale[:3]]
        return sorted(out, key=lambda lt: lt[1])

    def fail_cap(self) -> float:
        """Where a failed pass requirement caps the course: just under the lowest passing letter."""
        if "fail_cap" in self.raw:
            return float(self.raw["fail_cap"])
        passing = [floor for floor, _, _ in self.scale if floor > 0]
        return (min(passing) - 1) if passing else 0.0

    def presence(self):
        return next((c for c in self.components if c.type == "presence"), None)

    def misses(self) -> int:
        p = self.presence()
        return p.presence_counts()[1] if p else 0

    # ---- lookup
    def item_names(self, it: dict, comp: Component) -> set:
        names = {it["name"], *it.get("aliases", [])}
        n = item_number(it["name"])
        if n is not None:
            names |= {f"{p}{n}" for p in comp.raw.get("prefixes", [])}
        if len(comp.counted) == 1:
            names |= {comp.short, comp.id, *comp.raw.get("aliases", [])}
        return {norm(s) for s in names if norm(s)}

    def find_item(self, query: str, create: bool = False):
        """Return (component, item, created)."""
        q = norm(query)
        hits = [(c, it) for c in self.components if c.type != "better" for it in c.items
                if q in self.item_names(it, c)]
        if len(hits) == 1:
            return hits[0][0], hits[0][1], False
        if len(hits) > 1:
            exact = [(c, it) for c, it in hits if norm(it["name"]) == q]
            if len(exact) == 1:
                return exact[0][0], exact[0][1], False
            raise GradeError(f"'{query}' matches several {self.code} items: "
                             + ", ".join(it["name"] for _, it in hits))
        if create:
            n = item_number(query)
            for c in self.components:
                tpl = c.raw.get("new_item")
                if not tpl or n is None:
                    continue
                prefixes = list(c.raw.get("prefixes", [])) + [re.sub(r"\{n+\}", "", tpl).strip()]
                if any(norm(f"{p}{n}") == q for p in prefixes):
                    it = {"name": fill(tpl, {"n": n, "nn": f"{n:02d}"})}
                    c.items.append(it)
                    c.items.sort(key=lambda i: item_number(i["name"]) or 0)
                    return c, it, True
        raise GradeError(f"No {self.code} item called '{query}'. Try one of: "
                         + ", ".join(it["name"] for c in self.components for it in c.items) + ".")

    # ---- math
    def values(self, x, rules: bool = True) -> dict:
        vals = {c.id: c.value(x) for c in self.components if c.type != "better"}
        rl = self.rules.get("replace_lowest")
        if rules and rl and self.misses() <= rl.get("max_misses", 10 ** 6) and vals.get(rl["with"]) is not None:
            known = [t for t in rl["tests"] if vals.get(t) is not None]
            if known:
                low = min(known, key=lambda t: vals[t])
                if vals[rl["with"]] > vals[low]:
                    vals[low] = vals[rl["with"]]
        for c in self.components:
            if c.type == "better":
                refs = [vals[r] for r in c.raw["of"] if vals.get(r) is not None]
                vals[c.id] = max(refs) if refs else None
        return vals

    def penalty(self, vals: dict) -> float:
        if self.raw.get("min_effect") != "deduct":
            return 0.0
        return sum(max(0.0, c.min - vals[c.id]) for c in self.components
                   if c.min is not None and vals.get(c.id) is not None)

    def gate_failed(self, vals: dict) -> bool:
        for gate in self.gates:
            for chk in gate.get("checks", []):
                if chk["type"] == "min" and vals.get(chk["component"]) is not None \
                        and vals[chk["component"]] < chk["min"]:
                    return True
                if chk["type"] == "avg_min":
                    vs = [vals.get(c) for c in chk["components"]]
                    if None not in vs and sum(vs) / len(vs) < chk["min"]:
                        return True
        return False

    def total(self, x, rules: bool = True) -> float:
        vals = self.values(x, rules)
        t = sum(c.weight * (vals[c.id] or 0.0) / 100 for c in self.components)
        if rules:
            t -= self.penalty(vals)
            if self.gate_failed(vals):
                t = min(t, self.fail_cap())
        return t

    def open_components(self):
        return [c for c in self.components if c.is_open()]

    def banked(self) -> float:
        """Points banked or on pace before any open work (x = 0, no rules)."""
        return self.total(0.0, rules=False)

    def open_weight(self) -> float:
        return self.total(1.0, rules=False) - self.total(0.0, rules=False)

    def need(self, target: float):
        """('need', pct) | ('locked', None) | ('out', best_possible) | ('done', final)."""
        if not self.open_components():
            return ("done", self.total(0.0))
        if self.total(0.0) >= target - 1e-9:
            return ("locked", None)
        top = self.total(1.0)
        if top < target - 1e-9:
            return ("out", top)
        lo, hi = 0.0, 1.0
        for _ in range(60):
            mid = (lo + hi) / 2
            if self.total(mid) >= target - 1e-9:
                hi = mid
            else:
                lo = mid
        return ("need", hi * 100)

    def graded_avg(self):
        pairs = [(c.weight, c.gauge()) for c in self.components]
        pairs = [(w, v) for w, v in pairs if v is not None]
        if not pairs:
            return None
        return sum(w * v for w, v in pairs) / sum(w for w, _ in pairs)

    def exemption(self):
        """Exemption status (e.g. skip the final with a test average above 90): dict or None."""
        ex = self.rules.get("exemption")
        if not ex:
            return None
        tests = [self.comp(t) for t in ex["tests"]]
        marks = [t.current() for t in tests]
        done = [m for m in marks if m is not None]
        remaining = [t for t, m in zip(tests, marks) if m is None]
        avg = sum(done) / len(done) if done else None
        low = min(done) if done else None
        avg_above = ex.get("avg_above", 0)
        need = (avg_above * len(tests) - sum(done)) / len(remaining) if remaining else None
        lost = []
        max_misses = ex.get("max_misses")
        if max_misses is not None and self.misses() > max_misses:
            lost.append(f"{self.misses()} missed classes (limit {max_misses})")
        each = ex.get("each_at_least")
        if each is not None and low is not None and low < each:
            lost.append(f"a test at {fpct(low)} (each must be ≥{fnum(each)}%)")
        if not remaining and avg is not None and avg <= avg_above:
            lost.append(f"test average {fpct(avg)} (needs >{fnum(avg_above)}%)")
        if remaining and need is not None and need > 100:
            lost.append(f"remaining tests would need >{fpct(need)}")
        state = "lost" if lost else ("earned" if not remaining else "open")
        return {"state": state, "avg": avg, "low": low, "need": need, "remaining": remaining,
                "lost": lost, "rule": ex}

    def summary(self) -> dict:
        """Everything the window, the report and `status --json` show for one course."""
        ga = self.graded_avg()
        return {
            "code": self.code, "title": self.title, "credits": self.credits,
            "graded_avg": ga, "graded_letter": self.letter(ga) if ga is not None else None,
            "banked": self.banked(), "open": self.open_weight(),
            "open_components": [f"{c.open_label()} ({fnum(c.open_weight(), 1)}%)" for c in self.open_components()],
            "targets": self.targets(),
            "need": {letter: self.need(t) for letter, t in self.targets()},
            "gates": [plain(self.gate_status(g)) for g in self.gates],
            "flags": [f"{it['name']}: {it['flag']}" for c in self.components for it in c.items
                      if it.get("flag") and not graded(it)],
        }

    # ---- gate status
    def gate_status(self, gate: dict) -> str:
        vals = self.values(None)
        out, unwritten = [], []
        for chk in gate.get("checks", []):
            kind = chk["type"]
            if kind == "min":
                v, c = vals.get(chk["component"]), self.comp(chk["component"])
                if v is None:
                    unwritten.append(lower(c.short))
                elif v >= chk["min"]:
                    out.append(f"✅ {lower(c.short)} {fpct(v)}")
                else:
                    out.append(f"❌ {lower(c.short)} {fpct(v)} — below {fnum(chk['min'])}%")
            elif kind == "avg_min":
                comps = [self.comp(c) for c in chk["components"]]
                vs = [vals.get(c.id) for c in comps]
                done = [v for v in vs if v is not None]
                names = " + ".join(lower(c.short) for c in comps)
                if not done:
                    unwritten += [lower(c.short) for c in comps]
                elif len(done) == len(vs):
                    avg = sum(done) / len(done)
                    out.append(f"{'✅' if avg >= chk['min'] else '❌'} {names} average {fpct(avg)}")
                else:
                    rest = [lower(c.short) for c, v in zip(comps, vs) if v is None]
                    req = (chk["min"] * len(vs) - sum(done)) / len(rest)
                    out.append(f"⬜ {' + '.join(rest)} needs ≥{fpct(max(req, 0))} to keep the {fnum(chk['min'])}% average")
            elif kind == "mins":
                parts, pending, icon = [], [], "🟢"
                for c in self.components:
                    if c.min is None:
                        continue
                    if c.type == "capped" and c.graded_items():
                        banked = 100 * c.earned() / c.raw["cap"]
                        state = "min met" if banked >= c.min else ("on pace" if (c.value(None) or 0) >= c.min else "behind")
                        if state == "behind":
                            icon = "🔴"
                        parts.append(f"{lower(c.short)} {c.fpts(c.earned())}/{fnum(c.raw['cap'])} ({state})")
                        continue
                    v = c.current()
                    if v is None:
                        pending.append(lower(c.short))
                    elif v < c.min:
                        icon = "🔴"
                        parts.append(f"{lower(c.short)} {fpct(v)} — {fnum(c.min - v, 1)} pts under the {fnum(c.min)}% minimum")
                    else:
                        parts.append(f"{lower(c.short)} {fpct(v)}")
                if pending:
                    parts.append(", ".join(pending) + " still to come")
                out.append(icon + " " + " · ".join(parts))
            elif kind == "submitted":
                for cid in chk["components"]:
                    c = self.comp(cid)
                    flagged = [it for it in c.counted if it.get("flag") and not graded(it)]
                    missed = [it for it in c.counted if it.get("missed")]
                    g = len(c.graded_items()) - len(missed)
                    bits = [f"⚠️ {it['name']} {it['flag']} — confirm it went in" for it in flagged]
                    bits += [f"❌ {it['name']} recorded as missed" for it in missed]
                    bits.append(f"{'✅ ' if not bits else ''}{g} of {len(c.counted)} {c.short} graded")
                    out.append(" · ".join(bits))
            elif kind == "touched":
                touched = [c.short for c in self.components if c.graded_items()]
                untouched = [lower(c.short) for c in self.components if not c.graded_items()]
                if not untouched:
                    out.append("✅ every category started")
                else:
                    out.append(f"🟡 {' + '.join(lower(t) for t in touched) or 'nothing'} opened · "
                               f"{', '.join(untouched)} still at zero")
        order = [lower(c.short) for c in self.components]
        seen = sorted(set(unwritten), key=order.index)
        if seen:
            out.insert(0, f"⬜ {' + '.join(seen)} not written yet")
        return " · ".join(out) if out else "⬜"


class GradeBook:
    """Every current course plus the library's settings: term name, letter scale, target letters."""

    def __init__(self, data: dict):
        self.data = data
        self.scale = scale_rows(data.get("scale") or DEFAULT_SCALE)
        self.target_spec = data.get("targets") or []
        self.grade_points = {letter: pts for _, letter, pts in self.scale if pts is not None}
        self.courses = [Course(c, self) for c in data.get("courses", [])]
        for c in self.courses:   # a course's own scale may add letters the library's doesn't have
            for _, letter, pts in c.scale:
                if pts is not None:
                    self.grade_points.setdefault(letter, pts)

    @property
    def term(self) -> str:
        return self.data.get("term") or "This term"

    def letter(self, pct: float) -> str:
        for floor, letter, _ in self.scale:
            if pct >= floor - 1e-9:
                return letter
        return self.scale[-1][1]

    def points(self, letter):
        """Grade points for a letter: 'A-' -> 3.7. None for a W or no grade; unknown letters raise."""
        if letter is None or str(letter).upper() in NO_GPA:
            return None
        if letter not in self.grade_points:
            raise GradeError(f"'{letter}' is not a letter on the scale: "
                             + ", ".join(list(self.grade_points) + sorted(NO_GPA)) + ".")
        return self.grade_points[letter]

    def letter_choices(self) -> list:
        return list(self.grade_points) + ["W"]

    def course(self, query: str) -> Course:
        q = norm(query)
        for c in self.courses:
            keys = {norm(c.code), norm(c.key), norm(c.title), *(norm(a) for a in c.raw.get("aliases", []))}
            digits = re.sub(r"\D", "", c.code)
            if q in keys or (q.isdigit() and q == digits):
                return c
        partial = [c for c in self.courses if q and (norm(c.code).startswith(q) or q in norm(c.title))]
        if len(partial) == 1:
            return partial[0]
        if not self.courses:
            raise GradeError("No courses yet: add one from your syllabus first.")
        raise GradeError(f"No course '{query}'. Courses: " + ", ".join(c.code for c in self.courses) + ".")


# --------------------------------------------------------------------------- operations

class Change:
    def __init__(self, course: Course, comp: Component, item: dict, entry: str, message: str, note: str = ""):
        self.course, self.comp, self.item = course, comp, item
        self.entry, self.message, self.note = entry, message, note


def where_phrase(course: Course) -> str:
    opens = [c for c in course.open_components() if c.type != "better"]
    return " + ".join(c.open_label() for c in opens) if len(opens) <= 2 else f"the remaining {len(opens)} assessments"


def need_phrase(course: Course) -> str:
    letter, target = course.targets()[0]
    kind, v = course.need(target)
    if kind == "need":
        return f"{letter} now needs ~{fpct(v)} across {where_phrase(course)}"
    if kind == "locked":
        return f"{letter} locked in"
    if kind == "out":
        return f"{letter} out of reach (max ~{fpct(v)})"
    return f"finished at {fpct(v)}"


def need_cell(result) -> str:
    kind, v = result
    if kind == "locked":
        return "locked in"
    if kind == "out":
        return f"out of reach (max ~{fpct(v)})"
    if kind == "done":
        return f"finished at {fpct(v)}"
    return f"~{fpct(v)}"


def log_mark(book: GradeBook, course_q: str, item_q: str, mark: str, date: str = None, note: str = None,
             desc: str = None, when: dt.date = None) -> Change:
    """Store a mark on the book (in memory) and append a log row."""
    when = when or dt.date.today()
    course = book.course(course_q)
    comp, item, created = course.find_item(item_q, create=True)
    fields = parse_mark(comp, item, mark)
    before = comp.item_mark(item) if graded(item) else None
    for k in MARK_KEYS:
        item.pop(k, None)
    item.pop("flag", None)
    item.pop("pending", None)
    item.update(fields)
    if desc:
        item["desc"] = desc
    item["logged"] = date or short_date(when)
    label = fill(comp.raw.get("log_label", "{name}"), comp.fields(item))
    shown = comp.item_mark(item)
    if comp.unit == "presence":
        free = comp.raw.get("free_misses")
        shown = (f"{shown} (present)" if item.get("present")
                 else "missed" + (f" ({course.misses()} of {free} free misses used)" if free else ""))
    elif comp.type == "capped":
        shown = f"{shown} ({comp.fpts(comp.earned())} / {fnum(comp.raw['cap'])} banked)"
    if before:
        shown += f" (was {before})"
    if note:
        shown += f" · {note}"
    entry = f"{course.code} {label} {comp.item_short(item)}{', ' + note if note else ''}; {need_phrase(course)}"
    book.data.setdefault("log", []).append({
        "date": date or short_date(when), "course": course.code, "item": label, "mark": shown,
        "ref": f"{course.key}/{comp.id}/{item['name']}", "entry": entry})
    return Change(course, comp, item, entry, f"{course.code}: log {label}", note or "")


def clear_mark(book: GradeBook, course_q: str, item_q: str) -> Change:
    course = book.course(course_q)
    comp, item, _ = course.find_item(item_q)
    if not graded(item):
        raise GradeError(f"{course.code} {item['name']} has no mark to clear.")
    for k in MARK_KEYS + ("logged",):
        item.pop(k, None)
    ref = f"{course.key}/{comp.id}/{item['name']}"
    log = book.data.get("log", [])
    entry = None
    for i in range(len(log) - 1, -1, -1):
        if log[i].get("ref") == ref:
            entry = log.pop(i).get("entry")
            break
    label = fill(comp.raw.get("log_label", "{name}"), comp.fields(item))
    return Change(course, comp, item, entry, f"{course.code}: clear {label}")


def flag_item(book: GradeBook, course_q: str, item_q: str, text: str = None) -> Change:
    course = book.course(course_q)
    comp, item, _ = course.find_item(item_q)
    if text:
        item["flag"] = text
        entry = f"{course.code} {item['name']} flagged: {text}"
        msg = f"{course.code}: flag {item['name']}"
    else:
        item.pop("flag", None)
        entry = f"{course.code} {item['name']} flag cleared"
        msg = f"{course.code}: unflag {item['name']}"
    return Change(course, comp, item, entry, msg)


def item_hint(comp: Component, item: dict) -> str:
    if comp.unit == "presence":
        return "present, missed, or the score shown (e.g. 3.5/10)"
    if comp.unit == "pts":
        if item.get("max"):
            return f"points out of {fnum(item['max'], 2, True)} (e.g. {fnum(item['max'])}), or missed"
        return "points earned (e.g. 4 or 4/4), or missed"
    return "percent (85 or 85%) or score (17/20), or missed"


# --------------------------------------------------------------------------- GPA

SEASONS = {"fall": ("sepdec", "autumn"), "autumn": ("sepdec", "fall"), "winter": ("janapr",),
           "spring": ("mayjun", "janmay"), "summer": ("mayjun", "julaug", "mayaug")}
SOURCE_TEXT = {
    "term GPA": "letter deduced from the term GPA, not read off the transcript",
    "sheet": "letter taken from a grade sheet, not read off the transcript",
}
SOURCE_SHORT = {"term GPA": "from term GPA", "sheet": "from sheet"}


def gpa_of(qp: float, hours: float):
    return qp / hours if hours else None


def fgpa(v) -> str:
    """'3.72', or '—' when there are no GPA hours."""
    return fixed(v, 2) if v is not None else "—"


def r2(v):
    """A GPA or point total for JSON, rounded the way transcripts show it."""
    return float(fixed(v, 2)) if v is not None else None


def subject_of(code: str) -> str:
    """'BIOL 1010' -> 'BIOL', 'MATH-201' -> 'MATH'."""
    m = re.match(r"\s*([A-Za-z]+)", code or "")
    return m[1].upper() if m else "Other"


class PastCourse:
    """One course on the transcript."""

    def __init__(self, raw: dict, term: "PastTerm", book: GradeBook, credits: float):
        self.raw, self.term = raw, term
        self.code, self.title = raw["code"], raw.get("title", "")
        self.letter = raw.get("letter")
        self.credits = float(raw.get("credits", credits))
        self.source = raw.get("letter_source")
        self.components = raw.get("components", [])
        try:
            self.points = book.points(self.letter)
        except GradeError as e:
            raise GradeError(f"{term.name} {self.code}: {e}") from None

    @property
    def counted(self) -> bool:
        return self.points is not None and self.credits > 0

    @property
    def hours(self) -> float:
        return self.credits if self.counted else 0.0

    @property
    def qp(self) -> float:
        return self.points * self.credits if self.counted else 0.0

    @property
    def subject(self) -> str:
        return subject_of(self.code)

    @property
    def sheet_letter(self):
        return self.raw.get("sheet_letter")

    def letter_text(self) -> str:
        return self.letter if self.letter is not None else "no grade recorded"

    def sheet_differs(self) -> bool:
        return bool(self.sheet_letter and self.letter and self.sheet_letter != self.letter)

    def warns(self) -> bool:
        """A discrepancy worth a warning sign, not just a note on where the letter came from."""
        return self.letter is None or self.sheet_differs()

    def flags(self, short: bool = False) -> list:
        """Warnings for lists: where the letter came from, or why it doesn't count."""
        out = []
        if self.letter is None:
            out.append("no grade" if short else "no grade recorded")
        elif str(self.letter).upper() in NO_GPA:
            out.append("no GPA effect")
        if self.source in SOURCE_SHORT:
            out.append(SOURCE_SHORT[self.source] if short else "letter " + SOURCE_SHORT[self.source])
        if self.sheet_differs():
            out.append(f"sheet said {self.sheet_letter}")
        return out


class PastTerm:
    def __init__(self, raw: dict, book: GradeBook, credits: float):
        self.raw = raw
        self.name, self.code = raw["term"], raw.get("code")
        self.courses = [PastCourse(c, self, book, credits) for c in raw.get("courses", [])]
        self.recorded_gpa = raw.get("term_gpa")
        self.recorded_cumulative = raw.get("cumulative_gpa")
        self.cum_hours = self.cum_qp = 0.0  # running totals through this term, set by Transcript
        self.before = None                  # the cumulative GPA going into this term

    @property
    def hours(self) -> float:
        return sum(c.hours for c in self.courses)

    @property
    def qp(self) -> float:
        return sum(c.qp for c in self.courses)

    @property
    def gpa(self):
        return gpa_of(self.qp, self.hours)

    @property
    def cumulative(self):
        return gpa_of(self.cum_qp, self.cum_hours)

    def gpa_text(self) -> str:
        """'3.20', 'no grade recorded', or 'no GPA hours' for a term with nothing that counts."""
        if self.gpa is not None:
            return fixed(self.gpa, 2)
        if any(c.letter is None for c in self.courses):
            return "no grade recorded"
        return "no GPA hours"

    def keys(self) -> set:
        """'Fall 2025' answers to fall2025 and autumn2025, plus its own code if it has one."""
        k = norm(self.name)
        keys = {k} | ({norm(self.code)} if self.code else set())
        m = re.match(r"([a-z]+)(\d{4})$", k)
        if m:
            keys |= {alias + m[2] for alias in SEASONS.get(m[1], ())}
            keys |= {season + m[2] for season, names in SEASONS.items() if m[1] in names}
        return keys


class Transcript:
    """Every past term, with every GPA recomputed from the letters."""

    def __init__(self, data: dict, book: GradeBook):
        self.data, self.book = data or {}, book
        self.standing = self.data.get("standing", {}) or {}
        credits = float(self.data.get("default_credits", DEFAULT_CREDITS))
        self.terms = [PastTerm(t, book, credits) for t in self.data.get("terms", [])]
        hours = qp = 0.0
        for t in self.terms:
            t.before = gpa_of(qp, hours)
            hours, qp = hours + t.hours, qp + t.qp
            t.cum_hours, t.cum_qp = hours, qp
        self.hours, self.qp = hours, qp

    @property
    def gpa(self):
        return gpa_of(self.qp, self.hours)

    @property
    def empty(self) -> bool:
        return not self.terms

    def courses(self) -> list:
        return [c for t in self.terms for c in t.courses]

    def mismatches(self) -> list:
        """Every GPA figure the transcript records that the letters don't reproduce (empty = all agree)."""
        out = []
        checks = [("Credit hours", self.hours, self.standing.get("credit_hours")),
                  ("Quality points", self.qp, self.standing.get("quality_points")),
                  ("Cumulative GPA", self.gpa, self.standing.get("cumulative_gpa"))]
        for t in self.terms:
            checks += [(f"{t.name} term GPA", t.gpa, t.recorded_gpa),
                       (f"{t.name} cumulative GPA", t.cumulative, t.recorded_cumulative)]
        for label, mine, theirs in checks:
            if theirs is not None and (mine is None or fixed(mine) != fixed(theirs)):
                out.append(f"{label}: the letters give {fgpa(mine)}, the transcript says {fixed(theirs)}")
        return out

    def term(self, query: str) -> PastTerm:
        q = norm(query)
        hits = [t for t in self.terms if q in t.keys()]
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise GradeError(f"'{query}' matches several terms: " + ", ".join(t.name for t in hits) + ".")
        if not self.terms:
            raise GradeError("No past terms yet: import a transcript first.")
        raise GradeError(f"No past term '{query}'. Terms: " + ", ".join(t.name for t in self.terms) + ".")

    def find_courses(self, query: str, within: list = None) -> list:
        q = norm(query)
        return [c for c in (within if within is not None else self.courses())
                if q == norm(c.code) or (q.isdigit() and q == re.sub(r"\D", "", c.code))]

    def resolve(self, query: str, course: str = None):
        """`term` arguments -> (term, course or None). QUERY may be a term or a past course code."""
        try:
            term = self.term(query)
        except GradeError:
            hits = self.find_courses(query)
            if course is not None or not hits:
                raise
            if len(hits) > 1:
                raise GradeError(f"{hits[0].code} was taken in " + ", ".join(c.term.name for c in hits)
                                 + ": name the term too.") from None
            return hits[0].term, hits[0]
        if course is None:
            return term, None
        hits = self.find_courses(course, term.courses)
        if not hits:
            raise GradeError(f"No {course} in {term.name}. Courses: " + ", ".join(c.code for c in term.courses) + ".")
        return term, hits[0]

    def by_subject(self, limit: int = 4) -> list:
        """The most-taken subjects, then Other: graded courses in order taken, hours, quality points, GPA."""
        graded_courses = [c for c in self.courses() if c.counted]
        counts = Counter(c.subject for c in graded_courses)
        top = [s for s, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])) if n >= 2][:limit]
        rows = []
        for s in top + ["Other"]:
            cs = [c for c in graded_courses if (c.subject == s if s != "Other" else c.subject not in top)]
            hours, qp = sum(c.hours for c in cs), sum(c.qp for c in cs)
            rows.append({"subject": s, "courses": [(c.code, c.letter) for c in cs],
                         "hours": hours, "qp": qp, "gpa": gpa_of(qp, hours)})
        return rows


def pace(book: GradeBook) -> list:
    """The term in progress, each course at the letter of its graded work so far."""
    rows = []
    for c in book.courses:
        ga = c.graded_avg()
        fallback = c.targets()[0][0]
        rows.append({"code": c.code, "title": c.title, "credits": c.credits,
                     "graded_avg": ga, "letter": c.letter(ga) if ga is not None else fallback,
                     "assumed": ga is None})
    return rows


def project(transcript: Transcript, picks) -> dict:
    """picks: [(credits, letter)] for the term in progress -> its GPA and the new cumulative."""
    hours = qp = 0.0
    for credits, letter in picks:
        p = transcript.book.points(letter)
        if p is not None:
            hours, qp = hours + credits, qp + credits * p
    return {"term_hours": hours, "term_qp": qp, "term_gpa": gpa_of(qp, hours),
            "hours": transcript.hours + hours, "qp": transcript.qp + qp,
            "cumulative": gpa_of(transcript.qp + qp, transcript.hours + hours)}


def what_if(book: GradeBook, specs) -> dict:
    """['A-', 'BIOL 1010=B+'] -> {course code: letter}. A bare letter sets every course; COURSE=LETTER wins."""
    out = {}
    for bare in (True, False):
        for spec in specs or []:
            course_q, sep, letter = spec.rpartition("=")
            if bool(sep) == bare:
                continue
            letter = letter.strip().upper()
            book.points(letter)  # unknown letters raise
            for code in [c.code for c in book.courses] if bare else [book.course(course_q.strip()).code]:
                out[code] = letter
    return out


# --------------------------------------------------------------------------- text reports

def gpa_report(tr: Transcript, overrides: dict = None) -> str:
    book, st = tr.book, tr.standing
    top = fnum(max(book.grade_points.values() or [4.0]), 1)
    lines = [f"Cumulative GPA {fgpa(tr.gpa)} / {top} · {fnum(tr.hours)} credit hours · {fixed(tr.qp)} quality points"
             + (f"  ({st['as_of']})" if st.get("as_of") else "")]
    if tr.empty:
        lines[0] += "  (no transcript imported yet)"
    lines += [f"  warning: {m}" for m in tr.mismatches()]
    subjects = [row for row in tr.by_subject() if row["courses"]]
    if subjects:
        lines += ["", "By subject"]
    for row in subjects:
        n = len(row["courses"])
        if row["subject"] == "Other":
            detail = ", ".join(sorted({subject_of(code) for code, _ in row["courses"]}))
        else:
            detail = " · ".join(f"{code.split()[-1]} {letter}" for code, letter in row["courses"])
        lines.append(f"  {row['subject']:<6} {fgpa(row['gpa'])}  {n:>2} course{'s' if n != 1 else ' '}  {detail}")
    rows = pace(book)
    if not rows:
        return "\n".join(lines)
    at_pace = project(tr, [(r["credits"], r["letter"]) for r in rows])
    lines += ["", f"{book.term} projection: {fnum(at_pace['term_hours'])} hours on top of {fnum(tr.hours)}"]
    lines.append(f"  {'Course':<12}{'Graded so far':<15}{'Pace':<13}" + ("What-if" if overrides else ""))
    for r in rows:
        ga = fpct(r["graded_avg"]) if r["graded_avg"] is not None else "nothing back"
        p = r["letter"] + (" (assumed)" if r["assumed"] else "")
        lines.append(f"  {r['code']:<12}{ga:<15}{p:<13}" + (overrides.get(r["code"], r["letter"]) if overrides else ""))

    def outcome(label, res):
        return (f"  {label:<9} term GPA {fgpa(res['term_gpa'])} · cumulative {fgpa(res['cumulative'])}"
                f" on {fnum(res['hours'])} h ({fixed(res['qp'])} quality points)")
    lines.append(outcome("At pace:", at_pace))
    if overrides:
        lines.append(outcome("What-if:", project(tr, [(r["credits"], overrides.get(r["code"], r["letter"])) for r in rows])))
    same = " · ".join(f"{l} {fgpa(project(tr, [(r['credits'], l) for r in rows])['cumulative'])}"
                      for l in list(book.grade_points)[:5])
    lines.append(f"  The same letter in every course gives a cumulative of: {same}")
    return "\n".join(lines)


def terms_report(tr: Transcript) -> str:
    if tr.empty:
        return "No past terms yet: import a transcript (see `prompt transcript`)."
    lines = [f"{'Term':<16}{'Courses':>8}{'Hours':>7}  {'Term GPA':<19}{'Cumulative':>10}"]
    for t in tr.terms:
        skipped = ", ".join(f"{c.code} {c.letter_text()}" for c in t.courses if not c.counted)
        lines.append(f"{t.name:<16}{len(t.courses):>8}{fnum(t.hours):>7}  {t.gpa_text():<19}{fgpa(t.cumulative):>10}"
                     + (f"   {skipped}" if skipped else ""))
    lines.append(f"Overall: {fnum(tr.hours)} credit hours · {fixed(tr.qp)} quality points · "
                 f"cumulative GPA {fgpa(tr.gpa)}")
    lines += [f"warning: {m}" for m in tr.mismatches()]
    return "\n".join(lines)


def term_report(t: PastTerm) -> str:
    n = len(t.courses)
    lines = [t.name + (f" ({t.code})" if t.code else ""),
             f"  {n} course{'s' if n != 1 else ''} · {fnum(t.hours)} credit hours · {fixed(t.qp)} quality points",
             f"  Term GPA {t.gpa_text()} · cumulative "
             + (f"{fgpa(t.before)} → " if t.before is not None and t.hours else "") + fgpa(t.cumulative), ""]
    for c in t.courses:
        pts = f"{c.points:.1f}" if c.counted else ""
        lines.append(f"  {c.code:<10} {c.letter or '—':<3}{pts:>5}  {c.title}")
        if c.flags():
            lines.append(" " * 23 + ("[!] " if c.warns() else "") + "; ".join(c.flags()))
    return "\n".join(lines)


def past_course_report(c: PastCourse) -> str:
    """A past course's breakdown, for `term ... COURSE` and the window's Past semesters view."""
    lines = [f"{c.code} — {c.title}" if c.title else c.code]
    meta = [c.term.name, f"{fnum(c.credits)} credit hours"]
    if c.raw.get("instructor"):
        meta.append(f"instructor {c.raw['instructor']}")
    lines.append("  " + " · ".join(meta))
    if c.letter is None:
        lines.append("  Letter: no grade recorded, so it isn't in the GPA")
    elif not c.counted:
        lines.append(f"  Letter: {c.letter}, no credit hours and no GPA effect")
    else:
        lines.append(f"  Letter: {c.letter} = {c.points:.1f} points × {fnum(c.credits)} credits"
                     f" = {fixed(c.qp)} quality points")
    if c.source in SOURCE_TEXT:
        lines.append(f"  Source: {SOURCE_TEXT[c.source]}")
    if c.sheet_differs():
        pct = c.raw.get("tracked_percent")
        lines.append(f"  [!] The grade sheet showed {c.sheet_letter}" + (f" ({fpct(pct, 2)})" if pct is not None else "")
                     + f"; the official letter is {c.letter}.")
    if c.components:
        lines += ["", f"  {'Component':<21}{'Weight':>8}{'Mark':>9}{'Earned':>9}"]
        for x in c.components:
            mark = fpct(x["mark"], 2) if x.get("mark") is not None else "—"
            earned = fnum(x["earned"], 2) if x.get("earned") is not None else "—"
            if not x.get("counted", True):
                earned = "excluded"
            lines.append(f"  {str(x['name'])[:20]:<21}{fpct(x['weight'], 2):>8}{mark:>9}{earned:>9}")
        if c.raw.get("bonus"):
            lines.append(f"  {'Bonus':<21}{'':>8}{'':>9}{'+' + fnum(c.raw['bonus'], 2):>9}")
        total = c.raw.get("computed_total")
        if total is not None:
            weight = sum(x["weight"] for x in c.components if x.get("counted", True))
            lines.append(f"  {'Total':<21}{fpct(weight, 2):>8}{'':>9}{fnum(total, 2):>9}")
    if c.raw.get("note"):
        lines.append(f"  Note: {c.raw['note']}")
    return "\n".join(lines)


def past_course_json(c: PastCourse) -> dict:
    return {**c.raw, "term": c.term.name, "points": c.points, "counted": c.counted,
            "quality_points": r2(c.qp), "flags": c.flags()}


def term_json(t: PastTerm, courses: bool = True) -> dict:
    return {"term": t.name, "code": t.code,
            "courses": [past_course_json(c) for c in t.courses] if courses else len(t.courses),
            "credit_hours": num(t.hours), "quality_points": r2(t.qp), "term_gpa": r2(t.gpa),
            "cumulative_gpa": r2(t.cumulative), "term_gpa_text": t.gpa_text()}


def gpa_json(tr: Transcript, overrides: dict = None) -> dict:
    rows = pace(tr.book)
    at_pace = project(tr, [(r["credits"], r["letter"]) for r in rows])
    picked = project(tr, [(r["credits"], overrides.get(r["code"], r["letter"])) for r in rows]) if overrides else None

    def rounded(res):
        return {k: (num(v) if k.endswith("hours") else r2(v)) for k, v in res.items()} if res else None
    return {
        "standing": {"credit_hours": num(tr.hours), "quality_points": r2(tr.qp), "cumulative_gpa": r2(tr.gpa),
                     "as_of": tr.standing.get("as_of")},
        "mismatches": tr.mismatches(),
        "subjects": [{**s, "courses": [list(c) for c in s["courses"]], "hours": num(s["hours"]),
                      "qp": r2(s["qp"]), "gpa": r2(s["gpa"])} for s in tr.by_subject() if s["courses"]],
        "projection": {"term": tr.book.term,
                       "courses": [{**r, "what_if": (overrides or {}).get(r["code"])} for r in rows],
                       "pace": rounded(at_pace), "what_if": rounded(picked)},
    }


def course_report(course: Course, brief: bool = False) -> str:
    s = course.summary()
    lines = [f"{course.code} — {course.title}" if course.title else course.code]
    ga = s["graded_avg"]
    lines.append(f"  Graded work: {fpct(ga)} ({s['graded_letter']})" if ga is not None else "  Graded work: nothing back yet")
    if not brief:
        for c in course.components:
            mn = f" min {fnum(c.min)}%" if c.min is not None else ""
            lines.append(f"  {c.short[:22]:<22}{fnum(c.weight) + '%':>5}  {plain(c.standing_text()):<30} "
                         f"{c.projection_text()}{mn}")
    opens = " + ".join(s["open_components"])
    if opens:
        needs = " · ".join(f"{l} {need_cell(v).replace('~', '')}" for l, v in s["need"].items())
        lines.append(f"  Needed on {opens}:  {needs}")
        lines.append(f"  Basis: {fnum(s['banked'], 1)} pts banked or on pace, {fnum(s['open'], 1)}% open")
    else:
        lines.append(f"  Final: {need_cell(next(iter(s['need'].values())))}"
                     + (f" ({course.letter(course.total(0.0))})" if s["need"] else ""))
    ex = course.exemption()
    if ex:
        grade = ex["rule"].get("grade")
        lines.append(f"  Exemption{' (' + grade + ')' if grade else ''}: {ex['state']}"
                     + (f" ({'; '.join(ex['lost'])})" if ex["lost"] else ""))
    for g in s["gates"]:
        lines.append(f"  Requirement: {g}")
    for f in s["flags"]:
        lines.append(f"  Flag: {f}")
    for n in course.raw.get("notes", []) if not brief else []:
        lines.append(f"  Note: {n}")
    return "\n".join(lines)
