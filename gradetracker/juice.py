"""Eye candy for the Grade Tracker window: colour maths, canvas gradients, tweens, toasts, confetti.

Tkinter has no gradients, alpha blending or animation built in, so this fakes them: gradients
are stacks of thin rectangles, images are drawn pixel row by pixel row, and every animation is
a chain of root.after() frames. Standard library only, like the rest of the app.
"""
from __future__ import annotations

import math
import random
import time
import tkinter as tk


# --------------------------------------------------------------------------- colour

def rgb(c: str):
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def hexc(r, g, b) -> str:
    return "#%02x%02x%02x" % (max(0, min(255, round(r))), max(0, min(255, round(g))), max(0, min(255, round(b))))


def mix(a: str, b: str, t: float) -> str:
    """Blend colour a toward b; t=0 is a, t=1 is b."""
    (r1, g1, b1), (r2, g2, b2) = rgb(a), rgb(b)
    return hexc(r1 + (r2 - r1) * t, g1 + (g2 - g1) * t, b1 + (b2 - b1) * t)


def along(stops, t: float) -> str:
    """The colour at t (0..1) on a gradient through the given stops."""
    if len(stops) == 1:
        return stops[0]
    t = max(0.0, min(1.0, t)) * (len(stops) - 1)
    i = min(int(t), len(stops) - 2)
    return mix(stops[i], stops[i + 1], t - i)


# --------------------------------------------------------------------------- easing

def ease_out(t: float) -> float:
    return 1 - (1 - t) ** 3


def ease_back(t: float) -> float:
    """Ease out with a small overshoot, for things that should land with a bounce."""
    c = 1.4
    return 1 + (c + 1) * (t - 1) ** 3 + c * (t - 1) ** 2


def ease_in_out(t: float) -> float:
    return 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


class Animator:
    """Runs named tweens on Tk's event loop. Starting a tween cancels the one with the same key.

    step(t) gets eased progress 0..1 every frame; done() runs once at the end. With enabled=False
    (the window's --calm flag) every tween jumps straight to its last frame.
    """

    FRAME_MS = 15

    def __init__(self, root: tk.Misc, enabled: bool = True):
        self.root, self.enabled = root, enabled
        self.jobs = {}

    def run(self, key, ms: int, step, done=None, ease=ease_out, delay: int = 0):
        self.cancel(key)
        if not self.enabled or ms <= 0:
            step(1.0)
            if done:
                done()
            return
        clock = {"start": time.perf_counter() + delay / 1000, "last": time.perf_counter()}

        def frame():
            now = time.perf_counter()
            # A long gap between frames (a slow redraw, a layout pass) pauses the tween instead of
            # letting it jump ahead, so nothing finishes before it's been seen.
            stall = now - clock["last"] - self.FRAME_MS / 1000
            if stall > 0.06 and now > clock["start"]:
                clock["start"] += stall
            clock["last"] = now
            start = clock["start"]
            raw = 0.0 if now < start else min(1.0, (now - start) * 1000 / ms)
            try:
                step(ease(raw))
            except tk.TclError:   # the widget went away mid-tween
                self.jobs.pop(key, None)
                return
            if raw >= 1.0:
                self.jobs.pop(key, None)
                if done:
                    done()
            else:
                self.jobs[key] = self.root.after(self.FRAME_MS, frame)
        frame()

    def cancel(self, key):
        job = self.jobs.pop(key, None)
        if job:
            self.root.after_cancel(job)


# --------------------------------------------------------------------------- canvas drawing

def round_points(x0, y0, x1, y1, r):
    """Polygon points for a rounded rectangle (use with smooth=True)."""
    r = max(0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    return [x0 + r, y0, x0 + r, y0, x1 - r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y0 + r, x1, y1 - r,
            x1, y1 - r, x1, y1, x1 - r, y1, x1 - r, y1, x0 + r, y1, x0 + r, y1, x0, y1, x0, y1 - r,
            x0, y1 - r, x0, y0 + r, x0, y0 + r, x0, y0]


def rounded(cv: tk.Canvas, x0, y0, x1, y1, r, **kw):
    return cv.create_polygon(round_points(x0, y0, x1, y1, r), smooth=True, **kw)


def vgradient(cv: tk.Canvas, x0, y0, x1, y1, top: str, bottom: str, step: int = 3, tags=()):
    """A top-to-bottom gradient rectangle made of horizontal slices."""
    h = y1 - y0
    if h <= 0:
        return
    n = max(1, int(h // step))
    for i in range(n):
        a = y0 + h * i / n
        b = y0 + h * (i + 1) / n
        cv.create_rectangle(x0, a, x1, b + 1, fill=mix(top, bottom, i / max(n - 1, 1)), outline="", tags=tags)


def hgradient(cv: tk.Canvas, x0, y0, x1, y1, stops, step: int = 3, tags=()):
    """A left-to-right gradient through the stops, made of vertical slices."""
    w = x1 - x0
    if w <= 0:
        return
    n = max(1, int(w // step))
    for i in range(n):
        a = x0 + w * i / n
        b = x0 + w * (i + 1) / n
        cv.create_rectangle(a, y0, b + 1, y1, fill=along(stops, i / max(n - 1, 1)), outline="", tags=tags)


def ring(cv: tk.Canvas, cx, cy, r, width, frac: float, stops, track: str, tags=()):
    """A progress ring from 12 o'clock, clockwise, coloured along the stops."""
    box = (cx - r, cy - r, cx + r, cy + r)
    cv.create_oval(*box, outline=track, width=width, tags=tags)
    frac = max(0.0, min(1.0, frac))
    if frac <= 0:
        return
    segs = max(1, int(48 * frac))
    total = 359.9 * frac
    for i in range(segs):
        start = 90 - total * i / segs
        cv.create_arc(*box, start=start, extent=-(total / segs + 0.8), style="arc", width=width,
                      outline=along(stops, i / max(segs - 1, 1) * frac), tags=tags)
    # round end cap
    ang = math.radians(90 - total)
    ex, ey = cx + r * math.cos(ang), cy - r * math.sin(ang)
    cap = width / 2
    cv.create_oval(ex - cap, ey - cap, ex + cap, ey + cap, fill=along(stops, frac), outline="", tags=tags)


# --------------------------------------------------------------------------- images

def _put_row(img: tk.PhotoImage, y: int, x0: int, colours):
    if colours:
        img.put("{" + " ".join(colours) + "}", to=(x0, y))


def app_icon(size: int, stops) -> tk.PhotoImage:
    """A rounded square with a diagonal gradient and three rising white bars: a tiny bar chart."""
    img = tk.PhotoImage(width=size, height=size)
    r = size * 0.24
    for y in range(size):
        row, start = [], None
        for x in range(size):
            dx = max(r - x - 0.5, 0, x + 0.5 - (size - r))
            dy = max(r - y - 0.5, 0, y + 0.5 - (size - r))
            if dx * dx + dy * dy <= r * r:
                if start is None:
                    start = x
                row.append(along(stops, (x + y) / (2 * size - 2)))
            elif start is not None:
                break
        if start is not None:
            _put_row(img, y, start, row)
    bw = max(2, round(size * 0.14))
    gap = max(1, round(size * 0.08))
    left = (size - (3 * bw + 2 * gap)) // 2
    base = round(size * 0.76)
    for i, hfrac in enumerate((0.30, 0.46, 0.62)):
        x0 = left + i * (bw + gap)
        top = base - round(size * hfrac)
        img.put("#ffffff", to=(x0, top, x0 + bw, base))
    return img


def dot(colour: str, size: int, ring_only: bool = False, bg: str = None) -> tk.PhotoImage:
    """A filled (or hollow) circle, centred in a size x size transparent image."""
    img = tk.PhotoImage(width=size + 4, height=size)
    c = (size - 1) / 2
    r = size / 2
    inner = r - max(1.5, size * 0.18)
    for y in range(size):
        row, start = [], None
        for x in range(size):
            d = math.hypot(x - c, y - c)
            if d <= r and (not ring_only or d >= inner):
                if start is None:
                    start = x
                edge = max(0.0, min(1.0, r - d))   # soften the rim toward the background
                row.append(mix(bg, colour, edge) if bg and edge < 1 else colour)
            elif start is not None and not ring_only:
                break
            elif start is not None and ring_only:
                _put_row(img, y, start, row)
                row, start = [], None
        if start is not None:
            _put_row(img, y, start, row)
    return img


def bar_image(stops, w: int, h: int) -> tk.PhotoImage:
    """A small vertical gradient bar, e.g. the accent tick in front of a heading."""
    img = tk.PhotoImage(width=w + 6, height=h)
    for y in range(h):
        img.put(along(stops, y / max(h - 1, 1)), to=(0, y, w, y + 1))
    return img


# --------------------------------------------------------------------------- toast

class Toaster:
    """Slides a small notice up into the window's bottom-right corner, then away again."""

    def __init__(self, root: tk.Misc, anim: Animator, px, font, bg: str, fg: str, line: str):
        self.root, self.anim, self.px, self.font = root, anim, px, font
        self.bg, self.fg, self.line = bg, fg, line
        self.box = None
        self.hide_job = None

    def show(self, text: str, colour: str, icon: str = "✓", ms: int = 3200):
        px = self.px
        if self.box is not None:
            self.box.destroy()
        if self.hide_job:
            self.root.after_cancel(self.hide_job)
        box = tk.Frame(self.root, bg=self.line, padx=1, pady=1)
        inner = tk.Frame(box, bg=self.bg)
        inner.pack(fill="both", expand=True)
        tk.Frame(inner, bg=colour, width=px(4)).pack(side="left", fill="y")
        tk.Label(inner, text=icon, bg=self.bg, fg=colour, font=(self.font[0], self.font[1] + 4, "bold"),
                 padx=px(10)).pack(side="left")
        tk.Label(inner, text=text, bg=self.bg, fg=self.fg, font=self.font, justify="left",
                 wraplength=px(340), pady=px(10)).pack(side="left", padx=(0, px(16)))
        self.box = box
        hidden, shown = px(90), -px(22)
        box.place(relx=1.0, rely=1.0, x=-px(22), y=hidden, anchor="se")
        box.lift()
        self.anim.run("toast", 420, lambda t: box.place_configure(y=hidden + (shown - hidden) * t), ease=ease_back)

        def hide():
            self.hide_job = None
            if self.box is not box:
                return
            self.anim.run("toast", 300, lambda t: box.place_configure(y=shown + (hidden - shown) * t),
                          done=box.destroy, ease=ease_in_out)
        self.hide_job = self.root.after(ms, hide)


# --------------------------------------------------------------------------- confetti

def confetti(root: tk.Misc, colours, origin=None, count: int = 90, ms: int = 1700, power: float = 1.0) -> bool:
    """A burst of confetti over the window, from origin (window-relative x, y). False if unsupported.

    It draws on a borderless, click-through overlay window whose background colour is keyed out
    (Windows' -transparentcolor), so the real window stays fully usable underneath.
    """
    key = "#010203"
    try:
        root.update_idletasks()
        top = tk.Toplevel(root)
        top.overrideredirect(True)
        top.attributes("-transparentcolor", key)
        top.attributes("-topmost", True)
    except tk.TclError:
        try:
            top.destroy()
        except Exception:
            pass
        return False
    w, h = root.winfo_width(), root.winfo_height()
    top.geometry(f"{w}x{h}+{root.winfo_rootx()}+{root.winfo_rooty()}")
    cv = tk.Canvas(top, bg=key, highlightthickness=0, width=w, height=h)
    cv.pack()
    scale = root.winfo_fpixels("1i") / 96
    ox, oy = origin or (w / 2, h * 0.4)
    parts = []
    for _ in range(count):
        ang = random.uniform(math.radians(200), math.radians(340))
        speed = random.uniform(7, 17) * scale * power
        size = random.uniform(5, 10) * scale
        p = {"x": ox, "y": oy, "vx": math.cos(ang) * speed, "vy": math.sin(ang) * speed,
             "s": size, "spin": random.uniform(0, math.tau), "vs": random.uniform(0.15, 0.4),
             "round": random.random() < 0.3}
        colour = random.choice(colours)
        if p["round"]:
            p["id"] = cv.create_oval(0, 0, 0, 0, fill=colour, outline="")
        else:
            p["id"] = cv.create_polygon(0, 0, 0, 0, 0, 0, 0, 0, fill=colour, outline="")
        parts.append(p)
    gravity, drag = 0.42 * scale, 0.985
    start = time.perf_counter()

    def frame():
        el = (time.perf_counter() - start) * 1000
        if el >= ms:
            top.destroy()
            return
        for p in parts:
            p["vx"] *= drag
            p["vy"] = p["vy"] * drag + gravity
            p["x"] += p["vx"]
            p["y"] += p["vy"]
            p["spin"] += p["vs"]
            s = p["s"]
            if p["round"]:
                cv.coords(p["id"], p["x"] - s / 2, p["y"] - s / 2, p["x"] + s / 2, p["y"] + s / 2)
            else:   # a spinning paper rectangle: flatten it as it turns
                a, flat = p["spin"], abs(math.cos(p["spin"] * 1.7)) * 0.9 + 0.1
                dx, dy = math.cos(a) * s / 2, math.sin(a) * s / 2
                ex, ey = -math.sin(a) * s * flat / 3, math.cos(a) * s * flat / 3
                cv.coords(p["id"], p["x"] - dx - ex, p["y"] - dy - ey, p["x"] + dx - ex, p["y"] + dy - ey,
                          p["x"] + dx + ex, p["y"] + dy + ey, p["x"] - dx + ex, p["y"] - dy + ey)
        top.after(16, frame)
    frame()
    return True
