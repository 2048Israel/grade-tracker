"""Reading the files your AI makes from a syllabus or a transcript, checking them, and adding them.

The formats are spelled out in data/syllabus-prompt.md and data/transcript-prompt.md, the
prompts you give your AI. This module is forgiving about how the JSON arrives (inside a ```json
block, with chatter around it, with a trailing comma) and strict about what's in it: every error
names the course and component it's in, so you can fix it or ask your AI to.
"""
from __future__ import annotations

import copy
import json
import re

from .engine import (MARK_KEYS, NO_GPA, ROLE_ALIASES, TYPE_ALIASES, TYPES, GradeBook, GradeError, Transcript,
                     fnum, graded, lower, norm, scale_rows)

FORMAT_COURSE = "grade-tracker-course"
FORMAT_COURSES = "grade-tracker-courses"
FORMAT_TRANSCRIPT = "grade-tracker-transcript"
CHECK_TYPES = ("min", "avg_min", "submitted", "mins", "touched")
ROLES = ("solve", "project")
KEEP_ON_ITEM = MARK_KEYS + ("logged", "flag", "desc")


# --------------------------------------------------------------------------- getting the JSON out

FENCE_RE = re.compile(r"```[a-zA-Z]*\s*\n(.*?)```", re.S)


def _loose(text: str) -> str:
    """Undo the usual chat-reply damage: smart quotes, // comments, trailing commas."""
    text = text.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    text = re.sub(r"(?m)^\s*//.*$", "", text)
    return re.sub(r",(\s*[}\]])", r"\1", text)


def extract_json(text: str):
    """The JSON object in a pasted reply or a file. Raises GradeError with a hint if there isn't one."""
    text = (text or "").lstrip("﻿").strip()
    if not text:
        raise GradeError("Nothing to read: paste your AI's reply, or open the file it gave you.")
    candidates = [m.group(1) for m in FENCE_RE.finditer(text)] + [text]
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if starts:
        start = min(starts)
        end = max(text.rfind("}"), text.rfind("]"))
        if end > start:
            candidates.append(text[start:end + 1])
    first_error = None
    for strict in (True, False):
        for cand in candidates:
            try:
                return json.loads(cand if strict else _loose(cand))
            except json.JSONDecodeError as e:
                first_error = first_error or e
    hint = (" Copy the whole reply, from the first { to the last }. If the AI stopped part-way, ask it to"
            " \"send the complete JSON again\".")
    if first_error:
        raise GradeError(f"That isn't valid JSON (line {first_error.lineno}, column {first_error.colno}:"
                         f" {first_error.msg}).{hint}")
    raise GradeError("No JSON found." + hint)


# --------------------------------------------------------------------------- checking a course

def _number(v, what: str, errors: list, lo=None, hi=None, required=True):
    """'20', '20%', 20 -> 20.0; records an error and returns None otherwise."""
    if v is None:
        if required:
            errors.append(f"{what} is missing.")
        return None
    if isinstance(v, bool):
        errors.append(f"{what} must be a number, not {v}.")
        return None
    if isinstance(v, str):
        s = v.strip().rstrip("%").strip()
        try:
            v = float(s)
        except ValueError:
            errors.append(f"{what} must be a number, not '{v}'.")
            return None
    if not isinstance(v, (int, float)):
        errors.append(f"{what} must be a number.")
        return None
    v = float(v)
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        errors.append(f"{what} is {fnum(v)}; it must be between {fnum(lo)} and {fnum(hi)}."
                      if hi is not None else f"{what} is {fnum(v)}; it can't be below {fnum(lo)}.")
        return None
    return int(v) if v.is_integer() else v


def slug(s: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")
    return out or "part"


def singular(label: str) -> str:
    """'Quizzes' -> 'Quiz', 'Labs' -> 'Lab', 'Activities' -> 'Activity', 'Homework' -> 'Homework'."""
    base = re.sub(r"\s*\(.*?\)", "", label).strip()
    for end, repl in (("zzes", "z"), ("ies", "y"), ("sses", "ss"), ("ches", "ch"), ("s", "")):
        if base.lower().endswith(end) and not base.lower().endswith("ss"):
            return base[: len(base) - len(end)] + repl
    return base


def check_scale(raw, where: str, errors: list):
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        errors.append(f"{where}: \"scale\" must be a list like [[90, \"A+\", 4.3], [85, \"A\", 4.0], ...].")
        return None
    out, letters = [], set()
    for i, row in enumerate(raw, start=1):
        if isinstance(row, dict):
            row = [row.get("min", row.get("floor")), row.get("letter"), row.get("points")]
        if not isinstance(row, list) or len(row) < 2:
            errors.append(f"{where}: scale row {i} must be [lowest percent, letter, grade points].")
            continue
        floor = _number(row[0], f"{where}: scale row {i} percent", errors, 0, 100)
        letter = str(row[1]).strip() if row[1] is not None else ""
        pts = _number(row[2], f"{where}: scale row {i} grade points", errors, 0, 10, required=False) \
            if len(row) > 2 else None
        if not letter:
            errors.append(f"{where}: scale row {i} has no letter.")
            continue
        if letter in letters:
            errors.append(f"{where}: the scale lists {letter} twice.")
        letters.add(letter)
        if floor is not None:
            out.append([floor, letter] + ([pts] if pts is not None else []))
    out.sort(key=lambda r: -r[0])
    if out and out[-1][0] > 0:
        errors.append(f"{where}: the scale's lowest letter must start at 0 (e.g. [0, \"F\", 0]).")
    return out


def check_course(raw, where: str = "course"):
    """-> (normalized course, errors, warnings). The input is left alone."""
    errors, warnings = [], []
    if not isinstance(raw, dict):
        return None, [f"{where} must be an object {{...}}."], []
    c = copy.deepcopy(raw)
    for k in ("format", "version"):
        c.pop(k, None)
    code = str(c.get("code") or "").strip()
    title = str(c.get("title") or "").strip()
    if not code:
        if title:
            code = title
            warnings.append(f"{title}: no course code in the file, so the title is used as the code.")
        else:
            return None, [f"{where} has no \"code\" (like \"BIOL 1010\") and no \"title\"."], []
    c["code"], c["title"] = code, title
    where = code
    c["key"] = norm(code) or slug(code)
    if "credits" in c:
        cr = _number(c["credits"], f"{where}: credits", errors, 0, 30)
        if cr is not None:
            c["credits"] = cr
    if "scale" in c:
        c["scale"] = check_scale(c["scale"], where, errors)
        if not c["scale"]:
            c.pop("scale")
    if isinstance(c.get("notes"), str):
        c["notes"] = [c["notes"]]
    if c.get("notes") is not None and not (isinstance(c["notes"], list) and all(isinstance(n, str) for n in c["notes"])):
        errors.append(f"{where}: \"notes\" must be a list of sentences.")
    if c.get("min_effect") not in (None, "deduct", "fail"):
        errors.append(f"{where}: \"min_effect\" can only be \"deduct\" (leave it out otherwise).")
    if c.get("min_effect") == "fail":
        c.pop("min_effect")

    comps = c.get("components")
    if not isinstance(comps, list) or not comps:
        errors.append(f"{where}: \"components\" must list the graded parts of the course (labs, midterm, final...).")
        return c, errors, warnings

    ids, names, gates = set(), {}, list(c.get("gates") or [])
    for i, comp in enumerate(comps, start=1):
        if not isinstance(comp, dict):
            errors.append(f"{where}: component {i} must be an object {{...}}.")
            continue
        label = str(comp.get("label") or comp.get("name") or comp.get("id") or "").strip()
        if not label:
            errors.append(f"{where}: component {i} has no \"label\".")
            label = f"Part {i}"
        comp.pop("name", None)
        comp["label"] = label
        cid = slug(comp.get("id") or label)
        while cid in ids:
            cid += "_2"
        comp["id"] = cid
        ids.add(cid)
        at = f"{where} → {label}"
        w = _number(comp.get("weight"), f"{at}: weight", errors, 0, 100)
        comp["weight"] = w if w is not None else 0
        kind = str(comp.get("type", "average")).strip().lower()
        kind = TYPE_ALIASES.get(kind, kind)
        if kind not in TYPES:
            errors.append(f"{at}: type '{comp.get('type')}' isn't one of: {', '.join(TYPES)}.")
            kind = "average"
        comp["type"] = kind
        if "role" in comp:
            role = str(comp["role"]).strip().lower()
            role = ROLE_ALIASES.get(role, role)
            if role not in ROLES:
                errors.append(f"{at}: role '{comp['role']}' must be \"exam\" or \"coursework\".")
            comp["role"] = role

        # items
        items = comp.get("items")
        count = comp.pop("count", None)
        base = str(comp.pop("item_name", "") or singular(label))
        if items is None:
            if count is not None:
                n = _number(count, f"{at}: count", errors, 1, 400)
                items = [{"name": f"{base} {k}"} for k in range(1, int(n or 0) + 1)]
            elif kind in ("better", "presence"):
                items = []
            else:
                items = [{"name": label}]
        if not isinstance(items, list):
            errors.append(f"{at}: \"items\" must be a list.")
            items = []
        clean = []
        for k, it in enumerate(items, start=1):
            if isinstance(it, str):
                it = {"name": it}
            if not isinstance(it, dict) or not str(it.get("name") or "").strip():
                errors.append(f"{at}: item {k} has no \"name\".")
                continue
            it["name"] = str(it["name"]).strip()
            for field, lo in (("max", 0.0001), ("weight", 0)):
                if field in it:
                    v = _number(it[field], f"{at} → {it['name']}: {field}", errors, lo)
                    if v is None:
                        it.pop(field)
                    else:
                        it[field] = v
            if "mark" in it:
                v = _number(it["mark"], f"{at} → {it['name']}: mark", errors, 0)
                if v is None:
                    it.pop("mark")
                else:
                    it["mark"] = v
            key = norm(it["name"])
            if key in names:
                renamed = f"{singular(label)} {it['name']}" if norm(singular(label)) not in key else f"{label}: {it['name']}"
                warnings.append(f"{where}: two items are called '{it['name']}'; the one in {label} is now '{renamed}'.")
                it["name"], key = renamed, norm(renamed)
            names[key] = label
            clean.append(it)
        comp["items"] = clean
        if kind not in ("better", "presence") and not clean:
            errors.append(f"{at} has no items.")

        # type-specific settings
        if kind == "points":
            default = comp.pop("max_each", None)
            for it in clean:
                if "max" not in it and default is not None:
                    it["max"] = default
                if "max" not in it and "mark" not in it:
                    errors.append(f"{at} → {it['name']}: points items need \"max\" (what it's out of).")
        if kind == "capped":
            cap = _number(comp.get("cap"), f"{at}: cap (the points that earn full marks)", errors, 0.0001)
            if cap is not None:
                comp["cap"] = cap
        if kind == "presence":
            free = _number(comp.get("free_misses", 0), f"{at}: free_misses", errors, 0, 400)
            comp["free_misses"] = int(free or 0)
            comp.setdefault("assume", 100)
            day = comp.get("new_item") or "Day {n}"
            comp["new_item"] = day
            comp.setdefault("prefixes", [re.sub(r"\{n+\}", "", day).strip(), "#"])
        if kind == "better":
            of = comp.get("of")
            if not isinstance(of, list) or len(of) < 2:
                errors.append(f"{at}: a \"higher_of\" component needs \"of\": [two or more component ids].")
                comp["of"] = []
        if "drop_lowest" in comp:
            d = _number(comp.pop("drop_lowest"), f"{at}: drop_lowest", errors, 0, 400)
            if d:
                comp["best"] = len(clean) - int(d)
        if "best" in comp:
            b = _number(comp["best"], f"{at}: best", errors, 1, 400)
            if b is None or b >= len(clean):
                if b is not None:
                    warnings.append(f"{at}: best {fnum(b)} of {len(clean)} items means none are dropped; ignored.")
                comp.pop("best")
            else:
                comp["best"] = int(b)
        for field in ("min", "assume"):
            if field in comp:
                v = _number(comp[field], f"{at}: {field}", errors, 0, 100)
                if v is None:
                    comp.pop(field)
                else:
                    comp[field] = v
        must = comp.pop("must_pass", None)
        if must is not None:
            v = _number(must, f"{at}: must_pass", errors, 0, 100)
            if v is not None:
                short = re.sub(r"\s*\(.*?\)", "", label).strip() or label
                gates.append({"text": f"≥{fnum(v)}% on the {lower(short)} or you fail the course",
                              "checks": [{"type": "min", "component": cid, "min": v}]})

    # cross references
    valid = {comp["id"]: comp for comp in comps if isinstance(comp, dict)}
    for comp in valid.values():
        if comp["type"] == "better":
            bad = [r for r in comp["of"] if r not in valid or valid[r]["type"] == "better"]
            if bad:
                errors.append(f"{where} → {comp['label']}: \"of\" names {', '.join(map(str, bad))}, which"
                              f" {'is' if len(bad) == 1 else 'are'}n't other components (ids: {', '.join(valid)}).")

    def known(cid, at):
        if cid not in valid:
            errors.append(f"{at} refers to '{cid}', which isn't a component id (ids: {', '.join(valid)}).")
            return False
        return True

    clean_gates = []
    for i, gate in enumerate(gates, start=1):
        at = f"{where}: requirement {i}"
        if not isinstance(gate, dict) or not isinstance(gate.get("checks"), list) or not gate["checks"]:
            errors.append(f"{at} needs \"checks\": [{{\"type\": \"min\", \"component\": \"final\", \"min\": 50}}].")
            continue
        for chk in gate["checks"]:
            kind = chk.get("type") if isinstance(chk, dict) else None
            if kind not in CHECK_TYPES:
                errors.append(f"{at}: check type '{kind}' isn't one of: {', '.join(CHECK_TYPES)}.")
                continue
            if kind in ("min", "avg_min") and _number(chk.get("min"), f"{at}: min", errors, 0, 100) is None:
                continue
            if kind == "min":
                known(chk.get("component"), at)
            if kind in ("avg_min", "submitted"):
                refs = chk.get("components")
                if not isinstance(refs, list) or not refs:
                    errors.append(f"{at}: a '{kind}' check needs \"components\": [ids].")
                else:
                    for r in refs:
                        known(r, at)
        gate.setdefault("text", "Pass requirement")
        clean_gates.append(gate)
    if clean_gates:
        c["gates"] = clean_gates
    else:
        c.pop("gates", None)

    rules = c.get("rules") or {}
    if not isinstance(rules, dict):
        errors.append(f"{where}: \"rules\" must be an object.")
        rules = {}
    rl = rules.get("replace_lowest")
    if rl is not None:
        if not isinstance(rl, dict) or not isinstance(rl.get("tests"), list) or "with" not in rl:
            errors.append(f"{where}: replace_lowest needs \"tests\": [ids] and \"with\": id.")
        else:
            for r in rl["tests"] + [rl["with"]]:
                known(r, f"{where}: replace_lowest")
    ex = rules.get("exemption")
    if ex is not None:
        if not isinstance(ex, dict) or not isinstance(ex.get("tests"), list):
            errors.append(f"{where}: exemption needs \"tests\": [ids] and \"avg_above\": a percent.")
        else:
            for r in ex["tests"]:
                known(r, f"{where}: exemption")
    unknown_rules = set(rules) - {"replace_lowest", "exemption"}
    if unknown_rules:
        warnings.append(f"{where}: ignored unknown rules: {', '.join(sorted(unknown_rules))}.")
    if rules:
        c["rules"] = rules
    else:
        c.pop("rules", None)

    total = sum(float(comp.get("weight") or 0) for comp in valid.values())
    if valid and total <= 0:
        errors.append(f"{where}: the component weights add up to 0%.")
    elif valid and abs(total - 100) > 0.01:
        warnings.append(f"{where}: the weights add up to {fnum(total)}%, not 100%. Check them against the syllabus"
                        " (bonus marks are fine).")
    return c, errors, warnings


# --------------------------------------------------------------------------- checking a transcript

def check_transcript(raw):
    """-> (normalized transcript, errors, warnings)."""
    errors, warnings = [], []
    if not isinstance(raw, dict):
        return None, ["The transcript must be an object {...}."], []
    t = copy.deepcopy(raw)
    t.pop("version", None)
    t["format"] = FORMAT_TRANSCRIPT
    if "scale" in t:
        t["scale"] = check_scale(t["scale"], "transcript", errors)
        if not t["scale"]:
            t.pop("scale")
    if "default_credits" in t:
        t["default_credits"] = _number(t["default_credits"], "default_credits", errors, 0, 30) or 3
    terms = t.get("terms")
    if not isinstance(terms, list) or not terms:
        errors.append("The transcript needs \"terms\": [ ... ] with at least one term.")
        return t, errors, warnings
    for i, term in enumerate(terms, start=1):
        if not isinstance(term, dict):
            errors.append(f"Term {i} must be an object.")
            continue
        name = str(term.get("term") or term.get("name") or "").strip()
        term.pop("name", None)
        if not name:
            errors.append(f"Term {i} has no \"term\" name (like \"Fall 2025\").")
            name = f"Term {i}"
        term["term"] = name
        for field in ("term_gpa", "cumulative_gpa"):
            if term.get(field) is not None:
                v = _number(term[field], f"{name}: {field}", errors, 0, 10)
                term[field] = v
        courses = term.get("courses")
        if not isinstance(courses, list):
            errors.append(f"{name}: \"courses\" must be a list.")
            term["courses"] = []
            continue
        for k, course in enumerate(courses, start=1):
            if not isinstance(course, dict) or not str(course.get("code") or "").strip():
                errors.append(f"{name}: course {k} has no \"code\".")
                continue
            course["code"] = str(course["code"]).strip()
            letter = course.get("letter")
            course["letter"] = str(letter).strip().upper() if letter not in (None, "") else None
            if "credits" in course:
                v = _number(course["credits"], f"{name} {course['code']}: credits", errors, 0, 30)
                course["credits"] = v if v is not None else 3
    st = t.get("standing")
    if st is not None and not isinstance(st, dict):
        errors.append("\"standing\" must be an object like {\"credit_hours\": 60, \"cumulative_gpa\": 3.41}.")
        t.pop("standing")
    return t, errors, warnings


# --------------------------------------------------------------------------- the import as a whole

class Import:
    """What a pasted reply or file holds, checked: courses or a transcript, with errors and warnings."""

    def __init__(self):
        self.kind = None          # "courses" | "transcript"
        self.courses = []
        self.transcript = None
        self.scale = None         # a letter scale the file brings, for the whole library
        self.term = None
        self.errors, self.warnings = [], []

    @property
    def ok(self) -> bool:
        return not self.errors and (bool(self.courses) or self.transcript is not None)


def read_import(text: str) -> Import:
    imp = Import()
    try:
        obj = extract_json(text)
    except GradeError as e:
        imp.errors.append(str(e))
        return imp
    fmt = obj.get("format") if isinstance(obj, dict) else None
    if isinstance(obj, dict) and (fmt == FORMAT_TRANSCRIPT or ("terms" in obj and "components" not in obj)):
        imp.kind = "transcript"
        imp.transcript, imp.errors, imp.warnings = check_transcript(obj)
        imp.scale = (imp.transcript or {}).get("scale")
        return imp
    if isinstance(obj, list):
        raws, shared = obj, {}
    elif isinstance(obj, dict) and isinstance(obj.get("courses"), list) and "components" not in obj:
        raws, shared = obj["courses"], obj
    elif isinstance(obj, dict):
        raws, shared = [obj], {}
    else:
        imp.errors.append("Expected a course file (an object with \"components\") or a transcript (with \"terms\").")
        return imp
    imp.kind = "courses"
    if shared.get("scale") is not None:
        imp.scale = check_scale(shared["scale"], "file", imp.errors)
    for i, raw in enumerate(raws, start=1):
        if isinstance(raw, dict):
            for k in ("term", "school"):
                if shared.get(k) and not raw.get(k):
                    raw[k] = shared[k]
            if shared.get("scale") and not raw.get("scale"):
                raw["scale"] = shared["scale"]
        course, errs, warns = check_course(raw, f"Course {i}")
        imp.errors += errs
        imp.warnings += warns
        if course is not None:
            imp.courses.append(course)
    keys = [c["key"] for c in imp.courses]
    for k in sorted({k for k in keys if keys.count(k) > 1}):
        imp.errors.append(f"The file has {keys.count(k)} courses with the code {k.upper()}.")
    if imp.scale is None:
        scales = [c.get("scale") for c in imp.courses if c.get("scale")]
        imp.scale = scales[0] if scales else None
    terms = [c.get("term") for c in imp.courses if c.get("term")]
    imp.term = terms[0] if terms else shared.get("term")
    return imp


def scale_differs(a, b) -> bool:
    return bool(a and b) and [tuple(r[:3]) for r in scale_rows(a)] != [tuple(r[:3]) for r in scale_rows(b)]


def plan(imp: Import, library: dict, transcript: dict, adopt_scale: bool = None):
    """Check the import against what's already there. -> (new library, new transcript, notes, errors).

    Nothing is written: the caller saves the two dicts if errors is empty. adopt_scale=None means
    "only when the library has no scale yet".
    """
    lib = copy.deepcopy(library)
    tr = copy.deepcopy(transcript or {})
    notes, errors = [], list(imp.errors)
    if errors:
        return lib, tr, notes, errors
    if imp.scale and (adopt_scale or (adopt_scale is None and not lib.get("scale"))):
        if scale_differs(lib.get("scale"), imp.scale):
            notes.append("The app's letter scale now matches this file.")
        lib["scale"] = imp.scale
    if imp.kind == "transcript":
        tr = imp.transcript
        n = sum(len(t.get("courses", [])) for t in tr.get("terms", []))
        notes.append(f"Past semesters: {len(tr['terms'])} terms, {n} courses"
                     + (" (replaces the transcript you had)" if transcript and transcript.get("terms") else "") + ".")
    else:
        if imp.term and not lib.get("term"):
            lib["term"] = imp.term
        existing = {c["key"]: i for i, c in enumerate(lib.get("courses", []))}
        for course in imp.courses:
            if course["key"] in existing:
                old = lib["courses"][existing[course["key"]]]
                kept = carry_marks(old, course)
                lib["courses"][existing[course["key"]]] = course
                notes.append(f"{course['code']}: replaced the course you had"
                             + (f", keeping its {kept} mark{'s' if kept != 1 else ''}." if kept else "."))
            else:
                lib.setdefault("courses", []).append(course)
                notes.append(f"{course['code']}: added.")
    try:   # the real test: build everything the window will show
        book = GradeBook(lib)
        for c in book.courses:
            c.summary()
        trans = Transcript(tr, book)
        for m in trans.mismatches():
            notes.append("Check the transcript: " + m + ". A letter or credit value may have been misread.")
    except GradeError as e:
        errors.append(str(e) + (" Add it to the scale or ask your AI to fix the letter." if "scale" in str(e) else ""))
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as e:
        errors.append(f"The file looks right but the math can't use it ({type(e).__name__}: {e}).")
    return lib, tr, notes, errors


def carry_marks(old: dict, new: dict) -> int:
    """Copy marks from the course you had onto its replacement, by component and item name. -> count."""
    by_comp, by_name = {}, {}
    for comp in old.get("components", []):
        for it in comp.get("items", []):
            if graded(it) or it.get("flag"):
                by_comp[(comp["id"], norm(it["name"]))] = it
                by_name.setdefault(norm(it["name"]), it)
    kept = 0
    for comp in new.get("components", []):
        names = {norm(it["name"]) for it in comp.get("items", [])}
        for it in comp.get("items", []):
            if graded(it):
                continue
            src = by_comp.pop((comp["id"], norm(it["name"])), None) or by_name.get(norm(it["name"]))
            if src:
                by_name.pop(norm(it["name"]), None)
                for k in KEEP_ON_ITEM:
                    if k in src:
                        it[k] = src[k]
                kept += graded(it)
        if comp.get("type") == "presence":   # logged days the new file doesn't list
            for (cid, key), src in list(by_comp.items()):
                if cid == comp["id"] and key not in names:
                    comp["items"].append(copy.deepcopy(src))
                    by_comp.pop((cid, key))
                    kept += 1
    return kept


def remove_course(library: dict, code: str) -> dict:
    lib = copy.deepcopy(library)
    gone = [c for c in lib.get("courses", []) if c["code"] == code]
    lib["courses"] = [c for c in lib.get("courses", []) if c["code"] != code]
    keys = {c["key"] for c in gone}
    lib["log"] = [r for r in lib.get("log", []) if r.get("ref", "").split("/")[0] not in keys]
    return lib


def blank_course(course: dict) -> dict:
    """A course file with the marks taken out, to share with classmates."""
    out = {"format": FORMAT_COURSE, "version": 1}
    out.update(copy.deepcopy(course))
    out.pop("key", None)
    for comp in out.get("components", []):
        if comp.get("type") == "presence":
            comp["items"] = []
        for it in comp.get("items", []):
            for k in KEEP_ON_ITEM:
                it.pop(k, None)
    return out


# --------------------------------------------------------------------------- describing what was read

def describe_course(c: dict) -> list:
    """Plain lines a student can hold up against their syllabus."""
    head = c["code"] + (f" — {c['title']}" if c.get("title") else "")
    meta = [f"{fnum(c.get('credits', 3))} credits"] + ([c["term"]] if c.get("term") else [])
    lines = [head, "  " + " · ".join(meta)]
    for comp in c.get("components", []):
        n = len(comp.get("items", []))
        kind = comp.get("type", "average")
        bits = []
        if kind == "presence":
            bits.append("attendance" + (f", {comp['free_misses']} free misses" if comp.get("free_misses") else ""))
        elif kind == "better":
            bits.append("the higher of " + " / ".join(comp.get("of", [])))
        elif kind == "capped":
            bits.append(f"{n} items, full marks at {fnum(comp.get('cap', 0))} pts")
        elif kind == "points":
            total = sum(float(it.get("max", 0)) for it in comp.get("items", []))
            bits.append(f"{n} item{'s' if n != 1 else ''}, {fnum(total)} pts")
        elif n > 1:
            bits.append(f"{n} items")
        if comp.get("best"):
            bits.append(f"best {comp['best']} count")
        if comp.get("role") == "solve" or (kind == "average" and n == 1 and comp.get("role") != "project"):
            bits.append("exam")
        if comp.get("min") is not None:
            bits.append(f"minimum {fnum(comp['min'])}%")
        marks = sum(1 for it in comp.get("items", []) if graded(it))
        if marks:
            bits.append(f"{marks} marked")
        lines.append(f"  {fnum(comp.get('weight', 0)):>5}%  {comp['label']}" + (f"  ({', '.join(bits)})" if bits else ""))
    total = sum(float(comp.get("weight") or 0) for comp in c.get("components", []))
    lines.append(f"  {fnum(total):>5}%  total")
    for gate in c.get("gates", []):
        lines.append(f"  Must: {gate.get('text', '')}")
    rl = (c.get("rules") or {}).get("replace_lowest")
    if rl:
        lines.append(f"  Rule: {rl['with']} replaces the lowest of {', '.join(rl['tests'])} if it's higher")
    ex = (c.get("rules") or {}).get("exemption")
    if ex:
        lines.append(f"  Rule: exemption with a test average above {fnum(ex.get('avg_above', 0))}%")
    if c.get("min_effect") == "deduct":
        lines.append("  Rule: falling short of a minimum costs the shortfall, point for point")
    for n in c.get("notes", []):
        lines.append(f"  Note: {n}")
    return lines


def describe_transcript(t: dict) -> list:
    lines = []
    for term in t.get("terms", []):
        courses = term.get("courses", [])
        gpa = f" · term GPA {term['term_gpa']}" if term.get("term_gpa") is not None else ""
        lines.append(f"{term['term']}: {len(courses)} course{'s' if len(courses) != 1 else ''}{gpa}")
        lines.append("  " + " · ".join(f"{c['code']} {c.get('letter') or '—'}" for c in courses))
    st = t.get("standing") or {}
    if st.get("cumulative_gpa") is not None:
        lines.append(f"Cumulative GPA on the transcript: {st['cumulative_gpa']}")
    return lines


def unknown_letters(t: dict, scale) -> list:
    """Letters on the transcript that the scale has no grade points for (and aren't W, P...)."""
    have = {r[1] for r in scale_rows(scale)}
    seen = []
    for term in t.get("terms", []):
        for c in term.get("courses", []):
            letter = c.get("letter")
            if letter and letter not in have and letter.upper() not in NO_GPA and letter not in seen:
                seen.append(letter)
    return seen


__all__ = ["Import", "read_import", "plan", "check_course", "check_transcript", "extract_json", "carry_marks",
           "remove_course", "blank_course", "describe_course", "describe_transcript"]
