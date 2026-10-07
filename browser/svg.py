"""A small SVG rasterizer (our own code; Pillow only fills polygons).

Supports <svg viewBox>, <g>, <path> (all commands, including arcs),
<rect>, <circle>, <ellipse>, <line>, <polyline>, <polygon>, <use>,
transforms (matrix/translate/scale/rotate/skew), fill/stroke/opacity
(presentation attributes and style=""), currentColor, fill-rule via
even-odd masks, and linear gradients (approximated by their first stop).
Used for <img src="*.svg">, CSS background SVGs and inline <svg> in HTML.
"""
import math
import re

from PIL import Image, ImageChops, ImageDraw

from .dom import Element
from .html_parser import parse
from .style import parse_color

SUPERSAMPLE = 3
MAX_DIM = 2000


def _num(v, default=0.0):
    if v is None:
        return default
    m = re.match(r"\s*(-?\d*\.?\d+(?:e[-+]?\d+)?)", str(v), re.I)
    return float(m.group(1)) if m else default


# --- affine matrices [a, b, c, d, e, f] (SVG order) ----------------------

IDENTITY = (1, 0, 0, 1, 0, 0)


def mul(m, n):
    a1, b1, c1, d1, e1, f1 = m
    a2, b2, c2, d2, e2, f2 = n
    return (a1 * a2 + c1 * b2, b1 * a2 + d1 * b2,
            a1 * c2 + c1 * d2, b1 * c2 + d1 * d2,
            a1 * e2 + c1 * f2 + e1, b1 * e2 + d1 * f2 + f1)


def apply(m, x, y):
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def parse_transform(text):
    m = IDENTITY
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", text or ""):
        v = [float(x) for x in re.findall(r"-?\d*\.?\d+(?:e[-+]?\d+)?", args, re.I)]
        if name == "matrix" and len(v) == 6:
            t = tuple(v)
        elif name == "translate" and v:
            t = (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0)
        elif name == "scale" and v:
            t = (v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0)
        elif name == "rotate" and v:
            r = math.radians(v[0])
            t = (math.cos(r), math.sin(r), -math.sin(r), math.cos(r), 0, 0)
            if len(v) == 3:
                t = mul(mul((1, 0, 0, 1, v[1], v[2]), t), (1, 0, 0, 1, -v[1], -v[2]))
        elif name == "skewX" and v:
            t = (1, 0, math.tan(math.radians(v[0])), 1, 0, 0)
        elif name == "skewY" and v:
            t = (1, math.tan(math.radians(v[0])), 0, 1, 0, 0)
        else:
            continue
        m = mul(m, t)
    return m


# --- path data -----------------------------------------------------------

_TOKEN = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]|-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def path_to_subpaths(d):
    """Convert path data into a list of point lists (curves flattened)."""
    tokens = _TOKEN.findall(d or "")
    subpaths = []
    cur = []
    x = y = sx = sy = 0.0
    lcx = lcy = None  # last control point, for S/T
    cmd = None
    i = 0
    n = len(tokens)

    def nums(k):
        nonlocal i
        out = []
        for _ in range(k):
            if i >= n or tokens[i].isalpha():
                raise ValueError
            out.append(float(tokens[i]))
            i += 1
        return out

    def flag():
        # arc flags may be written without separators: "a1 1 0 00 1 1"
        nonlocal i
        t = tokens[i]
        if t in ("0", "1"):
            i += 1
            return float(t)
        if t[0] in "01" and len(t) > 1:
            tokens[i] = t[1:]
            return float(t[0])
        i += 1
        return float(t)

    while i < n:
        t = tokens[i]
        if t.isalpha():
            cmd = t
            i += 1
            if cmd in "Zz":
                if cur:
                    cur.append((sx, sy))
                    subpaths.append(cur)
                    cur = []
                x, y = sx, sy
                lcx = lcy = None
                continue
        elif cmd is None:
            break
        try:
            rel = cmd.islower()
            c = cmd.upper()
            ox, oy = (x, y) if rel else (0.0, 0.0)
            if c == "M":
                px, py = nums(2)
                x, y = ox + px, oy + py
                if cur:
                    subpaths.append(cur)
                cur = [(x, y)]
                sx, sy = x, y
                cmd = "l" if rel else "L"   # subsequent pairs are lineto
                lcx = lcy = None
            elif c == "L":
                px, py = nums(2)
                x, y = ox + px, oy + py
                cur.append((x, y))
                lcx = lcy = None
            elif c == "H":
                (px,) = nums(1)
                x = ox + px
                cur.append((x, y))
                lcx = lcy = None
            elif c == "V":
                (py,) = nums(1)
                y = oy + py
                cur.append((x, y))
                lcx = lcy = None
            elif c in "CS":
                if c == "C":
                    x1, y1, x2, y2, px, py = nums(6)
                    x1, y1 = ox + x1, oy + y1
                else:
                    x2, y2, px, py = nums(4)
                    x1, y1 = (2 * x - lcx, 2 * y - lcy) if lcx is not None else (x, y)
                x2, y2, px, py = ox + x2, oy + y2, ox + px, oy + py
                cur.extend(_cubic((x, y), (x1, y1), (x2, y2), (px, py)))
                lcx, lcy = x2, y2
                x, y = px, py
            elif c in "QT":
                if c == "Q":
                    x1, y1, px, py = nums(4)
                    x1, y1 = ox + x1, oy + y1
                else:
                    px, py = nums(2)
                    x1, y1 = (2 * x - lcx, 2 * y - lcy) if lcx is not None else (x, y)
                px, py = ox + px, oy + py
                cur.extend(_quad((x, y), (x1, y1), (px, py)))
                lcx, lcy = x1, y1
                x, y = px, py
            elif c == "A":
                rx, ry, rot = nums(3)
                large, sweep = flag(), flag()
                px, py = nums(2)
                px, py = ox + px, oy + py
                cur.extend(_arc(x, y, rx, ry, rot, large, sweep, px, py))
                x, y = px, py
                lcx = lcy = None
            else:
                i += 1
            if not cur:
                cur = [(x, y)]
        except (ValueError, IndexError):
            break
    if cur:
        subpaths.append(cur)
    return subpaths


def _cubic(p0, p1, p2, p3, steps=12):
    out = []
    for k in range(1, steps + 1):
        t = k / steps
        mt = 1 - t
        out.append((mt ** 3 * p0[0] + 3 * mt * mt * t * p1[0] + 3 * mt * t * t * p2[0] + t ** 3 * p3[0],
                    mt ** 3 * p0[1] + 3 * mt * mt * t * p1[1] + 3 * mt * t * t * p2[1] + t ** 3 * p3[1]))
    return out


def _quad(p0, p1, p2, steps=10):
    out = []
    for k in range(1, steps + 1):
        t = k / steps
        mt = 1 - t
        out.append((mt * mt * p0[0] + 2 * mt * t * p1[0] + t * t * p2[0],
                    mt * mt * p0[1] + 2 * mt * t * p1[1] + t * t * p2[1]))
    return out


def _arc(x1, y1, rx, ry, phi_deg, large, sweep, x2, y2):
    """Elliptical arc (SVG endpoint parameterisation) -> points."""
    if rx == 0 or ry == 0:
        return [(x2, y2)]
    rx, ry = abs(rx), abs(ry)
    phi = math.radians(phi_deg)
    cp, sp = math.cos(phi), math.sin(phi)
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p = cp * dx + sp * dy
    y1p = -sp * dx + cp * dy
    lam = (x1p ** 2) / (rx ** 2) + (y1p ** 2) / (ry ** 2)
    if lam > 1:
        rx *= math.sqrt(lam)
        ry *= math.sqrt(lam)
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    coef = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        coef = -coef
    cxp = coef * rx * y1p / ry
    cyp = -coef * ry * x1p / rx
    cx = cp * cxp - sp * cyp + (x1 + x2) / 2
    cy = sp * cxp + cp * cyp + (y1 + y2) / 2

    def ang(ux, uy, vx, vy):
        a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        return a
    t1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dt = ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dt > 0:
        dt -= 2 * math.pi
    elif sweep and dt < 0:
        dt += 2 * math.pi
    steps = max(4, int(abs(dt) / (math.pi / 16)))
    out = []
    for k in range(1, steps + 1):
        t = t1 + dt * k / steps
        ex, ey = rx * math.cos(t), ry * math.sin(t)
        out.append((cp * ex - sp * ey + cx, sp * ex + cp * ey + cy))
    return out


def _ellipse_points(cx, cy, rx, ry, steps=48):
    return [(cx + rx * math.cos(2 * math.pi * k / steps), cy + ry * math.sin(2 * math.pi * k / steps))
            for k in range(steps + 1)]


# --- rendering -------------------------------------------------------------

INHERITED = ("fill", "stroke", "stroke-width", "fill-rule", "fill-opacity", "stroke-opacity", "color",
             "stroke-linecap", "stroke-linejoin", "visibility", "display")


class SvgImage:
    """Behaves enough like a PIL image for layout (size) and painting (render)."""

    def __init__(self, root, current_color="#000000", document=None):
        self.root = root
        self.current_color = current_color
        self.document = document
        self.ids = {}
        for n in root.descendants():
            if isinstance(n, Element) and "id" in n.attributes:
                self.ids[n.attributes["id"]] = n
        vb = root.attributes.get("viewbox")
        self.viewbox = None
        if vb:
            parts = [float(v) for v in re.findall(r"-?\d*\.?\d+(?:e[-+]?\d+)?", vb)]
            if len(parts) == 4 and parts[2] > 0 and parts[3] > 0:
                self.viewbox = parts
        w = root.attributes.get("width")
        h = root.attributes.get("height")
        wn = _num(w, 0) if w and "%" not in w else 0
        hn = _num(h, 0) if h and "%" not in h else 0
        if self.viewbox:
            ratio = self.viewbox[3] / self.viewbox[2]
            if wn and not hn:
                hn = wn * ratio
            elif hn and not wn:
                wn = hn / ratio
            elif not wn and not hn:
                wn, hn = self.viewbox[2], self.viewbox[3]
        self.size = (int(round(wn or 300)), int(round(hn or 150)))
        self.has_explicit_size = bool(w and "%" not in w) or bool(h and "%" not in h)
        self.mode = "RGBA"
        self._cache = {}

    def render(self, w, h):
        w, h = max(1, min(MAX_DIM, int(w))), max(1, min(MAX_DIM, int(h)))
        key = (w, h)
        if key in self._cache:
            return self._cache[key]
        ss = SUPERSAMPLE if w * h < 400 * 400 else 1
        W, H = w * ss, h * ss
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        if self.viewbox:
            vx, vy, vw, vh = self.viewbox
            par = self.root.attributes.get("preserveaspectratio", "xMidYMid meet")
            if par.strip().startswith("none"):
                sx, sy = W / vw, H / vh
                base = (sx, 0, 0, sy, -vx * sx, -vy * sy)
            else:
                s = min(W / vw, H / vh) if "slice" not in par else max(W / vw, H / vh)
                tx = (W - vw * s) / 2 - vx * s
                ty = (H - vh * s) / 2 - vy * s
                base = (s, 0, 0, s, tx, ty)
        else:
            sx, sy = W / max(1, self.size[0]), H / max(1, self.size[1])
            base = (sx, 0, 0, sy, 0, 0)
        ctx = {"fill": "black", "stroke": "none", "stroke-width": "1", "color": self.current_color,
               "fill-rule": "nonzero", "fill-opacity": "1", "stroke-opacity": "1", "opacity": 1.0}
        self._render_children(img, self.root, base, ctx, depth=0)
        if ss > 1:
            img = img.resize((w, h), Image.LANCZOS)
        if len(self._cache) > 8:
            self._cache.clear()
        self._cache[key] = img
        return img

    # PIL-image-like helpers used by the painter
    def resize(self, size):
        return self.render(*size)

    def _props(self, el, ctx):
        c = dict(ctx)
        a = el.attributes
        for k in INHERITED:
            if k in a:
                c[k] = a[k]
        st = a.get("style")
        if st:
            for decl in st.split(";"):
                if ":" in decl:
                    k, v = decl.split(":", 1)
                    k = k.strip().lower()
                    if k in INHERITED or k == "opacity":
                        c[k] = v.strip()
        # Author CSS (e.g. ".icon path { fill: currentColor }") beats presentation attributes.
        css = getattr(el, "style", None) or {}
        for k in ("fill", "stroke", "stroke-width", "fill-opacity", "fill-rule"):
            if k in css and k not in ("fill-rule",):
                c[k] = css[k]
        op = a.get("opacity")
        if st and "opacity" in c and not isinstance(c["opacity"], float):
            op = c["opacity"]
        if op is not None:
            c["opacity"] = ctx.get("opacity", 1.0) * _num(op, 1.0)
        elif not isinstance(c.get("opacity"), float):
            c["opacity"] = ctx.get("opacity", 1.0)
        return c

    def _render_children(self, img, el, m, ctx, depth):
        if depth > 40:
            return
        for child in el.children:
            if isinstance(child, Element):
                self._render(img, child, m, ctx, depth + 1)

    def _render(self, img, el, m, ctx, depth):
        tag = el.tag
        if tag in ("defs", "clippath", "mask", "symbol", "lineargradient", "radialgradient", "style",
                   "title", "desc", "metadata", "pattern", "filter", "script", "text", "marker"):
            return
        ctx = self._props(el, ctx)
        if ctx.get("display") == "none" or ctx.get("visibility") == "hidden":
            return
        if "transform" in el.attributes:
            m = mul(m, parse_transform(el.attributes["transform"]))
        a = el.attributes
        if tag in ("g", "a", "svg", "switch"):
            if tag == "svg" and el is not self.root:
                m = mul(m, (1, 0, 0, 1, _num(a.get("x")), _num(a.get("y"))))
            self._render_children(img, el, m, ctx, depth)
            return
        if tag == "use":
            ref = a.get("href") or a.get("xlink:href") or ""
            target = self.ids.get(ref.lstrip("#"))
            if target is None and self.document is not None and ref.startswith("#"):
                target = self.document.find_by_id(ref[1:])   # sprite sheet elsewhere in the page
            if target is not None and target is not el:
                m2 = mul(m, (1, 0, 0, 1, _num(a.get("x")), _num(a.get("y"))))
                if target.tag == "symbol":
                    self._render_children(img, target, m2, self._props(target, ctx), depth)
                else:
                    self._render(img, target, m2, ctx, depth)
            return
        subpaths, closed = self._geometry(el)
        if not subpaths:
            return
        pts = [[apply(m, x, y) for x, y in sp] for sp in subpaths]
        scale = math.sqrt(abs(m[0] * m[3] - m[1] * m[2])) or 1
        fill = self._paint(ctx.get("fill"), ctx)
        if fill and closed:
            self._fill(img, pts, fill, ctx.get("fill-opacity"), ctx["opacity"])
        stroke = self._paint(ctx.get("stroke"), ctx)
        if stroke:
            width = max(1, int(round(_num(ctx.get("stroke-width"), 1) * scale)))
            alpha = int(255 * max(0, min(1, _num(ctx.get("stroke-opacity"), 1) * ctx["opacity"])))
            layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
            d = ImageDraw.Draw(layer)
            rgb = _rgb(stroke)
            for sp in pts:
                if len(sp) > 1:
                    d.line(sp, fill=rgb + (alpha,), width=width, joint="curve")
                    if ctx.get("stroke-linecap") == "round" and width > 2:
                        r = width / 2
                        for (px, py) in (sp[0], sp[-1]):
                            d.ellipse((px - r, py - r, px + r, py + r), fill=rgb + (alpha,))
            img.alpha_composite(layer)

    def _paint(self, value, ctx):
        if value is None:
            return None
        v = value.strip()
        if v in ("none", "transparent", ""):
            return None
        if v.lower() == "currentcolor":
            v = ctx.get("color") or self.current_color
            if v.lower() == "currentcolor":
                v = self.current_color
        m = re.match(r"url\(\s*#([^)\s]+)\s*\)", v)
        if m:
            grad = self.ids.get(m.group(1))
            if grad is not None:
                stops = [s for s in grad.descendants() if isinstance(s, Element) and s.tag == "stop"]
                if not stops and ("href" in grad.attributes or "xlink:href" in grad.attributes):
                    ref = self.ids.get((grad.attributes.get("href") or grad.attributes.get("xlink:href")).lstrip("#"))
                    stops = [s for s in ref.descendants() if isinstance(s, Element) and s.tag == "stop"] if ref else []
                if stops:
                    st = stops[len(stops) // 2]
                    col = st.attributes.get("stop-color")
                    if col is None and "style" in st.attributes:
                        mm = re.search(r"stop-color\s*:\s*([^;]+)", st.attributes["style"])
                        col = mm.group(1) if mm else None
                    return parse_color(col or "black")
            return None
        return parse_color(v, parse_color(ctx.get("color", "black")) or "#000000")

    def _fill(self, img, pts, color, fill_opacity, opacity):
        alpha = max(0, min(1, _num(fill_opacity, 1) * opacity))
        polys = [p for p in pts if len(p) > 2]
        if not polys:
            return
        if len(polys) == 1:
            mask = Image.new("L", img.size, 0)
            ImageDraw.Draw(mask).polygon(polys[0], fill=255)
        else:
            # Even-odd composition of subpaths so that holes (letters, rings) stay open.
            mask = Image.new("L", img.size, 0)
            for p in polys:
                tmp = Image.new("L", img.size, 0)
                ImageDraw.Draw(tmp).polygon(p, fill=255)
                mask = ImageChops.difference(mask, tmp)
        if alpha < 1:
            mask = mask.point(lambda v: int(v * alpha))
        layer = Image.new("RGBA", img.size, _rgb(color) + (255,))
        layer.putalpha(mask)
        img.alpha_composite(layer)

    def _geometry(self, el):
        a = el.attributes
        tag = el.tag
        if tag == "path":
            return path_to_subpaths(a.get("d", "")), True
        if tag == "rect":
            x, y = _num(a.get("x")), _num(a.get("y"))
            w, h = _num(a.get("width")), _num(a.get("height"))
            if "%" in a.get("width", ""):
                w = (self.viewbox[2] if self.viewbox else self.size[0]) * w / 100
            if "%" in a.get("height", ""):
                h = (self.viewbox[3] if self.viewbox else self.size[1]) * h / 100
            if w <= 0 or h <= 0:
                return [], True
            rx = _num(a.get("rx", a.get("ry", 0)))
            ry = _num(a.get("ry", a.get("rx", 0)))
            if rx or ry:
                rx, ry = min(rx or ry, w / 2), min(ry or rx, h / 2)
                d = ("M%f,%f H%f A%f,%f 0 0 1 %f,%f V%f A%f,%f 0 0 1 %f,%f H%f A%f,%f 0 0 1 %f,%f V%f A%f,%f 0 0 1 %f,%f Z" %
                     (x + rx, y, x + w - rx, rx, ry, x + w, y + ry, y + h - ry, rx, ry, x + w - rx, y + h,
                      x + rx, rx, ry, x, y + h - ry, y + ry, rx, ry, x + rx, y))
                return path_to_subpaths(d), True
            return [[(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]], True
        if tag == "circle":
            r = _num(a.get("r"))
            if r <= 0:
                return [], True
            return [_ellipse_points(_num(a.get("cx")), _num(a.get("cy")), r, r)], True
        if tag == "ellipse":
            return [_ellipse_points(_num(a.get("cx")), _num(a.get("cy")), _num(a.get("rx")), _num(a.get("ry")))], True
        if tag == "line":
            return [[(_num(a.get("x1")), _num(a.get("y1"))), (_num(a.get("x2")), _num(a.get("y2")))]], False
        if tag in ("polyline", "polygon"):
            nums = [float(v) for v in re.findall(r"-?\d*\.?\d+(?:e[-+]?\d+)?", a.get("points", ""))]
            pts = list(zip(nums[0::2], nums[1::2]))
            if tag == "polygon" and pts:
                pts.append(pts[0])
            return ([pts] if pts else []), tag == "polygon"
        return [], False


def _rgb(color):
    return (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16))


def from_bytes(data):
    text = data.decode("utf-8", errors="replace")
    doc = parse(text)
    for n in doc.descendants():
        if isinstance(n, Element) and n.tag == "svg":
            return SvgImage(n)
    raise ValueError("no <svg> element")
