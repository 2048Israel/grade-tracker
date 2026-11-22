"""The Grade Tracker window.

Three views, switched with the tabs at the top (or Ctrl+1 / Ctrl+2 / Ctrl+3):
  Courses         log this term's marks; every course's standing and what it needs per letter
  GPA             cumulative GPA, term-by-term chart, GPA by subject, this term's projection
  Past semesters  every past term and course; click a course for its breakdown

Courses and transcripts come in through "Add course": copy the prompt, give it and your syllabus
to your own AI, paste back what it writes. Every number goes through engine.py, the same code
the command line uses. The look and motion live in juice.py; --calm (or GRADE_TRACKER_CALM=1)
turns the motion and confetti off.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import queue
import re
import sys
import threading
import traceback
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk

from . import DATA, __version__, coursefile as cf, engine as g, juice as j, prompt, updater
from .report import build_html
from .store import Library

HOME_PAGE = "https://github.com/2048Israel/grade-tracker"

# Neon on navy.
BG, PANEL, FIELD, LINE = "#0a0f1a", "#111a2a", "#1a2639", "#253550"
TEXT, MUTED, GOLD = "#eaf1fb", "#8b9cb6", "#ffcf70"
ACCENT, VIOLET, PINK, CYAN, MINT = "#4f9dff", "#9d6bff", "#ff5fa2", "#3dd6f5", "#2fe6b0"
GOOD, WARN, BAD = "#6fe3a1", "#ffc94d", "#ff6b6b"
SELECT, HOVER_ROW, PANE = "#1c2c47", "#162238", "#0c1321"
NEON = (ACCENT, VIOLET, PINK)
# Chart series, checked for colour-blind separation and contrast against PANEL.
TERM_BAR, CUM_LINE, CUM_GLOW = ACCENT, "#ffb547", "#3d2c15"
GRIDLINE, HOVER_BAND = "#18243a", "#152035"
# One colour per letter, used for pills, dots, meters and chart bars.
LETTER_COLOURS = {"A+": MINT, "A": CYAN, "A-": ACCENT, "B+": VIOLET, "B": "#f5b83d", "B-": "#ff9a4d",
                  "C+": "#ff8170", "C": "#ff6b6b", "C-": "#ff5c7c", "D+": "#ff5470", "D": "#ff4d6d",
                  "D-": "#f04060", "E": "#e5383b", "F": "#e5383b", "W": MUTED}

VIEWS = (("marks", "Courses"), ("gpa", "GPA"), ("past", "Past semesters"))


def need_text(result, target: float):
    """(text, colour) for one target cell."""
    kind, v = result
    if kind == "locked":
        return "locked", GOOD
    if kind == "out":
        return "out", BAD
    if kind == "done":
        return f"{g.fnum(v, 1)}%", GOOD if v >= target - 1e-9 else BAD
    return f"{g.fnum(v, 1)}%", GOOD if v <= 85 else (WARN if v <= 100 else BAD)


def signed(delta) -> str:
    """'▲ +0.07', '▼ −0.04', '±0.00' for a change in GPA."""
    s = g.fixed(abs(delta), 2)
    if s == "0.00":
        return "±0.00"
    return ("▲ +" if delta > 0 else "▼ −") + s


def letter_colour(letter) -> str:
    return LETTER_COLOURS.get(letter or "", MUTED)


def tint(colour: str, amount: float = 0.18) -> str:
    """colour faded into the panel, for chip backgrounds."""
    return j.mix(PANEL, colour, amount)


def style_key(letter: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", lambda m: {"+": "plus", "-": "minus"}.get(m[0], "x"), letter)


def documents_dir() -> Path:
    for p in (Path.home() / "Documents", Path.home()):
        if p.is_dir():
            return p
    return Path.cwd()


class App:
    def __init__(self, root: tk.Tk, lib: Library, calm: bool = False, check_updates: bool = False):
        self.root, self.lib = root, lib
        self.jobs = queue.Queue()  # (callback, result) from background threads, run on Tk's thread
        self.release = None        # a newer release found on GitHub
        self.updating = False
        self.anim = j.Animator(root, enabled=not calm)
        self.grow = 1.0          # chart bars' growth, 0..1, tweened when the GPA view opens
        self.proj_shown = None   # (term GPA, cumulative) the chart's projected column is drawn at
        self.shown = {}          # number currently on each GPA tile, for count-ups
        self.dots = {}           # letter -> PhotoImage dot, for the past-semesters tree
        self.meters = []         # (canvas, fraction, colour) bars that fill when their view opens
        self.book = None
        self.mtime = None
        self.items = []          # [(component, item name)] behind the item dropdown
        self.selected = None     # course code shown in the detail pane
        self.transcript = None   # past terms, scored with the book's letter scale (may be empty)
        self.tr_error = None     # why the transcript didn't load, shown in its views
        self.tr_mtime = None
        self.overrides = {}      # what-if letters the user picked: {course code: letter}
        self.slots = []          # chart columns, for hover and click
        self.past_ids = {}       # tree item id -> PastTerm or PastCourse
        self.view = "marks"
        self.scale = root.winfo_fpixels("1i") / 96
        self.fonts()
        self.style()
        self.images()
        self.toast = j.Toaster(root, self.anim, self.px, self.f_base, FIELD, TEXT, LINE)
        self.build()
        self.reload()
        self.show_view("marks")
        root.bind("<FocusIn>", self.on_focus)
        if check_updates:
            root.after(1500, lambda: self.check_for_update(quiet=True))

    def px(self, n):
        return int(round(n * self.scale))

    # ------------------------------------------------------------------ look
    def fonts(self):
        self.f_base = ("Segoe UI", 10)
        self.f_small = ("Segoe UI", 9)
        self.f_bold = ("Segoe UI Semibold", 10)
        self.f_head = ("Segoe UI Semibold", 11)
        self.f_title = ("Segoe UI Black", 18)
        self.f_hero = ("Segoe UI Semibold", 26)
        self.f_chip = ("Segoe UI Semibold", 9)
        self.f_mono = ("Consolas", 10)

    def images(self):
        """Generated once: the window icon, the heading tick, and a dot per letter."""
        px = self.px
        self.icon = j.app_icon(64, NEON)
        self.logo = j.app_icon(px(36), NEON)
        self.tick = j.bar_image(NEON, px(4), px(16))
        for letter, colour in list(LETTER_COLOURS.items()) + [("", MUTED)]:
            self.dots[letter] = j.dot(colour, px(11), bg=PANEL)
        self.dots["term"] = j.dot(TEXT, px(11), ring_only=True, bg=PANEL)
        self.root.iconphoto(True, self.icon)

    def style(self):
        r = self.root
        r.configure(bg=BG)
        s = ttk.Style(r)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=TEXT, fieldbackground=FIELD, bordercolor=LINE,
                    lightcolor=FIELD, darkcolor=FIELD, troughcolor=PANEL, font=self.f_base,
                    selectbackground=SELECT, selectforeground=TEXT, insertcolor=TEXT)
        s.configure("Card.TFrame", background=PANEL, borderwidth=1, relief="solid", bordercolor=LINE,
                    lightcolor=LINE, darkcolor=LINE)
        s.configure("Flat.TFrame", background=PANEL, borderwidth=0)
        s.configure("Card.TLabel", background=PANEL, foreground=TEXT)
        s.configure("Field.TLabel", background=PANEL, foreground=MUTED, font=self.f_chip)
        s.configure("Hint.TLabel", background=PANEL, foreground=MUTED, font=self.f_small)
        s.configure("Head.TLabel", background=PANEL, foreground=TEXT, font=self.f_head)
        s.configure("TCheckbutton", background=PANEL, foreground=TEXT, indicatorbackground=FIELD,
                    indicatorforeground=TEXT)
        s.map("TCheckbutton", background=[("active", PANEL)], indicatorbackground=[("selected", ACCENT)])
        s.configure("TEntry", padding=self.px(6), fieldbackground=FIELD, foreground=TEXT, bordercolor=LINE,
                    lightcolor=FIELD, darkcolor=FIELD)
        s.map("TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)])
        s.configure("TCombobox", padding=self.px(5), fieldbackground=FIELD, background=FIELD, foreground=TEXT,
                    arrowcolor=TEXT, bordercolor=LINE, lightcolor=FIELD, darkcolor=FIELD, arrowsize=self.px(14))
        s.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", TEXT)],
              selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", TEXT)],
              bordercolor=[("focus", ACCENT)], background=[("active", LINE)])
        r.option_add("*TCombobox*Listbox.background", FIELD)
        r.option_add("*TCombobox*Listbox.foreground", TEXT)
        r.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        r.option_add("*TCombobox*Listbox.selectForeground", BG)
        r.option_add("*TCombobox*Listbox.font", self.f_base)
        s.configure("Accent.TButton", background=ACCENT, foreground="#06101f", font=self.f_bold, borderwidth=0,
                    focusthickness=0, padding=(self.px(20), self.px(8)))
        s.map("Accent.TButton", background=[("disabled", LINE), ("pressed", "#3b7fe0"), ("active", "#77b4ff")],
              foreground=[("disabled", MUTED)])
        s.configure("Good.TButton", background=GOOD, foreground="#06101f", font=self.f_bold, borderwidth=0,
                    focusthickness=0, padding=(self.px(20), self.px(8)))
        for letter, colour in LETTER_COLOURS.items():   # the "every course at one letter" buttons
            name = f"{style_key(letter)}.Small.TButton"
            s.configure(name, background=tint(colour, 0.16), foreground=colour, borderwidth=0, focusthickness=0,
                        font=self.f_chip, padding=(self.px(10), self.px(5)))
            s.map(name, background=[("active", tint(colour, 0.32))], foreground=[("active", TEXT)])
        s.configure("TButton", background=FIELD, foreground=TEXT, borderwidth=0, focusthickness=0,
                    padding=(self.px(14), self.px(7)))
        s.map("TButton", background=[("disabled", PANEL), ("active", LINE)], foreground=[("disabled", MUTED)])
        s.configure("Top.TButton", background=BG, foreground=TEXT, borderwidth=0, focusthickness=0,
                    font=self.f_bold, padding=(self.px(12), self.px(6)))
        s.map("Top.TButton", background=[("active", SELECT)])
        s.configure("Version.TButton", background=BG, foreground=MUTED, borderwidth=0, focusthickness=0,
                    font=self.f_small, padding=(self.px(8), self.px(6)))
        s.map("Version.TButton", background=[("active", SELECT)], foreground=[("active", TEXT)])
        s.configure("Update.TButton", background=MINT, foreground="#06101f", borderwidth=0, focusthickness=0,
                    font=self.f_bold, padding=(self.px(14), self.px(6)))
        s.map("Update.TButton", background=[("disabled", tint(MINT, 0.4)), ("active", "#6ff0c8")],
              foreground=[("disabled", TEXT)])
        s.configure("Mint.Horizontal.TProgressbar", troughcolor=FIELD, background=MINT, bordercolor=FIELD,
                    lightcolor=MINT, darkcolor=MINT, thickness=self.px(8))
        s.configure("TopAccent.TButton", background=tint(ACCENT, 0.28), foreground=TEXT, borderwidth=0,
                    focusthickness=0, font=self.f_bold, padding=(self.px(14), self.px(6)))
        s.map("TopAccent.TButton", background=[("active", tint(ACCENT, 0.5))])
        s.configure("Small.TButton", background=FIELD, foreground=TEXT, borderwidth=0, focusthickness=0,
                    font=self.f_small, padding=(self.px(10), self.px(4)))
        s.map("Small.TButton", background=[("active", LINE)])
        s.configure("Link.TButton", background=PANEL, foreground=MUTED, font=self.f_small, padding=(0, self.px(2)))
        s.map("Link.TButton", background=[("active", PANEL)], foreground=[("active", TEXT)])
        s.configure("Letter.TCombobox", padding=(self.px(6), self.px(2)))
        # Past semesters tree
        s.configure("Past.Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, borderwidth=0,
                    rowheight=self.px(29), font=self.f_base, indent=self.px(16))
        s.map("Past.Treeview", background=[("selected", SELECT)], foreground=[("selected", "#ffffff")])
        s.layout("Past.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        s.configure("Past.Treeview.Heading", background=PANEL, foreground=MUTED, font=self.f_small, borderwidth=0,
                    relief="flat", padding=(self.px(4), self.px(4)))
        s.map("Past.Treeview.Heading", background=[("active", PANEL)], foreground=[("active", TEXT)])
        s.configure("Past.Vertical.TScrollbar", background=FIELD, troughcolor=PANEL, bordercolor=PANEL,
                    lightcolor=FIELD, darkcolor=FIELD, arrowcolor=MUTED, gripcount=0)
        s.map("Past.Vertical.TScrollbar", background=[("active", LINE)])

    # ------------------------------------------------------------------ layout
    def build(self):
        r, px = self.root, self.px
        r.title("Grade Tracker")
        r.minsize(px(1000), px(620))
        outer = tk.Frame(r, bg=BG, padx=px(18), pady=px(14))
        outer.pack(fill="both", expand=True)

        top = tk.Frame(outer, bg=BG)
        top.pack(fill="x")
        tk.Label(top, image=self.logo, bg=BG).pack(side="left")
        tk.Label(top, text="Grade Tracker", bg=BG, fg=TEXT, font=self.f_title).pack(side="left", padx=(px(10), 0))

        # Tabs: text on a canvas, with a pill behind the active one that slides when you switch.
        self.nav = tk.Canvas(top, bg=BG, highlightthickness=0, height=px(38))
        self.nav.pack(side="left", padx=(px(22), 0))
        head_font = tkfont.Font(font=self.f_head)
        x, padx = 0, px(16)
        self.tabs = {}
        for key, label in VIEWS:
            w = head_font.measure(label) + 2 * padx
            tid = self.nav.create_text(x + w / 2, px(19), text=label, fill=MUTED, font=self.f_head,
                                       tags=("label", f"tab-{key}"))
            self.nav.create_rectangle(x, 0, x + w, px(38), fill=BG, outline="", tags=("hit", f"tab-{key}"))
            self.tabs[key] = (tid, x, x + w)
            self.nav.tag_bind(f"tab-{key}", "<Button-1>", lambda e, k=key: self.show_view(k))
            self.nav.tag_bind(f"tab-{key}", "<Enter>", lambda e, t=tid: (self.nav.itemconfig(t, fill=TEXT),
                                                                          self.nav.config(cursor="hand2")))
            self.nav.tag_bind(f"tab-{key}", "<Leave>", lambda e, k=key, t=tid: (
                self.nav.itemconfig(t, fill=TEXT if self.view == k else MUTED), self.nav.config(cursor="")))
            x += w + px(4)
        self.nav.config(width=x)
        self.pill = j.rounded(self.nav, 0, px(3), 1, px(35), px(17), fill=SELECT, outline="")
        self.pill_line = self.nav.create_line(0, px(34), 1, px(34), fill=ACCENT, width=px(2), capstyle="round")
        self.nav.tag_raise("label")
        self.nav.tag_lower("hit")
        self.pill_at = None

        # Right: actions, the version / update button, and today's cumulative as a coloured chip.
        ttk.Button(top, text="⚙", style="Top.TButton", command=self.open_settings, cursor="hand2",
                   width=2).pack(side="right", pady=(px(4), 0))
        self.update_btn = ttk.Button(top, text=f"v{__version__}", style="Version.TButton", cursor="hand2",
                                     command=self.on_update_click)
        self.update_btn.pack(side="right", pady=(px(4), 0), padx=(0, px(4)))
        ttk.Button(top, text="Report", style="Top.TButton", command=self.export_report,
                   cursor="hand2").pack(side="right", pady=(px(4), 0), padx=(0, px(4)))
        ttk.Button(top, text="＋ Add course", style="TopAccent.TButton", command=lambda: self.open_import("syllabus"),
                   cursor="hand2").pack(side="right", pady=(px(4), 0), padx=(0, px(8)))
        self.chip = tk.Label(top, text="", bg=tint(MINT), fg=MINT, font=self.f_chip, padx=px(10), pady=px(4))
        self.chip.pack(side="right", pady=(px(4), 0), padx=(0, px(14)))
        for i, (key, _) in enumerate(VIEWS, start=1):
            r.bind(f"<Control-Key-{i}>", lambda e, k=key: self.show_view(k))
        r.bind("<Control-n>", lambda e: self.open_import("syllabus"))
        r.bind("<Control-p>", lambda e: self.export_report())

        # A thin neon rule under the header.
        rule = tk.Canvas(outer, bg=BG, highlightthickness=0, height=px(2))
        rule.pack(fill="x", pady=(px(10), 0))
        rule.bind("<Configure>", lambda e: (rule.delete("all"),
                                            j.hgradient(rule, 0, 0, e.width, px(2), (BG,) + NEON + (BG,), step=4)))

        stack = tk.Frame(outer, bg=BG)
        stack.pack(fill="both", expand=True, pady=(px(14), 0))
        stack.rowconfigure(0, weight=1)
        stack.columnconfigure(0, weight=1)
        self.views = {"marks": self.build_marks(stack), "gpa": self.build_gpa(stack), "past": self.build_past(stack),
                      "welcome": self.build_welcome(stack)}

    def build_welcome(self, parent):
        """The first screen, before any course is added: the three steps."""
        px = self.px
        view = tk.Frame(parent, bg=BG)
        view.grid(row=0, column=0, sticky="nsew")
        card = ttk.Frame(view, style="Card.TFrame", padding=(px(34), px(28)))
        card.place(relx=0.5, rely=0.45, anchor="center")
        tk.Label(card, text="Let's set up your courses", bg=PANEL, fg=TEXT, font=("Segoe UI Black", 20),
                 anchor="w").pack(fill="x")
        tk.Label(card, text="Grade Tracker turns each syllabus into a tracker that tells you what you need on the "
                            "rest of the term for every letter grade.", bg=PANEL, fg=MUTED, font=self.f_base,
                 justify="left", wraplength=px(560), anchor="w").pack(fill="x", pady=(px(6), px(18)))
        steps = (
            ("Copy the prompt", "One click. It tells any AI exactly how to read a syllabus for this app.",
             ("Copy AI prompt", lambda: self.copy_prompt("syllabus"), "Accent.TButton")),
            ("Give it to your AI with your syllabus", "Open ChatGPT, Claude, Gemini or Copilot, paste the prompt, "
                            "and attach your syllabus: the PDF, screenshots, or photos of the pages. Several "
                            "courses at once works too.", None),
            ("Paste its reply here", "The app checks it and shows you what it read before anything is saved.",
             ("Add course…", lambda: self.open_import("syllabus"), "Accent.TButton")),
        )
        for n, (title, body, button) in enumerate(steps, start=1):
            row = tk.Frame(card, bg=PANEL)
            row.pack(fill="x", pady=(0, px(14)))
            badge = tk.Canvas(row, width=px(34), height=px(34), bg=PANEL, highlightthickness=0)
            badge.pack(side="left", anchor="n")
            badge.create_oval(px(2), px(2), px(32), px(32), fill=tint(NEON[n - 1], 0.3), outline=NEON[n - 1],
                              width=px(2))
            badge.create_text(px(17), px(17), text=str(n), fill=TEXT, font=self.f_head)
            texts = tk.Frame(row, bg=PANEL)
            texts.pack(side="left", fill="x", expand=True, padx=(px(14), 0))
            tk.Label(texts, text=title, bg=PANEL, fg=TEXT, font=self.f_head, anchor="w").pack(fill="x")
            tk.Label(texts, text=body, bg=PANEL, fg=MUTED, font=self.f_small, anchor="w", justify="left",
                     wraplength=px(380)).pack(fill="x")
            if button:
                label, cmd, style = button
                ttk.Button(row, text=label, style=style, command=cmd, cursor="hand2").pack(side="right", anchor="n")
        foot = tk.Frame(card, bg=PANEL)
        foot.pack(fill="x", pady=(px(8), 0))
        ttk.Button(foot, text="Try it with an example course", style="Link.TButton", command=self.add_example,
                   cursor="hand2").pack(side="left")
        ttk.Button(foot, text="Import a transcript for your GPA history", style="Link.TButton",
                   command=lambda: self.open_import("transcript"), cursor="hand2").pack(side="right")
        return view

    def build_marks(self, parent):
        px = self.px
        body = tk.Frame(parent, bg=BG)
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # ---- left: entry form
        form = ttk.Frame(body, style="Card.TFrame", padding=px(18))
        form.grid(row=0, column=0, sticky="nsw", padx=(0, px(14)))
        self.heading(form, "Log a mark").grid(row=0, column=0, columnspan=2, sticky="w")

        self.course_var, self.item_var = tk.StringVar(), tk.StringVar()
        self.mark_var, self.date_var, self.note_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        width = 34

        def field(row, label, widget):
            ttk.Label(form, text=label, style="Field.TLabel").grid(row=row, column=0, columnspan=2, sticky="w",
                                                                   pady=(px(12), px(3)))
            widget.grid(row=row + 1, column=0, columnspan=2, sticky="ew")

        self.course_box = ttk.Combobox(form, textvariable=self.course_var, state="readonly", width=width)
        field(1, "COURSE", self.course_box)
        self.course_box.bind("<<ComboboxSelected>>", lambda e: self.on_course())
        self.item_box = ttk.Combobox(form, textvariable=self.item_var, state="readonly", width=width, height=18)
        field(3, "ITEM", self.item_box)
        self.item_box.bind("<<ComboboxSelected>>", lambda e: self.on_item())
        self.mark_entry = ttk.Entry(form, textvariable=self.mark_var, width=width, font=("Segoe UI", 12))
        field(5, "MARK", self.mark_entry)
        self.mark_entry.bind("<Return>", lambda e: self.save())
        self.hint = ttk.Label(form, text="", style="Hint.TLabel", wraplength=px(270))
        self.hint.grid(row=7, column=0, columnspan=2, sticky="w", pady=(px(4), 0))

        row2 = ttk.Frame(form, style="Flat.TFrame")
        ttk.Label(row2, text="LOGGED", style="Field.TLabel").grid(row=0, column=0, sticky="w", pady=(0, px(3)))
        ttk.Label(row2, text="NOTE (optional)", style="Field.TLabel").grid(row=0, column=1, sticky="w",
                                                                          padx=(px(10), 0), pady=(0, px(3)))
        ttk.Entry(row2, textvariable=self.date_var, width=9).grid(row=1, column=0, sticky="w")
        ttk.Entry(row2, textvariable=self.note_var, width=21).grid(row=1, column=1, sticky="ew", padx=(px(10), 0))
        row2.columnconfigure(1, weight=1)
        row2.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(px(12), 0))

        buttons = ttk.Frame(form, style="Flat.TFrame")
        buttons.grid(row=9, column=0, columnspan=2, sticky="ew", pady=(px(18), 0))
        self.save_btn = ttk.Button(buttons, text="Save  ✓", style="Accent.TButton", command=self.save,
                                   cursor="hand2")
        self.save_btn.pack(side="left")
        self.clear_btn = ttk.Button(buttons, text="Clear mark", style="Link.TButton", command=self.clear)
        self.clear_btn.pack(side="right")

        self.status = tk.Label(form, text="", bg=PANEL, fg=MUTED, font=self.f_small, justify="left",
                               anchor="w", wraplength=px(280))
        self.status.grid(row=10, column=0, columnspan=2, sticky="ew", pady=(px(12), 0), padx=(0, px(16)))

        self.heading(form, "Recent").grid(row=11, column=0, columnspan=2, sticky="w", pady=(px(14), px(6)))
        self.recent = tk.Text(form, bg=PANEL, fg=MUTED, font=self.f_small, relief="flat", wrap="none",
                              height=8, width=40, highlightthickness=0, cursor="arrow", padx=0, pady=0,
                              spacing1=px(2), spacing3=px(2))
        for tag, colour in (("date", MUTED), ("code", ACCENT), ("item", TEXT), ("mark", GOLD)):
            self.recent.tag_configure(tag, foreground=colour)
        self.recent.grid(row=12, column=0, columnspan=2, sticky="nsew")
        form.rowconfigure(12, weight=1)

        # ---- right: standing
        right = ttk.Frame(body, style="Card.TFrame", padding=px(18))
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)
        head = ttk.Frame(right, style="Flat.TFrame")
        head.grid(row=0, column=0, sticky="ew")
        self.heading(head, "Standing").pack(side="left")
        self.standing_hint = ttk.Label(head, text="average needed on what's left", style="Hint.TLabel")
        self.standing_hint.pack(side="left", padx=(px(10), 0), pady=(px(2), 0))
        self.target_chips = tk.Frame(head, bg=PANEL)
        self.target_chips.pack(side="right")

        self.grid = tk.Frame(right, bg=PANEL)
        self.grid.grid(row=1, column=0, sticky="ew", pady=(px(12), px(14)))

        self.detail = self.text_pane(right, height=14)
        self.detail.master.grid(row=2, column=0, sticky="nsew")
        tools = ttk.Frame(right, style="Flat.TFrame")
        tools.grid(row=3, column=0, sticky="ew", pady=(px(8), 0))
        ttk.Button(tools, text="Remove course", style="Link.TButton", command=self.remove_course).pack(side="right")
        ttk.Button(tools, text="Share course file", style="Link.TButton",
                   command=self.share_course).pack(side="right", padx=(0, px(16)))
        ttk.Button(tools, text="Update from a new file", style="Link.TButton",
                   command=lambda: self.open_import("syllabus")).pack(side="right", padx=(0, px(16)))
        return body

    def text_pane(self, parent, height, width=80):
        """The bordered monospace read-out used for course details."""
        px = self.px
        wrap = tk.Frame(parent, bg=LINE, padx=1, pady=1)
        text = tk.Text(wrap, bg=PANE, fg=TEXT, font=self.f_mono, relief="flat", wrap="word",
                       padx=px(12), pady=px(10), height=height, width=width, insertbackground=TEXT,
                       selectbackground=SELECT, highlightthickness=0, cursor="arrow")
        text.pack(fill="both", expand=True)
        for tag, colour in (("title", GOLD), ("good", GOOD), ("warn", WARN), ("bad", BAD), ("muted", MUTED),
                            ("need", ACCENT)):
            text.tag_configure(tag, foreground=colour)
        text.tag_configure("title", font=("Consolas", 11, "bold"))
        text.config(state="disabled")
        return text

    def autowrap(self, label, start=300):
        """Wrap a label's text to whatever width the layout gives it."""
        label.config(wraplength=self.px(start))
        label.bind("<Configure>", lambda e: label.config(wraplength=max(e.width - 4, 40)))
        return label

    def heading(self, parent, text):
        """A card heading with the neon tick in front of it."""
        return ttk.Label(parent, text=text, style="Head.TLabel", image=self.tick, compound="left")

    def card(self, parent, title, hint=""):
        """A bordered panel with a ticked heading; returns (card, heading row)."""
        px = self.px
        card = ttk.Frame(parent, style="Card.TFrame", padding=px(18))
        head = ttk.Frame(card, style="Flat.TFrame")
        head.pack(fill="x")
        self.heading(head, title).pack(side="left")
        if hint:
            ttk.Label(head, text=hint, style="Hint.TLabel").pack(side="left", padx=(px(10), 0), pady=(px(2), 0))
        return card, head

    # ------------------------------------------------------------------ GPA view
    def build_gpa(self, parent):
        px = self.px
        view = tk.Frame(parent, bg=BG)
        view.grid(row=0, column=0, sticky="nsew")
        view.columnconfigure(0, weight=3)
        view.columnconfigure(1, weight=2)
        view.rowconfigure(1, weight=1, minsize=px(250))   # the chart never gets squeezed flat

        tiles = tk.Frame(view, bg=BG)
        tiles.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, px(14)))
        self.tiles = {}
        stripes = {"gpa": (ACCENT, VIOLET), "hours": (CYAN, ACCENT), "qp": (VIOLET, PINK), "proj": (PINK, GOLD)}
        for i, key in enumerate(("gpa", "hours", "qp", "proj")):
            tiles.columnconfigure(i, weight=1, uniform="tile")
            frame = tk.Frame(tiles, bg=LINE, padx=1, pady=1)
            frame.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else px(14), 0))
            stripe = tk.Canvas(frame, bg=PANEL, highlightthickness=0, height=px(3))
            stripe.pack(fill="x")
            stripe.bind("<Configure>", lambda e, c=stripe, s=stripes[key]: (
                c.delete("all"), j.hgradient(c, 0, 0, e.width, e.height, s, step=4)))
            box = tk.Frame(frame, bg=PANEL, padx=px(18), pady=px(12))
            box.pack(fill="both", expand=True)
            if key == "gpa":   # the cumulative gets a ring: how far up the scale it sits
                self.ring = tk.Canvas(box, bg=PANEL, highlightthickness=0, width=px(70), height=px(70))
                self.ring.pack(side="right", padx=(px(8), 0))
            texts = tk.Frame(box, bg=PANEL)
            texts.pack(side="left", fill="both", expand=True)
            head = tk.Label(texts, text="", bg=PANEL, fg=MUTED, font=self.f_chip, anchor="w")
            head.pack(fill="x")
            value = tk.Label(texts, text="—", bg=PANEL, fg=TEXT, font=self.f_hero, anchor="w")
            value.pack(fill="x")
            sub = self.autowrap(tk.Label(texts, text="", bg=PANEL, fg=MUTED, font=self.f_small, anchor="w",
                                         justify="left"), 200)
            sub.pack(fill="x")
            self.tiles[key] = (head, value, sub)

        # ---- term-by-term chart
        chart_card, head = self.card(view, "Term by term", "click a term to open it")
        chart_card.grid(row=1, column=0, sticky="nsew", padx=(0, px(14)))
        legend = tk.Frame(head, bg=PANEL)
        legend.pack(side="right")
        for kind, label in (("bar", "Term GPA"), ("line", "Cumulative"), ("proj", "Projected")):
            sw = tk.Canvas(legend, width=px(26) if kind == "bar" else px(18), height=px(12), bg=PANEL,
                           highlightthickness=0)
            if kind == "bar":
                for k, letter in enumerate(("B+", "A-", "A+")):
                    sw.create_rectangle(px(2 + 8 * k), px(5 - 2 * k), px(8 + 8 * k), px(12),
                                        fill=letter_colour(letter), outline="")
            elif kind == "line":
                sw.create_line(0, px(6), px(18), px(6), fill=CUM_LINE, width=px(2))
                sw.create_oval(px(5), px(2), px(13), px(10), fill=CUM_LINE, outline=PANEL, width=1)
            else:
                sw.create_rectangle(px(4), px(2), px(14), px(11), outline=PINK, width=px(2))
            sw.pack(side="left", padx=(px(12), px(4)))
            tk.Label(legend, text=label, bg=PANEL, fg=MUTED, font=self.f_small).pack(side="left")
        self.chart = tk.Canvas(chart_card, bg=PANEL, highlightthickness=0, height=px(270), width=px(560))
        self.chart.pack(fill="both", expand=True, pady=(px(10), 0))
        self.chart.bind("<Configure>", lambda e: self.draw_chart())
        self.chart.bind("<Motion>", self.chart_hover)
        self.chart.bind("<Leave>", lambda e: (self.chart.delete("hover"), self.chart.config(cursor="")))
        self.chart.bind("<Button-1>", self.chart_click)

        # ---- by subject
        subj_card, _ = self.card(view, "By subject", "graded past courses")
        subj_card.grid(row=2, column=0, sticky="nsew", padx=(0, px(14)), pady=(px(14), 0))
        self.subject_rows = tk.Frame(subj_card, bg=PANEL)
        self.subject_rows.pack(fill="x", pady=(px(8), 0))
        self.subject_rows.columnconfigure(3, weight=1)

        # ---- projection
        proj_card, head = self.card(view, "Projection")
        self.proj_title = head.winfo_children()[0]
        proj_card.grid(row=1, column=1, rowspan=2, sticky="nsew")
        ttk.Button(head, text="Reset to pace", style="Link.TButton", command=self.reset_what_if).pack(side="right")
        self.autowrap(ttk.Label(proj_card, text="Pace is the letter of each course's graded work so far. Pick a "
                                                "letter to try another outcome; W drops the course from the term.",
                                style="Hint.TLabel", justify="left")).pack(fill="x", pady=(px(6), 0))
        self.proj_rows = tk.Frame(proj_card, bg=PANEL)
        self.proj_rows.pack(fill="x", pady=(px(10), 0))
        for col in range(4):
            self.proj_rows.columnconfigure(col, weight=1 if col == 0 else 0)
        self.quick_label = tk.Label(proj_card, text="Every course at one letter  (button shows the cumulative it gives)",
                                    bg=PANEL, fg=MUTED, font=self.f_small, anchor="w")
        self.quick_label.pack(fill="x", pady=(px(12), px(4)))
        self.quick = tk.Frame(proj_card, bg=PANEL)
        self.quick.pack(fill="x")
        sep = tk.Frame(proj_card, bg=LINE, height=1)
        sep.pack(fill="x", pady=(px(14), px(10)))
        self.proj_result = tk.Frame(proj_card, bg=PANEL)
        self.proj_result.pack(fill="x")
        for col in range(3):
            self.proj_result.columnconfigure(col, weight=1)
        return view

    # ------------------------------------------------------------------ Past semesters view
    def build_past(self, parent):
        px = self.px
        view = tk.Frame(parent, bg=BG)
        view.grid(row=0, column=0, sticky="nsew")
        view.columnconfigure(0, weight=1)   # the tree takes what the details pane doesn't need
        view.rowconfigure(0, weight=1)

        left, head = self.card(view, "Past semesters")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, px(14)))
        ttk.Button(head, text="Collapse all", style="Link.TButton",
                   command=lambda: self.open_all(False)).pack(side="right")
        ttk.Button(head, text="Expand all", style="Link.TButton",
                   command=lambda: self.open_all(True)).pack(side="right", padx=(0, px(14)))
        ttk.Button(head, text="Import transcript…", style="Link.TButton",
                   command=lambda: self.open_import("transcript")).pack(side="right", padx=(0, px(14)))
        self.past_hint = ttk.Label(head, text="", style="Hint.TLabel")
        self.past_hint.pack(side="left", padx=(px(10), 0), pady=(px(2), 0))

        wrap = tk.Frame(left, bg=PANEL)
        wrap.pack(fill="both", expand=True, pady=(px(10), 0))
        cols = (("letter", "LETTER", 52, "center"), ("gpa", "TERM GPA", 68, "center"),
                ("cum", "CUMULATIVE", 82, "center"), ("check", "NOTE", 108, "w"))
        self.tree = ttk.Treeview(wrap, columns=[c[0] for c in cols], style="Past.Treeview", selectmode="browse")
        self.tree.heading("#0", text="TERM · COURSE", anchor="w")
        self.tree.column("#0", width=px(150), minwidth=px(150), stretch=True)
        for key, label, w, anchor in cols:
            self.tree.heading(key, text=label, anchor=anchor)
            self.tree.column(key, width=px(w), minwidth=px(w // 2), stretch=False, anchor=anchor)
        self.tree.tag_configure("term", font=self.f_bold, foreground=TEXT)
        self.tree.tag_configure("course", foreground=TEXT)
        self.tree.tag_configure("flagged", foreground=TEXT)
        self.tree.tag_configure("off", foreground=MUTED)
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview, style="Past.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")   # packed first so a narrow window squeezes the tree, not this
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.on_past_select())

        right, _ = self.card(view, "Details")
        right.grid(row=0, column=1, sticky="nsew")
        self.past_detail = self.text_pane(right, height=20, width=50)  # fits a component table
        self.past_detail.master.pack(fill="both", expand=True, pady=(px(10), 0))
        return view

    # ------------------------------------------------------------------ navigation
    def show_view(self, key: str):
        changed = key != self.view or self.pill_at is None
        self.view = key
        empty = key == "marks" and self.book is not None and not self.book.courses
        self.views["welcome" if empty else key].tkraise()
        for k, (tid, _, _) in self.tabs.items():
            self.nav.itemconfig(tid, fill=TEXT if k == key else MUTED)
        self.slide_pill(key)
        self.update_subtitle()
        if key == "marks":
            if not empty:
                self.mark_entry.focus_set()
            if changed:
                self.fill_meters()
        elif key == "gpa":
            if changed:
                self.enter_gpa()
            else:
                self.draw_chart()
        elif key == "past":
            self.tree.focus_set()

    def slide_pill(self, key: str):
        """Glide the tab pill from where it is to the chosen tab, landing with a little bounce."""
        px = self.px
        _, x0, x1 = self.tabs[key]
        start = self.pill_at or (x0, x1)

        def step(t):
            a = start[0] + (x0 - start[0]) * t
            b = start[1] + (x1 - start[1]) * t
            self.nav.coords(self.pill, *j.round_points(a, px(3), b, px(35), px(17)))
            self.nav.coords(self.pill_line, a + px(18), px(34), b - px(18), px(34))
            self.pill_at = (a, b)
        self.anim.run("pill", 380, step, ease=j.ease_back)

    def intro(self, fade: bool):
        """Startup: fade the window in and play the opening view's entrance."""
        if fade:
            self.anim.run("fade", 300, lambda t: self.root.attributes("-alpha", t))
        if self.view == "gpa":
            self.enter_gpa()
        elif self.view == "marks":
            self.fill_meters()

    def enter_gpa(self):
        """The GPA view's entrance: bars grow in one after another, tiles count up, the ring sweeps."""
        self.anim.run("grow", 900, self.set_grow, ease=lambda t: t)
        self.count_tiles(from_zero=True)
        self.fill_meters()

    def set_grow(self, t):
        self.grow = t
        self.draw_chart()

    def fill_meters(self):
        """Animate every registered meter from empty to its value."""
        self.meters = [m for m in self.meters if m[0].winfo_exists()]

        def step(t):
            self.meter_t = t
            for cv, frac, colour in self.meters:
                self.draw_meter(cv, frac, colour)
        self.anim.run("meters", 750, step)

    def draw_meter(self, cv, frac, colour):
        try:
            w, h = cv.winfo_width(), cv.winfo_height()
            cv.delete("all")
        except tk.TclError:
            return
        if w <= 2:
            return
        j.rounded(cv, 0, 0, w, h, h / 2, fill=FIELD, outline="")
        fill = w * frac * getattr(self, "meter_t", 1.0)
        if frac > 0 and fill > 0.5:
            j.rounded(cv, 0, 0, max(h, fill), h, h / 2, fill=colour, outline="")

    def meter(self, parent, frac: float, colour: str, width: int, height: int, bg=PANEL):
        """A rounded progress bar; registered so it fills when its view opens."""
        cv = tk.Canvas(parent, width=width, height=height, bg=bg, highlightthickness=0)
        frac = max(0.0, min(1.0, frac))
        cv.bind("<Configure>", lambda e: self.draw_meter(cv, frac, colour))
        self.meters.append((cv, frac, colour))
        return cv

    # ------------------------------------------------------------------ GPA tiles
    def set_tile(self, key: str, value, fmt):
        """Point a tile at a new number: it counts there if the GPA view is showing, else just jumps."""
        self.tile_targets = getattr(self, "tile_targets", {})
        if value is None:
            self.tile_targets.pop(key, None)
            self.shown.pop(key, None)
            self.tiles[key][1].config(text="—")
            if key == "gpa":
                self.draw_ring(None)
            return
        self.tile_targets[key] = (value, fmt)
        if self.view == "gpa" and key in self.shown:
            start = self.shown[key]
            self.anim.run(f"tile-{key}", 600, lambda t: self.show_tile(key, start + (value - start) * t, fmt))
        else:
            self.show_tile(key, value, fmt)

    def show_tile(self, key, value, fmt):
        self.shown[key] = value
        self.tiles[key][1].config(text=fmt(value))
        if key == "gpa":
            self.draw_ring(value)

    def count_tiles(self, from_zero: bool = False):
        targets = dict(getattr(self, "tile_targets", {}))
        if not targets:
            return
        starts = {k: 0.0 if from_zero else self.shown.get(k, v) for k, (v, _) in targets.items()}
        for k in targets:
            self.anim.cancel(f"tile-{k}")

        def step(t):
            for k, (v, fmt) in targets.items():
                self.show_tile(k, starts[k] + (v - starts[k]) * t, fmt)
        self.anim.run("tiles", 1000, step)

    def top_points(self) -> float:
        return max(self.book.grade_points.values()) if self.book and self.book.grade_points else 4.0

    def draw_ring(self, gpa):
        cv, px = self.ring, self.px
        cv.delete("all")
        c, r = px(35), px(28)
        j.ring(cv, c, c, r, px(7), (gpa or 0) / self.top_points(), NEON, FIELD)
        letter = self.gpa_letter(gpa) if gpa else ""
        cv.create_text(c, c, text=letter, fill=letter_colour(letter), font=self.f_head)

    def gpa_letter(self, gpa) -> str:
        """The letter whose grade points a GPA reaches: 3.62 -> B+, 3.74 -> A-."""
        if gpa is None or not self.book or not self.book.grade_points:
            return ""
        best = min(self.book.grade_points, key=self.book.grade_points.get)
        for letter, pts in sorted(self.book.grade_points.items(), key=lambda kv: kv[1]):
            if gpa + 1e-9 >= pts:
                best = letter
        return best

    def update_subtitle(self):
        if not self.book:
            return
        tr = self.transcript
        self.standing_hint.config(text=f"{self.book.term} · average needed on what's left")
        if tr and tr.gpa is not None:
            colour = letter_colour(self.gpa_letter(tr.gpa))
            self.chip.config(text=f"CGPA  {g.fgpa(tr.gpa)}", fg=colour, bg=tint(colour, 0.2))
        else:
            self.chip.config(text="CGPA  —", fg=MUTED, bg=FIELD)

    # ------------------------------------------------------------------ data
    def reload(self, keep_item: str = None):
        try:
            self.book = self.lib.load()
            self.mtime = self.lib.mtime()
        except Exception as e:  # bad JSON etc.: show it, keep the window
            messagebox.showerror("Grade Tracker", f"Could not read {self.lib.data_path}:\n\n{e}")
            self.book = g.GradeBook({"courses": []})
        codes = [f"{c.code} — {c.title}" if c.title else c.code for c in self.book.courses]
        self.course_box["values"] = codes
        if codes and (not self.course_var.get() or self.course_var.get() not in codes):
            self.course_var.set(codes[0])
        if self.book.courses:
            self.refresh_items(keep_item)
            self.show_detail(self.course().code)
        else:
            self.items = []
            self.selected = None
        self.refresh_targets()
        self.refresh_grid()
        self.refresh_recent()
        self.load_transcript()
        self.update_subtitle()
        if self.view == "marks":
            self.views["welcome" if not self.book.courses else "marks"].tkraise()

    def load_transcript(self):
        try:
            self.transcript = self.lib.load_transcript(self.book)
            self.tr_error = None
        except Exception as e:  # the marks view keeps working without it
            self.transcript, self.tr_error = None, str(e)
        self.tr_mtime = self.lib.transcript_mtime()
        codes = {c.code for c in self.book.courses}
        self.overrides = {k: v for k, v in self.overrides.items() if k in codes}
        self.refresh_gpa()
        self.refresh_past()

    def on_focus(self, _event=None):
        if self.lib.mtime() != self.mtime:
            self.reload(self.current_item())
        elif self.lib.transcript_mtime() != self.tr_mtime:
            self.load_transcript()
            self.update_subtitle()

    def course(self) -> g.Course:
        value = self.course_var.get()
        for c in self.book.courses:
            if value == c.code or value.startswith(c.code + " — "):
                return c
        return self.book.courses[0]

    def refresh_items(self, keep: str = None):
        course = self.course()
        self.items, labels = [], []
        for comp in course.components:
            if comp.type == "better":
                continue
            for it in comp.items:
                tag = ""
                if g.graded(it):
                    tag = f"   {comp.item_short(it)}"
                elif it.get("flag"):
                    tag = "   ⚠ flagged"
                if it.get("exclude"):
                    tag += "  (not counted)"
                self.items.append((comp, it["name"]))
                prefixed = len(comp.items) > 1 and not it["name"].lower().startswith(comp.short.lower())
                labels.append(f"{comp.short}: {it['name']}{tag}" if prefixed else f"{it['name']}{tag}")
            tpl = comp.raw.get("new_item")
            if tpl:
                nxt = max((g.item_number(it["name"]) or 0 for it in comp.items), default=0) + 1
                name = g.fill(tpl, {"n": nxt, "nn": f"{nxt:02d}"})
                self.items.append((comp, name))
                labels.append(f"{comp.short}: + new {name}")
        self.item_box["values"] = labels
        names = [n for _, n in self.items]
        if keep in names:
            idx = names.index(keep)
        else:
            idx = next((i for i, (c, n) in enumerate(self.items)
                        if not any(it["name"] == n and (g.graded(it) or it.get("exclude")) for it in c.items)), 0)
        if self.items:
            self.item_box.current(idx)
            self.on_item()
        else:
            self.item_var.set("")
            self.hint.config(text="This course has nothing to log.")

    def current_item(self):
        i = self.item_box.current()
        return self.items[i][1] if 0 <= i < len(self.items) else None

    def on_course(self):
        self.refresh_items()
        self.show_detail(self.course().code)
        self.refresh_grid()

    def on_item(self):
        i = self.item_box.current()
        if i < 0 or i >= len(self.items):
            return
        comp, name = self.items[i]
        it = next((x for x in comp.items if x["name"] == name), {"name": name})
        hint = g.item_hint(comp, it)
        if g.graded(it):
            hint += f"  ·  now {comp.item_short(it)} (saving replaces it)"
        elif it.get("flag"):
            hint += f"  ·  ⚠ {it['flag']}"
        if it.get("due"):
            hint += f"  ·  due {it['due']}"
        if name not in [x["name"] for x in comp.items]:
            hint += "  ·  adds a new day, dated with LOGGED"
        self.hint.config(text=hint)
        if not self.date_var.get():
            self.date_var.set(g.short_date(g.dt.date.today()))
        if self.view == "marks":
            self.mark_entry.focus_set()

    # ------------------------------------------------------------------ standing grid
    def grid_targets(self):
        """The target letters the grid's columns show: the first course's (they're usually all the same)."""
        if self.book and self.book.courses:
            return self.book.courses[0].targets()
        return []

    def refresh_targets(self):
        for w in self.target_chips.winfo_children():
            w.destroy()
        px = self.px
        for letter, floor in reversed(self.grid_targets()):
            tk.Label(self.target_chips, text=f"{letter} {g.fnum(floor)}", bg=tint(letter_colour(letter)),
                     fg=letter_colour(letter), font=self.f_chip, padx=px(7), pady=px(1)).pack(
                side="left", padx=(px(6), 0), pady=(px(2), 0))

    def refresh_grid(self):
        for w in self.grid.winfo_children():
            w.destroy()
        self.meters = [m for m in self.meters if m[0].winfo_exists()]
        px = self.px
        self.rows = {}
        letters = [l for l, _ in self.grid_targets()]
        heads = ["", "Course", "Graded", "", ""] + letters + ["Watch"]
        ncol = len(heads)
        for col, h in enumerate(heads):
            tk.Label(self.grid, text=h.upper(), bg=PANEL, fg=MUTED, font=self.f_chip, anchor="w").grid(
                row=0, column=col, sticky="ew", padx=(px(8), px(4)), pady=(0, px(6)))
        self.grid.columnconfigure(ncol - 1, weight=1)
        for r, course in enumerate(self.book.courses, start=1):
            s = course.summary()
            chosen = course.code == self.selected
            bg = SELECT if chosen else PANEL
            # A full-width frame under the row's cells: it carries the row colour between them.
            band = tk.Frame(self.grid, bg=bg, cursor="hand2")
            band.grid(row=r, column=0, columnspan=ncol, sticky="nsew", pady=px(1))
            plain = [band]

            def cell(widget, col, sticky="w", padx=(px(8), px(4))):
                widget.grid(row=r, column=col, sticky=sticky, padx=padx, pady=px(1))
                widget.bind("<Button-1>", lambda e, code=course.code: self.pick_course(code))
                return widget

            stripe = cell(tk.Frame(self.grid, bg=ACCENT if chosen else bg, width=px(3)), 0, "ns", (0, 0))
            if not chosen:
                plain.append(stripe)
            plain.append(cell(tk.Label(self.grid, text=course.code, bg=bg, fg=TEXT, font=self.f_bold,
                                       cursor="hand2"), 1))
            if s["graded_avg"] is not None:
                colour = letter_colour(s["graded_letter"])
                plain.append(cell(tk.Label(self.grid, text=f"{g.fnum(s['graded_avg'], 1)}%", bg=bg, fg=TEXT,
                                           font=self.f_base, cursor="hand2"), 2))
                cell(tk.Label(self.grid, text=s["graded_letter"], bg=colour, fg="#06101f", font=self.f_chip,
                              width=3, cursor="hand2"), 3)
                bar = cell(self.meter(self.grid, s["graded_avg"] / 100, colour, px(70), px(6), bg=bg), 4)
                plain.append(bar)
            else:
                plain.append(cell(tk.Label(self.grid, text="—", bg=bg, fg=MUTED, font=self.f_base), 2))
            floors = dict(s["targets"])
            for col, letter in enumerate(letters, start=5):
                if letter in s["need"]:
                    text, colour = need_text(s["need"][letter], floors[letter])
                    cell(tk.Label(self.grid, text=text, bg=tint(colour, 0.14), fg=colour, font=self.f_bold,
                                  padx=px(7), cursor="hand2"), col)
                else:
                    plain.append(cell(tk.Label(self.grid, text="—", bg=bg, fg=MUTED, font=self.f_base), col))
            watch, colour = "", MUTED
            if s["flags"]:
                watch, colour = "⚠ " + s["flags"][0].split(":")[0] + (f" +{len(s['flags']) - 1}" if len(s["flags"]) > 1 else ""), WARN
            elif any("[x]" in gt for gt in s["gates"]):
                watch, colour = "✗ requirement at risk", BAD
            else:
                watch, colour = ("✓ requirements ok", GOOD) if s["gates"] else ("", MUTED)
            plain.append(cell(tk.Label(self.grid, text=watch, bg=bg, fg=colour, font=self.f_small, anchor="w",
                                       cursor="hand2"), ncol - 1, "ew"))
            self.rows[course.code] = (plain, bg)
            for w in plain + [band]:
                w.bind("<Enter>", lambda e, code=course.code: self.hover_row(code, True), add="+")
                w.bind("<Leave>", lambda e, code=course.code: self.hover_row(code, False), add="+")
            band.bind("<Button-1>", lambda e, code=course.code: self.pick_course(code))
        self.fill_meters()

    def hover_row(self, code: str, on: bool):
        plain, bg = self.rows.get(code, ([], PANEL))
        colour = HOVER_ROW if on and bg == PANEL else bg
        for w in plain:
            try:
                w.config(bg=colour)
            except tk.TclError:
                pass

    def flash_row(self, code: str, colour: str = GOOD):
        """Pulse a course's row: it lights up in colour and fades back."""
        plain, bg = self.rows.get(code, ([], PANEL))
        if not plain:
            return

        def step(t):
            c = j.mix(tint(colour, 0.35), bg, t)
            for w in plain:
                try:
                    w.config(bg=c)
                except tk.TclError:
                    pass
        self.anim.run(f"flash-{code}", 1100, step, ease=j.ease_in_out)

    def pick_course(self, code: str):
        for v in self.course_box["values"]:
            if v == code or v.startswith(code + " — "):
                self.course_var.set(v)
        self.on_course()

    def show_detail(self, code: str):
        self.selected = code
        course = self.book.course(code)
        self.detail.config(state="normal")
        self.detail.delete("1.0", "end")
        lines = g.course_report(course).splitlines()
        self.detail.insert("end", lines[0] + "\n", "title")
        for line in lines[1:]:
            tag = None
            if line.lstrip().startswith("Needed on"):
                tag = "need"
            elif line.lstrip().startswith(("Requirement:", "Flag:")):
                tag = "bad" if "[x]" in line else ("warn" if ("[!]" in line or "Flag:" in line or "[~]" in line)
                                                   else "good" if "[ok]" in line else "muted")
            elif line.lstrip().startswith(("Basis:", "Exemption", "Note:")):
                tag = "muted"
            self.detail.insert("end", g.plain(line) + "\n", tag)
        self.detail.config(state="disabled")

    def refresh_recent(self):
        rows = self.book.data.get("log", [])[-8:][::-1]
        t = self.recent
        t.config(state="normal")
        t.delete("1.0", "end")
        if not rows:
            t.insert("end", "Nothing logged yet.", "date")
        for i, r in enumerate(rows):
            mark = r["mark"].replace("**", "").split(" · ")[0]
            item = r["item"] if len(r["item"]) <= 22 else r["item"][:21].rstrip() + "…"
            mark = mark if len(mark) <= 16 else mark[:15].rstrip() + "…"
            t.insert("end", f"{r['date']:<7}", "date")
            t.insert("end", f"{r['course']}  ", "code")
            t.insert("end", f"{item}  ", "item")
            t.insert("end", mark + ("\n" if i < len(rows) - 1 else ""), "mark")
        t.config(state="disabled")

    # ------------------------------------------------------------------ GPA: numbers
    def pace_rows(self):
        return g.pace(self.book)

    def picks(self, rows, what_if: bool = True):
        return [(r["credits"], self.overrides.get(r["code"], r["letter"]) if what_if else r["letter"]) for r in rows]

    def refresh_gpa(self):
        px = self.px
        for frame in (self.subject_rows, self.proj_rows, self.proj_result, self.quick):
            for w in frame.winfo_children():
                w.destroy()
        tr = self.transcript
        self.proj_title.config(text=f"{self.book.term} projection")
        if not tr:
            for head, value, sub in self.tiles.values():
                head.config(text="")
                value.config(text="—")
                sub.config(text="")
            self.tiles["gpa"][2].config(text=f"Could not load the transcript:\n{self.tr_error}", fg=BAD)
            self.draw_chart()
            return
        counted = [c for c in tr.courses() if c.counted]
        skipped = [c for c in tr.courses() if not c.counted]
        self.tiles["gpa"][0].config(text="CUMULATIVE GPA")
        self.set_tile("gpa", tr.gpa, g.fgpa)
        self.tiles["gpa"][2].config(text=f"out of {g.fnum(self.top_points(), 1)}" if not tr.empty
                                    else "import a transcript on Past semesters", fg=MUTED)
        self.tiles["hours"][0].config(text="CREDIT HOURS")
        self.set_tile("hours", tr.hours, lambda v: g.fnum(round(v)))
        self.tiles["hours"][2].config(text=f"{len(counted)} graded courses"
                                      + (f" · {len(skipped)} not counted" if skipped else ""))
        self.tiles["qp"][0].config(text="QUALITY POINTS")
        self.set_tile("qp", tr.qp, g.fixed)
        self.tiles["qp"][2].config(text="grade points × credits, summed")

        # by subject
        subjects = [s for s in tr.by_subject(limit=3) if s["courses"]]
        if not subjects:
            self.autowrap(tk.Label(self.subject_rows, text="Your GPA by subject shows up here once you import a "
                                                           "transcript.", bg=PANEL, fg=MUTED, font=self.f_small,
                                   anchor="w", justify="left"), 400).grid(row=0, column=0, columnspan=4, sticky="ew")
        for r, row in enumerate(subjects):
            pady = (0, px(9))
            colour = letter_colour(self.gpa_letter(row["gpa"]))
            tk.Label(self.subject_rows, text=row["subject"], bg=PANEL, fg=TEXT, font=self.f_bold, anchor="w",
                     width=6).grid(row=r, column=0, sticky="w", pady=pady)
            tk.Label(self.subject_rows, text=g.fgpa(row["gpa"]), bg=PANEL, fg=colour, font=self.f_bold,
                     anchor="e").grid(row=r, column=1, sticky="e", padx=(px(4), px(10)), pady=pady)
            self.meter(self.subject_rows, (row["gpa"] or 0) / self.top_points(), colour, px(120), px(8)).grid(
                row=r, column=2, sticky="w", pady=pady)
            n = len(row["courses"])
            if row["subject"] == "Other":   # one line each, so the chart above keeps its room
                subs = sorted({g.subject_of(code) for code, _ in row["courses"]})
                detail = f"{n} course{'s' if n != 1 else ''} · " + ", ".join(subs[:5]) + (" …" if len(subs) > 5 else "")
            else:
                shown = [f"{code.split()[-1]} {letter}" for code, letter in row["courses"]]
                detail = f"{n} course{'s' if n != 1 else ''} · " + " · ".join(shown[:4]) + (" …" if n > 4 else "")
            self.autowrap(tk.Label(self.subject_rows, text=detail, bg=PANEL, fg=MUTED, font=self.f_small,
                                   anchor="w", justify="left"), 260).grid(row=r, column=3, sticky="ew",
                                                                         padx=(px(12), 0), pady=pady)

        # projection rows
        rows = self.pace_rows()
        if not rows:
            tk.Label(self.proj_rows, text="Add this term's courses to project your GPA.", bg=PANEL, fg=MUTED,
                     font=self.f_small, anchor="w").grid(row=0, column=0, sticky="w")
        else:
            for col, h in enumerate(("COURSE", "GRADED", "PACE", "WHAT-IF")):
                tk.Label(self.proj_rows, text=h, bg=PANEL, fg=MUTED, font=self.f_chip, anchor="w").grid(
                    row=0, column=col, sticky="w", padx=(0, px(14)), pady=(0, px(6)))
        choices = self.book.letter_choices()
        for r, row in enumerate(rows, start=1):
            ga = f"{g.fnum(row['graded_avg'], 1)}%" if row["graded_avg"] is not None else "nothing back"
            tk.Label(self.proj_rows, text=row["code"], bg=PANEL, fg=TEXT, font=self.f_bold, anchor="w").grid(
                row=r, column=0, sticky="w", padx=(0, px(14)), pady=px(4))
            tk.Label(self.proj_rows, text=ga, bg=PANEL, fg=TEXT, font=self.f_base, anchor="w").grid(
                row=r, column=1, sticky="w", padx=(0, px(14)))
            pace = tk.Frame(self.proj_rows, bg=PANEL)
            pace.grid(row=r, column=2, sticky="w", padx=(0, px(14)))
            tk.Label(pace, text=row["letter"], bg=letter_colour(row["letter"]), fg="#06101f", font=self.f_chip,
                     width=3).pack(side="left")
            if row["assumed"]:
                tk.Label(pace, text="assumed", bg=PANEL, fg=MUTED, font=self.f_small).pack(side="left",
                                                                                         padx=(px(6), 0))
            var = tk.StringVar(value=self.overrides.get(row["code"], row["letter"]))
            box = ttk.Combobox(self.proj_rows, textvariable=var, values=choices, state="readonly", width=4,
                               style="Letter.TCombobox", height=len(choices))
            box.grid(row=r, column=3, sticky="w")
            box.bind("<<ComboboxSelected>>", lambda e, code=row["code"], v=var, p=row["letter"]: self.pick_letter(
                code, v.get(), p))
        if rows:
            for i, letter in enumerate(list(self.book.grade_points)[:3]):
                cum = g.project(tr, [(r["credits"], letter) for r in rows])["cumulative"]
                style = f"{style_key(letter)}.Small.TButton" if letter in LETTER_COLOURS else "Small.TButton"
                ttk.Button(self.quick, text=f"{letter}  →  {g.fgpa(cum)}", style=style, cursor="hand2",
                           command=lambda l=letter: self.set_all(l)).pack(side="left", padx=(0 if i == 0 else px(6), 0))
        self.refresh_projection(rows)
        self.fill_meters()

    def refresh_projection(self, rows=None):
        px = self.px
        for w in self.proj_result.winfo_children():
            w.destroy()
        tr = self.transcript
        rows = self.pace_rows() if rows is None else rows
        head, value, sub = self.tiles["proj"]
        head.config(text=f"{self.book.term.upper()} · PROJECTED")
        if not rows:
            self.set_tile("proj", None, g.fgpa)
            sub.config(text="no courses this term yet", fg=MUTED)
            self.proj_shown = None
            self.draw_chart()
            return
        pace = g.project(tr, self.picks(rows, what_if=False))
        picked = g.project(tr, self.picks(rows))
        changed = bool(self.overrides)
        heads = ("", "AT PACE", "WHAT-IF" if changed else "WHAT-IF (= pace)")
        for col, h in enumerate(heads):
            tk.Label(self.proj_result, text=h, bg=PANEL, fg=MUTED, font=self.f_small, anchor="e" if col else "w").grid(
                row=0, column=col, sticky="e" if col else "w", pady=(0, px(4)))

        def change(res):   # between the rounded figures, so 3.68 against 3.72 reads −0.04
            if res["cumulative"] is None or tr.gpa is None:
                return 0.0
            return float(g.fgpa(res["cumulative"])) - float(g.fgpa(tr.gpa))
        lines = [("Term GPA", g.fgpa(pace["term_gpa"]), g.fgpa(picked["term_gpa"]), None, None),
                 (f"Cumulative on {g.fnum(picked['hours'])} h" if pace["hours"] == picked["hours"] else "Cumulative",
                  g.fgpa(pace["cumulative"]), g.fgpa(picked["cumulative"]), None, None)]
        if tr.gpa is not None:
            lines.append(("Change from " + g.fgpa(tr.gpa), signed(change(pace)), signed(change(picked)),
                          change(pace), change(picked)))
        lines.append(("Hours · quality points", f"{g.fnum(pace['hours'])} · {g.fixed(pace['qp'])}",
                      f"{g.fnum(picked['hours'])} · {g.fixed(picked['qp'])}", None, None))
        for r, (label, a, b, da, db) in enumerate(lines, start=1):
            big = r <= 2
            font = ("Segoe UI Semibold", 14) if big else self.f_base
            tk.Label(self.proj_result, text=label, bg=PANEL, fg=TEXT if big else MUTED, font=self.f_base if big
                     else self.f_small, anchor="w").grid(row=r, column=0, sticky="w", pady=px(2))
            for col, (val, delta) in enumerate(((a, da), (b, db)), start=1):
                if delta is not None:   # the sign carries the meaning; colour only repeats it
                    fg = MUTED if g.fixed(abs(delta)) == "0.00" else (GOOD if delta > 0 else BAD)
                else:
                    fg = MUTED if col == 1 and changed and big else TEXT
                tk.Label(self.proj_result, text=val, bg=PANEL, fg=fg, font=font, anchor="e").grid(
                    row=r, column=col, sticky="e", pady=px(2))
        self.set_tile("proj", picked["cumulative"], g.fgpa)
        delta = change(picked)
        if tr.gpa is None:
            sub.config(text=f"{'what-if' if changed else 'at pace'} · term {g.fgpa(picked['term_gpa'])}", fg=MUTED)
        else:
            sub.config(text=f"{'what-if' if changed else 'at pace'} · {signed(delta)} · term {g.fgpa(picked['term_gpa'])}",
                       fg=MUTED if g.fixed(abs(delta)) == "0.00" else (GOOD if delta > 0 else BAD))
        # Slide the chart's projected column to the new numbers rather than jumping.
        new = (picked["term_gpa"], picked["cumulative"])
        old = self.proj_shown or new

        def step(t):
            self.proj_shown = tuple(None if b is None else (b if a is None else a + (b - a) * t)
                                    for a, b in zip(old, new))
            self.draw_chart()
        self.anim.run("proj", 450, step, ease=j.ease_back)

    def pick_letter(self, code: str, letter: str, pace_letter: str):
        if letter == pace_letter:
            self.overrides.pop(code, None)
        else:
            self.overrides[code] = letter
        self.refresh_projection()

    def set_all(self, letter: str):
        rows = self.pace_rows()
        self.overrides = {r["code"]: letter for r in rows if r["letter"] != letter}
        self.refresh_gpa()

    def reset_what_if(self):
        self.overrides = {}
        self.refresh_gpa()

    # ------------------------------------------------------------------ GPA: chart
    def chart_slots(self):
        """One column per past term, then this term's projection."""
        tr = self.transcript
        slots = []
        for t in tr.terms:
            months, _, year = t.name.rpartition(" ")
            if not re.fullmatch(r"\d{4}", year):
                months, year = t.name, ""
            tip = [f"{t.name} · {len(t.courses)} course{'s' if len(t.courses) != 1 else ''}",
                   f"Term GPA {t.gpa_text()} · cumulative {g.fgpa(t.cumulative)}"]
            tip += [f"{c.code}  {c.letter_text()}" for c in t.courses]
            empty = "W" if t.gpa is None and all(str(c.letter).upper() in g.NO_GPA for c in t.courses) else "—"
            slots.append({"l1": months or t.name, "l2": year, "n": len(t.courses), "gpa": t.gpa,
                          "cum": t.cumulative, "proj": False, "term": t, "empty": empty, "tip": tip})
        rows = self.pace_rows()
        if rows:
            res = g.project(tr, self.picks(rows))
            name = self.book.term
            season, _, year = name.rpartition(" ")
            if not re.fullmatch(r"\d{4}", year):
                season, year = name, ""
            tip = [f"{name} · projected",
                   f"Term GPA {g.fgpa(res['term_gpa'])} · cumulative {g.fgpa(res['cumulative'])} on {g.fnum(res['hours'])} h"]
            tip += [f"{r['code']}  {self.overrides.get(r['code'], r['letter'])}"
                    + ("  what-if" if r["code"] in self.overrides else "  pace") for r in rows]
            gpa, cum = self.proj_shown or (res["term_gpa"], res["cumulative"])
            slots.append({"l1": season or name, "l2": year, "n": len(rows), "gpa": gpa,
                          "cum": cum, "proj": True, "term": None, "empty": "W", "tip": tip})
        return slots

    def round_top(self, cv, x0, y0, x1, y1, r, fill, tags=()):
        """A bar with 4px rounded top corners and a square base (repeated points stay sharp)."""
        r = min(r, (x1 - x0) / 2, max(y1 - y0, 0))
        pts = [x0, y1, x0, y1, x0, y0 + r, x0, y0, x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1, x1, y1]
        cv.create_polygon(pts, smooth=True, fill=fill, outline="", tags=tags)

    def gradient_bar(self, cv, x0, y0, x1, y1, colour, lit: bool = False, tags="bar"):
        """A chart bar: bright letter colour at the top fading darker toward the base, rounded cap."""
        if y1 - y0 < 1:
            return
        top = j.mix(colour, "#ffffff", 0.3) if lit else colour
        base = j.mix(PANEL, colour, 0.6 if lit else 0.38)
        cap = min(self.px(5), (y1 - y0))
        self.round_top(cv, x0, y0, x1, y0 + cap + 1, self.px(4), top, tags=tags)
        j.vgradient(cv, x0, y0 + cap, x1, y1, top, base, step=self.px(3), tags=tags)

    def dashed(self, cv, a, b, colour, width, dash=7, gap=5):
        """A dashed segment drawn piece by piece (Tk on Windows only dashes 1px lines)."""
        (x0, y0), (x1, y1) = a, b
        length = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
        if length == 0:
            return
        step, pos = dash + gap, 0.0
        while pos < length:
            end = min(pos + dash, length)
            cv.create_line(x0 + (x1 - x0) * pos / length, y0 + (y1 - y0) * pos / length,
                           x0 + (x1 - x0) * end / length, y0 + (y1 - y0) * end / length,
                           fill=colour, width=width, capstyle="round")
            pos += step

    def draw_chart(self):
        cv, px = self.chart, self.px
        cv.delete("all")
        self.slots = []
        W, H = cv.winfo_width(), cv.winfo_height()
        if W < 100 or H < 100:
            return
        if not self.transcript:
            cv.create_text(W / 2, H / 2, text="The transcript didn't load", fill=MUTED, font=self.f_base)
            return
        slots = self.chart_slots()
        if not slots:
            cv.create_text(W / 2, H / 2, text="Import a transcript on Past semesters to see your GPA history,\n"
                                              "and add courses to project this term.", fill=MUTED,
                           font=self.f_base, justify="center")
            return
        top = self.top_points()
        ml, mr, mt, mb = px(52), px(40), px(18), px(58)
        ph, pw = H - mt - mb, W - ml - mr
        sw = pw / len(slots)

        def y(v):
            return mt + ph * (1 - v / top)
        for v in range(0, int(top) + 1):
            cv.create_line(ml, y(v), W - mr, y(v), fill=LINE if v == 0 else GRIDLINE)
            cv.create_text(ml - px(10), y(v), text="0" if v == 0 else f"{v}.0", anchor="e", fill=MUTED,
                           font=self.f_small)
        cv.create_text(ml - px(10), H - mb + px(45), text="courses", anchor="e", fill=MUTED, font=self.f_small)

        small = tkfont.Font(font=self.f_small)
        if max(small.measure(s["l1"]) for s in slots) + px(6) > sw:   # 'Sep–Dec' won't fit: 'Sep'
            for s in slots:
                s["l1"] = re.split(r"[–-]", s["l1"])[0][:6]
        points = []
        n = len(slots)
        lag = 0.45 / max(n - 1, 1)   # each bar starts a beat after the one before it

        def grown(i):
            return j.ease_back(max(0.0, min(1.0, (self.grow - i * lag) / 0.55)))
        for i, s in enumerate(slots):
            cx = ml + sw * (i + 0.5)
            bw = min(sw * 0.56, px(30))
            s["x0"], s["x1"], s["right"], s["cx"], s["bw"] = ml + sw * i, ml + sw * (i + 1), cx + bw / 2, cx, bw
            k = grown(i)
            if s["gpa"] is not None and s["proj"]:
                w2 = px(2)
                top_y = y(s["gpa"] * k)
                cv.create_rectangle(cx - bw / 2 + w2 / 2, top_y, cx + bw / 2 - w2 / 2, y(0),
                                    fill=j.mix(PANEL, PINK, 0.12), outline="", tags="bar")
                self.dashed(cv, (cx - bw / 2 + w2 / 2, y(0)), (cx - bw / 2 + w2 / 2, top_y), PINK, w2, px(5), px(3))
                self.dashed(cv, (cx + bw / 2 - w2 / 2, y(0)), (cx + bw / 2 - w2 / 2, top_y), PINK, w2, px(5), px(3))
                cv.create_line(cx - bw / 2, top_y, cx + bw / 2, top_y, fill=PINK, width=w2)
            elif s["gpa"] is not None:
                s["colour"] = letter_colour(self.gpa_letter(s["gpa"]))
                s["bar"] = (cx - bw / 2, y(s["gpa"]), cx + bw / 2, y(0))
                self.gradient_bar(cv, cx - bw / 2, y(s["gpa"] * k), cx + bw / 2, y(0), s["colour"])
            else:
                cv.create_text(cx, y(0) - px(10), text=s["empty"], fill=MUTED, font=self.f_small)
            ink = TEXT if s["proj"] else MUTED
            cv.create_text(cx, H - mb + px(11), text=s["l1"], fill=ink, font=self.f_small)
            cv.create_text(cx, H - mb + px(26), text=s["l2"], fill=ink, font=self.f_small)
            cv.create_text(cx, H - mb + px(45), text=str(s["n"]), fill=MUTED, font=self.f_small)
            if s["cum"] is not None and k > 0:
                points.append((cx, y(s["cum"]), s))

        past = [(x, yy) for x, yy, s in points if not s["proj"]]
        if len(past) > 1:   # a wide dim stroke under a thin bright one reads as a glow
            flat = [c for p in past for c in p]
            cv.create_line(*flat, fill=CUM_GLOW, width=px(7), joinstyle="round", capstyle="round", tags="line")
            cv.create_line(*flat, fill=CUM_LINE, width=px(2), joinstyle="round", tags="line")
        proj = next(((x, yy) for x, yy, s in points if s["proj"]), None)
        if proj and past:
            self.dashed(cv, past[-1], proj, CUM_LINE, px(2), dash=px(6), gap=px(4))
        last = next((s for _, _, s in reversed(points) if not s["proj"]), None)
        r = px(5)
        for x, yy, s in points:
            if s["proj"]:
                cv.create_oval(x - r, yy - r, x + r, yy + r, fill=PANEL, outline=CUM_LINE, width=px(2), tags="line")
            else:
                cv.create_oval(x - r, yy - r, x + r, yy + r, fill=CUM_LINE, outline=PANEL, width=px(2), tags="line")
        if last:   # label the line's ends only: today's cumulative and the projected one
            x, yy = past[-1]
            cv.create_text(x, yy - px(14), text=g.fgpa(last["cum"]), fill=TEXT, font=self.f_bold, tags="line")
        if proj:
            right = slots[-1]["right"] if slots[-1]["gpa"] is not None else proj[0] + r
            cv.create_text(right + px(6), proj[1], text=g.fgpa(slots[-1]["cum"]), anchor="w", fill=TEXT,
                           font=self.f_bold, tags="line")
        self.slots = slots
        self.chart_box = (mt, H)

    def slot_at(self, x):
        return next((s for s in self.slots if s["x0"] <= x < s["x1"]), None)

    def chart_hover(self, e):
        cv, px = self.chart, self.px
        cv.delete("hover")
        s = self.slot_at(e.x)
        if not s:
            cv.config(cursor="")
            return
        mt, H = self.chart_box
        band = cv.create_rectangle(s["x0"] + 1, mt - px(10), s["x1"] - 1, H - px(2), fill=HOVER_BAND, outline="",
                                   tags="hover")
        cv.tag_lower(band)
        if s.get("bar") and self.grow >= 1:   # light the hovered bar up
            x0, y0, x1, y1 = s["bar"]
            grow = px(2)
            self.gradient_bar(cv, x0 - grow, y0 - grow, x1 + grow, y1, s["colour"], lit=True, tags="hover")
            cv.tag_raise("line")
        text = cv.create_text(0, 0, text="\n".join(s["tip"]), anchor="nw", fill=TEXT, font=self.f_small,
                              tags="hover")
        x0, y0, x1, y1 = cv.bbox(text)
        w, h = x1 - x0 + px(24), y1 - y0 + px(16)
        W = cv.winfo_width()
        tx = e.x + px(16) if e.x + px(16) + w < W else e.x - px(16) - w
        ty = max(px(2), min(e.y - h / 2, H - h - px(2)))
        accent = s.get("colour", PINK)
        j.rounded(cv, tx, ty, tx + w, ty + h, px(8), fill=FIELD, outline=LINE, tags="hover")
        cv.create_rectangle(tx + px(1), ty + px(8), tx + px(4), ty + h - px(8), fill=accent, outline="",
                            tags="hover")
        cv.coords(text, tx + px(13), ty + px(8))
        cv.tag_raise(text)
        cv.config(cursor="" if s["proj"] else "hand2")

    def chart_click(self, e):
        s = self.slot_at(e.x)
        if s and s["term"] is not None:
            self.show_view("past")
            self.select_past(s["term"])

    # ------------------------------------------------------------------ Past semesters: tree
    def refresh_past(self):
        tree = self.tree
        keep = tree.selection()[0] if tree.selection() else None
        opened = {iid for iid in tree.get_children() if tree.item(iid, "open")}
        tree.delete(*tree.get_children())
        self.past_ids = {}
        tr = self.transcript
        if not tr:
            self.past_hint.config(text="")
            self.write_detail(self.past_detail, ["Could not load the transcript", "", self.tr_error or ""])
            return
        if tr.empty:
            self.past_hint.config(text="")
            self.write_detail(self.past_detail, [
                "No past semesters yet", "",
                "Import your transcript to see every past term, your",
                "cumulative GPA, and how this term moves it.", "",
                "  1. Click Import transcript… and copy the prompt.",
                "  2. Give it to your AI with your unofficial transcript",
                "     (a PDF, screenshots or photos all work).",
                "  3. Paste its reply back into the app.", "",
                "The app recomputes every GPA from the letters and",
                "warns you if a figure doesn't match the transcript."])
            return
        self.past_hint.config(text=f"{g.fnum(tr.hours)} h · {g.fixed(tr.qp)} QP")
        latest = next((f"t{i}" for i in range(len(tr.terms) - 1, -1, -1) if tr.terms[i].hours), None)
        for i, t in enumerate(tr.terms):
            tid = f"t{i}"
            n = len(t.courses)
            label = f"{t.name}   ·   {n} course{'s' if n != 1 else ''}"
            dot = self.dots.get(self.gpa_letter(t.gpa), self.dots[""]) if t.gpa is not None else self.dots["term"]
            tree.insert("", "end", iid=tid, text="  " + label, tags=("term",), image=dot,
                        open=tid in opened if opened else tid == latest,
                        values=("", t.gpa_text() if t.gpa is not None else "—", g.fgpa(t.cumulative),
                                "" if t.gpa is not None else t.gpa_text()))
            self.past_ids[tid] = t
            for k, c in enumerate(t.courses):
                cid = f"{tid}c{k}"
                flags = c.flags(short=True)
                tree.insert(tid, "end", iid=cid, text=f"  {c.code}  {c.title}",
                            image=self.dots.get(c.letter or "", self.dots[""]),
                            tags=("off",) if not c.counted else (("flagged",) if flags else ("course",)),
                            values=(c.letter or "—", "", "", ("⚠ " if c.warns() else "") + " · ".join(flags)))
                self.past_ids[cid] = c
        target = keep if keep in self.past_ids else latest
        if target:
            tree.selection_set(target)
            tree.see(target)
        self.on_past_select()

    def open_all(self, state: bool):
        for iid in self.tree.get_children():
            self.tree.item(iid, open=state)

    def select_past(self, term):
        iid = next((k for k, v in self.past_ids.items() if v is term), None)
        if iid:
            self.tree.item(iid, open=True)
            self.tree.selection_set(iid)
            self.tree.see(iid)
            kids = self.tree.get_children(iid)
            if kids:
                self.tree.see(kids[-1])
                self.tree.see(iid)

    def on_past_select(self):
        sel = self.tree.selection()
        obj = self.past_ids.get(sel[0]) if sel else None
        if obj is None:
            return
        if isinstance(obj, g.PastTerm):
            self.tree.item(sel[0], open=True)
            lines = g.term_report(obj).splitlines()
            lines += ["", "  Click a course for its details."]
            mism = [m for m in self.transcript.mismatches() if m.startswith(obj.name + " ")]
            lines += [f"  [!] {m}" for m in mism]
        else:
            lines = g.past_course_report(obj).splitlines()
        self.write_detail(self.past_detail, lines)

    def write_detail(self, text: tk.Text, lines):
        """Show report lines; wrapped lines hang under their own indent (a course row's title column)."""
        cw = tkfont.Font(font=self.f_mono).measure("0")
        text.config(state="normal")
        text.delete("1.0", "end")
        for i, line in enumerate(lines):
            s = line.strip()
            if i == 0:
                tag = "title"
            elif "[!]" in line:
                tag, line = "warn", line.replace("[!]", "⚠")
            elif s.startswith(("Note:", "No grade sheet", "Click a course", "Component ", "Could not")):
                tag = "muted"
            elif s.startswith(("Letter:", "Total ", "Term GPA", "Source:")):
                tag = "need"
            elif line.startswith(" " * 23):   # a course's note in the term list
                tag = "muted"
            else:
                tag = None
            lead = len(line) - len(line.lstrip(" "))
            hang = 23 if re.match(r"  [A-Z]{2,5}[ -]?\d{3,4}", line) else lead + 2 + (4 if "⚠" in line else 0)
            indent = f"hang{hang}"
            text.tag_configure(indent, lmargin2=hang * cw)
            text.insert("end", line + "\n", (tag, indent) if tag else (indent,))
        text.config(state="disabled")

    # ------------------------------------------------------------------ actions
    def set_status(self, text: str, kind: str = "muted"):
        self.status.config(text=text, fg={"good": GOOD, "bad": BAD, "warn": WARN}.get(kind, MUTED))
        if kind == "bad":
            self.shake(self.status)

    def shake(self, widget):
        """A quick, decaying side-to-side wobble: 'no'."""
        px = self.px
        amp = px(8)

        def step(t):
            off = abs(round(amp * (1 - t) * math.sin(t * 6 * math.pi)))
            widget.grid_configure(padx=(off, 2 * amp - off))
        self.anim.run(f"shake-{id(widget)}", 420, step, ease=lambda t: t,
                      done=lambda: widget.grid_configure(padx=(0, 2 * amp)))

    def save(self):
        i = self.item_box.current()
        if i < 0 or i >= len(self.items):
            return
        comp, name = self.items[i]
        course = self.course()
        try:
            book = self.lib.load()  # always start from what's on disk
            c = book.course(course.code)
            try:
                _, it, _ = c.find_item(name)
            except g.GradeError:
                it = None
            if it and g.graded(it):
                if not messagebox.askyesno("Replace mark?",
                                           f"{course.code} {name} already has {comp.item_short(it)}.\n\nReplace it?"):
                    return
            desc = self.date_var.get().strip() if it is None else None
            change = g.log_mark(book, course.code, name, self.mark_var.get(),
                                date=self.date_var.get().strip() or None, note=self.note_var.get().strip() or None,
                                desc=desc)
            self.lib.save(book)
        except g.GradeError as e:
            self.set_status(str(e), "bad")
            self.toast.show(str(e).splitlines()[0][:90], BAD, icon="✗", ms=4500)
            return
        except Exception as e:  # pragma: no cover - surfaced to the user
            self.set_status(f"Unexpected error: {e}", "bad")
            traceback.print_exc()
            return
        self.mark_var.set("")
        self.note_var.set("")
        self.set_status("Saved: " + change.entry, "good")
        try:
            pct = change.comp.item_pct(change.item)
        except Exception:   # an attendance-style item has no percentage
            pct = None
        self.reload()
        self.celebrate(course, pct, change.entry)

    def celebrate(self, course, pct, entry: str):
        """Feedback for a saved mark, scaled to how good it was."""
        letter = course.letter(pct) if pct is not None else None
        colour = letter_colour(letter) if letter else GOOD
        self.flash_row(course.code, colour)
        self.save_btn.configure(style="Good.TButton")
        self.root.after(700, lambda: self.save_btn.configure(style="Accent.TButton"))
        cheer = "Nice!" if pct is not None and pct >= 90 else "Saved"
        self.toast.show(f"{cheer}  {entry}", colour, icon="★" if pct is not None and pct >= 90 else "✓")
        if pct is None or pct < 80 or not self.anim.enabled:
            return
        bx = self.save_btn.winfo_rootx() - self.root.winfo_rootx() + self.save_btn.winfo_width() / 2
        by = self.save_btn.winfo_rooty() - self.root.winfo_rooty()
        big = pct >= 90
        j.confetti(self.root, list(NEON) + [MINT, GOLD, CYAN], origin=(bx, by),
                   count=110 if big else 40, power=1.0 if big else 0.7, ms=1800 if big else 1200)

    def clear(self):
        i = self.item_box.current()
        if i < 0 or i >= len(self.items):
            return
        comp, name = self.items[i]
        course = self.course()
        it = next((x for x in comp.items if x["name"] == name), None)
        if not it or not g.graded(it):
            self.set_status(f"{name} has no mark to clear.", "warn")
            return
        if not messagebox.askyesno("Clear mark?", f"Remove {course.code} {name} ({comp.item_short(it)})?"):
            return
        try:
            book = self.lib.load()
            g.clear_mark(book, course.code, name)
            self.lib.save(book)
        except g.GradeError as e:
            self.set_status(str(e), "bad")
            return
        self.set_status(f"Cleared {course.code} {name}.", "warn")
        self.reload(name)
        self.flash_row(course.code, WARN)
        self.toast.show(f"Cleared {course.code} {name}", WARN, icon="↺")

    # ------------------------------------------------------------------ courses in and out
    def copy_prompt(self, kind: str = "syllabus"):
        self.root.clipboard_clear()
        self.root.clipboard_append(prompt(kind))
        what = "syllabus" if kind == "syllabus" else "unofficial transcript"
        self.toast.show(f"Prompt copied. Paste it into your AI along with your {what}.", CYAN, icon="⧉", ms=4500)

    def open_import(self, kind: str = "syllabus"):
        ImportDialog(self, kind)

    def add_example(self):
        text = (DATA / "example-course.json").read_text(encoding="utf-8")
        ImportDialog(self, "syllabus", text=text)

    def apply_import(self, new_lib: dict, new_tr: dict, kind: str, notes) -> None:
        self.lib.save(new_lib)
        if kind == "transcript":
            self.lib.save_transcript(new_tr)
        self.reload()
        if kind == "transcript":
            self.show_view("past")
        else:
            self.show_view("marks")
            added = [c["code"] for c in new_lib.get("courses", [])]
            if added:
                self.pick_course(added[-1])
        self.toast.show(" ".join(n for n in notes if not n.startswith("Check"))[:140] or "Imported", GOOD, icon="✓",
                        ms=4500)
        if self.book.courses and self.anim.enabled:
            j.confetti(self.root, list(NEON) + [MINT, GOLD, CYAN], count=70, power=0.8, ms=1400)

    def remove_course(self):
        if not self.book.courses:
            return
        course = self.course()
        marks = sum(len(c.graded_items()) for c in course.components)
        if not messagebox.askyesno("Remove course?",
                                   f"Remove {course.code} and its {marks} mark{'s' if marks != 1 else ''}?\n\n"
                                   "The previous version stays in gradebook.json.bak until your next change."):
            return
        self.lib.save(cf.remove_course(self.lib.load_data(), course.code))
        self.course_var.set("")
        self.reload()
        self.toast.show(f"Removed {course.code}", WARN, icon="↺")

    def share_course(self):
        if not self.book.courses:
            return
        course = self.course()
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Save a course file to share", defaultextension=".json",
            initialdir=documents_dir(), initialfile=f"{course.code} course file.json",
            filetypes=[("Course file", "*.json"), ("All files", "*.*")])
        if not path:
            return
        Path(path).write_text(json.dumps(cf.blank_course(course.raw), ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
        self.toast.show(f"Saved {Path(path).name}: classmates can add it with Add course (no marks inside).",
                        CYAN, icon="⧉", ms=5000)

    def export_report(self):
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Save the printable report", defaultextension=".html",
            initialdir=documents_dir(), initialfile=f"Grade report - {self.book.term}.html".replace("/", "-"),
            filetypes=[("Web page", "*.html"), ("All files", "*.*")])
        if not path:
            return
        try:
            tr = self.transcript or g.Transcript({}, self.book)
            Path(path).write_text(build_html(self.book, tr), encoding="utf-8")
        except OSError as e:
            messagebox.showerror("Grade Tracker", f"Could not save the report:\n\n{e}")
            return
        webbrowser.open(Path(path).resolve().as_uri())
        self.toast.show("Report saved and opened in your browser: print it or save it as a PDF from there.",
                        CYAN, icon="⎙", ms=4500)

    def open_settings(self):
        SettingsDialog(self)

    def open_folder(self):
        self.lib.home.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(self.lib.home)  # Windows
        except AttributeError:
            webbrowser.open(self.lib.home.as_uri())

    # ------------------------------------------------------------------ updates
    def in_background(self, work, done):
        """Run work() on a thread; done(result_or_exception) then runs on Tk's thread."""
        def run():
            try:
                result = work()
            except Exception as e:   # handed to done() as the result
                result = e
            self.jobs.put((done, result))
        self.pending = getattr(self, "pending", 0) + 1
        threading.Thread(target=run, daemon=True).start()
        if self.pending == 1:
            self.root.after(100, self.poll_jobs)

    def poll_jobs(self):
        try:
            while True:
                done, result = self.jobs.get_nowait()
                self.pending -= 1
                done(result)
        except queue.Empty:
            pass
        if self.pending > 0:
            self.root.after(100, self.poll_jobs)

    def check_for_update(self, quiet: bool = False):
        """Ask GitHub for the latest release. quiet: say nothing unless there's a new version."""
        if self.updating:
            return
        if not quiet:
            self.update_btn.config(text="Checking…")

        def done(result):
            if isinstance(result, Exception):
                self.update_btn.config(text=f"v{__version__}")
                if not quiet:
                    self.toast.show(str(result), WARN, icon="!", ms=5000)
                return
            if result.is_newer():
                self.show_release(result, announce=True)
            else:
                self.update_btn.config(text=f"v{__version__}")
                if not quiet:
                    self.toast.show(f"You have the latest version (v{__version__}).", GOOD, icon="✓")
        self.in_background(updater.latest, done)

    def show_release(self, release, announce: bool = False):
        """A newer version exists: the header button turns into a bright Update button."""
        self.release = release
        self.update_btn.config(text="⬆  Update", style="Update.TButton")
        if announce:
            self.toast.show(f"Grade Tracker v{release.version} is out. Click Update at the top to install it.",
                            MINT, icon="⬆", ms=6000)
            self.pulse_update()

    def pulse_update(self):
        """Three soft pulses so the new button gets noticed."""
        s = ttk.Style(self.root)

        def step(t):
            s.configure("Update.TButton", background=j.mix(MINT, "#ffffff", 0.45 * abs(math.sin(t * 3 * math.pi))))
        self.anim.run("update-pulse", 1800, step, ease=lambda t: t,
                      done=lambda: s.configure("Update.TButton", background=MINT))

    def on_update_click(self):
        if self.updating:
            return
        if self.release:
            UpdateDialog(self, self.release)
        else:
            self.check_for_update(quiet=False)


class Dialog(tk.Toplevel):
    """A dark modal window with the app's look."""

    def __init__(self, app: App, title: str, w: int, h: int):
        super().__init__(app.root)
        self.app, px = app, app.px
        self.title(title)
        self.configure(bg=BG)
        self.transient(app.root)
        self.geometry(f"{px(w)}x{px(h)}+{app.root.winfo_rootx() + px(60)}+{app.root.winfo_rooty() + px(40)}")
        self.minsize(px(int(w * 0.75)), px(int(h * 0.7)))
        self.body = ttk.Frame(self, style="Card.TFrame", padding=px(20))
        self.body.pack(fill="both", expand=True, padx=px(14), pady=px(14))
        self.bind("<Escape>", lambda e: self.destroy())
        self.after(30, self.grab_set)

    def label(self, text, style="Field.TLabel", **kw):
        return ttk.Label(self.body, text=text, style=style, **kw)

    def text(self, height: int, readonly: bool = False):
        px = self.app.px
        wrap = tk.Frame(self.body, bg=LINE, padx=1, pady=1)
        t = tk.Text(wrap, bg=PANE, fg=TEXT, font=self.app.f_mono, relief="flat", wrap="word", height=height,
                    padx=px(10), pady=px(8), insertbackground=TEXT, selectbackground=SELECT, highlightthickness=0,
                    undo=True)
        bar = ttk.Scrollbar(wrap, orient="vertical", command=t.yview, style="Past.Vertical.TScrollbar")
        t.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        t.pack(side="left", fill="both", expand=True)
        for tag, colour in (("title", GOLD), ("good", GOOD), ("warn", WARN), ("bad", BAD), ("muted", MUTED),
                            ("need", ACCENT)):
            t.tag_configure(tag, foreground=colour)
        if readonly:
            t.config(state="disabled", cursor="arrow")
        return wrap, t


class ImportDialog(Dialog):
    """Paste (or open) what the AI wrote, see what the app read, then add it."""

    def __init__(self, app: App, kind: str = "syllabus", text: str = None):
        title = "Import a transcript" if kind == "transcript" else "Add a course"
        super().__init__(app, title, 820, 720)
        px = app.px
        self.kind = kind
        self.result = None
        self.pending = None
        b = self.body
        app.heading(b, title).pack(anchor="w")
        what = ("your unofficial transcript (a PDF, screenshots or photos)" if kind == "transcript"
                else "your syllabus (the PDF, screenshots or photos of the pages); several courses at once is fine")
        ttk.Label(b, text=f"1. Copy the prompt.   2. Give it to your AI (ChatGPT, Claude, Gemini, Copilot…) with {what}."
                          "   3. Paste its whole reply below, or open the file it gave you.",
                  style="Hint.TLabel", wraplength=px(740), justify="left").pack(fill="x", pady=(px(6), px(10)))
        row = ttk.Frame(b, style="Flat.TFrame")
        row.pack(fill="x")
        ttk.Button(row, text="Copy AI prompt", style="Accent.TButton" if not text else "TButton", cursor="hand2",
                   command=lambda: self.copy(kind)).pack(side="left")
        other = "syllabus" if kind == "transcript" else "transcript"
        ttk.Button(row, text=f"Copy the {other} prompt instead", style="Link.TButton",
                   command=lambda: self.copy(other)).pack(side="left", padx=(px(14), 0))
        ttk.Button(row, text="Open file…", command=self.open_file, cursor="hand2").pack(side="right")

        self.label("YOUR AI'S REPLY").pack(anchor="w", pady=(px(14), px(4)))
        wrap, self.input = self.text(10)
        wrap.pack(fill="both", expand=True)
        self.input.bind("<<Modified>>", self.on_change)

        self.label("WHAT THE APP READ").pack(anchor="w", pady=(px(14), px(4)))
        wrap, self.preview = self.text(11, readonly=True)
        wrap.pack(fill="both", expand=True)

        self.use_scale = tk.BooleanVar(value=False)
        self.scale_box = ttk.Checkbutton(b, text="Use this file's letter scale for every course and the GPA",
                                         variable=self.use_scale, command=self.check)
        bottom = ttk.Frame(b, style="Flat.TFrame")
        bottom.pack(fill="x", side="bottom", pady=(px(14), 0))
        self.add_btn = ttk.Button(bottom, text="Add to Grade Tracker", style="Accent.TButton", command=self.add,
                                  cursor="hand2")
        self.add_btn.pack(side="right")
        self.add_btn.state(["disabled"])
        ttk.Button(bottom, text="Cancel", command=self.destroy).pack(side="right", padx=(0, px(8)))
        self.summary = tk.Label(bottom, text="", bg=PANEL, fg=MUTED, font=app.f_small, anchor="w", justify="left")
        self.summary.pack(side="left", fill="x", expand=True)
        self.scale_slot = bottom
        self.show(["Nothing pasted yet."], "muted")
        if text:
            self.input.insert("1.0", text)
            self.check()
        self.input.focus_set()

    def copy(self, kind: str):
        self.app.copy_prompt(kind)
        self.summary.config(text="Prompt copied to the clipboard.", fg=CYAN)

    def open_file(self):
        downloads = Path.home() / "Downloads"
        path = filedialog.askopenfilename(parent=self, title="Open the file your AI made",
                                          initialdir=downloads if downloads.is_dir() else documents_dir(),
                                          filetypes=[("JSON or text", "*.json *.txt *.md"), ("All files", "*.*")])
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as e:
            self.show([f"Could not read {path}:", str(e)], "bad")
            return
        self.input.delete("1.0", "end")
        self.input.insert("1.0", text)
        self.check()

    def on_change(self, _e=None):
        if not self.input.edit_modified():
            return
        self.input.edit_modified(False)
        if self.pending:
            self.after_cancel(self.pending)
        self.pending = self.after(350, self.check)

    def show(self, lines, tag=None):
        p = self.preview
        p.config(state="normal")
        p.delete("1.0", "end")
        for item in lines:
            text, t = item if isinstance(item, tuple) else (item, tag)
            p.insert("end", text + "\n", t)
        p.config(state="disabled")

    def check(self):
        self.pending = None
        text = self.input.get("1.0", "end").strip()
        self.add_btn.state(["disabled"])
        self.result = None
        if not text:
            self.show(["Nothing pasted yet."], "muted")
            self.summary.config(text="")
            return
        imp = cf.read_import(text)
        lib_data = self.app.lib.load_data()
        tr_data = self.app.lib.load_transcript_data() if self.app.lib.transcript_path.exists() else {}
        differs = imp.scale and lib_data.get("scale") and cf.scale_differs(lib_data["scale"], imp.scale)
        if differs:
            self.scale_box.pack(in_=self.scale_slot, side="left", before=self.summary, padx=(0, self.app.px(12)))
        else:
            self.scale_box.pack_forget()
        new_lib, new_tr, notes, errors = cf.plan(imp, lib_data, tr_data,
                                                 adopt_scale=True if self.use_scale.get() else None)
        lines = []   # problems first, so they're never scrolled out of sight
        for e in errors:
            lines.append(("✗ " + e, "bad"))
        if errors:
            lines.append(("Fix it by hand above, or tell your AI what the error says and ask for the corrected JSON.",
                          "muted"))
            lines.append("")
        for w in imp.warnings:
            lines.append(("⚠ " + w, "warn"))
        for n in notes:
            lines.append(("⚠ " + n, "warn") if n.startswith("Check") else ("✓ " + n, "good"))
        if imp.warnings or notes:
            lines.append("")
        if imp.kind == "transcript" and imp.transcript:
            for line in cf.describe_transcript(imp.transcript):
                lines.append((line, "title" if not line.startswith(" ") and ":" in line else None))
        for c in imp.courses:
            for i, line in enumerate(cf.describe_course(c)):
                lines.append((line, "title" if i == 0 else ("muted" if line.strip().startswith("Note:") else None)))
            lines.append("")
        if imp.scale and imp.kind == "courses":
            lines.append(("Letter scale: " + ", ".join(f"{r[1]} {g.fnum(r[0])}" for r in imp.scale), "muted"))
        self.show(lines)
        if errors:
            self.summary.config(text=f"{len(errors)} problem{'s' if len(errors) != 1 else ''} to fix", fg=BAD)
            return
        if imp.kind == "transcript" and self.kind != "transcript":
            self.kind = "transcript"
        self.result = (new_lib, new_tr, imp.kind, notes)
        warn = len(imp.warnings) + sum(1 for n in notes if n.startswith("Check"))
        what = ("transcript" if imp.kind == "transcript"
                else f"{len(imp.courses)} course{'s' if len(imp.courses) != 1 else ''}")
        self.summary.config(text=f"Ready: {what}" + (f" · {warn} warning{'s' if warn != 1 else ''} to look at" if warn
                                                     else " · compare it with your syllabus, then add it"),
                            fg=WARN if warn else GOOD)
        self.add_btn.state(["!disabled"])

    def add(self):
        if not self.result:
            return
        new_lib, new_tr, kind, notes = self.result
        if kind == "transcript" and self.app.transcript and not self.app.transcript.empty:
            if not messagebox.askyesno("Replace transcript?", "This replaces the past semesters you imported before. "
                                                              "Continue?", parent=self):
                return
        try:
            self.app.apply_import(new_lib, new_tr, kind, notes)
        except OSError as e:
            messagebox.showerror("Grade Tracker", f"Could not save:\n\n{e}", parent=self)
            return
        self.destroy()


class UpdateDialog(Dialog):
    """What's new, then download, swap and restart, with a progress bar."""

    def __init__(self, app: App, release):
        super().__init__(app, "Update Grade Tracker", 640, 540)
        px = app.px
        self.release, self.progress, self.done = release, (0, 0), False
        b = self.body
        app.heading(b, f"Grade Tracker v{release.version} is available").pack(anchor="w")
        ttk.Label(b, text=f"You have v{__version__}. Your courses, marks and transcript stay exactly as they are: "
                          "they live in your data folder, not inside the app.",
                  style="Hint.TLabel", wraplength=px(580), justify="left").pack(fill="x", pady=(px(6), px(12)))
        self.label("WHAT'S NEW").pack(anchor="w", pady=(0, px(4)))
        wrap, notes = self.text(10, readonly=True)
        wrap.pack(fill="both", expand=True)
        notes.config(state="normal")
        notes.insert("1.0", release.notes or "See the release page for details.")
        notes.config(state="disabled")
        self.bar = ttk.Progressbar(b, style="Mint.Horizontal.TProgressbar", mode="determinate", maximum=100)
        bottom = ttk.Frame(b, style="Flat.TFrame")
        bottom.pack(fill="x", side="bottom", pady=(px(14), 0))
        if updater.can_self_update():
            self.go = ttk.Button(bottom, text="Update now", style="Update.TButton", command=self.start, cursor="hand2")
        else:   # running from source: there's no .exe to swap
            self.go = ttk.Button(bottom, text="Open the download page", style="Accent.TButton", cursor="hand2",
                                 command=lambda: (webbrowser.open(release.page), self.destroy()))
        self.go.pack(side="right")
        self.later = ttk.Button(bottom, text="Later", command=self.destroy)
        self.later.pack(side="right", padx=(0, px(8)))
        if updater.can_self_update():
            note = (f"{release.size / 1e6:.0f} MB · " if release.size else "") + "restarts when done"
        else:
            note = "Running from source: use git pull, or download the .exe."
        self.status = tk.Label(bottom, text=note, bg=PANEL, fg=MUTED,
                               font=app.f_small, anchor="w", justify="left", wraplength=px(330))
        self.status.pack(side="left", fill="x", expand=True)
        self.protocol("WM_DELETE_WINDOW", lambda: None if self.app.updating else self.destroy())

    def set_progress(self, done, total):   # called from the download thread: only stores numbers
        self.progress = (done, total)

    def tick(self):
        if self.done or not self.app.updating or not self.winfo_exists():
            return
        done, total = self.progress
        if total:
            self.bar["value"] = 100 * done / total
            self.status.config(text=f"Downloading… {done / 1e6:.1f} of {total / 1e6:.1f} MB")
        self.after(100, self.tick)

    def start(self):
        self.app.updating = True
        self.go.state(["disabled"])
        self.later.state(["disabled"])
        self.app.update_btn.config(text="Updating…")
        self.bar.pack(fill="x", side="bottom", pady=(self.app.px(12), 0))
        self.status.config(text="Starting the download…", fg=TEXT)
        self.tick()
        release = self.release
        self.app.in_background(lambda: updater.install(updater.download(release, self.set_progress)), self.finished)

    def finished(self, result):
        app = self.app
        if isinstance(result, Exception):
            app.updating = False
            app.update_btn.config(text="⬆  Update")
            self.go.state(["!disabled"])
            self.later.state(["!disabled"])
            self.bar.pack_forget()
            self.status.config(text=str(result), fg=BAD)
            return
        self.done = True
        self.bar["value"] = 100
        self.status.config(text="Installed. Restarting…", fg=GOOD)
        try:
            updater.restart(result, sys.argv[1:])
        except OSError as e:
            self.status.config(text=f"Installed v{self.release.version}. Start Grade Tracker again to use it ({e}).",
                               fg=WARN)
            return
        app.root.after(400, app.root.destroy)


class SettingsDialog(Dialog):
    """Term name, target letters, the letter scale, updates, and where the data lives."""

    def __init__(self, app: App):
        super().__init__(app, "Settings", 640, 690)
        px = app.px
        b = self.body
        data = app.lib.load_data()
        app.heading(b, "Settings").pack(anchor="w")

        self.label("THIS TERM'S NAME").pack(anchor="w", pady=(px(14), px(3)))
        self.term = tk.StringVar(value=data.get("term") or "")
        ttk.Entry(b, textvariable=self.term).pack(fill="x")

        self.label("TARGET LETTERS (the columns on the Courses tab)").pack(anchor="w", pady=(px(14), px(3)))
        targets = [t if isinstance(t, str) else t[0] for t in data.get("targets") or []]
        self.targets = tk.StringVar(value=", ".join(targets))
        ttk.Entry(b, textvariable=self.targets).pack(fill="x")
        ttk.Label(b, text="Leave empty for the top three letters of each course's scale, e.g. A-, A, A+ or B, A.",
                  style="Hint.TLabel").pack(anchor="w", pady=(px(3), 0))

        self.label("LETTER SCALE (lowest percent, letter, grade points: one per line)").pack(
            anchor="w", pady=(px(14), px(3)))
        wrap, self.scale_text = self.text(10)
        wrap.pack(fill="both", expand=True)
        rows = data.get("scale") or g.DEFAULT_SCALE
        self.scale_text.insert("1.0", "\n".join(" ".join(g.fnum(v) if isinstance(v, (int, float)) else str(v)
                                                         for v in r) for r in rows))
        ttk.Label(b, text="A course whose syllabus gives its own cutoffs keeps them; the grade points here drive the GPA.",
                  style="Hint.TLabel", wraplength=px(560), justify="left").pack(anchor="w", pady=(px(3), 0))

        self.check_updates = tk.BooleanVar(value=data.get("check_updates", True))
        ttk.Checkbutton(b, text="Check GitHub for a new version when Grade Tracker starts",
                        variable=self.check_updates).pack(anchor="w", pady=(px(12), 0))

        links = ttk.Frame(b, style="Flat.TFrame")
        links.pack(fill="x", pady=(px(14), 0))
        ttk.Button(links, text="Open data folder", style="Link.TButton", command=app.open_folder).pack(side="left")
        ttk.Button(links, text="Copy syllabus prompt", style="Link.TButton",
                   command=lambda: app.copy_prompt("syllabus")).pack(side="left", padx=(px(16), 0))
        ttk.Button(links, text="Copy transcript prompt", style="Link.TButton",
                   command=lambda: app.copy_prompt("transcript")).pack(side="left", padx=(px(16), 0))
        ttk.Button(links, text=f"Grade Tracker {__version__} · website", style="Link.TButton",
                   command=lambda: webbrowser.open(HOME_PAGE)).pack(side="right")

        bottom = ttk.Frame(b, style="Flat.TFrame")
        bottom.pack(fill="x", side="bottom", pady=(px(14), 0))
        ttk.Button(bottom, text="Save", style="Accent.TButton", command=self.save, cursor="hand2").pack(side="right")
        ttk.Button(bottom, text="Cancel", command=self.destroy).pack(side="right", padx=(0, px(8)))
        self.error = tk.Label(bottom, text="", bg=PANEL, fg=BAD, font=app.f_small, anchor="w", justify="left",
                              wraplength=px(380))
        self.error.pack(side="left", fill="x", expand=True)

    def save(self):
        errors = []
        rows = []
        for line in self.scale_text.get("1.0", "end").splitlines():
            parts = line.replace(",", " ").split()
            if parts:
                rows.append([parts[0], parts[1] if len(parts) > 1 else None] + parts[2:3])
        scale = cf.check_scale(rows, "Letter scale", errors)
        letters = [t.strip() for t in self.targets.get().replace(";", ",").split(",") if t.strip()]
        known = {r[1] for r in scale or []}
        unknown = [t for t in letters if t not in known]
        if unknown:
            errors.append(f"Target letters not on the scale: {', '.join(unknown)}.")
        if errors:
            self.error.config(text=errors[0])
            return
        data = self.app.lib.load_data()
        data["term"] = self.term.get().strip()
        data["targets"] = letters
        data["scale"] = scale
        data["check_updates"] = bool(self.check_updates.get())
        try:
            g.GradeBook(data)
        except (g.GradeError, ValueError) as e:
            self.error.config(text=str(e))
            return
        self.app.lib.save(data)
        self.app.reload()
        self.app.show_view(self.app.view)
        self.destroy()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Grade Tracker window")
    ap.add_argument("--data", help="data folder (default: %%APPDATA%%\\Grade Tracker, or GRADE_TRACKER_HOME)")
    ap.add_argument("--view", choices=[k for k, _ in VIEWS], default="marks", help="view to open on")
    ap.add_argument("--calm", action="store_true", help="no animations or confetti (also GRADE_TRACKER_CALM=1)")
    args = ap.parse_args(argv)
    calm = args.calm or os.environ.get("GRADE_TRACKER_CALM", "") not in ("", "0")
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on scaled displays
        # Its own taskbar identity, so Windows shows the window's icon instead of Python's.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("GradeTracker.App")
    except Exception:
        pass
    root = tk.Tk()
    fade = not calm
    if fade:
        try:
            root.attributes("-alpha", 0.0)
        except tk.TclError:
            fade = False
    updater.cleanup()   # the .exe an earlier update replaced
    lib = Library(args.data)
    try:
        wanted = lib.load_data().get("check_updates", True)
    except g.GradeError:
        wanted = True
    check = wanted and os.environ.get("GRADE_TRACKER_NO_UPDATE_CHECK", "") in ("", "0")
    try:
        app = App(root, lib, calm=calm, check_updates=check)
        app.show_view(args.view)
    except Exception:
        messagebox.showerror("Grade Tracker", traceback.format_exc())
        root.destroy()
        return 1
    # Lay out and paint the whole window once (still invisible) before anything animates; the first
    # paint takes long enough that an intro started earlier would be over before it was seen.
    root.wait_visibility(root)
    root.update()
    app.intro(fade)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
