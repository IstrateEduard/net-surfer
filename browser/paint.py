"""Display list: the drawing commands produced by the painter.

Every command is in document coordinates; the browser window subtracts
the scroll offset and only executes commands that intersect the viewport.
"""


class DisplayList:
    def __init__(self):
        self.commands = []
        self.hits = []          # (x1, y1, x2, y2, element) for links and form controls
        self.height = 0
        self.deferred = None    # positioned boxes with z-index > 0, painted last (see layout)
        self.clip = None        # clip rectangle in force while painting into this list
        self.layers = None      # [Layer]: fixed / sticky boxes that move with scrolling
        self.negatives = None   # positioned boxes with z-index < 0 in the current stacking context
        self.neg_index = 0      # where those go: just after the context's own background

    def add(self, cmd):
        self.commands.append(cmd)

    def hit_test(self, x, y):
        for x1, y1, x2, y2, node in reversed(self.hits):
            if x1 <= x <= x2 and y1 <= y <= y2:
                if getattr(node, "style", {}).get("pointer-events") == "none":
                    continue          # pointer-events: none -- clicks go to what is underneath
                return node
        return None

    def hit_test_at(self, x, y, scroll):
        """Hit test in document coordinates, with the window scrolled to `scroll`
        (fixed and sticky layers sit on top and move with the scroll)."""
        for layer in reversed(self.layers or ()):
            node = layer.list.hit_test(x, y - layer.offset(scroll))
            if node is not None:
                return node
        return self.hit_test(x, y)


class Layer:
    """Commands for a position: fixed or sticky box. They are drawn at their
    laid-out position plus offset(scroll):
      fixed   -> the scroll itself (stays put in the window);
      sticky  -> clamp(scroll + top - y0, 0, room) (sticks while its parent is on screen)."""

    def __init__(self, kind, y0=0.0, top=0.0, room=0.0):
        self.kind = kind
        self.list = DisplayList()
        self.y0, self.top, self.room = y0, top, room

    def offset(self, scroll):
        if self.kind in ("fixed", "fixedbg"):
            return scroll
        return max(0.0, min(scroll + self.top - self.y0, self.room))


class Command:
    top = bottom = left = right = 0

    def intersects(self, x1, y1, x2, y2):
        return self.bottom >= y1 and self.top <= y2 and self.right >= x1 and self.left <= x2

    def shifted_into(self, clip):
        return self


class DrawRect(Command):
    def __init__(self, x1, y1, x2, y2, color, outline=None, stipple=None):
        self.left, self.top, self.right, self.bottom = x1, y1, x2, y2
        self.color = color
        self.outline = outline
        self.stipple = stipple      # translucent fill: Tk draws every n-th pixel (see stipple_for)

    def execute(self, canvas, dx, dy):
        kw = {"stipple": self.stipple} if self.stipple else {}
        canvas.create_rectangle(self.left - dx, self.top - dy, self.right - dx, self.bottom - dy,
                                width=0 if not self.outline else 1, fill=self.color or "",
                                outline=self.outline or "", **kw)

    def clipped(self, clip):
        x1, y1, x2, y2 = clip
        l, t, r, b = max(self.left, x1), max(self.top, y1), min(self.right, x2), min(self.bottom, y2)
        if l >= r or t >= b:
            return None
        return DrawRect(l, t, r, b, self.color, self.outline, self.stipple)

    def __repr__(self):
        return "DrawRect(%d,%d,%d,%d,%s)" % (self.left, self.top, self.right, self.bottom, self.color)


class DrawOval(DrawRect):
    def execute(self, canvas, dx, dy):
        canvas.create_oval(self.left - dx, self.top - dy, self.right - dx, self.bottom - dy,
                           fill=self.color or "", outline=self.outline or "", width=1)

    def clipped(self, clip):
        return self if self.intersects(*clip) else None


class DrawText(Command):
    def __init__(self, x, y, text, font, color, width=None):
        self.left = x
        self.top = y
        self.text = text
        self.font = font
        self.color = color or "#000000"
        self.bottom = y + font.linespace
        self.right = x + (width if width is not None else font.measure(text))

    angle = 0          # rotated text (CSS transforms): degrees counter-clockwise, about `anchor`
    anchor = None

    def execute(self, canvas, dx, dy):
        if self.angle:
            ax, ay = self.anchor
            canvas.create_text(ax - dx, ay - dy, text=self.text, font=self.font.tk,
                               anchor="nw", fill=self.color, angle=self.angle)
            return
        canvas.create_text(self.left - dx, self.top - dy, text=self.text, font=self.font.tk,
                           anchor="nw", fill=self.color)

    def clipped(self, clip):
        x1, y1, x2, y2 = clip
        if self.angle:
            return self if self.intersects(*clip) else None
        mid = (self.top + self.bottom) / 2
        if mid < y1 or mid > y2 or self.left >= x2 or self.right <= x1:
            return None
        if self.left >= x1 - 0.5 and self.right <= x2 + 0.5:
            return self
        # Partly outside horizontally: keep only the characters inside.
        text, left = self.text, self.left
        while text and left < x1 - 0.5:
            left += self.font.measure(text[0])
            text = text[1:]
        while text and left + self.font.measure(text) > x2 + 0.5:
            text = text[:-1]
        if not text:
            return None
        return DrawText(left, self.top, text, self.font, self.color)

    def __repr__(self):
        return "DrawText(%d,%d,%r)" % (self.left, self.top, self.text[:30])


class DrawLine(Command):
    def __init__(self, x1, y1, x2, y2, color, thickness=1, dash=None):
        self.x1, self.y1, self.x2, self.y2 = x1, y1, x2, y2
        self.left, self.right = min(x1, x2), max(x1, x2)
        self.top, self.bottom = min(y1, y2), max(y1, y2)
        self.color = color or "#000000"
        self.thickness = thickness
        self.dash = dash

    def execute(self, canvas, dx, dy):
        kw = {"dash": self.dash} if self.dash else {}
        canvas.create_line(self.x1 - dx, self.y1 - dy, self.x2 - dx, self.y2 - dy,
                           fill=self.color, width=self.thickness, **kw)


class DrawRoundRect(Command):
    """A rectangle with rounded corners (border-radius), filled and/or outlined.
    Drawn as a polygon whose corners are quarter-circle arcs."""

    def __init__(self, x1, y1, x2, y2, radius, fill=None, outline=None, width=1, stipple=None):
        self.left, self.top, self.right, self.bottom = x1, y1, x2, y2
        self.radius = radius
        self.fill = fill
        self.outline = outline
        self.width = width
        self.stipple = stipple

    def points(self, dx=0, dy=0):
        import math
        x1, y1, x2, y2 = self.left - dx, self.top - dy, self.right - dx, self.bottom - dy
        r = max(0.0, min(self.radius, (x2 - x1) / 2, (y2 - y1) / 2))
        pts = []
        steps = max(3, min(16, int(r)))
        for cx, cy, a0 in ((x2 - r, y1 + r, -90), (x2 - r, y2 - r, 0), (x1 + r, y2 - r, 90), (x1 + r, y1 + r, 180)):
            for i in range(steps + 1):
                a = math.radians(a0 + 90 * i / steps)
                pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
        return pts

    def execute(self, canvas, dx, dy):
        pts = self.points(dx, dy)
        if self.fill:
            kw = {"stipple": self.stipple} if self.stipple else {}
            canvas.create_polygon(*pts, fill=self.fill, outline="", **kw)
        if self.outline and self.width > 0:
            canvas.create_line(*(pts + pts[:2]), fill=self.outline, width=self.width)

    def clipped(self, clip):
        x1, y1, x2, y2 = clip
        if not self.intersects(*clip):
            return None
        if self.left >= x1 and self.top >= y1 and self.right <= x2 and self.bottom <= y2:
            return self
        if self.fill:   # partly clipped: fall back to a plain rectangle
            return DrawRect(self.left, self.top, self.right, self.bottom, self.fill, stipple=self.stipple).clipped(clip)
        return None

    def clipped(self, clip):
        return self if self.intersects(*clip) else None


class DrawImage(Command):
    """Draw (part of) a decoded image. `crop` is a (x, y, w, h) source region
    already scaled, used for clipped background images."""

    def __init__(self, x, y, w, h, image, url, crop=None):
        self.left, self.top = x, y
        self.right, self.bottom = x + w, y + h
        self.w, self.h = int(round(w)), int(round(h))
        self.image = image      # PIL image
        self.url = url
        self.crop = crop
        self.full_size = (self.w, self.h)

    def execute(self, canvas, dx, dy):
        photo = canvas.browser_images.get(self)
        if photo is not None:
            canvas.create_image(self.left - dx, self.top - dy, image=photo, anchor="nw")

    def cache_key(self):
        return (self.url, self.full_size, self.crop)

    def clipped(self, clip):
        x1, y1, x2, y2 = clip
        if not self.intersects(*clip):
            return None
        if self.left >= x1 and self.top >= y1 and self.right <= x2 and self.bottom <= y2:
            return self
        # Crop the visible part.
        l, t = max(self.left, x1), max(self.top, y1)
        r, b = min(self.right, x2), min(self.bottom, y2)
        if r - l < 1 or b - t < 1:
            return None
        base = self.crop or (0, 0, self.w, self.h)
        crop = (base[0] + int(l - self.left), base[1] + int(t - self.top), int(r - l), int(b - t))
        img = DrawImage(l, t, r - l, b - t, self.image, self.url, crop=crop)
        img.full_size = self.full_size
        return img


def _fade_color(c, op):
    if not c or not isinstance(c, str) or not c.startswith("#") or len(c) != 7:
        return c
    r, g, b = int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)
    return "#%02x%02x%02x" % tuple(int(v * op + 255 * (1 - op)) for v in (r, g, b))


def faded(cmd, op):
    """A copy of a drawing command as if drawn with opacity `op` on white."""
    import copy
    c = copy.copy(cmd)
    for attr in ("color", "outline", "fill"):
        if hasattr(c, attr):
            setattr(c, attr, _fade_color(getattr(c, attr), op))
    if isinstance(c, DrawImage) and not hasattr(c.image, "render"):
        key = (c.url, op)
        img = _FADE_CACHE.get(key)
        if img is None:
            img = c.image.convert("RGBA")
            img.putalpha(img.getchannel("A").point(lambda a: int(a * op)))
            _FADE_CACHE[key] = img
        c.image = img
        c.url = "%s#opacity=%.2f" % (c.url, op)
    return c


_FADE_CACHE = {}


def stipple_for(alpha):
    """Tk has no alpha channel; a stipple pattern lets the backdrop show
    through a translucent fill in roughly the right proportion."""
    if alpha >= 0.9:
        return None
    if alpha < 0.19:
        return "gray12"
    if alpha < 0.37:
        return "gray25"
    if alpha < 0.62:
        return "gray50"
    return "gray75"


class LayerAnchor(Command):
    """Marks where a background-attachment: fixed layer sits in the paint
    order; the window keeps that layer's items just above this point."""

    def __init__(self, layer, clip):
        self.layer = layer
        self.left, self.top, self.right, self.bottom = clip

    def execute(self, canvas, dx, dy):
        canvas.create_line(self.left - dx, self.top - dy, self.left - dx, self.top - dy,
                           fill="", tags=("anchor%d" % id(self.layer),))

    def clipped(self, clip):
        return self


class DrawPolyline(Command):
    """A line through several points (wavy underlines)."""

    def __init__(self, pts, color, thickness=1):
        self.pts = pts
        xs, ys = pts[0::2], pts[1::2]
        self.left, self.right, self.top, self.bottom = min(xs), max(xs), min(ys), max(ys)
        self.color = color
        self.thickness = thickness

    def execute(self, canvas, dx, dy):
        canvas.create_line(*[v - (dx if i % 2 == 0 else dy) for i, v in enumerate(self.pts)],
                           fill=self.color, width=self.thickness)

    def clipped(self, clip):
        return self if self.intersects(*clip) else None


class DrawPolygon(Command):
    """A filled and/or outlined polygon (transformed rectangles)."""

    def __init__(self, pts, fill=None, outline=None, width=1, stipple=None):
        self.pts = pts
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        self.left, self.right, self.top, self.bottom = min(xs), max(xs), min(ys), max(ys)
        self.fill, self.outline, self.width, self.stipple = fill, outline, width, stipple
        self.color = fill

    def execute(self, canvas, dx, dy):
        flat = []
        for x, y in self.pts:
            flat += [x - dx, y - dy]
        if self.fill:
            kw = {"stipple": self.stipple} if self.stipple else {}
            canvas.create_polygon(*flat, fill=self.fill, outline="", **kw)
        if self.outline and self.width > 0:
            canvas.create_line(*(flat + flat[:2]), fill=self.outline, width=max(1, int(round(self.width))))

    def clipped(self, clip):
        return self if self.intersects(*clip) else None
