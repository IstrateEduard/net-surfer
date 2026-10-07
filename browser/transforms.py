"""CSS transforms: parse transform / translate / rotate / scale into a 2D
affine matrix, and apply it to already painted drawing commands.

A matrix (a, b, c, d, e, f) maps a point (x, y) to
(a*x + c*y + e, b*x + d*y + f), as in CSS matrix().
"""
import math
import re

from . import paint
from .style import length

IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def multiply(m, n):
    """m then... the matrix for applying n first, then m (m * n)."""
    a1, b1, c1, d1, e1, f1 = m
    a2, b2, c2, d2, e2, f2 = n
    return (a1 * a2 + c1 * b2, b1 * a2 + d1 * b2,
            a1 * c2 + c1 * d2, b1 * c2 + d1 * d2,
            a1 * e2 + c1 * f2 + e1, b1 * e2 + d1 * f2 + f1)


def apply(m, x, y):
    a, b, c, d, e, f = m
    return a * x + c * y + e, b * x + d * y + f


def invert(m):
    a, b, c, d, e, f = m
    det = a * d - b * c
    if abs(det) < 1e-12:
        return None
    ia, ib, ic, id_ = d / det, -b / det, -c / det, a / det
    return (ia, ib, ic, id_, -(ia * e + ic * f), -(ib * e + id_ * f))


def _angle(tok):
    tok = tok.strip().lower()
    m = re.match(r"^(-?[\d.]+(?:e-?\d+)?)(deg|rad|turn|grad)?$", tok)
    if not m:
        return 0.0
    v = float(m.group(1))
    unit = m.group(2) or "deg"
    return math.radians(v) if unit == "deg" else v if unit == "rad" else v * 2 * math.pi if unit == "turn" \
        else math.radians(v * 0.9)


def _num(tok):
    tok = tok.strip()
    if tok.endswith("%"):
        return float(tok[:-1]) / 100
    return float(tok)


def function_matrix(fn, args, w, h, fs):
    fn = fn.lower()
    a = [x.strip() for x in re.split(r"\s*,\s*|\s+", args.strip()) if x.strip()]

    def ln(tok, base):
        return length(tok, fs, base, 0) or 0.0
    if fn in ("translate", "translate3d"):
        return (1, 0, 0, 1, ln(a[0], w), ln(a[1], h) if len(a) > 1 else 0.0)
    if fn == "translatex":
        return (1, 0, 0, 1, ln(a[0], w), 0)
    if fn == "translatey":
        return (1, 0, 0, 1, 0, ln(a[0], h))
    if fn in ("scale", "scale3d"):
        sx = _num(a[0])
        sy = _num(a[1]) if len(a) > 1 else sx
        return (sx, 0, 0, sy, 0, 0)
    if fn == "scalex":
        return (_num(a[0]), 0, 0, 1, 0, 0)
    if fn == "scaley":
        return (1, 0, 0, _num(a[0]), 0, 0)
    if fn in ("rotate", "rotatez"):
        t = _angle(a[-1])
        return (math.cos(t), math.sin(t), -math.sin(t), math.cos(t), 0, 0)
    if fn == "rotate3d" and len(a) == 4:
        # only the z component is visible in 2D
        z = float(a[2])
        t = _angle(a[3]) * (1 if z >= 0 else -1) if abs(z) > 1e-9 else 0.0
        return (math.cos(t), math.sin(t), -math.sin(t), math.cos(t), 0, 0)
    if fn == "rotatex":     # seen from the front: squashes vertically
        return (1, 0, 0, math.cos(_angle(a[0])), 0, 0)
    if fn == "rotatey":
        return (math.cos(_angle(a[0])), 0, 0, 1, 0, 0)
    if fn == "skew":
        ax = math.tan(_angle(a[0]))
        ay = math.tan(_angle(a[1])) if len(a) > 1 else 0.0
        return (1, ay, ax, 1, 0, 0)
    if fn == "skewx":
        return (1, 0, math.tan(_angle(a[0])), 1, 0, 0)
    if fn == "skewy":
        return (1, math.tan(_angle(a[0])), 0, 1, 0, 0)
    if fn == "matrix" and len(a) == 6:
        return tuple(float(v) for v in a)
    if fn == "matrix3d" and len(a) == 16:
        v = [float(x) for x in a]
        return (v[0], v[1], v[4], v[5], v[12], v[13])
    return IDENTITY       # perspective() and unknown functions


def box_matrix(style, x1, y1, x2, y2, fs, include_translate=True):
    """Full matrix of a box's translate, rotate, scale and transform
    properties around its transform-origin, or None if there is none."""
    w, h = x2 - x1, y2 - y1
    parts = []
    tr = style.get("translate", "none")
    if tr and tr != "none" and include_translate:
        toks = tr.split()
        parts.append((1, 0, 0, 1, length(toks[0], fs, w, 0) or 0,
                      length(toks[1], fs, h, 0) or 0 if len(toks) > 1 else 0))
    ro = style.get("rotate", "none")
    if ro and ro != "none":
        t = _angle(ro.split()[-1])
        parts.append((math.cos(t), math.sin(t), -math.sin(t), math.cos(t), 0, 0))
    sc = style.get("scale", "none")
    if sc and sc != "none":
        toks = sc.split()
        sx = _num(toks[0])
        sy = _num(toks[1]) if len(toks) > 1 else sx
        parts.append((sx, 0, 0, sy, 0, 0))
    t = style.get("transform", "none")
    if t and t != "none":
        for fn, args in re.findall(r"([a-zA-Z0-9]+)\(([^()]*)\)", t):
            if not include_translate and fn.lower().startswith("translate"):
                continue
            try:
                parts.append(function_matrix(fn, args, w, h, fs))
            except (ValueError, IndexError):
                pass
    if not parts:
        return None
    m = IDENTITY
    for p in parts:
        m = multiply(m, p)
    if m == IDENTITY:
        return None
    ox, oy = _origin(style.get("transform-origin", "50% 50%"), w, h, fs)
    ox, oy = x1 + ox, y1 + oy
    return multiply((1, 0, 0, 1, ox, oy), multiply(m, (1, 0, 0, 1, -ox, -oy)))


def _origin(value, w, h, fs):
    toks = value.lower().split()
    kw = {"left": 0.0, "center": 0.5, "right": 1.0, "top": 0.0, "bottom": 1.0}
    if len(toks) == 1:
        toks.append("center")
    if toks[0] in ("top", "bottom") or toks[1] in ("left", "right"):
        toks[0], toks[1] = toks[1], toks[0]
    out = []
    for tok, base in zip(toks[:2], (w, h)):
        if tok in kw:
            out.append(kw[tok] * base)
        else:
            out.append(length(tok, fs, base, 0) or 0.0)
    return out[0], out[1]


def only_translation(m):
    return m is not None and abs(m[0] - 1) < 1e-9 and abs(m[3] - 1) < 1e-9 and abs(m[1]) < 1e-9 and abs(m[2]) < 1e-9


# --------------------------------------------------------------- commands
_IMAGE_CACHE = {}


def transform_list(dl, m):
    """Transform every command and hit area of a display list in place."""
    out = []
    for cmd in dl.commands:
        out.extend(transform_command(cmd, m))
    dl.commands = out
    hits = []
    for x1, y1, x2, y2, node in dl.hits:
        pts = [apply(m, x, y) for x, y in ((x1, y1), (x2, y1), (x2, y2), (x1, y2))]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        hits.append((min(xs), min(ys), max(xs), max(ys), node))
    dl.hits = hits


def _scale_of(m):
    return math.sqrt(abs(m[0] * m[3] - m[1] * m[2])) or 1.0


def transform_command(cmd, m):
    t = type(cmd).__name__
    if t == "DrawRect":
        pts = [apply(m, x, y) for x, y in ((cmd.left, cmd.top), (cmd.right, cmd.top),
                                           (cmd.right, cmd.bottom), (cmd.left, cmd.bottom))]
        return [paint.DrawPolygon(pts, cmd.color, cmd.outline, 1, getattr(cmd, "stipple", None))]
    if t in ("DrawRoundRect", "DrawOval"):
        if t == "DrawOval":
            cx, cy = (cmd.left + cmd.right) / 2, (cmd.top + cmd.bottom) / 2
            rx, ry = (cmd.right - cmd.left) / 2, (cmd.bottom - cmd.top) / 2
            raw = [(cx + rx * math.cos(k * math.pi / 16), cy + ry * math.sin(k * math.pi / 16)) for k in range(32)]
            fill, outline, width, stip = cmd.color, cmd.outline, 1, None
        else:
            flat = cmd.points()
            raw = list(zip(flat[0::2], flat[1::2]))
            fill, outline, width, stip = cmd.fill, cmd.outline, cmd.width, getattr(cmd, "stipple", None)
        pts = [apply(m, x, y) for x, y in raw]
        return [paint.DrawPolygon(pts, fill, outline, width * _scale_of(m), stip)]
    if t == "DrawText":
        x, y = apply(m, cmd.left, cmd.top)
        sx = math.hypot(m[0], m[1])
        angle = -math.degrees(math.atan2(m[1], m[0]))
        from .fonts import scaled_font
        font = scaled_font(cmd.font, sx) if abs(sx - 1) > 0.01 else cmd.font
        nt = paint.DrawText(x, y, cmd.text, font, cmd.color)
        nt.angle = angle if abs(angle) > 0.05 else 0
        if nt.angle:
            # bounding box of the rotated run
            w = cmd.right - cmd.left
            pts = [apply(m, px, py) for px, py in ((cmd.left, cmd.top), (cmd.left + w, cmd.top),
                                                   (cmd.left + w, cmd.bottom), (cmd.left, cmd.bottom))]
            nt.left, nt.top = min(p[0] for p in pts), min(p[1] for p in pts)
            nt.right, nt.bottom = max(p[0] for p in pts), max(p[1] for p in pts)
            nt.anchor = (x, y)
        return [nt]
    if t == "DrawLine":
        x1, y1 = apply(m, cmd.x1, cmd.y1)
        x2, y2 = apply(m, cmd.x2, cmd.y2)
        return [paint.DrawLine(x1, y1, x2, y2, cmd.color, max(1, int(round(cmd.thickness * _scale_of(m)))),
                               getattr(cmd, "dash", None))]
    if t == "DrawPolyline":
        pts = []
        for i in range(0, len(cmd.pts), 2):
            pts += list(apply(m, cmd.pts[i], cmd.pts[i + 1]))
        return [paint.DrawPolyline(pts, cmd.color, cmd.thickness)]
    if t == "DrawPolygon":
        return [paint.DrawPolygon([apply(m, x, y) for x, y in cmd.pts], cmd.fill, cmd.outline,
                                  cmd.width, cmd.stipple)]
    if t == "DrawImage":
        out = _transform_image(cmd, m)
        return [out] if out is not None else []
    return [cmd]


def _transform_image(cmd, m):
    from PIL import Image
    if only_translation(m):
        import copy
        c = copy.copy(cmd)
        c.left, c.right = c.left + m[4], c.right + m[4]
        c.top, c.bottom = c.top + m[5], c.bottom + m[5]
        return c
    inv = invert(m)
    if inv is None:
        return None
    img = cmd.image
    fw, fh = cmd.full_size
    try:
        if hasattr(img, "render"):
            img = img.render(max(1, fw), max(1, fh))
        elif (fw, fh) != img.size and fw > 0 and fh > 0:
            img = img.resize((max(1, fw), max(1, fh)))
        if cmd.crop:
            x, y, w, h = cmd.crop
            img = img.crop((x, y, x + max(1, w), y + max(1, h)))
        img = img.convert("RGBA")
    except Exception:
        return None
    corners = [apply(m, x, y) for x, y in ((cmd.left, cmd.top), (cmd.right, cmd.top),
                                           (cmd.right, cmd.bottom), (cmd.left, cmd.bottom))]
    bx1, by1 = min(p[0] for p in corners), min(p[1] for p in corners)
    bx2, by2 = max(p[0] for p in corners), max(p[1] for p in corners)
    W, H = max(1, int(math.ceil(bx2 - bx1))), max(1, int(math.ceil(by2 - by1)))
    if W * H > 4000 * 4000:
        return None
    key = (id(cmd.image), cmd.full_size, cmd.crop, tuple(round(v, 4) for v in m[:4]), W, H)
    out = _IMAGE_CACHE.get(key)
    if out is None:
        a, b, c, d, e, f = inv
        # output pixel (X, Y) is document point (bx1 + X, by1 + Y); its source
        # pixel is inv(point) - (left, top)
        data = (a, c, a * bx1 + c * by1 + e - cmd.left,
                b, d, b * bx1 + d * by1 + f - cmd.top)
        out = img.transform((W, H), Image.AFFINE, data, resample=Image.BILINEAR)
        if len(_IMAGE_CACHE) > 300:
            _IMAGE_CACHE.clear()
        _IMAGE_CACHE[key] = out
    return paint.DrawImage(bx1, by1, W, H, out, "xform:%x" % (hash(key) & 0xffffffffffff))
