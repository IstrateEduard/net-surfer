"""Paint-time effects on a box's drawing commands: filter, backdrop-filter,
mix-blend-mode, clip-path and clip: rect().

Tk draws flat colours, text and images, so colour effects are applied to
each command's colours and, for images, to their pixels with Pillow.
"""
import copy
import math
import re

from . import paint
from .style import length, to_rgba

# ------------------------------------------------------------ filters


def parse_filter(value, fs=16.0):
    """'blur(2px) grayscale(50%) drop-shadow(...)' -> [(name, args)]"""
    out = []
    if not value or value.strip() in ("none", "initial"):
        return out
    for name, args in re.findall(r"([a-zA-Z-]+)\(((?:[^()]|\([^()]*\))*)\)", value):
        name = name.lower()
        a = args.strip()
        if name == "drop-shadow":
            out.append((name, a))
            continue
        if name == "blur":
            out.append((name, length(a or "0", fs, None, 0) or 0.0))
            continue
        if name == "hue-rotate":
            m = re.match(r"(-?[\d.]+)(deg|rad|turn|grad)?", a or "0")
            v = float(m.group(1)) if m else 0.0
            unit = (m.group(2) if m else "deg") or "deg"
            out.append((name, v if unit == "deg" else math.degrees(v) if unit == "rad" else v * 360
                        if unit == "turn" else v * 0.9))
            continue
        if not a:
            v = 1.0 if name not in ("invert", "grayscale", "sepia") else 1.0
        elif a.endswith("%"):
            v = float(a[:-1]) / 100
        else:
            try:
                v = float(a)
            except ValueError:
                continue
        out.append((name, v))
    return out


def _clamp(v):
    return max(0.0, min(255.0, v))


def filter_rgb(rgb, filters):
    """Apply the colour part of a filter list to one (r, g, b) colour."""
    r, g, b = rgb
    for name, v in filters:
        if name == "brightness":
            r, g, b = r * v, g * v, b * v
        elif name == "contrast":
            r, g, b = ((c - 127.5) * v + 127.5 for c in (r, g, b))
        elif name == "grayscale":
            v = min(1.0, v)
            l = 0.2126 * r + 0.7152 * g + 0.0722 * b
            r, g, b = (c + (l - c) * v for c in (r, g, b))
        elif name == "sepia":
            v = min(1.0, v)
            sr = 0.393 * r + 0.769 * g + 0.189 * b
            sg = 0.349 * r + 0.686 * g + 0.168 * b
            sb = 0.272 * r + 0.534 * g + 0.131 * b
            r, g, b = r + (sr - r) * v, g + (sg - g) * v, b + (sb - b) * v
        elif name == "invert":
            v = min(1.0, v)
            r, g, b = (c + (255 - 2 * c) * v for c in (r, g, b))
        elif name == "saturate":
            l = 0.2126 * r + 0.7152 * g + 0.0722 * b
            r, g, b = (l + (c - l) * v for c in (r, g, b))
        elif name == "hue-rotate":
            t = math.radians(v)
            cos, sin = math.cos(t), math.sin(t)
            m = [0.213 + cos * 0.787 - sin * 0.213, 0.715 - cos * 0.715 - sin * 0.715, 0.072 - cos * 0.072 + sin * 0.928,
                 0.213 - cos * 0.213 + sin * 0.143, 0.715 + cos * 0.285 + sin * 0.140, 0.072 - cos * 0.072 - sin * 0.283,
                 0.213 - cos * 0.213 - sin * 0.787, 0.715 - cos * 0.715 + sin * 0.715, 0.072 + cos * 0.928 + sin * 0.072]
            r, g, b = (m[0] * r + m[1] * g + m[2] * b, m[3] * r + m[4] * g + m[5] * b,
                       m[6] * r + m[7] * g + m[8] * b)
        elif name == "opacity":
            r, g, b = (c * v + 255 * (1 - v) for c in (r, g, b))
        r, g, b = _clamp(r), _clamp(g), _clamp(b)
    return r, g, b


def _hex(rgb):
    return "#%02x%02x%02x" % tuple(int(round(_clamp(c))) for c in rgb)


def _map_color(c, fn):
    if not c or not isinstance(c, str) or not c.startswith("#") or len(c) != 7:
        return c
    return _hex(fn((int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16))))


_IMG_CACHE = {}


def map_commands(cmds, color_fn, image_fn, key):
    """Copies of `cmds` with every colour passed through color_fn and every
    image through image_fn (a PIL RGBA -> RGBA function); `key` names the
    effect so processed images can be cached."""
    out = []
    for cmd in cmds:
        c = copy.copy(cmd)
        for attr in ("color", "outline", "fill"):
            if hasattr(c, attr):
                setattr(c, attr, _map_color(getattr(c, attr), color_fn))
        if isinstance(c, paint.DrawImage) and image_fn is not None:
            ck = (key, id(cmd.image), cmd.full_size, cmd.crop)
            img = _IMG_CACHE.get(ck)
            if img is None:
                try:
                    src = cmd.image
                    fw, fh = cmd.full_size
                    if hasattr(src, "render"):
                        src = src.render(max(1, fw), max(1, fh))
                    elif (fw, fh) != src.size and fw > 0 and fh > 0:
                        src = src.resize((max(1, fw), max(1, fh)))
                    if cmd.crop:
                        x, y, w, h = cmd.crop
                        src = src.crop((x, y, x + max(1, w), y + max(1, h)))
                    img = image_fn(src.convert("RGBA"))
                except Exception:
                    img = None
                if len(_IMG_CACHE) > 300:
                    _IMG_CACHE.clear()
                _IMG_CACHE[ck] = img
            if img is not None:
                c.image = img
                c.crop = None
                c.full_size = img.size
                c.url = "%s#%x" % (cmd.url, hash(ck) & 0xffffffffffff)
        out.append(c)
    return out


def _image_filter(filters):
    from PIL import ImageFilter

    def fn(img):
        alpha = img.getchannel("A")
        px = img.convert("RGB")
        lut = []
        # per-channel colour effects through a sampled lookup when channel-separable
        separable = all(n in ("brightness", "contrast", "invert", "opacity") for n, _ in filters)
        if separable:
            for ch in range(3):
                for v in range(256):
                    rgb = [0, 0, 0]
                    rgb[ch] = v
                    lut.append(int(round(filter_rgb(tuple(rgb), filters)[ch])))
            px = px.point(lut)
        else:
            data = [filter_rgb(p, filters) for p in px.getdata()] if px.size[0] * px.size[1] <= 250000 else None
            if data is not None:
                px.putdata([tuple(int(c) for c in p) for p in data])
        for name, v in filters:
            if name == "blur" and v > 0:
                px = px.filter(ImageFilter.GaussianBlur(v))
                alpha = alpha.filter(ImageFilter.GaussianBlur(v))
        for name, v in filters:
            if name == "opacity":
                alpha = alpha.point(lambda a: int(a * v))
        out = px.convert("RGBA")
        out.putalpha(alpha)
        return out
    return fn


def apply_filter(dl, value, fs, current_color):
    """filter: on a box's display list."""
    filters = parse_filter(value, fs)
    if not filters:
        return
    color_filters = [f for f in filters if f[0] not in ("blur", "drop-shadow")]
    shadows = []
    for name, args in filters:
        if name == "drop-shadow":
            shadows.append(_shadow(args, fs, current_color))
    cmds = dl.commands
    if color_filters or any(f[0] == "blur" for f in filters):
        key = repr(filters)
        cmds = map_commands(cmds, lambda rgb: filter_rgb(rgb, color_filters), _image_filter(filters), key)
    out = []
    for sh in shadows:
        if sh is None:
            continue
        dx, dy, col = sh
        moved = []
        for c in cmds:
            m = copy.copy(c)
            _shift(m, dx, dy)
            moved.append(m)
        out += map_commands(moved, lambda rgb, col=col: col, _solid_image(col), "shadow" + repr(col))
    dl.commands = out + cmds


def _shadow(args, fs, current):
    nums, color = [], None
    for tok in re.findall(r"[a-z]+\([^)]*\)|\S+", args):
        px = length(tok, fs, None, None)
        if px is not None and (tok[0].isdigit() or tok[0] in "-.+"):
            nums.append(px)
        else:
            color = tok
    if len(nums) < 2:
        return None
    rgba = to_rgba(color or "black", current) or (0, 0, 0, 1)
    a = rgba[3]
    blur = nums[2] if len(nums) > 2 else 0
    a = a * (0.6 if blur > 0 else 1.0)
    rgb = tuple(c * a + 255 * (1 - a) for c in rgba[:3])
    return nums[0], nums[1], rgb


def _solid_image(rgb):
    def fn(img):
        from PIL import Image
        out = Image.new("RGBA", img.size, tuple(int(c) for c in rgb) + (255,))
        out.putalpha(img.getchannel("A"))
        return out
    return fn


def _shift(c, dx, dy):
    for a, d in (("left", dx), ("right", dx), ("x1", dx), ("x2", dx), ("top", dy), ("bottom", dy),
                 ("y1", dy), ("y2", dy)):
        if hasattr(c, a):
            setattr(c, a, getattr(c, a) + d)
    if hasattr(c, "pts"):
        if c.pts and isinstance(c.pts[0], tuple):
            c.pts = [(x + dx, y + dy) for x, y in c.pts]
        else:
            c.pts = [v + (dx if i % 2 == 0 else dy) for i, v in enumerate(c.pts)]
    if getattr(c, "anchor", None):
        c.anchor = (c.anchor[0] + dx, c.anchor[1] + dy)


def backdrop(dl, rect, value, fs, current_color):
    """backdrop-filter: draw filtered copies of what is already painted
    behind the box, clipped to the box."""
    filters = parse_filter(value, fs)
    if not filters:
        return
    color_filters = [f for f in filters if f[0] not in ("blur", "drop-shadow")]
    behind = []
    for c in dl.commands:
        if c.intersects(*rect):
            cl = c.clipped(rect)
            if cl is not None:
                behind.append(cl)
    if not behind:
        # nothing painted behind: the canvas itself (white) gets filtered
        rgb = filter_rgb((255, 255, 255), color_filters)
        if rgb != (255, 255, 255):
            dl.add(paint.DrawRect(*rect, _hex(rgb)))
        return
    dl.commands.extend(map_commands(behind, lambda rgb: filter_rgb(rgb, color_filters),
                                    _image_filter(filters), "backdrop" + repr(filters)))


# ------------------------------------------------------------ blending
BLEND = {
    "multiply": lambda s, b: s * b / 255,
    "screen": lambda s, b: s + b - s * b / 255,
    "darken": min,
    "lighten": max,
    "difference": lambda s, b: abs(s - b),
    "exclusion": lambda s, b: s + b - 2 * s * b / 255,
    "overlay": lambda s, b: (2 * s * b / 255) if b < 128 else (255 - 2 * (255 - s) * (255 - b) / 255),
    "hard-light": lambda s, b: (2 * s * b / 255) if s < 128 else (255 - 2 * (255 - s) * (255 - b) / 255),
    "color-dodge": lambda s, b: 255 if s >= 255 else min(255, b * 255 / (255 - s)),
    "color-burn": lambda s, b: 0 if s <= 0 else max(0, 255 - (255 - b) * 255 / s),
    "soft-light": lambda s, b: b - (255 - 2 * s) * b * (255 - b) / 65025 if s < 128 else
    b + (2 * s - 255) * ((math.sqrt(b / 255) * 255) - b) / 255,
}


def apply_blend(dl, mode, backdrop_rgb):
    """mix-blend-mode against the solid colour behind the box."""
    fn = BLEND.get(mode)
    if fn is None:
        if mode in ("luminosity", "color", "hue", "saturation"):
            fn = BLEND["multiply"] if mode != "luminosity" else (lambda s, b: s)
        else:
            return
    br = backdrop_rgb

    def col(rgb):
        return tuple(fn(s, b) for s, b in zip(rgb, br))

    def img(im):
        from PIL import Image
        px = im.convert("RGB")
        if px.size[0] * px.size[1] <= 250000:
            px.putdata([tuple(int(_clamp(v)) for v in col(p)) for p in px.getdata()])
        out = px.convert("RGBA")
        out.putalpha(im.getchannel("A"))
        return out
    dl.commands = map_commands(dl.commands, col, img, "blend" + mode + repr(br))


# ------------------------------------------------------------ clipping
def clip_shape(value, box_rect, fs):
    """clip-path value -> ('rect', (x1, y1, x2, y2)) or ('poly', [points]) or None."""
    v = (value or "none").strip()
    if v in ("none", "initial") or v.startswith("url("):
        return None
    x1, y1, x2, y2 = box_rect
    w, h = x2 - x1, y2 - y1
    m = re.match(r"(inset|circle|ellipse|polygon|rect|xywh)\((.*)\)", v, re.S)
    if not m:
        return None
    kind, args = m.group(1), m.group(2).strip()

    def L(tok, base):
        return length(tok, fs, base, 0) or 0.0
    if kind == "inset":
        args = args.split(" round ")[0]
        t = args.split()
        t = (t * 4)[:4] if len(t) == 1 else (t + t[:2])[:4] if len(t) == 2 else (t + [t[1]])[:4]
        return ("rect", (x1 + L(t[3], w), y1 + L(t[0], h), x2 - L(t[1], w), y2 - L(t[2], h)))
    if kind in ("rect", "xywh"):
        t = args.split(" round ")[0].split()
        if kind == "rect" and len(t) >= 4:
            return ("rect", (x1 + L(t[3], w), y1 + L(t[0], h), x1 + L(t[1], w), y1 + L(t[2], h)))
        if len(t) >= 4:
            return ("rect", (x1 + L(t[0], w), y1 + L(t[1], h), x1 + L(t[0], w) + L(t[2], w),
                             y1 + L(t[1], h) + L(t[3], h)))
        return None
    if kind in ("circle", "ellipse"):
        shape, _, at = args.partition(" at ")
        if args.startswith("at "):
            shape, at = "", args[3:]
        cx, cy = x1 + w / 2, y1 + h / 2
        if at.strip():
            p = at.split()
            kw = {"left": 0, "center": 0.5, "right": 1, "top": 0, "bottom": 1}
            px = [kw[t] * (w if i == 0 else h) if t in kw else L(t, w if i == 0 else h) for i, t in enumerate(p[:2])]
            if len(px) == 1:
                px.append(h / 2)
            cx, cy = x1 + px[0], y1 + px[1]
        parts = shape.split()
        if kind == "circle":
            ref = math.hypot(w, h) / math.sqrt(2)
            r = L(parts[0], ref) if parts and parts[0] not in ("closest-side", "farthest-side") else \
                (min(cx - x1, x2 - cx, cy - y1, y2 - cy) if not parts or parts[0] == "closest-side"
                 else max(cx - x1, x2 - cx, cy - y1, y2 - cy))
            rx = ry = r
        else:
            rx = L(parts[0], w) if parts else min(cx - x1, x2 - cx)
            ry = L(parts[1], h) if len(parts) > 1 else min(cy - y1, y2 - cy)
        pts = [(cx + rx * math.cos(k * math.pi / 24), cy + ry * math.sin(k * math.pi / 24)) for k in range(48)]
        return ("poly", pts)
    if kind == "polygon":
        pts = []
        for pair in args.split(","):
            t = pair.split()
            if t and t[0] in ("nonzero", "evenodd"):
                continue
            if len(t) >= 2:
                pts.append((x1 + L(t[0], w), y1 + L(t[1], h)))
        return ("poly", pts) if len(pts) >= 3 else None
    return None


def clip_rect_value(value, box_rect):
    """clip: rect(top, right, bottom, left) for absolutely positioned boxes."""
    m = re.match(r"rect\((.*)\)", (value or "").strip())
    if not m:
        return None
    t = [x for x in re.split(r"[\s,]+", m.group(1).strip()) if x]
    if len(t) != 4:
        return None
    x1, y1, x2, y2 = box_rect
    vals = []
    for i, tok in enumerate(t):
        if tok == "auto":
            vals.append(None)
        else:
            vals.append(length(tok, 16, None, 0) or 0.0)
    top = y1 + vals[0] if vals[0] is not None else y1
    right = x1 + vals[1] if vals[1] is not None else x2
    bottom = y1 + vals[2] if vals[2] is not None else y2
    left = x1 + vals[3] if vals[3] is not None else x1
    return ("rect", (left, top, right, bottom))


def _inside(pt, poly):
    x, y = pt
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi:
            inside = not inside
        j = i
    return inside


def _clip_poly(subject, clip):
    """Sutherland-Hodgman: subject polygon clipped by a convex clip polygon."""
    def side(p, a, b):
        return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
    # orient the clip polygon counter-clockwise in screen terms
    area = sum(clip[i][0] * clip[(i + 1) % len(clip)][1] - clip[(i + 1) % len(clip)][0] * clip[i][1]
               for i in range(len(clip)))
    if area < 0:
        clip = list(reversed(clip))
    out = list(subject)
    for i in range(len(clip)):
        a, b = clip[i], clip[(i + 1) % len(clip)]
        inp, out = out, []
        if not inp:
            break
        s = inp[-1]
        for e in inp:
            if side(e, a, b) >= 0:
                if side(s, a, b) < 0:
                    out.append(_intersect(s, e, a, b))
                out.append(e)
            elif side(s, a, b) >= 0:
                out.append(_intersect(s, e, a, b))
            s = e
    return out


def _intersect(p, q, a, b):
    x1, y1 = p
    x2, y2 = q
    x3, y3 = a
    x4, y4 = b
    den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(den) < 1e-12:
        return q
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / den
    return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))


def apply_clip(dl, shape):
    """Keep only what lies inside a clip-path / clip shape."""
    kind, data = shape
    if kind == "rect":
        x1, y1, x2, y2 = data
        if x2 <= x1 or y2 <= y1:
            dl.commands, dl.hits = [], []
            return
        dl.commands = [c2 for c2 in (c.clipped(data) for c in dl.commands) if c2 is not None]
        dl.hits = [(max(a, x1), max(b, y1), min(c, x2), min(d, y2), n) for a, b, c, d, n in dl.hits
                   if c > x1 and a < x2 and d > y1 and b < y2]
        return
    poly = data
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    bbox = (min(xs), min(ys), max(xs), max(ys))
    out = []
    for c in dl.commands:
        t = type(c).__name__
        if t in ("DrawRect", "DrawPolygon", "DrawRoundRect"):
            if t == "DrawRect":
                pts = [(c.left, c.top), (c.right, c.top), (c.right, c.bottom), (c.left, c.bottom)]
                fill, outline, width, stip = c.color, c.outline, 1, getattr(c, "stipple", None)
            elif t == "DrawRoundRect":
                flat = c.points()
                pts = list(zip(flat[0::2], flat[1::2]))
                fill, outline, width, stip = c.fill, c.outline, c.width, c.stipple
            else:
                pts, fill, outline, width, stip = c.pts, c.fill, c.outline, c.width, c.stipple
            clipped = _clip_poly(pts, poly)
            if len(clipped) >= 3:
                out.append(paint.DrawPolygon(clipped, fill, outline, width, stip))
        elif t == "DrawImage":
            img = _mask_image(c, poly)
            if img is not None:
                out.append(img)
        elif t == "DrawText":
            mid = ((c.left + c.right) / 2, (c.top + c.bottom) / 2)
            if _inside(mid, poly):
                out.append(c)
        else:
            cl = c.clipped(bbox) if hasattr(c, "clipped") else c
            if cl is not None:
                out.append(cl)
    dl.commands = out
    dl.hits = [h for h in dl.hits if _inside(((h[0] + h[2]) / 2, (h[1] + h[3]) / 2), poly)]


def _mask_image(cmd, poly):
    from PIL import Image, ImageDraw
    try:
        src = cmd.image
        fw, fh = cmd.full_size
        if hasattr(src, "render"):
            src = src.render(max(1, fw), max(1, fh))
        elif (fw, fh) != src.size and fw > 0 and fh > 0:
            src = src.resize((max(1, fw), max(1, fh)))
        if cmd.crop:
            x, y, w, h = cmd.crop
            src = src.crop((x, y, x + max(1, w), y + max(1, h)))
        src = src.convert("RGBA")
    except Exception:
        return None
    mask = Image.new("L", src.size, 0)
    ImageDraw.Draw(mask).polygon([(x - cmd.left, y - cmd.top) for x, y in poly], fill=255)
    alpha = Image.composite(src.getchannel("A"), mask, mask)
    src.putalpha(alpha)
    c = copy.copy(cmd)
    c.image, c.crop, c.full_size = src, None, src.size
    c.url = "%s#clip%x" % (cmd.url, hash(tuple(poly)) & 0xffffffffffff)
    return c
