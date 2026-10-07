"""CSS transitions and @keyframes animations.

The Animator keeps, for one page, the running transitions (started when a
:hover/:focus/... restyle changes a property listed in `transition`) and
animations (elements with `animation-name`). The window calls tick() on a
timer; tick() writes the interpolated values into the elements' computed
style dicts and says whether a repaint or a new layout is needed.
"""
import math
import re

from .css_parser import _split_top_level
from .dom import Element, Text
from .style import INHERITED, PAINT_ONLY, length, to_rgba

# Derived values cached inside style dicts that must be dropped when a value changes.
_CACHES = ("-text-color", "-shadows", "-deco")
PAINT_ANIMATABLE = PAINT_ONLY | {"transform", "translate", "rotate", "scale", "filter", "clip-path",
                                 "background-position", "box-shadow", "text-shadow", "outline-offset"}


def parse_time(v, default=0.0):
    v = (v or "").strip().lower()
    try:
        if v.endswith("ms"):
            return float(v[:-2]) / 1000
        if v.endswith("s"):
            return float(v[:-1])
        return float(v)
    except ValueError:
        return default


def _items(value):
    return [v.strip() for v in _split_top_level(value or "", ",")]


def _nth(lst, i, default):
    return lst[i % len(lst)] if lst else default


# ------------------------------------------------------------ easing
def _bezier(x1, y1, x2, y2):
    def sample(t, a, b):
        return 3 * a * t * (1 - t) ** 2 + 3 * b * t * t * (1 - t) + t ** 3

    def solve(x):
        lo, hi = 0.0, 1.0
        for _ in range(30):
            mid = (lo + hi) / 2
            if sample(mid, x1, x2) < x:
                lo = mid
            else:
                hi = mid
        return sample((lo + hi) / 2, y1, y2)
    return solve


_NAMED = {
    "linear": lambda t: t,
    "ease": _bezier(0.25, 0.1, 0.25, 1.0),
    "ease-in": _bezier(0.42, 0, 1, 1),
    "ease-out": _bezier(0, 0, 0.58, 1),
    "ease-in-out": _bezier(0.42, 0, 0.58, 1),
}


def easing(spec):
    spec = (spec or "ease").strip().lower()
    if spec in _NAMED:
        return _NAMED[spec]
    m = re.match(r"cubic-bezier\(([^)]*)\)", spec)
    if m:
        try:
            nums = [float(v) for v in m.group(1).split(",")]
            return _bezier(*nums[:4])
        except (ValueError, TypeError):
            return _NAMED["ease"]
    if spec in ("step-start", "step-end"):
        return (lambda t: 1.0 if t > 0 else 0.0) if spec == "step-start" else (lambda t: 1.0 if t >= 1 else 0.0)
    m = re.match(r"steps\(\s*(\d+)\s*(?:,\s*([a-z-]+))?\s*\)", spec)
    if m:
        n = max(1, int(m.group(1)))
        pos = m.group(2) or "end"

        def stepped(t):
            if pos in ("start", "jump-start"):
                return min(1.0, math.floor(t * n + 1) / n) if t > 0 else 0.0
            if pos == "jump-both":
                return min(1.0, math.floor(t * n + 1) / (n + 1))
            if pos == "jump-none" and n > 1:
                return min(1.0, math.floor(t * n) / (n - 1))
            return math.floor(t * n) / n if t < 1 else 1.0
        return stepped
    if spec.startswith("linear("):
        return _NAMED["linear"]
    return _NAMED["ease"]


# ------------------------------------------------------------ interpolation
_NUM_UNIT = re.compile(r"^(-?\d*\.?\d+(?:e-?\d+)?)([a-z%]*)$", re.I)


def interpolate(prop, a, b, t, fs=16.0):
    """Value of `prop` a fraction t of the way from a to b (strings)."""
    if a == b:
        return a
    if t <= 0:
        return a
    if t >= 1:
        return b
    a, b = (a or "").strip(), (b or "").strip()
    if prop in ("transform",):
        return _interp_transform(a, b, t, fs)
    if "color" in prop or prop in ("fill", "stroke"):
        ca, cb = to_rgba(a), to_rgba(b)
        if ca and cb:
            vals = [ca[i] + (cb[i] - ca[i]) * t for i in range(4)]
            if vals[3] >= 0.999:
                return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(v)))) for v in vals[:3])
            return "rgba(%d, %d, %d, %.3f)" % (tuple(max(0, min(255, int(round(v)))) for v in vals[:3]) + (vals[3],))
    pa, pb = a.split(), b.split()
    if len(pa) == len(pb) and pa:
        out = []
        for x, y in zip(pa, pb):
            v = _interp_scalar(x, y, t, fs)
            if v is None:
                return a if t < 0.5 else b
            out.append(v)
        return " ".join(out)
    if prop == "visibility":
        return "visible" if "visible" in (a, b) else (a if t < 0.5 else b)
    return a if t < 0.5 else b


def _interp_scalar(x, y, t, fs):
    mx, my = _NUM_UNIT.match(x), _NUM_UNIT.match(y)
    if mx and my:
        ux, uy = mx.group(2).lower(), my.group(2).lower()
        vx, vy = float(mx.group(1)), float(my.group(1))
        if ux == uy or (vx == 0 and not ux) or (vy == 0 and not uy):
            unit = ux or uy
            return "%g%s" % (vx + (vy - vx) * t, unit)
        px, py = length(x, fs, None, None), length(y, fs, None, None)
        if px is not None and py is not None:
            return "%gpx" % (px + (py - px) * t)
        return None
    if x == y:
        return x
    return None


def _fn_list(v):
    return [(fn.lower(), [a.strip() for a in re.split(r"\s*,\s*|\s+", args.strip()) if a.strip()])
            for fn, args in re.findall(r"([a-zA-Z0-9]+)\(([^()]*)\)", v)]


_IDENTITY_ARGS = {"translate": "0px", "translatex": "0px", "translatey": "0px", "translate3d": "0px",
                  "scale": "1", "scalex": "1", "scaley": "1", "rotate": "0deg", "rotatez": "0deg",
                  "skew": "0deg", "skewx": "0deg", "skewy": "0deg"}


def _interp_transform(a, b, t, fs):
    fa = _fn_list(a) if a != "none" else []
    fb = _fn_list(b) if b != "none" else []
    if not fa:
        fa = [(fn, [_IDENTITY_ARGS.get(fn, "0")] * len(args)) for fn, args in fb]
    if not fb:
        fb = [(fn, [_IDENTITY_ARGS.get(fn, "0")] * len(args)) for fn, args in fa]
    if [f for f, _ in fa] != [f for f, _ in fb]:
        return a if t < 0.5 else b
    out = []
    for (fn, xa), (_, xb) in zip(fa, fb):
        n = max(len(xa), len(xb))
        xa = xa + [_IDENTITY_ARGS.get(fn, "0")] * (n - len(xa))
        xb = xb + [_IDENTITY_ARGS.get(fn, "0")] * (n - len(xb))
        args = []
        for x, y in zip(xa, xb):
            v = _interp_scalar(x, y, t, fs)
            args.append(v if v is not None else (x if t < 0.5 else y))
        out.append("%s(%s)" % (fn, ", ".join(args)))
    return " ".join(out)


# ------------------------------------------------------------ the animator
class Animator:
    def __init__(self, page):
        self.page = page
        self.transitions = {}   # (node, prop) -> [from, to, start, duration, delay, easing]
        self.animations = {}    # (node, name) -> dict
        self.base = {}          # (node, prop) -> value before an animation changed it

    def active(self):
        return bool(self.transitions) or any(not a.get("finished") for a in self.animations.values())

    # -- discovery
    def scan(self, now):
        """Start animations for elements whose animation-name is set (and stop
        those whose element lost it)."""
        keyframes = getattr(self.page, "keyframes", {}) or {}
        seen = set()
        if keyframes and self.page.document is not None:
            stack = [self.page.document]
            while stack:
                n = stack.pop()
                if isinstance(n, Element):
                    names = n.style.get("animation-name", "none")
                    if names and names != "none":
                        self._start(n, names, keyframes, now, seen)
                    stack.extend(c for c in n.children if isinstance(c, Element))
        for key in [k for k in self.animations if k not in seen]:
            self._restore(self.animations.pop(key))

    def _start(self, node, names, keyframes, now, seen):
        s = node.style
        name_list = _items(names)
        durs = _items(s.get("animation-duration", "0s"))
        funcs = _items(s.get("animation-timing-function", "ease"))
        delays = _items(s.get("animation-delay", "0s"))
        counts = _items(s.get("animation-iteration-count", "1"))
        dirs = _items(s.get("animation-direction", "normal"))
        fills = _items(s.get("animation-fill-mode", "none"))
        states = _items(s.get("animation-play-state", "running"))
        for i, name in enumerate(name_list):
            name = name.strip("\"'")
            if name == "none" or name not in keyframes:
                continue
            key = (node, name)
            seen.add(key)
            count = _nth(counts, i, "1")
            spec = {
                "node": node, "name": name, "frames": keyframes[name],
                "duration": parse_time(_nth(durs, i, "0s")), "easing": _nth(funcs, i, "ease"),
                "delay": parse_time(_nth(delays, i, "0s")),
                "count": float("inf") if count == "infinite" else max(0.0, float(count or 1)),
                "direction": _nth(dirs, i, "normal"), "fill": _nth(fills, i, "none"),
                "paused": _nth(states, i, "running") == "paused",
            }
            old = self.animations.get(key)
            if old is not None:
                spec["start"] = old["start"]
                spec["finished"] = old.get("finished", False)
                spec["props"] = old.get("props", set())
            else:
                spec["start"] = now
                spec["props"] = set()
            self.animations[key] = spec

    # -- transitions
    def on_restyle(self, changes, now):
        """changes: [(node, prop, old, new)] from a :hover/:focus restyle. For
        properties listed in the element's `transition`, put the old value
        back and animate toward the new one."""
        started = False
        for node, prop, old, new in changes:
            s = node.style
            props = _items(s.get("transition-property", "all"))
            durs = _items(s.get("transition-duration", "0s"))
            if not durs or all(parse_time(d) <= 0 for d in durs):
                continue
            idx = None
            for i, p in enumerate(props):
                if p == prop or p == "all" or (p and prop.startswith(p + "-")):
                    idx = i
            if idx is None:
                continue
            dur = parse_time(_nth(durs, idx, "0s"))
            if dur <= 0 or old is None or new is None or prop.startswith("-"):
                continue
            key = (node, prop)
            cur = self.transitions.get(key)
            start_from = old
            if cur is not None:      # retarget a running transition from where it is now
                start_from = node.style.get(prop, old)
            delay = parse_time(_nth(_items(s.get("transition-delay", "0s")), idx, "0s"))
            func = _nth(_items(s.get("transition-timing-function", "ease")), idx, "ease")
            self.transitions[key] = [start_from, new, now, dur, delay, easing(func)]
            self._set(node, prop, start_from)
            started = True
        return started

    # -- frames
    def tick(self, now):
        """Advance everything to time `now`. Returns (changed, needs_layout)."""
        changed = layout = False
        for key, (a, b, start, dur, delay, ease) in list(self.transitions.items()):
            node, prop = key
            t = (now - start - delay) / dur if dur > 0 else 1.0
            t = max(0.0, min(1.0, t))
            value = interpolate(prop, a, b, ease(t), node.style.get("-font-px", 16))
            if self._set(node, prop, value):
                changed = True
                layout = layout or prop not in PAINT_ANIMATABLE
            if t >= 1:
                del self.transitions[key]
        for spec in self.animations.values():
            if spec.get("finished") or spec["paused"]:
                continue
            c, l2 = self._frame(spec, now)
            changed, layout = changed or c, layout or l2
        return changed, layout

    def _frame(self, spec, now):
        node, frames = spec["node"], spec["frames"]
        dur = spec["duration"]
        elapsed = now - spec["start"] - spec["delay"]
        changed = layout = False
        if dur <= 0:
            iteration, prog = spec["count"], 1.0
        elif elapsed < 0:
            if spec["fill"] not in ("backwards", "both"):
                return False, False
            iteration, prog = 0, 0.0
        else:
            total = dur * spec["count"]
            if elapsed >= total:
                spec["finished"] = True
                if spec["fill"] not in ("forwards", "both"):
                    self._restore(spec)
                    return True, any(p not in PAINT_ANIMATABLE for p in spec["props"])
                iteration = spec["count"]
                prog = 1.0 if spec["count"] % 1 == 0 else spec["count"] % 1
                iteration = math.ceil(spec["count"]) - 1
            else:
                iteration = int(elapsed // dur)
                prog = (elapsed - iteration * dur) / dur
        d = spec["direction"]
        reverse = d == "reverse" or (d == "alternate" and iteration % 2 == 1) or \
            (d == "alternate-reverse" and iteration % 2 == 0)
        if reverse:
            prog = 1 - prog
        props = set()
        for _, decls in frames:
            props.update(p for p, _ in decls)
        for prop in props:
            if prop.startswith("--") or prop.startswith("animation"):
                continue
            pts = [(off, dict(decls).get(prop)) for off, decls in frames if prop in dict(decls)]
            base = self.base.get((node, prop), node.style.get(prop))
            if (node, prop) not in self.base:
                self.base[(node, prop)] = node.style.get(prop)
            if not pts or pts[0][0] > 0:
                pts.insert(0, (0.0, base))
            if pts[-1][0] < 1:
                pts.append((1.0, base))
            for k in range(1, len(pts)):
                if prog <= pts[k][0] or k == len(pts) - 1:
                    (o1, v1), (o2, v2) = pts[k - 1], pts[k]
                    local = (prog - o1) / (o2 - o1) if o2 > o1 else 1.0
                    func = spec["easing"]
                    for off, decls in frames:      # a keyframe may set its own timing function
                        if off == o1 and "animation-timing-function" in dict(decls):
                            func = dict(decls)["animation-timing-function"]
                    value = interpolate(prop, v1, v2, easing(func)(max(0.0, min(1.0, local))),
                                        node.style.get("-font-px", 16))
                    break
            spec["props"].add(prop)
            if self._set(node, prop, value):
                changed = True
                layout = layout or prop not in PAINT_ANIMATABLE
        return changed, layout

    def _restore(self, spec):
        node = spec["node"]
        for prop in spec.get("props", ()):
            base = self.base.pop((node, prop), None)
            if base is not None:
                self._set(node, prop, base)

    def _set(self, node, prop, value):
        """Write an animated value into the computed style (and into the
        descendants that inherited the old value)."""
        s = node.style
        old = s.get(prop)
        if old == value or value is None:
            return False
        s[prop] = value
        for k in _CACHES:
            s.pop(k, None)
        if prop == "font-size":
            px = length(value, s.get("-font-px", 16), s.get("-font-px", 16), None)
            if px:
                s["-font-px"] = px
        if prop in INHERITED:
            stack = list(node.children)
            while stack:
                c = stack.pop()
                cs = getattr(c, "style", None)
                if cs is None or cs.get(prop) != old:
                    continue
                cs[prop] = value
                for k in _CACHES:
                    cs.pop(k, None)
                if isinstance(c, Element) or isinstance(c, Text):
                    stack.extend(c.children)
        return True
