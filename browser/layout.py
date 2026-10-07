"""Layout engine: styled DOM -> box tree -> positions and sizes -> display list.

Box types
  BlockBox     block container (block children, or an inline formatting context)
  TableBox     auto table layout (column min/max widths, colspan)
  FlexBox      single- and multi-line flexbox (row and column)
  GridBox      simple grid (explicit column tracks, auto placement)
  ImageBox     replaced <img>
  ControlBox   form controls (<input>, <select>, <textarea>)

Inline formatting contexts are laid out into LineBoxes holding TextFrags
and AtomFrags (inline-blocks, images, controls). Floats are placed into a
FloatContext shared by one block formatting context, and line boxes are
shortened around them. Absolutely positioned boxes are laid out by their
nearest positioned ancestor once its size is known.
"""
import math
import re

from . import css_parser, paint
from .dom import Element, Text
from .fonts import get_font, small_caps
from .style import INHERITED, INITIAL, length, parse_color, to_rgba

SIDES = ("top", "right", "bottom", "left")
T, R, B, L = 0, 1, 2, 3

BLOCK_LEVEL = {
    "block", "list-item", "flow-root", "table", "flex", "grid", "table-caption", "table-row-group",
    "table-header-group", "table-footer-group", "table-row", "table-cell", "-webkit-box", "run-in",
    "table-column-group", "table-column", "-ms-flexbox",
}
ATOMIC_INLINE = {"inline-block", "inline-flex", "inline-grid", "inline-table", "-webkit-inline-box", "-ms-inline-flexbox"}
CONTROLS = {"input", "select", "textarea"}

_color_cache = {}


def color_of(value, current="#000000"):
    key = (value, current)
    c = _color_cache.get(key)
    if c is None and key not in _color_cache:
        c = parse_color(value, current)
        _color_cache[key] = c
    return c


class LayoutContext:
    def __init__(self, base_url, images, viewport_width, viewport_height):
        self.base_url = base_url
        self.images = images            # absolute url -> PIL image (or None if failed)
        self.viewport_width = viewport_width
        self.viewport_height = viewport_height
        self.positions = {}             # element -> y of its first box (for #fragment links)
        self.box_scroll = {}            # element -> (x, y) scroll offset of overflow: auto boxes
        self.resizers = []              # [(box, resize mode, grip rect)] found while painting
        self.scrollers = []             # [(box, clip, max x, max y)] found while painting
        self.focused = None             # focused form control element

    def resolve(self, src):
        try:
            return str(self.base_url.resolve(src))
        except ValueError:
            return None


class FloatContext:
    def __init__(self):
        self.left = []   # (y1, y2, x_edge)
        self.right = []

    def edges(self, y, h, x1, x2):
        left, right = x1, x2
        y2 = y + max(h, 1)
        for a, b, e in self.left:
            if a < y2 and b > y:
                left = max(left, e)
        for a, b, e in self.right:
            if a < y2 and b > y:
                right = min(right, e)
        return left, right

    def next_bottom(self, y):
        bottoms = [b for a, b, e in self.left + self.right if b > y]
        return min(bottoms) if bottoms else None

    def clear_y(self, side, y):
        items = []
        if side in ("left", "both"):
            items += self.left
        if side in ("right", "both"):
            items += self.right
        for a, b, e in items:
            y = max(y, b)
        return y

    def bottom(self):
        return max([b for a, b, e in self.left + self.right] or [0])

    def empty(self):
        return not self.left and not self.right


def anon_style(parent_style):
    s = dict(INITIAL)
    for k in INHERITED:
        if k in parent_style:
            s[k] = parent_style[k]
    s["-vars"] = parent_style.get("-vars", {})
    s["-font-px"] = parent_style.get("-font-px", 16.0)
    s["font-size"] = parent_style.get("font-size", "16px")
    s["display"] = "block"
    return s


def line_height_px(style, font):
    lh = style.get("line-height", "normal").strip().lower()
    fs = style.get("-font-px", 16.0)
    if lh == "normal":
        return font.linespace
    try:
        return float(lh) * fs
    except ValueError:
        pass
    v = length(lh, fs, fs)
    return v if v is not None and v > 0 else font.linespace


# ---------------------------------------------------------------------------
# Box tree construction


def build_box(node, ctx):
    """Build the layout box for an element (any display type)."""
    style = node.style
    display = style.get("display", "inline")
    if isinstance(node, Element):
        if node.tag == "img" or (node.tag == "input" and node.attributes.get("type", "").lower() == "image"):
            return ImageBox(node, style, ctx)
        if node.tag == "svg":
            return SvgBox(node, style, ctx)
        if node.tag in CONTROLS:
            return ControlBox(node, style, ctx)
    if display in ("table", "inline-table"):
        return TableBox(node, style, ctx)
    if display in ("-webkit-box", "-webkit-inline-box") and _line_clamp(style):
        return BlockBox(node, style, ctx)      # -webkit-line-clamp: a block whose lines are cut
    if display in ("flex", "inline-flex", "-webkit-box", "-webkit-inline-box", "-ms-flexbox", "-ms-inline-flexbox"):
        return FlexBox(node, style, ctx)
    if display in ("grid", "inline-grid"):
        return GridBox(node, style, ctx)
    return BlockBox(node, style, ctx)


def is_out_of_flow(style):
    return style.get("position") in ("absolute", "fixed")


def is_float(style):
    return style.get("float", "none") in ("left", "right") and not is_out_of_flow(style)


def gather(node, seq, ctx):
    """Flatten a node into a sequence of layout items.

    Items: ("text", TextNode) | ("open", el) | ("close", el) | ("br", el)
           | ("atom", box) | ("float", box) | ("block", box) | ("abs", box)
    """
    if isinstance(node, Text):
        seq.append(("text", node))
        return
    if not isinstance(node, Element):
        return
    style = node.style
    display = style.get("display", "inline")
    if display == "none":
        return
    if is_out_of_flow(style):
        seq.append(("abs", build_box(node, ctx)))
        return
    if is_float(style):
        seq.append(("float", build_box(node, ctx)))
        return
    if display == "contents":
        for child in node.children:
            gather(child, seq, ctx)
        return
    if node.tag == "br":
        seq.append(("br", node))
        return
    if node.tag in ("img", "input", "select", "textarea", "svg") or display in ATOMIC_INLINE:
        if display in BLOCK_LEVEL:
            seq.append(("block", build_box(node, ctx)))
        else:
            seq.append(("atom", build_box(node, ctx)))
        return
    if display in BLOCK_LEVEL:
        seq.append(("block", build_box(node, ctx)))
        return
    # inline element
    seq.append(("open", node))
    for child in node.children:
        gather(child, seq, ctx)
    seq.append(("close", node))


def _is_blank_run(items):
    for kind, obj in items:
        if kind == "text":
            if obj.text.strip() or obj.style.get("white-space", "normal") in ("pre", "pre-wrap", "break-spaces") and "\n" in obj.text and False:
                return False
        elif kind in ("atom", "float", "br"):
            return False
        elif kind == "open":
            s = obj.style
            if any(length(s.get("padding-" + side), 16, 0, 0) for side in ("left", "right")) and \
                    color_of(s.get("background-color")):
                return False
    return True


# ---------------------------------------------------------------------------
# Base box


class Box:
    is_atomic = False

    def __init__(self, node, style, ctx):
        self.node = node
        self.style = style
        self.ctx = ctx
        self.children = []
        self.x = self.y = 0.0
        self.width = self.height = 0.0
        self.m = [0.0] * 4
        self.p = [0.0] * 4
        self.b = [0.0] * 4
        self.abs_items = []      # [(box, dx, dy, anchor)] waiting for this containing block (see queue_abs)
        self.abs_boxes = []      # laid-out absolutely positioned descendants we contain
        self._intrinsic = None
        self.is_float = is_float(style)
        self.position = style.get("position", "static")

    # --- geometry ------------------------------------------------------
    @property
    def fs(self):
        return self.style.get("-font-px", 16.0)

    def compute_edges(self, cb_w):
        s = self.style
        fs = self.fs
        self.m_auto = [False] * 4
        for i, side in enumerate(SIDES):
            mv = length(s.get("margin-" + side, "0"), fs, cb_w, auto=None)
            if mv is None:
                self.m_auto[i] = s.get("margin-" + side, "0").strip().lower() == "auto"
                mv = 0.0
            self.m[i] = mv
            pv = length(s.get("padding-" + side, "0"), fs, cb_w, auto=0) or 0
            self.p[i] = max(0.0, pv)
            bs = s.get("border-%s-style" % side, "none")
            if bs in ("none", "hidden"):
                self.b[i] = 0.0
            else:
                bw = length(s.get("border-%s-width" % side, "medium"), fs, None, auto=3)
                self.b[i] = max(0.0, bw or 0)

    def horiz_extra(self):
        return self.p[L] + self.p[R] + self.b[L] + self.b[R]

    def vert_extra(self):
        return self.p[T] + self.p[B] + self.b[T] + self.b[B]

    def border_box(self):
        return (self.x - self.p[L] - self.b[L], self.y - self.p[T] - self.b[T],
                self.x + self.width + self.p[R] + self.b[R], self.y + self.height + self.p[B] + self.b[B])

    def outer_width(self):
        return self.width + self.horiz_extra() + self.m[L] + self.m[R]

    def outer_height(self):
        return self.height + self.vert_extra() + self.m[T] + self.m[B]

    def margin_top_edge(self):
        return self.y - self.p[T] - self.b[T] - self.m[T]

    def margin_left_edge(self):
        return self.x - self.p[L] - self.b[L] - self.m[L]

    def specified(self, prop, base):
        v = self.style.get(prop, "auto")
        px = length(v, self.fs, base, auto=None)
        return px

    def content_width_from_spec(self, cb_w):
        w = self.specified("width", cb_w)
        if w is None:
            return None
        if self.style.get("box-sizing") == "border-box":
            w -= self.horiz_extra()
        return max(0.0, w)

    def clamp_width(self, w, cb_w):
        bb = self.style.get("box-sizing") == "border-box"
        mx = self.specified("max-width", cb_w)
        if mx is not None:
            if bb:
                mx -= self.horiz_extra()
            w = min(w, mx)
        mn = self.specified("min-width", cb_w)
        if mn is not None:
            if bb:
                mn -= self.horiz_extra()
            w = max(w, mn)
        return max(0.0, w)

    def resolve_height(self, content_h, cb_h=None):
        if getattr(self, "_forced_h", None) is not None:   # stretched/flexed by a flex or grid parent
            return self._forced_h
        h =self.specified("height", cb_h) if (cb_h is not None or "%" not in self.style.get("height", "")) else None
        bb = self.style.get("box-sizing") == "border-box"
        if h is None:
            ratio = _aspect_ratio(self.style)
            if ratio:   # aspect-ratio: height follows the width (but fits the content)
                w = self.width + (self.horiz_extra() if bb else 0)
                content_h = max(content_h, w / ratio - (self.vert_extra() if bb else 0))
        if h is not None:
            if bb:
                h -= self.vert_extra()
        else:
            h = content_h
        mx = self.specified("max-height", cb_h) if "%" not in self.style.get("max-height", "") else None
        if mx is not None:
            h = min(h, mx - (self.vert_extra() if bb else 0))
        mn = self.specified("min-height", cb_h) if "%" not in self.style.get("min-height", "") else None
        if mn is not None:
            h = max(h, mn - (self.vert_extra() if bb else 0))
        return max(0.0, h)

    def shrink_to_fit(self, avail_outer):
        mn, mx = self.intrinsic()
        return min(max(mn, avail_outer), mx)

    # --- movement ------------------------------------------------------
    def translate(self, dx, dy):
        if not dx and not dy:
            return
        self.x += dx
        self.y += dy
        self.translate_contents(dx, dy)

    def translate_contents(self, dx, dy):
        for c in self.children:
            # Out-of-flow children are moved by their containing block (abs_boxes).
            if not is_out_of_flow(c.style):
                c.translate(dx, dy)
        for c in self.abs_boxes:
            c.translate(dx, dy)

    # --- positioning helpers -------------------------------------------
    def establishes_positioning(self):
        return self.position in ("relative", "absolute", "fixed", "sticky") or \
            self.style.get("transform", "none") not in ("none", "")

    def layout_abs_children(self):
        """Lay out absolutely positioned boxes whose containing block is us."""
        if not self.abs_items:
            return
        x1, y1, x2, y2 = self.border_box()
        cbx, cby = x1 + self.b[L], y1 + self.b[T]
        cbw = (x2 - x1) - self.b[L] - self.b[R]
        cbh = (y2 - y1) - self.b[T] - self.b[B]
        items, self.abs_items = self.abs_items, []
        for box, sx, sy in _static_positions(items):
            layout_absolute(box, cbx, cby, cbw, cbh, sx, sy)
            self.abs_boxes.append(box)

    def apply_relative_offset(self):
        if self.position != "relative":
            return
        s = self.style
        cbw = self.ctx.viewport_width
        left = length(s.get("left", "auto"), self.fs, cbw, auto=None)
        right = length(s.get("right", "auto"), self.fs, cbw, auto=None)
        top = length(s.get("top", "auto"), self.fs, None, auto=None)
        bottom = length(s.get("bottom", "auto"), self.fs, None, auto=None)
        dx = left if left is not None else (-right if right is not None else 0)
        dy = top if top is not None else (-bottom if bottom is not None else 0)
        self.translate(dx, dy)

    # --- painting ------------------------------------------------------
    def visible(self):
        return self.style.get("visibility", "visible") == "visible"

    def hidden_entirely(self):
        s = self.style
        if s.get("opacity", "1").strip() in ("0", "0.0", "0%"):
            return True
        clip = s.get("clip", "auto").strip()
        if clip.startswith("rect(") and is_out_of_flow(s):
            nums = re.findall(r"-?\d+(?:\.\d+)?", clip)
            if len(nums) == 4 and (int(nums[0]) >= int(nums[2]) or int(nums[1]) <= int(nums[3])):
                return True
            if all(n == "0" for n in nums):
                return True
        cp = s.get("clip-path", "none").replace(" ", "")
        if cp in ("inset(50%)", "inset(100%)", "circle(0)", "circle(0px)", "polygon(0000)"):
            return True
        return False

    def paint_background(self, dl):
        s = self.style
        x1, y1, x2, y2 = self.border_box()
        if getattr(self, "suppress_bg", False):
            pass
        else:
            bg = color_of(s.get("background-color"), color_of(s.get("color")))
            img = s.get("background-image", "none")
            gradient = "gradient(" in img and not img.startswith("url(")
            mask = s.get("mask-image", "none")
            if not mask.startswith("url("):
                mask = s.get("-webkit-mask-image", "none")
            radius = border_radius(self, x2 - x1, y2 - y1)
            paint_box_shadow(self, dl, x1, y1, x2, y2, radius)
            clips = [c.strip() for c in s.get("background-clip", "border-box").split(",")]
            if clips[-1] == "text":
                # background-clip: text -> the text takes the background's colour
                self.style["-text-fill"] = bg or (_gradient_color(img) if gradient else None)
                bg, img = None, "none"
            elif clips[-1] in ("padding-box", "content-box") and bg:
                cx1, cy1, cx2, cy2 = _box_area(self, clips[-1])
                dl.add(paint.DrawRect(cx1, cy1, cx2, cy2, bg))
                bg = None
            if mask.startswith("url("):
                paint_masked_background(self, dl, mask, bg, x1, y1, x2, y2)
            else:
                if bg and x2 > x1 and y2 > y1:
                    stip = None
                    raw = s.get("background-color", "")
                    if raw and ("rgba" in raw or "/" in raw or "hsla" in raw or (raw.startswith("#") and len(raw) in (5, 9))
                                or "color-mix" in raw or "transparent" in raw):
                        rgba = to_rgba(raw, color_of(s.get("color")) or "#000000")
                        if rgba and 0.02 < rgba[3] < 0.9:
                            # translucent: the real colour, stippled over what is behind
                            bg = "#%02x%02x%02x" % tuple(max(0, min(255, int(c))) for c in rgba[:3])
                            stip = paint.stipple_for(rgba[3])
                    if radius:
                        dl.add(paint.DrawRoundRect(x1, y1, x2, y2, radius, fill=bg, stipple=stip))
                    else:
                        dl.add(paint.DrawRect(x1, y1, x2, y2, bg, stipple=stip))
                if img != "none":
                    paint_background_layers(self, dl, img, x1, y1, x2, y2, radius)
        if not paint_border_image(self, dl, x1, y1, x2, y2):
            paint_borders(self, dl, x1, y1, x2, y2)

    def paint(self, dl):
        pass


def _gradient_color(value):
    colors = re.findall(r"#[0-9a-fA-F]{3,8}|rgba?\([^)]*\)|hsla?\([^)]*\)|\b[a-z]+\b", value)
    parsed = [parse_color(c) for c in colors]
    parsed = [c for c in parsed if c]
    if not parsed:
        return None
    if len(parsed) == 1:
        return parsed[0]
    a, b = parsed[0], parsed[-1]
    mix = tuple((int(a[i:i + 2], 16) + int(b[i:i + 2], 16)) // 2 for i in (1, 3, 5))
    return "#%02x%02x%02x" % mix


def border_radius(box, w, h):
    """One corner radius for the box (the largest specified), 0 if square."""
    s = box.style
    vals = []
    for name in ("border-radius", "border-top-left-radius", "border-top-right-radius",
                 "border-bottom-left-radius", "border-bottom-right-radius"):
        v = s.get(name)
        if v and v not in ("0", "0px", "initial", "none"):
            first = v.split("/")[0].split()
            if first:
                r = length(first[0], box.fs, min(w, h), 0) or 0
                vals.append(r)
    return max(vals) if vals else 0


def paint_box_shadow(box, dl, x1, y1, x2, y2, radius):
    """Outer box-shadow (first shadow), blur approximated by fading rings."""
    v = box.style.get("box-shadow", "none")
    if not v or v.strip().lower() in ("none", "initial", "unset", "inherit"):
        return
    from .css_parser import _split_top_level
    for shadow in _split_top_level(v, ","):
        toks = css_split(shadow.strip())
        if "inset" in toks:
            continue
        nums, color = [], None
        for t in toks:
            px = length(t, box.fs, None, None)
            if px is not None and (t[0].isdigit() or t[0] in "-.+" or t.startswith("calc")):
                nums.append(px)
            else:
                color = t
        if len(nums) < 2:
            continue
        ox, oy = nums[0], nums[1]
        blur = max(0.0, nums[2]) if len(nums) > 2 else 0.0
        spread = nums[3] if len(nums) > 3 else 0.0
        c = color_of(color or "currentcolor", color_of(box.style.get("color")) or "#000000")
        if not c:
            continue
        r0, g0, b0 = int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)
        steps = max(1, min(6, int(blur / 2)))
        for i in range(steps, 0, -1):
            grow = spread + blur * i / (steps + 1)
            f = (i / (steps + 1)) if blur else 0.0     # outer rings fade toward white
            col = "#%02x%02x%02x" % (int(r0 + (255 - r0) * f), int(g0 + (255 - g0) * f), int(b0 + (255 - b0) * f))
            rect = (x1 + ox - grow, y1 + oy - grow, x2 + ox + grow, y2 + oy + grow)
            if radius and color_of(box.style.get("background-color")):
                # an opaque rounded box covers the middle: draw the shadow rounded
                dl.add(paint.DrawRoundRect(*rect, radius + grow, fill=col))
            else:
                # a shadow only shows outside the box itself (which may be transparent)
                for band in _rect_minus(rect, (x1, y1, x2, y2)):
                    dl.add(paint.DrawRect(*band, col))
        return


def _rect_minus(a, b):
    """Rectangle a with rectangle b cut out, as up to four rectangles."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = max(b[0], ax1), max(b[1], ay1), min(b[2], ax2), min(b[3], ay2)
    if bx1 >= bx2 or by1 >= by2:
        return [a]
    out = []
    if ay1 < by1:
        out.append((ax1, ay1, ax2, by1))
    if by2 < ay2:
        out.append((ax1, by2, ax2, ay2))
    if ax1 < bx1:
        out.append((ax1, by1, bx1, by2))
    if bx2 < ax2:
        out.append((bx2, by1, ax2, by2))
    return out


def paint_outline(box, dl):
    """`outline` (focus rings): drawn outside the border box, on top."""
    s = box.style
    st = s.get("outline-style", "none")
    if st in (None, "none", "hidden"):
        return
    w = length(s.get("outline-width", "medium"), box.fs, None, 3) or 0
    if w <= 0:
        return
    current = color_of(s.get("color")) or "#000000"
    oc = s.get("outline-color", "currentcolor")
    c = color_of(oc if oc not in ("invert", "auto") else "currentcolor", current) or current
    off = length(s.get("outline-offset", "0"), box.fs, None, 0) or 0
    x1, y1, x2, y2 = box.border_box()
    x1, y1, x2, y2 = x1 - off - w, y1 - off - w, x2 + off + w, y2 + off + w
    radius = border_radius(box, x2 - x1, y2 - y1)
    if radius:
        dl.add(paint.DrawRoundRect(x1 + w / 2, y1 + w / 2, x2 - w / 2, y2 - w / 2, radius + off, outline=c, width=w))
        return
    dash = (2, 2) if st == "dotted" else ((6, 3) if st == "dashed" else None)
    if dash:
        for ax, ay, bx, by in ((x1, y1 + w / 2, x2, y1 + w / 2), (x1, y2 - w / 2, x2, y2 - w / 2),
                               (x1 + w / 2, y1, x1 + w / 2, y2), (x2 - w / 2, y1, x2 - w / 2, y2)):
            dl.add(paint.DrawLine(ax, ay, bx, by, c, max(1, int(round(w))), dash))
        return
    dl.add(paint.DrawRect(x1, y1, x2, y1 + w, c))
    dl.add(paint.DrawRect(x1, y2 - w, x2, y2, c))
    dl.add(paint.DrawRect(x1, y1, x1 + w, y2, c))
    dl.add(paint.DrawRect(x2 - w, y1, x2, y2, c))


def paint_borders(box, dl, x1, y1, x2, y2):
    s = box.style
    current = color_of(s.get("color")) or "#000000"
    bt, br, bb, bl = box.b
    if not (bt or br or bb or bl):
        return
    radius = border_radius(box, x2 - x1, y2 - y1)
    if radius and bt == br == bb == bl:
        c = color_of(s.get("border-top-color", "currentcolor"), current)
        if c:
            dl.add(paint.DrawRoundRect(x1 + bt / 2, y1 + bt / 2, x2 - bt / 2, y2 - bt / 2, max(0, radius - bt / 2),
                                       outline=c, width=bt))
        return
    dashed = {side: s.get("border-%s-style" % side) for side in ("top", "right", "bottom", "left")}
    def col(side):
        c = color_of(s.get("border-%s-color" % side, "currentcolor"), current)
        st = s.get("border-%s-style" % side)
        if c and st in ("inset", "groove") and side in ("top", "left"):
            c = _shade(c, 0.6)
        elif c and st in ("outset", "ridge") and side in ("bottom", "right"):
            c = _shade(c, 0.6)
        return c
    sides = (("top", bt, (x1, y1, x2, y1 + bt)), ("bottom", bb, (x1, y2 - bb, x2, y2)),
             ("left", bl, (x1, y1, x1 + bl, y2)), ("right", br, (x2 - br, y1, x2, y2)))
    for side, w, (rx1, ry1, rx2, ry2) in sides:
        if not w:
            continue
        c = col(side)
        if not c:
            continue
        st = dashed[side]
        if st in ("dotted", "dashed"):
            # a dashed line along the middle of the border strip
            dash = (max(1, int(w)), max(1, int(w))) if st == "dotted" else (max(3, int(w * 3)), max(2, int(w * 2)))
            if side in ("top", "bottom"):
                ym = (ry1 + ry2) / 2
                dl.add(paint.DrawLine(rx1, ym, rx2, ym, c, max(1, int(round(w))), dash))
            else:
                xm = (rx1 + rx2) / 2
                dl.add(paint.DrawLine(xm, ry1, xm, ry2, c, max(1, int(round(w))), dash))
        elif st == "double" and w >= 3:
            t = w / 3
            if side in ("top", "bottom"):
                dl.add(paint.DrawRect(rx1, ry1, rx2, ry1 + t, c))
                dl.add(paint.DrawRect(rx1, ry2 - t, rx2, ry2, c))
            else:
                dl.add(paint.DrawRect(rx1, ry1, rx1 + t, ry2, c))
                dl.add(paint.DrawRect(rx2 - t, ry1, rx2, ry2, c))
        else:
            dl.add(paint.DrawRect(rx1, ry1, rx2, ry2, c))


def _shade(color, f):
    r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
    return "#%02x%02x%02x" % (int(r * f), int(g * f), int(b * f))


def _box_area(box, which):
    """Border, padding or content box rectangle."""
    x1, y1, x2, y2 = box.border_box()
    if which in ("padding-box", "content-box"):
        x1, y1, x2, y2 = x1 + box.b[L], y1 + box.b[T], x2 - box.b[R], y2 - box.b[B]
    if which == "content-box":
        x1, y1, x2, y2 = x1 + box.p[L], y1 + box.p[T], x2 - box.p[R], y2 - box.p[B]
    return x1, y1, x2, y2


def _nth(value, i, default):
    from .css_parser import _split_top_level
    items = [v.strip() for v in _split_top_level(value or default, ",")] or [default]
    return items[i % len(items)]


def paint_background_layers(box, dl, value, x1, y1, x2, y2, radius=0):
    """Every background-image layer, bottom (last) first, each with its own
    size, position, repeat, origin, clip and attachment."""
    from .css_parser import _split_top_level
    s = box.style
    layers = [v.strip() for v in _split_top_level(value, ",")]
    for i in range(len(layers) - 1, -1, -1):
        img = layers[i]
        if img in ("none", ""):
            continue
        clip_box = _nth(s.get("background-clip"), i, "border-box")
        if clip_box == "text":
            continue
        origin = _nth(s.get("background-origin"), i, "padding-box")
        attach = _nth(s.get("background-attachment"), i, "scroll")
        clip = _box_area(box, clip_box)
        area = _box_area(box, origin)
        size = _nth(s.get("background-size"), i, "auto")
        pos = _nth(s.get("background-position"), i, "0% 0%")
        rep = _nth(s.get("background-repeat"), i, "repeat")
        target = dl
        layer = None
        if attach == "fixed" and dl.layers is not None:
            # stays put while the page scrolls: positioned against the viewport
            layer = paint.Layer("fixedbg")
            layer.clip = clip
            target = layer.list
            area = (0, 0, box.ctx.viewport_width, box.ctx.viewport_height)
            layer.clip, clip = clip, area     # tile the whole window; clipped to the box while scrolling
        if img.startswith("url("):
            paint_background_image(box, target, img, area, clip, size, pos, rep)
        elif "gradient(" in img:
            ax1, ay1, ax2, ay2 = area
            gw, gh = ax2 - ax1, ay2 - ay1
            if size not in ("auto", "auto auto"):
                gw, gh = _layer_size(box, size, gw, gh, gw, gh)
            if rep.startswith(("no-repeat",)) or (gw >= ax2 - ax1 - 0.5 and gh >= ay2 - ay1 - 0.5):
                ox, oy = _bg_position(pos, (ax2 - ax1) - gw, (ay2 - ay1) - gh, box.fs)
                tmp = paint.DisplayList()
                paint_gradient(box, tmp, img, ax1 + ox, ay1 + oy, ax1 + ox + gw, ay1 + oy + gh,
                               radius if clip_box == "border-box" and layer is None else 0)
            else:   # tiled gradient (e.g. stripes)
                tmp = paint.DisplayList()
                paint_gradient(box, tmp, img, 0, 0, gw, gh)
                if tmp.commands:
                    cmd = tmp.commands[0]
                    _tile(target, cmd.image, cmd.url, area, clip, gw, gh, pos, rep, box.fs)
                    tmp.commands = []
            for c in tmp.commands:
                c2 = c.clipped(clip)
                if c2 is not None:
                    target.add(c2)
        if layer is not None and layer.list.commands:
            dl.layers.append(layer)
            dl.add(paint.LayerAnchor(layer, clip))


def _bg_position(pos, free_x, free_y, fs):
    """background-position with 1-4 values (keywords, lengths, percentages,
    and edge offsets like 'right 10px bottom 5px')."""
    toks = pos.lower().split()
    if not toks:
        return 0, 0
    if len(toks) == 1:
        toks = toks + (["center"] if toks[0] not in ("top", "bottom") else [])
        if toks[0] in ("top", "bottom"):
            toks = ["center", toks[0]]
    horiz, vert = None, None
    if len(toks) >= 3:
        i = 0
        while i < len(toks):
            kw = toks[i]
            off = toks[i + 1] if i + 1 < len(toks) and toks[i + 1] not in ("left", "right", "top", "bottom", "center") else None
            if kw in ("left", "right"):
                o = (length(off, fs, free_x, 0) or 0) if off else 0
                horiz = o if kw == "left" else free_x - o
            elif kw in ("top", "bottom"):
                o = (length(off, fs, free_y, 0) or 0) if off else 0
                vert = o if kw == "top" else free_y - o
            elif kw == "center":
                if horiz is None:
                    horiz = free_x / 2
                else:
                    vert = free_y / 2
            i += 2 if off else 1
        return (horiz if horiz is not None else free_x / 2), (vert if vert is not None else free_y / 2)
    a, b = toks[0], toks[1]
    if a in ("top", "bottom") or b in ("left", "right"):
        a, b = b, a
    return _bg_offset(a, free_x, fs, True), _bg_offset(b, free_y, fs, False)


def _tile(dl, img, url, area, clip, iw, ih, pos, rep, fs):
    ax1, ay1, ax2, ay2 = area
    bw, bh = ax2 - ax1, ay2 - ay1
    reps = rep.split()
    rx = reps[0] if reps else "repeat"
    ry = reps[1] if len(reps) > 1 else rx
    if rx == "repeat-x":
        rx, ry = "repeat", "no-repeat"
    elif rx == "repeat-y":
        rx, ry = "no-repeat", "repeat"
    if rx == "round" and iw > 0:
        iw = bw / max(1, round(bw / iw))
    if ry == "round" and ih > 0:
        ih = bh / max(1, round(bh / ih))
    iw, ih = max(1, iw), max(1, ih)
    ox, oy = _bg_position(pos, bw - iw, bh - ih, fs)
    xs, ys = [ax1 + ox], [ay1 + oy]
    if rx in ("repeat", "round", "space"):
        start = ax1 + ox - iw * math.ceil(max(0, ox - (clip[0] - ax1)) / iw)
        xs = [start + k * iw for k in range(int((clip[2] - start) // iw) + 2)][:80]
    if ry in ("repeat", "round", "space"):
        start = ay1 + oy - ih * math.ceil(max(0, oy - (clip[1] - ay1)) / ih)
        ys = [start + k * ih for k in range(int((clip[3] - start) // ih) + 2)][:80]
    count = 0
    for yy in ys:
        for xx in xs:
            cmd = paint.DrawImage(xx, yy, iw, ih, img, url).clipped(clip)
            if cmd:
                dl.add(cmd)
                count += 1
            if count > 600:
                return


def paint_background_image(box, dl, value, area=None, clip=None, size=None, pos=None, rep=None):
    m = css_parser.URL_RE.match(value)
    if not m:
        return
    url = box.ctx.resolve(css_parser.url_of(m))
    img = box.ctx.images.get(url)
    if img is None:
        return
    iw, ih = img.size
    if iw <= 0 or ih <= 0:
        return
    if area is None:
        area = box.border_box()
    clip = clip or area
    s = box.style
    size = size or s.get("background-size", "auto")
    pos = pos or s.get("background-position", "0% 0%")
    rep = rep or s.get("background-repeat", "repeat")
    bw, bh = area[2] - area[0], area[3] - area[1]
    iw, ih = _layer_size(box, size, iw, ih, bw, bh)
    _tile(dl, img, url, area, clip, iw, ih, pos, rep.lower(), box.fs)


_BORDER_IMAGE_CACHE = {}


def paint_border_image(box, dl, x1, y1, x2, y2):
    """border-image: the source image cut into nine slices drawn over the
    border area (corners as they are, edges stretched or repeated)."""
    s = box.style
    src = s.get("border-image-source", "none")
    if not src or src == "none":
        return False
    from PIL import Image
    if src.startswith("url("):
        m = css_parser.URL_RE.match(src)
        img = box.ctx.images.get(box.ctx.resolve(css_parser.url_of(m))) if m else None
        if img is None:
            return False
        if hasattr(img, "render"):
            img = img.render(*img.size)
        img = img.convert("RGBA")
    elif "gradient(" in src:
        img = _render_gradient(src.strip(), max(1, int(x2 - x1)), max(1, int(y2 - y1)),
                               color_of(s.get("color")) or "#000000", 0)
        if img is None:
            return False
    else:
        return False
    W, H = img.size
    sl = s.get("border-image-slice", "100%").split()
    fill = "fill" in sl
    sl = [v for v in sl if v != "fill"] or ["100%"]
    sl = (sl * 4)[:4] if len(sl) == 1 else (sl + sl[:2])[:4] if len(sl) == 2 else (sl + [sl[1]])[:4]
    cut = []
    for k, v in enumerate(sl):
        dim = H if k in (0, 2) else W
        cut.append(int(min(dim, float(v[:-1]) * dim / 100 if v.endswith("%") else float(v))))
    st, sr, sb, sll = cut
    widths = s.get("border-image-width")
    bw = list(box.b)
    if widths:
        wl = widths.split()
        wl = (wl * 4)[:4] if len(wl) == 1 else (wl + wl[:2])[:4] if len(wl) == 2 else (wl + [wl[1]])[:4]
        for k, v in enumerate(wl):
            if v == "auto":
                bw[k] = cut[k]
            elif re.match(r"^\d*\.?\d+$", v):
                bw[k] = float(v) * box.b[k]
            else:
                bw[k] = length(v, box.fs, (x2 - x1) if k in (1, 3) else (y2 - y1), box.b[k]) or 0
    outset = length((s.get("border-image-outset") or "0").split()[0], box.fs, None, 0) or 0
    x1, y1, x2, y2 = x1 - outset, y1 - outset, x2 + outset, y2 + outset
    t, r, b, l = bw
    rep = (s.get("border-image-repeat") or "stretch").split()
    rx, ry = rep[0], (rep[1] if len(rep) > 1 else rep[0])
    key0 = (id(img), W, H, tuple(cut))
    pieces = [
        ((0, 0, sll, st), (x1, y1, x1 + l, y1 + t), None),
        ((W - sr, 0, W, st), (x2 - r, y1, x2, y1 + t), None),
        ((0, H - sb, sll, H), (x1, y2 - b, x1 + l, y2), None),
        ((W - sr, H - sb, W, H), (x2 - r, y2 - b, x2, y2), None),
        ((sll, 0, W - sr, st), (x1 + l, y1, x2 - r, y1 + t), rx),
        ((sll, H - sb, W - sr, H), (x1 + l, y2 - b, x2 - r, y2), rx),
        ((0, st, sll, H - sb), (x1, y1 + t, x1 + l, y2 - b), ry),
        ((W - sr, st, W, H - sb), (x2 - r, y1 + t, x2, y2 - b), ry),
    ]
    if fill:
        pieces.append(((sll, st, W - sr, H - sb), (x1 + l, y1 + t, x2 - r, y2 - b), None))
    for n, (src_box, (dx1, dy1, dx2, dy2), mode) in enumerate(pieces):
        dw, dh = int(round(dx2 - dx1)), int(round(dy2 - dy1))
        if dw < 1 or dh < 1 or src_box[2] <= src_box[0] or src_box[3] <= src_box[1]:
            continue
        key = key0 + (n, dw, dh, mode)
        piece = _BORDER_IMAGE_CACHE.get(key)
        if piece is None:
            part = img.crop(src_box)
            if mode in ("repeat", "round", "space") and n >= 4:
                horizontal = n in (4, 5)
                pw, ph = part.size
                scale = (dh / ph) if horizontal else (dw / pw)
                tw, th = max(1, int(pw * scale)), max(1, int(ph * scale))
                if mode == "round":
                    count = max(1, round((dw if horizontal else dh) / (tw if horizontal else th)))
                    if horizontal:
                        tw = max(1, int(dw / count))
                    else:
                        th = max(1, int(dh / count))
                tile = part.resize((tw if horizontal else dw, dh if horizontal else th))
                piece = Image.new("RGBA", (dw, dh))
                if horizontal:
                    for xx in range(0, dw, tile.size[0]):
                        piece.paste(tile, (xx, 0))
                else:
                    for yy in range(0, dh, tile.size[1]):
                        piece.paste(tile, (0, yy))
            else:
                piece = part.resize((dw, dh))
            if len(_BORDER_IMAGE_CACHE) > 400:
                _BORDER_IMAGE_CACHE.clear()
            _BORDER_IMAGE_CACHE[key] = piece
        dl.add(paint.DrawImage(dx1, dy1, dw, dh, piece, "bimg:%x" % (hash(key) & 0xffffffffffff)))
    return True


def _layer_size(box, size, iw, ih, bw, bh):
    """Used size of a background or mask image for a background-size/mask-size value."""
    size = size.lower().strip()
    if size == "cover":
        scale = max(bw / iw, bh / ih)
        iw, ih = iw * scale, ih * scale
    elif size == "contain":
        scale = min(bw / iw, bh / ih)
        iw, ih = iw * scale, ih * scale
    elif size not in ("auto", "auto auto", ""):
        parts = css_split(size)
        w = length(parts[0], box.fs, bw, auto=None)
        h = length(parts[1], box.fs, bh, auto=None) if len(parts) > 1 else None
        if w and not h:
            h = ih * w / iw
        if h and not w:
            w = iw * h / ih
        if w and h:
            iw, ih = w, h
    return max(1, iw), max(1, ih)


_MASK_CACHE = {}


def paint_masked_background(box, dl, value, color, x1, y1, x2, y2):
    """CSS mask-image: the background colour shows only where the mask image
    is opaque (how Wikipedia and many icon sets draw single-colour icons)."""
    m = css_parser.URL_RE.match(value)
    img = box.ctx.images.get(box.ctx.resolve(css_parser.url_of(m))) if m else None
    w, h = int(round(x2 - x1)), int(round(y2 - y1))
    if img is None or not color or w < 1 or h < 1 or img.size[0] <= 0 or img.size[1] <= 0:
        return   # a mask that failed to load hides the background
    s = box.style

    def prop(name, default):
        v = s.get(name)
        if v is None:
            v = s.get("-webkit-" + name, default)
        return v

    size, pos, repeat = prop("mask-size", "auto"), prop("mask-position", "0% 0%"), prop("mask-repeat", "repeat")
    key = (id(img), w, h, color, size, pos, repeat, box.fs)
    out = _MASK_CACHE.get(key)
    if out is None:
        from PIL import Image, ImageColor
        iw, ih = _layer_size(box, size, img.size[0], img.size[1], w, h)
        iw, ih = max(1, int(round(iw))), max(1, int(round(ih)))
        if hasattr(img, "render"):      # SVG: rasterize at the mask size
            tile = img.render(iw, ih).convert("RGBA")
            if tile.size != (iw, ih):
                tile = tile.resize((iw, ih), Image.LANCZOS)
        else:
            tile = img.convert("RGBA").resize((iw, ih), Image.LANCZOS)
        tile = tile.getchannel("A")
        parts = pos.lower().split()
        ox = _bg_offset(parts[0], w - iw, box.fs, True) if parts else 0
        oy = _bg_offset(parts[1] if len(parts) > 1 else "center", h - ih, box.fs, False) if parts else 0
        rep = repeat.lower()
        xs, ys = [ox], [oy]
        if rep in ("repeat", "repeat-x", "round", "space"):
            start = ox % iw - iw if ox % iw else 0
            xs = [start + i * iw for i in range(int(w // iw) + 2)][:200]
        if rep in ("repeat", "repeat-y", "round", "space"):
            start = oy % ih - ih if oy % ih else 0
            ys = [start + i * ih for i in range(int(h // ih) + 2)][:200]
        alpha = Image.new("L", (w, h), 0)
        for yy in ys:
            for xx in xs:
                alpha.paste(tile, (int(round(xx)), int(round(yy))))
        try:
            rgb = ImageColor.getrgb(color)[:3]
        except ValueError:
            rgb = (0, 0, 0)
        out = Image.new("RGBA", (w, h), rgb + (255,))
        out.putalpha(alpha)
        if len(_MASK_CACHE) > 500:
            _MASK_CACHE.clear()
        _MASK_CACHE[key] = out
    dl.add(paint.DrawImage(x1, y1, w, h, out, "mask:%x" % (hash(key) & 0xffffffffffff)))


def _bg_offset(token, free, fs, horizontal):
    if token in ("left", "top"):
        return 0
    if token in ("right", "bottom"):
        return free
    if token == "center":
        return free / 2
    if token.endswith("%"):
        try:
            return free * float(token[:-1]) / 100
        except ValueError:
            return 0
    return length(token, fs, None, auto=0) or 0


def queue_abs(target, box, sx, sy, anchor):
    """Hand an absolutely positioned box to its containing block. The static
    position is kept relative to `anchor` (the box being laid out), because
    flex and grid items are laid out at the origin and moved afterwards."""
    target.abs_items.append((box, sx - anchor.x, sy - anchor.y, anchor))


def _static_positions(items):
    """(box, static x, static y) for queued boxes; a box queued twice (its
    parent was laid out again) keeps only its latest entry."""
    latest = {}
    for box, dx, dy, anchor in items:
        latest[id(box)] = (box, anchor.x + dx, anchor.y + dy)
    return list(latest.values())


def layout_absolute(box, cbx, cby, cbw, cbh, sx, sy):
    s = box.style
    fs = box.fs
    left = length(s.get("left", "auto"), fs, cbw, auto=None)
    right = length(s.get("right", "auto"), fs, cbw, auto=None)
    top = length(s.get("top", "auto"), fs, cbh, auto=None)
    bottom = length(s.get("bottom", "auto"), fs, cbh, auto=None)
    box.compute_edges(cbw)
    forced = None
    if box.content_width_from_spec(cbw) is None and left is not None and right is not None:
        forced = max(0, cbw - left - right)
    box.layout(0, 0, cbw, FloatContext(), forced_outer=forced, shrink=forced is None, positioned=box)
    if left is not None:
        x = cbx + left
    elif right is not None:
        x = cbx + cbw - right - box.outer_width()
    else:
        x = sx
    if top is not None:
        y = cby + top
    elif bottom is not None:
        y = cby + cbh - bottom - box.outer_height()
    else:
        y = sy
    box.translate(x - box.margin_left_edge(), y - box.margin_top_edge())


# ---------------------------------------------------------------------------
# Block boxes


class BlockBox(Box):
    def __init__(self, node, style, ctx, items=None):
        super().__init__(node, style, ctx)
        self.lines = []
        self.inline_items = None
        self.marker = None
        if items is None:
            items = []
            if node is not None:
                for child in node.children:
                    gather(child, items, ctx)
        self.setup_children(items)

    def setup_children(self, items):
        has_block = any(k in ("block",) for k, _ in items)
        if not has_block:
            self.inline_items = items
            return
        run = []
        for kind, obj in items:
            if kind == "block":
                self._flush_run(run)
                run = []
                self.children.append(obj)
            elif kind == "abs":
                self._flush_run(run)
                run = []
                self.children.append(obj)
            else:
                run.append((kind, obj))
        self._flush_run(run)

    def _flush_run(self, run):
        if not run:
            return
        if _is_blank_run(run):
            # still keep floats / abs
            return
        anon = BlockBox(None, anon_style(self.style), self.ctx, items=run)
        if not self.children:
            anon.anon_parent_node = self.node    # its first line is the parent's ::first-line
        self.children.append(anon)

    # --- intrinsic sizes ----------------------------------------------
    def intrinsic(self):
        if self._intrinsic is not None:
            return self._intrinsic
        if self.style.get("writing-mode", "horizontal-tb") in VERTICAL_MODES and not getattr(self, "_vlayout", False):
            # a vertical box is as wide as its lines are tall
            self._vlayout = True
            try:
                self._intrinsic = None
                mn, mx = self.intrinsic()
            finally:
                self._vlayout = False
            self._intrinsic = None
            self.compute_edges(0)
            font = get_font(self.style)
            lh = line_height_px(self.style, font)
            if self.style.get("text-orientation") == "upright" and self.style.get("writing-mode", "").startswith("vertical"):
                w = font.measure("W") + self.horiz_extra()
                self._intrinsic = (w, w)
                return self._intrinsic
            spec = length(self.style.get("height", "auto"), self.fs, None, None)
            avail = spec if spec else (self.ctx.viewport_height or 600) * 0.9
            lines = max(1, math.ceil((mx - self.horiz_extra()) / max(1.0, avail)))
            w = lines * lh + self.vert_extra() + self.m[L] + self.m[R]
            self._intrinsic = (w, w)
            return self._intrinsic
        self._intrinsic = (0, 0)  # guard against cycles
        self.compute_edges(0)
        extra = self.horiz_extra() + self.m[L] + self.m[R]
        spec = self.content_width_from_spec(None)
        if spec is not None:
            res = (spec + extra, spec + extra)
        else:
            if self.inline_items is not None:
                mn, mx = inline_intrinsic(self.inline_items, self.style)
            else:
                mn = mx = 0
                float_row = 0
                for c in self.children:
                    if is_out_of_flow(c.style):
                        continue
                    cmn, cmx = c.intrinsic()
                    mn = max(mn, cmn)
                    if c.is_float:
                        float_row += cmx
                        mx = max(mx, float_row)
                    else:
                        float_row = 0
                        mx = max(mx, cmx)
            mx_style = self.specified("max-width", None)
            if mx_style is not None:
                mx = min(mx, mx_style)
                mn = min(mn, mx_style)
            mn_style = self.specified("min-width", None)
            if mn_style is not None:
                mx = max(mx, mn_style)
                mn = max(mn, mn_style)
            res = (mn + extra, max(mn, mx) + extra)
        self._intrinsic = res
        return res

    # --- layout -------------------------------------------------------
    def establishes_bfc(self):
        s = self.style
        return (self.is_float or is_out_of_flow(s) or s.get("display") in ATOMIC_INLINE
                or s.get("display") in ("table-cell", "flow-root", "table-caption")
                or s.get("overflow", "visible") not in ("visible", "clip")
                or getattr(self, "flex_item", False) or self.node is not None and self.node.tag in ("html", "body", "button"))

    def layout(self, cb_x, y, cb_w, fc, forced_outer=None, shrink=False, positioned=None):
        """Lay out with the margin-box top-left at (cb_x, y) within a containing
        block of width cb_w."""
        self.vframe = None
        if self.style.get("writing-mode", "horizontal-tb") in VERTICAL_MODES and not getattr(self, "_vlayout", False):
            return self.layout_vertical(cb_x, y, cb_w, fc, forced_outer, shrink, positioned)
        self.abs_boxes = []
        self.compute_edges(cb_w)
        s = self.style
        horiz = self.horiz_extra()
        spec = self.content_width_from_spec(cb_w)
        ml, mr = self.m[L], self.m[R]
        if forced_outer is not None:
            w = forced_outer - ml - mr - horiz
        elif spec is not None:
            w = spec
        elif s.get("width", "auto").strip().lower() in INTRINSIC_WIDTHS:
            kw = s.get("width").strip().lower()
            mn, mx = self.intrinsic()
            outer = mx if "max" in kw else (mn if "min" in kw else self.shrink_to_fit(cb_w))
            w = outer - ml - mr - horiz
        elif shrink:
            w = self.shrink_to_fit(cb_w) - ml - mr - horiz
        else:
            w = cb_w - ml - mr - horiz
        w = self.clamp_width(w, cb_w)
        if forced_outer is None and not shrink:
            free = cb_w - w - horiz - ml - mr
            if self.m_auto[L] and self.m_auto[R]:
                ml = self.m[L] = max(0.0, (cb_w - w - horiz) / 2)
                self.m[R] = cb_w - w - horiz - ml
            elif self.m_auto[L]:
                self.m[L] = ml = max(0.0, cb_w - w - horiz - mr)
            elif free > 0 and s.get("text-align") in CENTERING and (spec is not None or self.style.get("max-width",
                                                                                                    "none") != "none"):
                # inside <center> or align="center": narrower blocks are centred too
                ml = self.m[L] = self.m[L] + free / 2
                self.m[R] = cb_w - w - horiz - ml
            elif self.m_auto[R] or free != 0:
                self.m[R] = cb_w - w - horiz - ml
        self.width = max(0.0, w)
        self.m[T] = effective_top_margin(self, cb_w, recompute=False)
        self.x = cb_x + self.m[L] + self.b[L] + self.p[L]
        self.y = y + self.m[T] + self.b[T] + self.p[T]
        if self.node is not None and self.node not in self.ctx.positions:
            self.ctx.positions[self.node] = y + self.m[T]

        if self.establishes_positioning():
            positioned = self
        own_fc = fc
        if self.establishes_bfc() or fc is None:
            own_fc = FloatContext()
        content_h = self.layout_contents(own_fc, positioned)
        if own_fc is not fc and not own_fc.empty():
            content_h = max(content_h, own_fc.bottom() - self.y)
        if self.inline_items is None and self.collapses_bottom_with_child():
            self.m[B] = collapse_margins(self.m[B], getattr(self, "last_margin", 0.0))
        self.height = self.resolve_height(content_h)
        if self.node is not None and self.node.tag == "button" and self.height > content_h + 0.5:
            # a <button> taller than its contents centres them vertically (as browsers do)
            self.translate_contents(0, (self.height - content_h) / 2)
        if s.get("container-type", "normal") in ("inline-size", "size") and self.node is not None:
            self.node.container_size = (self.width, self.height)   # for @container / cq units
        self.after_layout()
        if positioned is self:
            self.layout_abs_children()

    def layout_vertical(self, cb_x, y, cb_w, fc, forced_outer, shrink, positioned):
        """writing-mode: vertical-rl/-lr, sideways-rl/-lr: lay the contents out
        horizontally with the box's height as the line length, then turn the
        result a quarter turn when painting (see BlockBox.paint)."""
        s = self.style
        mode = s.get("writing-mode")
        upright = s.get("text-orientation", "mixed") == "upright" and mode.startswith("vertical")
        vp_h = self.ctx.viewport_height or 600
        logical = dict(s)
        for a, b in (("width", "height"), ("min-width", "min-height"), ("max-width", "max-height")):
            logical[a], logical[b] = s.get(b, "auto" if "max" not in a else "none"), s.get(a, "auto" if "max" not in b else "none")
        if logical.get("max-width") in ("auto", None):
            logical["max-width"] = "none"
        if logical.get("min-width") in (None,):
            logical["min-width"] = "auto"
        self.style = logical
        self._vlayout = True
        self.upright = upright
        saved_intrinsic, self._intrinsic = self._intrinsic, None   # logical sizes while laying out
        try:
            if upright:
                # upright text: characters stacked top to bottom, nothing turned
                font = get_font(s)
                logical["width"] = "%gpx" % max(font.measure("W"), 1)
                self.layout(cb_x, y, cb_w, fc, forced_outer=None, shrink=False, positioned=positioned)
                return
            spec = length(logical.get("width", "auto"), self.fs, None, None)
            self.layout(cb_x, y, spec if spec is not None else min(cb_w * 10, vp_h * 0.9), fc,
                        shrink=spec is None, positioned=positioned)
        finally:
            self.style = s
            self._vlayout = False
            self._intrinsic = saved_intrinsic
        if upright:
            return
        x1, y1, x2, y2 = self.border_box()
        self.vframe = (mode, x1, y1, x2 - x1, y2 - y1, self.width, self.height)
        # physical size: the logical block size becomes the width
        lw, lh = self.width, self.height
        self.width = lh + self.vert_extra() - self.horiz_extra()
        self.height = lw + self.horiz_extra() - self.vert_extra()
        self.width, self.height = max(0.0, self.width), max(0.0, self.height)

    def after_layout(self):
        if self.style.get("display") == "list-item":
            self.make_marker()

    def layout_contents(self, fc, positioned):
        if self.style.get("content-visibility") == "hidden":
            # contents are not laid out or painted; size containment
            self.lines = []
            cis = self.style.get("contain-intrinsic-size", "none").split()
            if cis and cis[0] != "none":
                return length(cis[-1].replace("auto", "").strip() or "0", self.fs, None, 0) or 0
            return 0.0
        cols = self.column_spec() if type(self) is BlockBox else None
        if cols is not None:
            return self.layout_columns(positioned, *cols)
        return self.layout_flow(fc, positioned)

    # --- multi-column layout ----------------------------------------------
    def column_spec(self):
        """(count, column width, gap) when column-count/column-width split this box."""
        s = self.style
        count = _int(s.get("column-count", "auto"), None)
        cw = length(s.get("column-width", "auto"), self.fs, None, None)
        if count is None and not cw:
            return None
        W = self.width
        gap = length(s.get("column-gap", "normal"), self.fs, W, None)
        if gap is None:
            gap = self.fs     # "normal" is 1em in multi-column layout
        if cw:
            n = max(1, int((W + gap) // (cw + gap)))
            if count:
                n = min(n, count)
        else:
            n = max(1, count)
        if n <= 1:
            return None
        return n, (W - gap * (n - 1)) / n, gap

    def layout_columns(self, positioned, n, colw, gap):
        """Lay the content out in one column of width colw, then cut it into n
        balanced columns side by side (cuts fall between block boxes, or
        between lines of inline content). Elements with column-span: all cut
        the columns into rows of columns and run across the full width;
        break-before/after: column force a new column."""
        W = self.width
        self.width = colw
        try:
            fc = FloatContext()
            h = self.layout_flow(fc, positioned)
            if not fc.empty():
                h = max(h, fc.bottom() - self.y)
        finally:
            self.width = W
        units, containers = [], []
        self._column_units(self, units, containers)
        self.col_rules = []
        if len(units) < 2 and not any(u[0] == "span" for u in units):
            return h
        # split into segments: runs of column content and spanners
        segments, cur = [], []
        for u in units:
            if u[0] == "span":
                if cur:
                    segments.append(("cols", cur))
                    cur = []
                segments.append(("span", u[1]))
            else:
                cur.append(u)
        if cur:
            segments.append(("cols", cur))
        cursor = units[0][1] if units[0][0] != "span" else units[0][1].margin_top_edge()
        start_cursor = cursor
        color = None
        rule = self.style.get("column-rule-style", "none")
        if rule not in ("none", "hidden"):
            rw = length(self.style.get("column-rule-width", "medium"), self.fs, None, 3) or 3
            color = color_of(self.style.get("column-rule-color", "currentcolor"), color_of(self.style.get("color")))
        for kind, seg in segments:
            if kind == "span":
                box = seg
                box.layout(self.x, cursor, W, FloatContext(), positioned=positioned)
                cursor = box.margin_top_edge() + box.outer_height()
                continue
            cols = self._balance(seg, n)
            top = seg[0][1]
            bottom = cursor
            for k, col in enumerate(cols):
                dx, dy = k * (colw + gap), cursor - col[0][1]
                for obj, t, b in col:
                    obj.translate(dx, dy)
                bottom = max(bottom, col[-1][2] + dy)
            if color:
                for k in range(1, len(cols)):
                    x = self.x + k * (colw + gap) - gap / 2
                    self.col_rules.append((x - rw / 2, cursor, x + rw / 2, bottom, color))
            cursor = bottom
        last_bottom = units[-1][2] if units[-1][0] != "span" else None
        tail = (h - (last_bottom - self.y)) if last_bottom is not None else 0
        new_h = max(0.0, cursor - self.y + max(0.0, tail))
        for c in containers:
            c.height = max(0.0, self.y + new_h - c.y)
        return new_h

    def _balance(self, units, n):
        """Cut units into at most n columns of the smallest possible height."""
        heights = [b - t for _, t, b in units]

        def forced(u):
            obj = u[0]
            st = getattr(obj, "style", None)
            return st is not None and st.get("break-before", "auto") in ("column", "always", "page", "left", "right")

        def pack(limit):
            cols, start = [[]], None
            for i, u in enumerate(units):
                prev_after = i > 0 and getattr(units[i - 1][0], "style", {}).get("break-after", "auto") in \
                    ("column", "always", "page")
                if cols[-1] and (u[2] - start > limit + 0.5 or forced(u) or prev_after):
                    cols.append([])
                if not cols[-1]:
                    start = u[1]
                cols[-1].append(u)
            return cols
        lo, hi = max(heights), units[-1][2] - units[0][1]
        for _ in range(25):   # smallest column height that fits in n columns
            mid = (lo + hi) / 2
            if len(pack(mid)) <= n:
                hi = mid
            else:
                lo = mid
        return pack(hi)

    def _column_units(self, box, units, containers):
        """Pieces that may go to different columns: lines of inline content,
        or block children; plain blocks holding only blocks are entered."""
        if box.inline_items is not None:
            for line in box.lines:
                units.append((line, line.y, line.y + line.height))
            return
        for c in box.children:
            if is_out_of_flow(c.style):
                continue
            if c.style.get("column-span", "none") == "all" and box is self:
                units.append(("span", c, None))
                continue
            plain = type(c) is BlockBox and not c.is_float and not c.establishes_bfc() and \
                c.style.get("break-inside", "auto") not in ("avoid", "avoid-column", "avoid-page") and \
                not _has_box_decoration(c)
            if plain and c.inline_items is None and c.children:
                containers.append(c)
                self._column_units(c, units, containers)
            elif plain and c.inline_items is not None and len(c.lines) > 1 and not c.marker:
                # a paragraph may continue in the next column, line by line
                for line in c.lines:
                    units.append((line, line.y, line.y + line.height))
            else:
                x1, y1, x2, y2 = c.border_box()
                units.append((c, y1, y2))

    def _rewrap(self, il, fc, positioned, mode):
        """text-wrap: balance (even line lengths) / pretty (no lone last word)."""
        W = self.width
        n = len(il.lines)

        def run(width):
            self.width = width
            try:
                l2 = InlineLayout(self, fc, positioned)
                l2.lines_ = l2.run(self.inline_items)
                return l2
            finally:
                self.width = W
        best_w = None
        if mode == "balance" and n <= 10:
            lo, hi = W * 0.3, W
            for _ in range(10):
                mid = (lo + hi) / 2
                if len(run(mid).lines_) <= n:
                    hi = mid
                else:
                    lo = mid
            best_w = hi
        elif mode == "pretty":
            last = il.lines[-1]
            words = sum(f.text.count(" ") + 1 for f in last.frags if isinstance(f, TextFrag))
            if words <= 1:
                for f in (0.97, 0.94, 0.9, 0.86):
                    l2 = run(W * f)
                    lw = l2.lines_[-1]
                    if len(l2.lines_) <= n + 1 and sum(g.text.count(" ") + 1 for g in lw.frags
                                                       if isinstance(g, TextFrag)) >= 2:
                        best_w = W * f
                        break
        if best_w is None or best_w >= W - 0.5:
            return il
        l2 = run(best_w)
        self.lines = l2.lines_
        align = self.style.get("text-align", "left")
        shift = (W - best_w) / 2 if align == "center" else (W - best_w) if align in ("right", "end") else 0
        for line in self.lines:
            line.right += W - best_w
            if shift:
                line.translate(shift, 0)
                line.right -= shift
                line.left -= shift
        return l2

    def layout_flow(self, fc, positioned):
        if self.inline_items is not None:
            self.children = []   # floats found in the inline content are re-added
            il = InlineLayout(self, fc, positioned)
            self.lines = il.run(self.inline_items)
            tws = self.style.get("text-wrap-style", "auto")
            if tws in ("balance", "pretty") and len(self.lines) > 1 and fc is not None and fc.empty() and \
                    not any(k == "float" for k, _ in self.inline_items):
                il = self._rewrap(il, fc, positioned, tws)
            if self.style.get("text-align", "left") == "justify":
                _justify(self)
            _align_last(self)
            clamp = _line_clamp(self.style)
            if clamp and len(self.lines) > clamp:
                self.lines = self.lines[:clamp]
                _ellipsize_line(self.lines[-1], self.x + self.width)
                last = self.lines[-1]
                il.total_height = last.y + last.height - self.y
            if self.style.get("text-overflow", "clip").strip().startswith("ellipsis") and \
                    self.style.get("overflow-x", self.style.get("overflow", "visible")) not in ("visible",):
                _apply_ellipsis(self)
            return il.total_height
        cursor = self.y
        prev_margin = 0.0
        first = True
        self.last_margin = 0.0
        for child in self.children:
            cs = child.style
            if is_out_of_flow(cs):
                target = positioned if (cs.get("position") == "absolute" and positioned is not None) else None
                if target is None:
                    target = self.ctx.root_box
                queue_abs(target, child, self.x, cursor, self)
                continue
            clear = cs.get("clear", "none")
            if clear in ("left", "right", "both") and fc is not None:
                cleared = fc.clear_y(clear, cursor + prev_margin)
                if cleared > cursor + prev_margin:
                    cursor = cleared
                    prev_margin = 0
            if child.is_float:
                place_float(child, fc, self.x, self.width, cursor + prev_margin, positioned)
                continue
            child.compute_edges(self.width)
            mt = effective_top_margin(child, self.width)
            if first and self.collapses_top_with_child():
                # Our own top margin already absorbed this child's (parent/first-child collapsing).
                gap = 0.0
            else:
                gap = collapse_margins(prev_margin, mt)
            # child.layout adds its own effective top margin; place it so its
            # border edge lands at cursor + gap.
            child.layout(self.x, cursor + gap - mt, self.width, fc, positioned=positioned)
            bottom = child.y + child.height + child.p[B] + child.b[B]
            empty = child.height == 0 and child.vert_extra() == 0 and child.inline_items is not None \
                if isinstance(child, BlockBox) else False
            if empty:
                prev_margin = collapse_margins(prev_margin, collapse_margins(mt, child.m[B])) if not first else \
                    collapse_margins(mt, child.m[B])
                continue
            cursor = bottom
            prev_margin = child.m[B]
            first = False
        self.last_margin = prev_margin
        if self.collapses_bottom_with_child():
            # The last child's bottom margin passes through us (see layout()).
            return cursor - self.y
        return cursor + prev_margin - self.y

    def collapses_top_with_child(self):
        return (self.inline_items is None and not self.establishes_bfc() and self.b[T] == 0
                and self.p[T] == 0 and type(self) is BlockBox)

    def collapses_bottom_with_child(self):
        return (self.inline_items is None and not self.establishes_bfc() and self.b[B] == 0
                and self.p[B] == 0 and type(self) is BlockBox and self.style.get("height", "auto") == "auto"
                and self.style.get("min-height", "auto") in ("0", "auto", "0px"))

    def first_baseline(self):
        if self.lines:
            ln = self.lines[0]
            return ln.y + ln.baseline
        for c in self.children:
            if isinstance(c, BlockBox) and not c.is_float and not is_out_of_flow(c.style):
                b = c.first_baseline()
                if b is not None:
                    return b
        return None

    def make_marker(self):
        self.image_marker = None
        s = self.style
        ms = getattr(self.node, "marker_style", None) if self.node is not None else None
        kind = s.get("list-style-type", "disc").strip().lower()
        image = s.get("list-style-image", "none")
        if image.startswith("url(") and self._image_marker(image):
            return
        mcontent = ms.get("content") if ms else None
        if kind == "none" and not (mcontent and mcontent not in ("normal", "none")):
            return
        if ms:
            s = ms     # ::marker colour / font
        font = get_font(s)
        index = self.list_index()
        if mcontent and mcontent not in ("normal", "none"):
            from .style import generated_text
            text = generated_text(mcontent, self.node)
        else:
            text = list_marker_text(kind, index)
        if text is None:
            return
        base = self.first_baseline()
        top = (base - font.ascent) if base is not None else self.y
        w = font.measure(text)
        rtl = self.style.get("direction") == "rtl"
        if s.get("list-style-position", "outside") == "inside":
            x = self.x + self.width - w if rtl else self.x
        elif rtl:
            x = self.x + self.width + self.p[R] + self.b[R] + font.size_px * 0.4
        else:
            x = self.x - self.p[L] - self.b[L] - w - font.size_px * 0.4
        self.marker = (x, top, text, font, color_of(s.get("color")) or "#000000")

    def _image_marker(self, value):
        """list-style-image: draw the image as the marker."""
        m = css_parser.URL_RE.match(value)
        img = self.ctx.images.get(self.ctx.resolve(css_parser.url_of(m))) if m else None
        if img is None:
            return False
        w, h = img.size
        font = get_font(self.style)
        if h > font.linespace * 1.5:
            w, h = w * font.linespace / h, font.linespace
        base = self.first_baseline()
        top = (base - h) if base is not None else self.y
        x = self.x - self.p[L] - self.b[L] - w - font.size_px * 0.4
        self.image_marker = (x, top, w, h, img, self.ctx.resolve(m.group(1)))
        return True

    def list_index(self):
        node = self.node
        if node is None or node.parent is None:
            return 1
        if "value" in node.attributes:
            try:
                return int(node.attributes["value"])
            except ValueError:
                pass
        parent = node.parent
        items = [c for c in parent.children if isinstance(c, Element) and c.style.get("display") == "list-item"]
        rev = isinstance(parent, Element) and parent.tag == "ol" and "reversed" in parent.attributes
        start = len(items) if rev else 1
        if isinstance(parent, Element) and parent.tag == "ol" and "start" in parent.attributes:
            start = _int(parent.attributes.get("start"), start)
        step = -1 if rev else 1
        idx = start
        for c in items:
            if "value" in c.attributes:
                idx = _int(c.attributes["value"], idx)
            if c is node:
                return idx
            idx += step
        return idx

    def translate_contents(self, dx, dy):
        super().translate_contents(dx, dy)
        for line in self.lines:
            line.translate(dx, dy)
        if self.marker:
            x, y, text, font, color = self.marker
            self.marker = (x + dx, y + dy, text, font, color)
        if getattr(self, "image_marker", None):
            x, y, w, h, img, url = self.image_marker
            self.image_marker = (x + dx, y + dy, w, h, img, url)

    # --- painting -----------------------------------------------------
    def paint(self, dl):
        if self.hidden_entirely():
            return
        if _defer_positioned(self, dl):
            return
        if getattr(self, "vframe", None) and not getattr(self, "painting_vertical", False):
            # vertical writing mode: paint as laid out (horizontally), then turn
            mode, bx, by, bw, bh, lw, lh = self.vframe
            pw, ph = self.width, self.height
            sub = paint.DisplayList()
            sub.deferred, sub.negatives, sub.layers, sub.clip = dl.deferred, dl.negatives, dl.layers, None
            self.width, self.height = lw, lh
            bx, by, bx2, by2 = self.border_box()     # where the box is now (flex/grid may have moved it)
            bw, bh = bx2 - bx, by2 - by
            self.painting_vertical = True
            try:
                self.paint(sub)
            finally:
                self.painting_vertical = False
                self.width, self.height = pw, ph
            if mode == "sideways-lr":      # a quarter turn counter-clockwise
                m = (0.0, -1.0, 1.0, 0.0, bx - by, by + bw + bx)
            else:                          # vertical-rl/-lr, sideways-rl: clockwise
                m = (0.0, 1.0, -1.0, 0.0, bx + bh + by, by - bx)
            from . import transforms
            transforms.transform_list(sub, m)
            dl.commands.extend(sub.commands)
            dl.hits.extend(sub.hits)
            return
        op = _opacity(self.style)
        if op < 0.995 and not getattr(self, "painting_opacity", False):
            # Translucent box: paint it, then fade everything it drew toward
            # white (Tk has no alpha channel).
            tmp = paint.DisplayList()
            tmp.deferred, tmp.layers, tmp.clip = dl.deferred, dl.layers, dl.clip
            tmp.negatives = dl.negatives
            self.painting_opacity = True
            try:
                self.paint(tmp)
            finally:
                self.painting_opacity = False
            for cmd in tmp.commands:
                dl.add(paint.faded(cmd, op))
            dl.hits.extend(tmp.hits)
            return
        sticky = self.ctx.sticky.get(id(self)) if dl.layers is not None else None
        if sticky is not None and not getattr(self, "painting_layer", False):
            layer = paint.Layer("sticky", *sticky)
            self.painting_layer = True
            try:
                self.paint(layer.list)
            finally:
                self.painting_layer = False
            dl.layers.append(layer)
            self.ctx.root_box.layer_boxes.append((self, layer))
            return
        if dl.deferred is not None and _makes_stacking_context(self) and not getattr(self, "painting_context", False):
            # A stacking context: its own positioned descendants are ordered
            # (by z-index) among themselves, then it is painted as one unit.
            sub = paint.DisplayList()
            sub.deferred, sub.negatives, sub.layers, sub.clip = [], [], dl.layers, dl.clip
            st = self.style
            if st.get("backdrop-filter", "none") not in ("none", None):
                from . import effects
                effects.backdrop(dl, self.border_box(), st["backdrop-filter"], self.fs,
                                 color_of(st.get("color")) or "#000000")
            self.painting_context = True
            try:
                self.paint(sub)
            finally:
                self.painting_context = False
            _drain_stacking(sub)
            _apply_effects(self, sub)
            m = _paint_matrix(self)
            if m is not None:
                from . import transforms
                transforms.transform_list(sub, m)
            dl.commands.extend(sub.commands)
            dl.hits.extend(sub.hits)
            return
        visible = self.visible()
        overflow = self.style.get("overflow", "visible")
        ox = self.style.get("overflow-x", overflow)
        oy = self.style.get("overflow-y", overflow)
        contain = self.style.get("contain", "none")
        if "paint" in contain or contain in ("strict", "content") or self.style.get("content-visibility") == "auto":
            ox = "clip" if ox == "visible" else ox     # paint containment clips like overflow: clip
            oy = "clip" if oy == "visible" else oy
        clip = None
        if (ox != "visible" or oy != "visible") and self.node is not None and \
                self.node.tag not in ("html", "body"):
            x1, y1, x2, y2 = self.border_box()
            clip = (x1 + self.b[L], y1 + self.b[T], x2 - self.b[R], y2 - self.b[B])
            if ox == "visible":     # only one axis clips
                clip = (-1e9, clip[1], 1e9, clip[3])
            elif oy == "visible":
                clip = (clip[0], -1e9, clip[2], 1e9)
        if visible:
            self.paint_background(dl)
        if getattr(self, "painting_context", False) or (self.node is not None and getattr(self.node, "tag", "") in
                                                        ("html", "body") and dl.negatives is not None):
            dl.neg_index = len(dl.commands)    # negative z-index children go right above this background
        target = dl
        if clip is not None:
            target = paint.DisplayList()
            target.deferred = dl.deferred
            target.negatives = dl.negatives
            target.layers = dl.layers
            target.clip = _intersect(dl.clip, clip)
        # overflow: auto/scroll boxes scroll their contents (mouse wheel, see gui).
        sx = sy = 0.0
        scrolls = clip is not None and (ox in ("auto", "scroll") or oy in ("auto", "scroll"))
        if scrolls:
            ex, ey = _content_extent(self)
            bx1, by1, bx2, by2 = self.border_box()
            max_sx = max(0.0, ex - (bx2 - self.b[R])) if ox in ("auto", "scroll") else 0.0
            max_sy = max(0.0, ey - (by2 - self.b[B])) if oy in ("auto", "scroll") else 0.0
            if max_sx or max_sy:
                sx, sy = self.ctx.box_scroll.get(self.node, (0.0, 0.0))
                sx, sy = min(max(0.0, sx), max_sx), min(max(0.0, sy), max_sy)
                self.ctx.box_scroll[self.node] = (sx, sy)
                self.ctx.scrollers.append((self, clip, max_sx, max_sy))
            else:
                scrolls = False
        if sx or sy:
            self.translate_contents(-sx, -sy)
        try:
            self.paint_contents(target, visible)
            if self.marker and visible:
                x, y, text, font, color = self.marker
                target.add(paint.DrawText(x, y, text, font, color))
            if getattr(self, "image_marker", None) and visible:
                x, y, w, h, img, url = self.image_marker
                target.add(paint.DrawImage(x, y, w, h, img, url))
            for box in self.abs_boxes:
                box.paint(target)
        finally:
            if sx or sy:
                self.translate_contents(sx, sy)
        if scrolls:
            bx1, by1, bx2, by2 = self.border_box()
            _paint_scrollbars(dl, (bx1 + self.b[L], by1 + self.b[T], bx2 - self.b[R], by2 - self.b[B]),
                              sx, sy, max_sx, max_sy)
        if visible:
            paint_outline(self, target if clip is None else dl)
            paint_resize_grip(self, dl)
        if clip is not None:
            for cmd in target.commands:
                c = cmd.clipped(clip)
                if c is not None:
                    dl.add(c)
            cx1, cy1, cx2, cy2 = clip
            for hx1, hy1, hx2, hy2, n in target.hits:
                if hx2 > cx1 and hx1 < cx2 and hy2 > cy1 and hy1 < cy2:
                    dl.hits.append((max(hx1, cx1), max(hy1, cy1), min(hx2, cx2), min(hy2, cy2), n))

    def paint_contents(self, dl, visible):
        if self.style.get("content-visibility") == "hidden":
            return
        for x1, y1, x2, y2, color in getattr(self, "col_rules", ()):
            dl.add(paint.DrawRect(x1, y1, x2, y2, color))
        floats = []
        for child in self.children:
            if child.is_float:
                floats.append(child)
            else:
                child.paint(dl)
        for line in self.lines:
            line.paint(dl)
        for f in floats:
            f.paint(dl)


INTRINSIC_WIDTHS = ("max-content", "min-content", "fit-content", "-moz-max-content", "-webkit-max-content",
                    "-moz-fit-content", "-webkit-fit-content", "-moz-min-content", "-webkit-min-content")


def _content_extent(box):
    """Right and bottom edge of everything inside a box (for scrolling)."""
    ex, ey = box.x + box.width, box.y + box.height
    stack = list(box.children) + list(box.abs_boxes)
    for line in getattr(box, "lines", ()):
        for f in line.frags:
            ex = max(ex, f.x + f.width)
            ey = max(ey, line.y + line.height)
    while stack:
        b = stack.pop()
        x1, y1, x2, y2 = b.border_box()
        ex, ey = max(ex, x2 + b.m[R]), max(ey, y2 + b.m[B])
        if b.style.get("overflow", "visible") != "visible":
            continue   # a clipping box hides its own overflow
        stack.extend(b.children)
        stack.extend(b.abs_boxes)
        for line in getattr(b, "lines", ()):
            for f in line.frags:
                if isinstance(f, AtomFrag):
                    stack.append(f.box)
                else:
                    ex = max(ex, f.x + f.width)
                    ey = max(ey, line.y + line.height)
    return ex, ey


def _paint_scrollbars(dl, clip, sx, sy, max_sx, max_sy):
    """Thin scroll position indicators for an overflow: auto/scroll box."""
    x1, y1, x2, y2 = clip
    w, h = x2 - x1, y2 - y1
    if max_sy and h > 10:
        th = max(16.0, h * h / (h + max_sy))
        ty = y1 + (h - th) * (sy / max_sy)
        dl.add(paint.DrawRect(x2 - 6, y1, x2, y2, "#eeeeee"))
        dl.add(paint.DrawRect(x2 - 6, ty, x2, ty + th, "#a0a0a0"))
    if max_sx and w > 10:
        tw = max(16.0, w * w / (w + max_sx))
        tx = x1 + (w - tw) * (sx / max_sx)
        dl.add(paint.DrawRect(x1, y2 - 6, x2, y2, "#eeeeee"))
        dl.add(paint.DrawRect(tx, y2 - 6, tx + tw, y2, "#a0a0a0"))


def _aspect_ratio(style):
    v = style.get("aspect-ratio", "auto")
    if not v or v == "auto":
        return None
    m = re.search(r"(\d*\.?\d+)\s*(?:/\s*(\d*\.?\d+))?", v)
    if not m:
        return None
    a, b = float(m.group(1)), float(m.group(2) or 1)
    return a / b if a > 0 and b > 0 else None


def _opacity(style):
    v = style.get("opacity", "1")
    try:
        o = float(v[:-1]) / 100 if v.endswith("%") else float(v)
    except (ValueError, AttributeError):
        return 1.0
    return max(0.0, min(1.0, o))


_GRADIENT_CACHE = {}


def paint_gradient(box, dl, value, x1, y1, x2, y2, radius=0):
    """linear-gradient() / radial-gradient() (and repeating-*), drawn as an image."""
    w, h = int(round(x2 - x1)), int(round(y2 - y1))
    if w < 1 or h < 1:
        return
    from .css_parser import _split_top_level
    layer = _split_top_level(value, ",")[0].strip()
    current = color_of(box.style.get("color")) or "#000000"
    key = (layer, w, h, current, radius)
    img = _GRADIENT_CACHE.get(key)
    if img is None:
        img = _render_gradient(layer, w, h, current, radius)
        if img is None:
            return
        if len(_GRADIENT_CACHE) > 300:
            _GRADIENT_CACHE.clear()
        _GRADIENT_CACHE[key] = img
    dl.add(paint.DrawImage(x1, y1, w, h, img, "gradient:%x" % (hash(key) & 0xffffffffffff)))


def _render_gradient(layer, w, h, current, radius):
    import math
    from PIL import Image, ImageDraw
    from .css_parser import _split_top_level
    from .style import to_rgba
    m = re.match(r"(repeating-)?(linear|radial|conic)-gradient\((.*)\)$", layer, re.S | re.I)
    if not m:
        return None
    repeating, kind, inner = bool(m.group(1)), m.group(2).lower(), m.group(3)
    args = [a.strip() for a in _split_top_level(inner, ",")]
    angle = 180.0
    first = args[0].lower() if args else ""
    if kind == "linear" and (first.startswith("to ") or re.match(r"^-?[\d.]+(deg|turn|rad|grad)$", first)):
        args = args[1:]
        if first.startswith("to "):
            sides = first[3:].split()
            dx = (1 if "right" in sides else -1 if "left" in sides else 0)
            dy = (1 if "bottom" in sides else -1 if "top" in sides else 0)
            angle = math.degrees(math.atan2(dx * h, -dy * w)) if (dx and dy) else \
                {(0, -1): 0, (1, 0): 90, (0, 1): 180, (-1, 0): 270}.get((dx, dy), 180)
        else:
            num = float(re.match(r"^-?[\d.]+", first).group(0))
            unit = first[len(re.match(r"^-?[\d.]+", first).group(0)):]
            angle = num * {"deg": 1, "turn": 360, "rad": 180 / math.pi, "grad": 0.9}[unit]
    elif kind != "linear" and args and not re.match(r"^(#|rgb|hsl|[a-z]+\s*$|[a-z]+\s+[\d.])", first) \
            or kind != "linear" and args and (" at " in " " + first or first.split()[0] in
                                            ("circle", "ellipse", "closest-side", "farthest-corner",
                                             "closest-corner", "farthest-side", "from")):
        args = args[1:]
    stops = []
    for a in args:
        toks = a.rsplit(None, 1)
        pos = None
        ctext = a
        if len(toks) == 2 and re.match(r"^-?[\d.]+(%|px)?$", toks[1]):
            ctext, p = toks[0], toks[1]
            pos = float(p[:-1]) / 100 if p.endswith("%") else (float(p[:-2]) if p.endswith("px") else float(p))
            if p.endswith("px"):
                pos = ("px", pos)
        if ctext.lower() == "currentcolor":
            ctext = current
        rgba = to_rgba(ctext, current)
        if rgba is None:
            continue
        stops.append([rgba, pos])
    if len(stops) < 2:
        if stops:
            stops.append(list(stops[0]))
        else:
            return None
    # Gradient line length
    if kind == "linear":
        rad = math.radians(angle)
        length_ = abs(w * math.sin(rad)) + abs(h * math.cos(rad))
    else:
        length_ = math.hypot(w / 2, h / 2)
    for st in stops:
        if isinstance(st[1], tuple):
            st[1] = st[1][1] / max(1.0, length_)
    if stops[0][1] is None:
        stops[0][1] = 0.0
    if stops[-1][1] is None:
        stops[-1][1] = 1.0
    i = 0
    while i < len(stops):   # spread unpositioned stops evenly
        if stops[i][1] is None:
            j = i
            while stops[j][1] is None:
                j += 1
            a, b = stops[i - 1][1], stops[j][1]
            for k in range(i, j):
                stops[k][1] = a + (b - a) * (k - i + 1) / (j - i + 1)
            i = j
        i += 1
    for k in range(1, len(stops)):
        stops[k][1] = max(stops[k][1], stops[k - 1][1])
    for k, st in enumerate(stops):   # fade to "transparent" keeps the neighbour's hue
        if st[0][3] == 0:
            near = next((stops[j][0] for j in sorted(range(len(stops)), key=lambda j: abs(j - k))
                         if stops[j][0][3] > 0), None)
            if near is not None:
                st[0] = (near[0], near[1], near[2], 0.0)
    first_pos, last_pos = stops[0][1], stops[-1][1]

    def color_at(t):
        if repeating and last_pos > first_pos:
            t = first_pos + (t - first_pos) % (last_pos - first_pos)
        if t <= stops[0][1]:
            return stops[0][0]
        for k in range(1, len(stops)):
            if t <= stops[k][1]:
                (c0, p0), (c1, p1) = stops[k - 1], stops[k]
                f = (t - p0) / (p1 - p0) if p1 > p0 else 1.0
                return tuple(c0[n] + (c1[n] - c0[n]) * f for n in range(4))
        return stops[-1][0]

    # Render small and scale up: gradients are smooth.
    sw, sh = max(1, min(w, 160)), max(1, min(h, 160))
    img = Image.new("RGBA", (sw, sh))
    px = img.load()
    if kind == "linear":
        rad = math.radians(angle)
        dxv, dyv = math.sin(rad), -math.cos(rad)
        for yy in range(sh):
            y = (yy + 0.5) * h / sh - h / 2
            for xx in range(sw):
                x = (xx + 0.5) * w / sw - w / 2
                t = (x * dxv + y * dyv) / max(1.0, length_) + 0.5
                c = color_at(t)
                px[xx, yy] = (int(c[0]), int(c[1]), int(c[2]), int(255 * c[3]))
    elif kind == "radial":
        rx, ry = w / 2 * math.sqrt(2), h / 2 * math.sqrt(2)   # farthest-corner ellipse
        if "circle" in first:
            rx = ry = math.hypot(w / 2, h / 2)
        for yy in range(sh):
            y = (yy + 0.5) * h / sh - h / 2
            for xx in range(sw):
                x = (xx + 0.5) * w / sw - w / 2
                c = color_at(math.hypot(x / max(1.0, rx), y / max(1.0, ry)))
                px[xx, yy] = (int(c[0]), int(c[1]), int(c[2]), int(255 * c[3]))
    else:   # conic
        for yy in range(sh):
            y = (yy + 0.5) * h / sh - h / 2
            for xx in range(sw):
                x = (xx + 0.5) * w / sw - w / 2
                c = color_at((math.degrees(math.atan2(x, -y)) % 360) / 360)
                px[xx, yy] = (int(c[0]), int(c[1]), int(c[2]), int(255 * c[3]))
    if (sw, sh) != (w, h):
        img = img.resize((w, h), Image.BILINEAR)
    if radius:
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=int(radius), fill=255)
        alpha = Image.composite(img.getchannel("A"), mask, mask)
        img.putalpha(alpha)
    return img


_FIRST_LINE_CACHE = {}


def _first_line_style(style, fl):
    """A text style with the properties a ::first-line rule declared."""
    key = (id(style), id(fl))
    hit = _FIRST_LINE_CACHE.get(key)
    if hit is not None and hit[0] is style:
        return hit[1]
    merged = dict(style)
    for k in fl.get("-declared", ()):
        if k in fl:
            merged[k] = fl[k]
    if "font-size" in fl.get("-declared", ()):
        merged["-font-px"] = fl["-font-px"]
    if len(_FIRST_LINE_CACHE) > 5000:
        _FIRST_LINE_CACHE.clear()
    _FIRST_LINE_CACHE[key] = (style, merged)
    return merged


def text_color(style, node):
    """Text colour; a translucent one is blended with the nearest opaque
    background behind it (not just white). Text whose fill is transparent
    inside a background-clip: text box takes that background's colour."""
    raw = style.get("color", "")
    fill = style.get("-webkit-text-fill-color", raw)
    if fill in ("transparent", "rgba(0,0,0,0)", "rgba(0, 0, 0, 0)") or raw == "transparent":
        n = node.parent if node is not None else None
        while n is not None and hasattr(n, "style"):
            if n.style.get("-text-fill"):
                return n.style["-text-fill"]
            n = n.parent
    base = color_of(raw) or "#000000"
    if not raw or not ("rgba" in raw or "/" in raw or "hsla" in raw or (raw.startswith("#") and len(raw) in (5, 9))
                       or "color-mix" in raw):
        return base
    rgba = to_rgba(raw)
    if not rgba or rgba[3] >= 0.99:
        return base
    back = (255, 255, 255)
    n = node.parent if node is not None else None
    while n is not None and hasattr(n, "style"):
        b = n.style.get("background-color")
        if b and b not in ("transparent", "initial"):
            c = to_rgba(b)
            if c and c[3] >= 0.99:
                back = c[:3]
                break
        n = n.parent
    a = rgba[3]
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(rgba[i] * a + back[i] * (1 - a)))) for i in range(3))


_RTL_CHARS = re.compile("[\u0590-\u08ff\ufb1d-\ufdff\ufe70-\ufefc]")
_LTR_CHARS = re.compile("[A-Za-z\u00c0-\u024f\u0370-\u03ff\u0400-\u04ff0-9]")


def _reorder_rtl(line):
    """Right-to-left paragraphs: words are laid out in reading (logical)
    order; put them in visual order: the line runs right to left, except
    runs of left-to-right words (Latin, numbers), which keep their order."""
    frags = sorted(line.frags, key=lambda f: f.x)
    n = len(frags)
    gaps = [frags[i + 1].x - (frags[i].x + frags[i].width) for i in range(n - 1)] + [0.0]
    kinds = []
    for f in frags:
        if isinstance(f, TextFrag):
            if f.style.get("direction") == "ltr" and f.style.get("unicode-bidi") in ("embed", "isolate",
                                                                                      "bidi-override"):
                kinds.append("L")
            elif _RTL_CHARS.search(f.text):
                kinds.append("R")
            elif _LTR_CHARS.search(f.text):
                kinds.append("L")
            else:
                kinds.append(None)
        else:
            kinds.append(None)
    levels = []
    for i, k in enumerate(kinds):
        if k == "L":
            levels.append(2)
        elif k == "R":
            levels.append(1)
        else:   # neutral: left-to-right only between two left-to-right words
            prev = next((kinds[j] for j in range(i - 1, -1, -1) if kinds[j]), None)
            nxt = next((kinds[j] for j in range(i + 1, n) if kinds[j]), None)
            levels.append(2 if prev == "L" and nxt == "L" else 1)
    order = list(range(n))
    for lvl in (2, 1):          # Unicode bidi rule L2: reverse runs at each level, highest first
        i = 0
        while i < n:
            if levels[order[i]] >= lvl:
                j = i
                while j < n and levels[order[j]] >= lvl:
                    j += 1
                order[i:j] = reversed(order[i:j])
                i = j
            else:
                i += 1
    x = frags[0].x
    for pos, idx in enumerate(order):
        f = frags[idx]
        f.x = x
        if pos + 1 < n:
            nxt = order[pos + 1]
            if abs(nxt - idx) == 1:
                gap = gaps[min(idx, nxt)]
            else:
                gap = max(gaps[idx], gaps[nxt - 1] if nxt > 0 else 0.0)
            x += f.width + max(0.0, gap)
    line.frags = [frags[i] for i in order]


def _spacing(style, prop):
    v = style.get(prop, "normal")
    if not v or v == "normal":
        return 0.0
    return length(v, style.get("-font-px", 16), None, 0) or 0.0


def _justify(block):
    """text-align: justify: widen the gaps between words so every line but
    the last (and lines ended by <br>) fills the box."""
    lines = block.lines
    for line in lines[:-1]:
        if getattr(line, "forced", False) or not line.frags:
            continue
        _justify_line(line)


def _justify_line(line):
    words = []
    for f in line.frags:
        if isinstance(f, TextFrag) and " " in f.text.strip():
            x = f.x
            for i, part in enumerate(f.text.split(" ")):
                if part:
                    pw = f.font.measure(part)
                    nf = TextFrag(x, part, pw, f.font, f.style, f.node)
                    nf.y, nf.shift = f.y, f.shift
                    nf.ascent, nf.descent = getattr(f, "ascent", 0), getattr(f, "descent", 0)
                    words.append(nf)
                    x += pw
                x += f.font.space_width
        else:
            words.append(f)
    gaps = [i for i in range(1, len(words))
            if words[i].x > words[i - 1].x + words[i - 1].width + 0.5]
    end = words[-1].x + words[-1].width
    free = line.right - end
    if not gaps or free <= 0.5 or free > (line.right - line.left) * 0.6:
        return
    per = free / len(gaps)
    gapset = set(gaps)
    shift = 0.0
    for i, f in enumerate(words):
        if i in gapset:
            shift += per
        if shift:
            f.x += shift
            if isinstance(f, AtomFrag):
                f.box.translate(shift, 0)
    line.frags = words


def _breaks_words(style):
    return style.get("overflow-wrap", style.get("word-wrap", "normal")) in ("break-word", "anywhere") or \
        style.get("word-wrap", "normal") == "break-word" or \
        style.get("word-break", "normal") in ("break-all", "break-word")


def _apply_ellipsis(block):
    """text-overflow: ellipsis: cut each line at the box edge and end it with "…"."""
    limit = block.x + block.width
    for line in block.lines:
        if not line.frags or max(f.x + f.width for f in line.frags) <= limit + 0.5:
            continue
        keep = []
        for f in line.frags:
            if f.x + f.width <= limit + 0.5:
                keep.append(f)
                continue
            if isinstance(f, TextFrag):
                ell = f.font.measure("\u2026")
                lo, hi = 0, len(f.text)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if f.x + f.font.measure(f.text[:mid]) + ell <= limit:
                        lo = mid
                    else:
                        hi = mid - 1
                f.text = f.text[:lo].rstrip() + "\u2026"
                f.width = f.font.measure(f.text)
                keep.append(f)
            elif keep and isinstance(keep[-1], TextFrag):
                last = keep[-1]
                last.text += "\u2026"
                last.width = last.font.measure(last.text)
            break
        line.frags = keep


def _apply_effects(box, sub):
    """filter, clip-path / clip and mix-blend-mode on a stacking context's
    painted commands (in that order, as in CSS)."""
    st = box.style
    f = st.get("filter", "none")
    if f and f != "none":
        from . import effects
        effects.apply_filter(sub, f, box.fs, color_of(st.get("color")) or "#000000")
    cp = st.get("clip-path", "none")
    shape = None
    if cp and cp != "none":
        from . import effects
        shape = effects.clip_shape(cp, box.border_box(), box.fs)
    elif is_out_of_flow(st) and st.get("clip", "auto").startswith("rect("):
        from . import effects
        shape = effects.clip_rect_value(st["clip"], box.border_box())
    if shape is not None:
        from . import effects
        effects.apply_clip(sub, shape)
    mode = st.get("mix-blend-mode", "normal")
    if mode and mode != "normal":
        from . import effects
        effects.apply_blend(sub, mode, _backdrop_rgb(box))


def _backdrop_rgb(box):
    n = box.node.parent if box.node is not None else None
    while n is not None and hasattr(n, "style"):
        c = to_rgba(n.style.get("background-color", "transparent") or "transparent")
        if c and c[3] >= 0.99:
            return c[:3]
        n = n.parent
    return (255, 255, 255)


def _paint_transformed(box, dl):
    """Images and form controls with transforms, filters, clip-path or blend
    modes: paint, then apply the effects and the transform."""
    if getattr(box, "painting_xf", False):
        return False
    m = _paint_matrix(box)
    if m is None and not _makes_stacking_context(box):
        return False
    tmp = paint.DisplayList()
    tmp.deferred, tmp.negatives, tmp.layers, tmp.clip = dl.deferred, dl.negatives, dl.layers, dl.clip
    box.painting_xf = True
    try:
        box.paint(tmp)
    finally:
        box.painting_xf = False
    _apply_effects(box, tmp)
    if m is not None:
        from . import transforms
        transforms.transform_list(tmp, m)
    dl.commands.extend(tmp.commands)
    dl.hits.extend(tmp.hits)
    return True


def _paint_matrix(box):
    """The box's transform matrix (translate, rotate, scale, transform)."""
    s = box.style
    t = s.get("transform", "none")
    if (not t or t == "none") and all(s.get(k, "none") in ("none", None) for k in ("translate", "rotate", "scale")):
        return None
    from . import transforms
    x1, y1, x2, y2 = box.border_box()
    return transforms.box_matrix(s, x1, y1, x2, y2, box.fs)


def _has_box_decoration(box):
    s = box.style
    return bool(color_of(s.get("background-color")) or s.get("background-image", "none") != "none"
                or any(box.b))


VERTICAL_MODES = ("vertical-rl", "vertical-lr", "sideways-rl", "sideways-lr", "tb-rl", "tb")


def _appearance_none(style):
    return (style.get("appearance") or style.get("-webkit-appearance") or style.get("-moz-appearance")
            or "auto") == "none"


def paint_resize_grip(box, dl):
    """resize: both/horizontal/vertical: a grip in the bottom-right corner
    the user can drag (see gui)."""
    r = box.style.get("resize", "none")
    if r in ("none", None) or box.node is None:
        return
    if box.style.get("overflow", "visible") == "visible" and getattr(box, "kind", None) != "textarea":
        return
    x1, y1, x2, y2 = box.border_box()
    gx, gy = x2 - box.b[R], y2 - box.b[B]
    for k in (4, 8):
        dl.add(paint.DrawLine(gx - k, gy - 1, gx - 1, gy - k, "#888888", 1))
    box.ctx.resizers.append((box, r, (gx - 12, gy - 12, gx, gy)))


CENTERING = ("-webkit-center", "-moz-center")


def _z_value(box):
    """The box's layer in its stacking context: None for ordinary in-flow
    boxes, 0 for positioned boxes with z-index: auto, else the z-index."""
    z = box.style.get("z-index", "auto")
    try:
        zi = int(z)
    except (TypeError, ValueError):
        zi = None
    if box.position not in ("static", None):
        return 0 if zi is None else zi
    if zi is not None and getattr(box, "flex_item", False):
        return zi        # flex and grid items honour z-index without being positioned
    return None


def _makes_stacking_context(box):
    s = box.style
    if box.position in ("fixed", "sticky"):
        return True
    if box.position != "static" and s.get("z-index", "auto") not in ("auto", None):
        return True
    if getattr(box, "flex_item", False) and s.get("z-index", "auto") not in ("auto", None):
        return True
    if any(s.get(k, "none") not in ("none", None) for k in ("translate", "rotate", "scale")):
        return True
    if s.get("clip-path", "none") not in ("none", None) or s.get("backdrop-filter", "none") not in ("none", None):
        return True
    if is_out_of_flow(s) and s.get("clip", "auto").startswith("rect("):
        return True
    return (s.get("transform", "none") not in ("none", "") or s.get("filter", "none") not in ("none", "")
            or s.get("isolation") == "isolate" or s.get("mix-blend-mode", "normal") != "normal"
            or _opacity(s) < 0.995 or "paint" in s.get("contain", "") or s.get("contain") in ("strict", "content")
            or s.get("will-change", "") in ("transform", "opacity"))


def _defer_positioned(box, dl):
    """Positioned boxes are painted after the in-flow content of their
    stacking context (z-index >= 0, in z order) or just above its
    background (z-index < 0). Returns True if the box was set aside."""
    if dl.deferred is None or getattr(box, "painting_deferred", False):
        return False
    z = _z_value(box)
    if z is None:
        return False
    entry = (z, getattr(box, "paint_order", 0), box, dl.clip)
    if z < 0 and dl.negatives is not None:
        dl.negatives.append(entry)
    else:
        dl.deferred.append(entry)
    return True


def _paint_set_aside(dl, items):
    out = paint.DisplayList()
    for _, _, box, clip in sorted(items, key=lambda d: (d[0], d[1])):
        tmp = paint.DisplayList()
        tmp.deferred, tmp.negatives, tmp.layers, tmp.clip = dl.deferred, dl.negatives, dl.layers, clip
        box.painting_deferred = True
        try:
            box.paint(tmp)
        finally:
            box.painting_deferred = False
        _merge_clipped(out, tmp, clip)
    return out


def _drain_stacking(dl):
    """Paint the positioned boxes a stacking context set aside."""
    while dl.negatives or dl.deferred:
        if dl.negatives:
            items = list(dl.negatives)
            dl.negatives.clear()
            out = _paint_set_aside(dl, items)
            i = min(dl.neg_index, len(dl.commands))
            dl.commands[i:i] = out.commands
            dl.neg_index = i + len(out.commands)
            dl.hits[0:0] = out.hits
            continue
        items = list(dl.deferred)
        dl.deferred.clear()
        out = _paint_set_aside(dl, items)
        dl.commands.extend(out.commands)
        dl.hits.extend(out.hits)


def _z_index(box):
    if box.position == "static":
        return 0
    try:
        return int(box.style.get("z-index", "auto"))
    except ValueError:
        return 0


def _intersect(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))


def _merge_clipped(dl, src, clip):
    """Append another display list's commands and hit areas, clipped."""
    if clip is None:
        dl.commands.extend(src.commands)
        dl.hits.extend(src.hits)
        return
    cx1, cy1, cx2, cy2 = clip
    for cmd in src.commands:
        c = cmd.clipped(clip)
        if c is not None:
            dl.add(c)
    for hx1, hy1, hx2, hy2, n in src.hits:
        if hx2 > cx1 and hx1 < cx2 and hy2 > cy1 and hy1 < cy2:
            dl.hits.append((max(hx1, cx1), max(hy1, cy1), min(hx2, cx2), min(hy2, cy2), n))


def collapse_margins(a, b):
    """Combine two adjoining vertical margins (CSS 2.1 section 8.3.1)."""
    if a >= 0 and b >= 0:
        return max(a, b)
    if a < 0 and b < 0:
        return min(a, b)
    return a + b


def effective_top_margin(box, cb_w, recompute=True):
    """A block's top margin after collapsing with its first in-flow child's."""
    if recompute:
        box.compute_edges(cb_w)
    mt = box.m[T]
    if not isinstance(box, BlockBox) or box.is_float or is_out_of_flow(box.style):
        return mt
    if not box.collapses_top_with_child():
        return mt
    for child in box.children:
        if is_out_of_flow(child.style) or child.is_float:
            continue
        if child.style.get("clear", "none") != "none":
            return mt
        return collapse_margins(mt, effective_top_margin(child, cb_w))
    return mt


def place_float(box, fc, cx, cw, y, positioned):
    """Position a float box within the content area [cx, cx+cw] at or below y."""
    box.compute_edges(cw)
    box.layout(0, 0, cw, FloatContext(), shrink=True, positioned=positioned)
    ow, oh = box.outer_width(), box.outer_height()
    side = box.style.get("float")
    yy = y
    for _ in range(200):
        left, right = fc.edges(yy, oh, cx, cx + cw)
        if right - left >= ow - 0.5 or (left == cx and right == cx + cw):
            break
        nb = fc.next_bottom(yy)
        if nb is None:
            break
        yy = nb
    left, right = fc.edges(yy, oh, cx, cx + cw)
    x = left if side == "left" else right - ow
    box.translate(x - box.margin_left_edge(), yy - box.margin_top_edge())
    if side == "left":
        fc.left.append((yy, yy + oh, x + ow))
    else:
        fc.right.append((yy, yy + oh, x))


def list_marker_text(kind, index):
    from .style import counter_marker, format_counter
    custom = counter_marker(index, kind)
    if custom is not None:
        return custom.rstrip()
    if kind in ("lower-greek", "disclosure-open", "disclosure-closed"):
        return format_counter(index, kind) + ("." if kind == "lower-greek" else "")
    if kind in ("disc",):
        return "•"
    if kind == "circle":
        return "◦"
    if kind == "square":
        return "▪"
    if kind in ("decimal", "decimal-leading-zero"):
        return ("%02d." if kind == "decimal-leading-zero" else "%d.") % index
    if kind in ("lower-alpha", "lower-latin"):
        return _alpha(index) + "."
    if kind in ("upper-alpha", "upper-latin"):
        return _alpha(index).upper() + "."
    if kind == "lower-roman":
        return _roman(index).lower() + "."
    if kind == "upper-roman":
        return _roman(index) + "."
    if kind.startswith(("\"", "'")):
        return kind.strip("\"'")
    return "•"


def _alpha(n):
    s = ""
    while n > 0:
        n -= 1
        s = chr(ord("a") + n % 26) + s
        n //= 26
    return s or "a"


def _roman(n):
    vals = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
            (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
    out = ""
    for v, r in vals:
        while n >= v:
            out += r
            n -= v
    return out or "0"


# ---------------------------------------------------------------------------
# Inline formatting


WS_COLLAPSE = re.compile(r"[ \t\n\r\f]+")
TOKEN_RE = re.compile(r"[^ \t\n\r\f]+|[ \t\n\r\f]+")


def transform_text(text, style):
    t = style.get("text-transform", "none")
    if t == "uppercase":
        return text.upper()
    if t == "lowercase":
        return text.lower()
    if t == "capitalize":
        return re.sub(r"\b(\w)", lambda m: m.group(1).upper(), text)
    return text


def inline_intrinsic(items, block_style):
    """(min-content, max-content) width of an inline formatting context."""
    mn = 0.0
    line = 0.0
    mx = 0.0
    pending_space = False
    for kind, obj in items:
        if kind == "text":
            style = obj.style
            font = get_font(style)
            ws = style.get("white-space", "normal")
            text = transform_text(obj.text, style)
            if ws in ("pre", "pre-wrap", "break-spaces", "pre-line"):
                parts = text.replace("\t", "        ").split("\n")
                for i, part in enumerate(parts):
                    if i > 0:
                        mx = max(mx, line)
                        line = 0
                    w = font.measure(part)
                    line += w
                    if ws == "pre":
                        mn = max(mn, w)
                    else:
                        for word in part.split():
                            mn = max(mn, font.measure(word))
                continue
            nowrap = ws == "nowrap"
            run = 0.0
            for tok in TOKEN_RE.findall(text):
                if tok[0] in " \t\n\r\f":
                    if line > 0:
                        pending_space = True
                    if nowrap:
                        run += font.space_width
                    continue
                w = font.measure(tok)
                if pending_space:
                    line += font.space_width
                    pending_space = False
                line += w
                if nowrap:
                    run += w
                    mn = max(mn, run)
                else:
                    mn = max(mn, w)
        elif kind == "br":
            mx = max(mx, line)
            line = 0
            pending_space = False
        elif kind in ("atom", "float"):
            amn, amx = obj.intrinsic()
            if pending_space and kind == "atom":
                line += get_font(block_style).space_width
                pending_space = False
            line += amx
            mn = max(mn, amn)
        elif kind in ("open", "close"):
            s = obj.style
            side = "left" if kind == "open" else "right"
            fs = s.get("-font-px", 16)
            if kind == "open" and pending_space and line > 0:
                line += get_font(s).space_width
                pending_space = False
            extra = (length(s.get("padding-" + side, "0"), fs, None, 0) or 0) + \
                    (length(s.get("margin-" + side, "0"), fs, None, 0) or 0)
            if s.get("border-%s-style" % side, "none") not in ("none", "hidden"):
                extra += length(s.get("border-%s-width" % side, "medium"), fs, None, 3) or 0
            line += max(0, extra)
    mx = max(mx, line)
    indent = length(block_style.get("text-indent", "0"), block_style.get("-font-px", 16), None, 0) or 0
    return mn, max(mn, mx + max(0, indent))


class TextFrag:
    __slots__ = ("x", "y", "width", "text", "font", "style", "ascent", "descent", "shift", "node")

    def __init__(self, x, text, width, font, style, node):
        self.x = x
        self.y = 0
        self.text = text
        self.width = width
        self.font = font
        self.style = style
        self.node = node
        self.shift = 0


class AtomFrag:
    def __init__(self, x, box):
        self.x = x
        self.y = 0
        self.box = box
        self.width = box.outer_width()


class LineBox:
    def __init__(self, x, y, left, right):
        self.x = x
        self.y = y
        self.left = left
        self.right = right
        self.frags = []
        self.spans = {}     # inline element -> [x1, x2]
        self.height = 0
        self.baseline = 0

    def translate(self, dx, dy):
        self.x += dx
        self.y += dy
        self.left += dx
        self.right += dx
        for f in self.frags:
            f.x += dx
            f.y += dy
            if isinstance(f, AtomFrag):
                f.box.translate(dx, dy)
        for el, span in self.spans.items():
            span[0] += dx
            span[1] += dx
            span[2] += dy
            span[3] += dy

    def paint(self, dl):
        for el, (x1, x2, y1, y2, first, last) in self.spans.items():
            s = el.style
            if s.get("visibility", "visible") != "visible":
                continue
            bg = color_of(s.get("background-color"))
            if not bg and "gradient(" in s.get("background-image", ""):
                bg = _gradient_color(s.get("background-image"))
            if bg:
                dl.add(paint.DrawRect(x1, y1, x2, y2, bg))
            fs = s.get("-font-px", 16)
            cur = color_of(s.get("color")) or "#000000"
            for side in SIDES:
                st = s.get("border-%s-style" % side, "none")
                if st in ("none", "hidden"):
                    continue
                if side == "left" and not first or side == "right" and not last:
                    continue
                bw = length(s.get("border-%s-width" % side, "medium"), fs, None, 3) or 0
                c = color_of(s.get("border-%s-color" % side, "currentcolor"), cur)
                if not bw or not c:
                    continue
                if side == "top":
                    dl.add(paint.DrawRect(x1, y1, x2, y1 + bw, c))
                elif side == "bottom":
                    dl.add(paint.DrawRect(x1, y2 - bw, x2, y2, c))
                elif side == "left":
                    dl.add(paint.DrawRect(x1, y1, x1 + bw, y2, c))
                else:
                    dl.add(paint.DrawRect(x2 - bw, y1, x2, y2, c))
        for f in self.frags:
            if isinstance(f, AtomFrag):
                f.box.paint(dl)
                link = f.box.style.get("-link-node")
                if link is not None:
                    x1, y1, x2, y2 = f.box.border_box()
                    dl.hits.append((x1, y1, x2, y2, link))
                continue
            s = f.style
            if s.get("visibility", "visible") != "visible":
                continue
            color = s.get("-text-color")
            if color is None:
                color = text_color(s, f.node)
                s["-text-color"] = color
            y = f.y
            shadows = s.get("-shadows")
            if shadows is None:
                shadows = _text_shadows(s.get("text-shadow", "none"), s, color)
                s["-shadows"] = shadows
            for sx, sy, scolor in shadows:
                dl.add(paint.DrawText(f.x + sx, y + sy, f.text, f.font, scolor, f.width))
            tcmd = paint.DrawText(f.x, y, f.text, f.font, color, f.width)
            tcmd.node = f.node          # for text selection
            dl.add(tcmd)
            deco = s.get("-deco")
            if deco is None:
                deco = _decorations(f.node)
                s["-deco"] = deco
            for kind, dcolor, dstyle, thick, offset, under in deco:
                dcolor = dcolor or color
                t = thick if thick else max(1, int(f.font.size_px / 14))
                if kind == "underline":
                    ly = y + f.font.ascent + max(1, f.font.descent // 2) + offset
                    if under:
                        ly = y + f.font.ascent + f.font.descent - t / 2 + offset
                elif kind == "line-through":
                    ly = y + f.font.ascent * 0.65
                else:
                    ly = y + 1
                _draw_decoration(dl, f.x, f.x + f.width, ly, dcolor, dstyle, t)
            link = s.get("-link-node")
            if link is not None:
                dl.hits.append((f.x, y, f.x + f.width, y + f.font.linespace, link))


def _decorations(text_node):
    """Text decorations propagate from ancestors (they are not inherited, but
    painted through). -> [(kind, colour, style, thickness, offset, under)]"""
    out = []
    node = text_node.parent if isinstance(text_node, Text) else text_node
    while node is not None and isinstance(node, Element):
        s = node.style
        d = s.get("text-decoration-line", s.get("text-decoration", "none"))
        if d not in ("none", "") and s.get("display") not in ATOMIC_INLINE:
            for kind in ("underline", "line-through", "overline"):
                if kind in d and kind not in [k[0] for k in out]:
                    c = s.get("text-decoration-color")
                    fs = s.get("-font-px", 16)
                    th = s.get("text-decoration-thickness", "auto")
                    thick = None
                    if th not in ("auto", "from-font", None):
                        thick = length(th, fs, fs, None)
                        thick = max(1, int(round(thick))) if thick else None
                    off = length(s.get("text-underline-offset", "auto"), fs, fs, 0) or 0
                    under = "under" in s.get("text-underline-position", "auto")
                    out.append((kind, color_of(c) if c and c != "currentcolor" else None,
                                s.get("text-decoration-style", "solid"), thick, off, under))
        if s.get("display") in BLOCK_LEVEL or s.get("display") in ATOMIC_INLINE or s.get("float", "none") != "none":
            # decorations of blocks do propagate to inline content; stop after this one
            if node.tag not in ("a",):
                break
        node = node.parent
    return out


def _draw_decoration(dl, x1, x2, y, color, style, t):
    if style == "wavy":
        amp, step = max(1.5, t * 1.2), max(3.0, t * 3)
        pts, x, up = [], x1, True
        while x < x2:
            pts += [x, y + (-amp if up else amp)]
            x += step
            up = not up
        pts += [x2, y]
        if len(pts) >= 4:
            dl.add(paint.DrawPolyline(pts, color, max(1, int(t))))
    elif style == "double":
        dl.add(paint.DrawLine(x1, y - t, x2, y - t, color, max(1, int(t))))
        dl.add(paint.DrawLine(x1, y + t, x2, y + t, color, max(1, int(t))))
    elif style in ("dotted", "dashed"):
        dash = (max(1, int(t)), max(2, int(t * 2))) if style == "dotted" else (max(3, int(t * 4)), max(2, int(t * 2)))
        dl.add(paint.DrawLine(x1, y, x2, y, color, max(1, int(t)), dash))
    else:
        dl.add(paint.DrawLine(x1, y, x2, y, color, max(1, int(t))))


def _text_shadows(value, style, color):
    """text-shadow -> [(dx, dy, colour)], drawn back to front. Blur is
    approximated by lightening the shadow colour."""
    if not value or value.strip() in ("none", "initial"):
        return []
    from .css_parser import _split_top_level
    out = []
    fs = style.get("-font-px", 16)
    for sh in _split_top_level(value, ","):
        nums, col = [], None
        for tok in css_split(sh.strip()):
            px = length(tok, fs, None, None)
            if px is not None and (tok[0].isdigit() or tok[0] in "-.+" or tok.startswith("calc")):
                nums.append(px)
            else:
                col = tok
        if len(nums) < 2:
            continue
        c = color_of(col, color) if col else color
        if not c:
            continue
        blur = nums[2] if len(nums) > 2 else 0
        if blur > 0:
            f = min(0.6, blur / 20)
            c = "#%02x%02x%02x" % tuple(int(int(c[i:i + 2], 16) * (1 - f) + 255 * f) for i in (1, 3, 5))
        out.append((nums[0], nums[1], c))
    out.reverse()
    return out


def _hyphen_split(word, avail, font, mode):
    """Break a word at a soft hyphen (or, with hyphens: auto, between
    letters) so that the first part plus '-' fits in `avail`."""
    plain_positions = []
    clean, k = "", 0
    for ch in word:
        if ch == "\xad":
            plain_positions.append(len(clean))
        else:
            clean += ch
    cands = list(plain_positions)
    if mode == "auto" and len(clean) >= 6 and clean.isalpha():
        vowels = "aeiouyAEIOUY"
        cands += [i for i in range(3, len(clean) - 2)
                  if clean[i - 1] in vowels and clean[i] not in vowels]
    best = None
    for i in sorted(set(cands)):
        if 0 < i < len(clean) and font.measure(clean[:i] + "-") <= avail:
            best = i
    if best is None:
        return None
    rest = word.replace("\xad", "")[best:] if not plain_positions else None
    if rest is None:   # keep the remaining soft hyphens in the rest
        count = 0
        for j, ch in enumerate(word):
            if ch != "\xad":
                count += 1
            if count == best:
                rest = word[j + 1:].lstrip("\xad")
                break
    return clean[:best] + "-", rest


def _line_clamp(style):
    v = style.get("-webkit-line-clamp") or style.get("line-clamp")
    if not v or v in ("none", "initial"):
        return None
    try:
        n = int(v)
    except ValueError:
        return None
    return n if n > 0 else None


def _ellipsize_line(line, limit):
    """End a line with "\u2026", cutting text so it fits before `limit`."""
    texts = [f for f in line.frags if isinstance(f, TextFrag)]
    if not texts:
        return
    f = texts[-1]
    ell = f.font.measure("\u2026")
    line.frags = line.frags[:line.frags.index(f) + 1]
    text = f.text
    while text and f.x + f.font.measure(text) + ell > limit:
        text = text[:-1]
    f.text = text.rstrip() + "\u2026"
    f.width = f.font.measure(f.text)


def _align_last(block):
    """text-align-last for the final line (and lines ended by <br>)."""
    last_mode = block.style.get("text-align-last", "auto")
    if last_mode in ("auto", None, "") or not block.lines:
        return
    targets = [l for l in block.lines if getattr(l, "forced", False)] + [block.lines[-1]]
    for line in targets:
        if not line.frags:
            continue
        if last_mode == "justify":
            _justify_line(line)
            continue
        left = min(f.x for f in line.frags)
        right = max(f.x + f.width for f in line.frags)
        free = (line.right - line.left) - (right - left)
        start = {"center": free / 2, "right": free, "end": free}.get(last_mode, 0)
        shift = line.left + start - left
        for f in line.frags:
            f.x += shift
            if isinstance(f, AtomFrag):
                f.box.translate(shift, 0)


class InlineLayout:
    def __init__(self, block, fc, positioned):
        self.block = block
        self.fc = fc
        self.positioned = positioned
        self.style = block.style
        self.strut_font = get_font(block.style)
        self.strut_lh = line_height_px(block.style, self.strut_font)
        self.lines = []
        self.line = None
        self.cursor = 0
        self.pending_space = False
        self.mergeable = False
        self.open = []          # stack of open inline elements
        self.y = block.y
        self.total_height = 0
        self.rtl = block.style.get("direction", "ltr") == "rtl"
        align = block.style.get("text-align", "start").lower()
        if align in ("-webkit-center", "-moz-center"):
            align = "center"
        if align in ("start", "match-parent", "-webkit-auto"):
            align = "right" if self.rtl else "left"
        elif align == "end":
            align = "left" if self.rtl else "right"
        self.align = align
        node = block.node
        if node is None and getattr(block, "anon_parent_node", None) is not None:
            node = block.anon_parent_node
        self.first_line = getattr(node, "first_line_style", None) if node is not None else None
        indent = length(block.style.get("text-indent", "0"), block.fs, block.width, 0) or 0
        self.indent = indent

    # -- line management ------------------------------------------------
    def new_line(self):
        if self.line is not None:
            self.finish_line()
        left, right = self.block.x, self.block.x + self.block.width
        if self.fc is not None and not self.fc.empty():
            left, right = self.fc.edges(self.y, self.strut_lh, left, right)
            # If floats leave no room at all, drop below them.
            guard = 0
            while right - left < min(40, self.block.width) and guard < 50:
                nb = self.fc.next_bottom(self.y)
                if nb is None:
                    break
                self.y = nb
                left, right = self.fc.edges(self.y, self.strut_lh, self.block.x, self.block.x + self.block.width)
                guard += 1
        self.line = LineBox(left, self.y, left, right)
        self.cursor = left + (self.indent if not self.lines else 0)
        self.pending_space = False
        self.mergeable = False
        for el in self.open:
            self._span_start(el, self.cursor, first=False)

    def _span_start(self, el, x, first):
        if _has_span_paint(el.style):
            self.line.spans[el] = [x, x, 0, 0, first, False]

    def _span_extend(self, x2):
        for el in self.open:
            span = self.line.spans.get(el)
            if span is not None:
                span[1] = max(span[1], x2)

    def finish_line(self):
        line = self.line
        self.lines.append(line)
        self.line = None
        # Vertical metrics: the strut plus every fragment.
        sf = self.strut_font
        half = (self.strut_lh - (sf.ascent + sf.descent)) / 2
        max_a = sf.ascent + half
        max_d = sf.descent + half
        has_content = False
        for f in line.frags:
            if isinstance(f, TextFrag):
                has_content = True
                font = f.font
                lh = line_height_px(f.style, font)
                hl = (lh - (font.ascent + font.descent)) / 2
                f.shift = _text_shift(f.node)
                f.ascent = font.ascent + hl - f.shift
                f.descent = font.descent + hl + f.shift
                max_a = max(max_a, f.ascent)
                max_d = max(max_d, f.descent)
            else:
                has_content = True
                h = f.box.outer_height()
                va = f.box.style.get("vertical-align", "baseline")
                if va == "middle":
                    xh = sf.ascent * 0.5
                    a, d = h / 2 + xh / 2, h / 2 - xh / 2
                elif va == "text-top":      # top aligned with the parent's font ascent
                    a, d = sf.ascent, max(0, h - sf.ascent)
                elif va == "text-bottom":   # bottom aligned with the parent's font descent
                    a, d = max(0, h - sf.descent), min(h, sf.descent)
                elif va == "top":
                    a, d = min(h, max_a), max(0, h - max_a)
                elif va == "bottom":
                    a, d = max(0, h - max_d), min(h, max_d)
                else:
                    a, d = h, 0
                    if f.box.inline_baseline() is not None:
                        a = f.box.inline_baseline()
                        d = h - a
                f.ascent, f.descent = a, d
                max_a = max(max_a, a)
                max_d = max(max_d, d)
        if not has_content and not line.spans:
            # empty line (e.g. only collapsed whitespace): no height
            if not getattr(line, "forced", False):
                self.lines.pop()
                return
        line.baseline = max_a
        line.height = max_a + max_d
        base = line.y + max_a
        if self.rtl and len(line.frags) > 1:
            _reorder_rtl(line)
        # Horizontal alignment.
        used = self.cursor - line.left
        free = line.right - line.left - used
        dx = 0
        if free > 0:
            if self.align == "center":
                dx = free / 2
            elif self.align in ("right", "end", "-webkit-right"):
                dx = free
        for f in line.frags:
            f.x += dx
            if isinstance(f, TextFrag):
                f.y = base - f.font.ascent + f.shift
            else:
                top = base - f.ascent
                f.box.translate(f.x - f.box.margin_left_edge(), top - f.box.margin_top_edge())
        for el, span in line.spans.items():
            span[0] += dx
            span[1] += dx
            s = el.style
            font = get_font(s)
            fs = s.get("-font-px", 16)
            pt = (length(s.get("padding-top", "0"), fs, None, 0) or 0) + _bw(s, "top")
            pb = (length(s.get("padding-bottom", "0"), fs, None, 0) or 0) + _bw(s, "bottom")
            span[2] = base - font.ascent - pt
            span[3] = base + font.descent + pb
        self.y = line.y + line.height
        self.total_height = self.y - self.block.y

    # -- content placement ---------------------------------------------
    def run(self, items):
        self.new_line()
        for kind, obj in items:
            if kind == "text":
                self.add_text(obj)
            elif kind == "open":
                self.open_inline(obj)
            elif kind == "close":
                self.close_inline(obj)
            elif kind == "br":
                self.line.forced = True
                self.new_line()
            elif kind == "atom":
                self.add_atom(obj)
            elif kind == "float":
                self.add_float(obj)
            elif kind == "abs":
                target = self.positioned or self.block.ctx.root_box
                queue_abs(target, obj, self.cursor, self.line.y, self.block)
        if self.line is not None:
            if self.line.frags or self.line.spans:
                self.finish_line()
            else:
                self.line = None
        self.total_height = self.y - self.block.y
        return self.lines

    def open_inline(self, el):
        s = el.style
        fs = s.get("-font-px", 16)
        ml = length(s.get("margin-left", "0"), fs, self.block.width, 0) or 0
        pl = (length(s.get("padding-left", "0"), fs, self.block.width, 0) or 0) + _bw(s, "left")
        if self.pending_space and self.line.frags:
            self.cursor += get_font(s).space_width
            self.pending_space = False
            # the line may still break here, even if the element's text is nowrap
            self.break_here = getattr(self, "space_breaks", True)
        self.cursor += ml
        self.open.append(el)
        self._span_start(el, self.cursor, first=True)
        self.cursor += pl
        self.mergeable = False
        if el not in self.block.ctx.positions:
            self.block.ctx.positions[el] = self.line.y

    def close_inline(self, el):
        s = el.style
        fs = s.get("-font-px", 16)
        mr = length(s.get("margin-right", "0"), fs, self.block.width, 0) or 0
        pr = (length(s.get("padding-right", "0"), fs, self.block.width, 0) or 0) + _bw(s, "right")
        self.cursor += pr
        span = self.line.spans.get(el)
        if span is not None:
            span[1] = max(span[1], self.cursor)
            span[5] = True
        if el in self.open:
            self.open.remove(el)
        self.cursor += mr
        self.mergeable = False

    def add_text(self, node):
        style = node.style
        font = get_font(style)
        text = transform_text(node.text, style)
        ws = style.get("white-space", "normal")
        if ws in ("pre", "pre-wrap", "break-spaces", "pre-line"):
            self.add_pre_text(node, text, font, ws)
            return
        nowrap = ws == "nowrap"
        fl = self.first_line
        caps = small_caps(style)
        if getattr(self.block, "upright", False):
            for ch in text:
                if ch.strip():
                    if self.line.frags:
                        self.new_line()
                    self.place_word(ch, font, style, node, True)
            return
        for tok in TOKEN_RE.findall(text):
            if tok[0] in " \t\n\r\f":
                self.pending_space = True
                self.space_breaks = not nowrap   # a space outside nowrap text is a break opportunity
                continue
            if caps:
                self.place_small_caps(tok, style, node, nowrap)
                continue
            if fl is not None and not self.lines:
                st = _first_line_style(style, fl)     # ::first-line applies to this word
                ff = get_font(st)
                line = self.line
                space = ff.space_width if (self.pending_space and line.frags) else 0
                if not nowrap and line.frags and self.cursor + space + ff.measure(tok) > line.right + 0.5:
                    self.new_line()                    # it wraps: it belongs to the second line
                    self.place_word(tok, font, style, node, nowrap)
                else:
                    self.place_word(tok, ff, st, node, nowrap)
            else:
                self.place_word(tok, font, style, node, nowrap)

    def add_pre_text(self, node, text, font, ws):
        style = node.style
        text = text.replace("\r\n", "\n")
        if "\t" in text:
            ts = style.get("tab-size", "8").strip()
            try:
                n = int(float(ts))
            except ValueError:
                px = length(ts, style.get("-font-px", 16), None, None)
                n = max(1, int(round(px / max(1, font.space_width)))) if px else 8
            text = "\n".join(t.expandtabs(max(0, n)) for t in text.split("\n"))
        parts = text.split("\n")
        for i, part in enumerate(parts):
            if i > 0:
                self.line.forced = True
                self.new_line()
            if not part:
                continue
            if ws == "pre":
                self.pending_space = False
                self.place_word(part, font, style, node, True, keep_spaces=True)
            elif ws == "pre-line":
                for tok in TOKEN_RE.findall(part):
                    if tok[0] in " \t":
                        self.pending_space = True
                    else:
                        self.place_word(tok, font, style, node, False)
            else:
                for tok in TOKEN_RE.findall(part):
                    self.place_word(tok, font, style, node, False, keep_spaces=True)

    def place_word(self, word, font, style, node, nowrap, keep_spaces=False):
        original = word
        if "\xad" in word:
            word = word.replace("\xad", "")     # soft hyphens are invisible unless the word breaks there
        w = font.measure(word)
        ls = _spacing(style, "letter-spacing")
        wsp = _spacing(style, "word-spacing")
        if ls:
            w += ls * len(word)
        if not nowrap and not keep_spaces and len(word) > 1 and w > self.line.right - self.line.left + 0.5 \
                and _breaks_words(style):
            self.place_long_word(word, font, style, node)
            return
        line = self.line
        has_content = bool(line.frags)
        space = font.space_width + wsp if (self.pending_space and has_content) else 0
        breakable = not nowrap or (self.pending_space and getattr(self, "space_breaks", True)) or \
            getattr(self, "break_here", False)
        self.break_here = False
        if not has_content and not nowrap and not keep_spaces and w > line.right - self.cursor + 0.5 and \
                ("\xad" in original or style.get("hyphens") == "auto") and style.get("hyphens", "manual") != "none":
            split = _hyphen_split(original, line.right - self.cursor, font, style.get("hyphens", "manual"))
            if split is not None:      # a word too long for an empty line: hyphenate it
                head, rest = split
                self.place_word(head, font, style, node, True)
                self.new_line()
                self.place_word(rest, font, style, node, nowrap)
                return
        if has_content and breakable and self.cursor + space + w > line.right + 0.5:
            hy = style.get("hyphens", "manual")
            if hy != "none" and not keep_spaces and not ls and ("\xad" in original or hy == "auto"):
                split = _hyphen_split(original, line.right - self.cursor - space, font, hy)
                if split is not None:
                    head, rest = split
                    self.place_word(head, font, style, node, True)
                    self.new_line()
                    self.place_word(rest, font, style, node, nowrap)
                    return
            if not (keep_spaces and word.isspace()):
                self.new_line()
                line = self.line
                space = 0
            else:
                return
        x = self.cursor + space
        last = line.frags[-1] if line.frags else None
        if ls:
            # letter-spacing: one fragment per character, spaced apart
            cx = x
            for ch in word:
                cw = font.measure(ch)
                line.frags.append(TextFrag(cx, ch, cw, font, style, node))
                cx += cw + ls
            self.cursor = x + w
            self.pending_space = False
            self.mergeable = False
            self._span_extend(self.cursor)
            return
        owner = getattr(node, "parent", None)
        ost = owner.style if owner is not None and hasattr(owner, "style") else style
        if ost.get("unicode-bidi") == "bidi-override" and ost.get("direction") == "rtl":
            word = word[::-1]              # <bdo dir=rtl>: characters drawn in reverse
        if self.mergeable and isinstance(last, TextFrag) and last.style is style and last.font is font \
                and not (wsp and space) and not self.rtl:
            last.text += (" " if space else "") + word
            last.width = x + w - last.x
        else:
            if space and isinstance(last, TextFrag) and self.mergeable is False and not self.open_spans_started_here():
                pass
            line.frags.append(TextFrag(x, word, w, font, style, node))
        self.cursor = x + w
        self.pending_space = False
        self.mergeable = True
        self._span_extend(self.cursor)

    def place_small_caps(self, word, style, node, nowrap):
        """font-variant: small-caps: lowercase letters drawn as smaller capitals."""
        big = get_font(style)
        small = get_font(style, 0.8)
        runs = re.findall(r"[a-z\u00df-\u00ff]+|[^a-z\u00df-\u00ff]+", word)
        first = True
        for run in runs:
            low = run == run.lower() and run != run.upper()
            self.place_word(run.upper() if low else run, small if low else big, style, node, nowrap or not first)
            first = False
            self.pending_space = False

    def place_long_word(self, word, font, style, node):
        """overflow-wrap: break-word/anywhere, word-break: break-all: a word
        wider than the line is cut into pieces that fit."""
        rest = word
        while rest:
            line = self.line
            space = font.space_width if (self.pending_space and line.frags) else 0
            avail = line.right - self.cursor - space
            if avail < font.measure(rest[0]) and line.frags:
                self.new_line()
                continue
            lo, hi = 1, len(rest)
            while lo < hi:          # longest prefix that fits
                mid = (lo + hi + 1) // 2
                if font.measure(rest[:mid]) <= avail:
                    lo = mid
                else:
                    hi = mid - 1
            piece, rest = rest[:lo], rest[lo:]
            self.place_word(piece, font, style, node, True)
            if rest:
                self.new_line()

    def open_spans_started_here(self):
        return False

    def add_atom(self, box):
        box.layout(0, 0, self.block.width, FloatContext(), shrink=True, positioned=self.positioned)
        w = box.outer_width()
        line = self.line
        space = self.strut_font.space_width if (self.pending_space and line.frags) else 0
        if line.frags and self.cursor + space + w > line.right + 0.5 and self.block.style.get("white-space") != "nowrap":
            self.new_line()
            line = self.line
            space = 0
        frag = AtomFrag(self.cursor + space, box)
        line.frags.append(frag)
        self.cursor += space + w
        self.pending_space = False
        self.mergeable = False
        self._span_extend(self.cursor)

    def add_float(self, box):
        y = self.line.y
        place_float(box, self.fc, self.block.x, self.block.width, y, self.positioned)
        self.block.children.append(box)
        # Re-evaluate the current line's space if it is still empty.
        if not self.line.frags and self.fc is not None:
            left, right = self.fc.edges(self.line.y, self.strut_lh, self.block.x, self.block.x + self.block.width)
            self.line.left = self.line.x = left
            self.line.right = right
            self.cursor = left + (self.indent if not self.lines else 0)


def _text_shift(text_node):
    """Baseline shift from vertical-align: sub/super/length on enclosing inline elements."""
    shift = 0.0
    node = text_node.parent
    while isinstance(node, Element):
        s = node.style
        if s.get("display", "inline") != "inline":
            break
        va = s.get("vertical-align", "baseline")
        parent_fs = node.parent.style.get("-font-px", 16) if isinstance(node.parent, Element) else 16
        if va == "super":
            shift -= parent_fs * 0.35
        elif va == "sub":
            shift += parent_fs * 0.2
        elif va not in ("baseline", "middle", "top", "bottom", "text-top", "text-bottom"):
            v = length(va, s.get("-font-px", 16), s.get("-font-px", 16), None)
            if v:
                shift -= v
        node = node.parent
    return shift


def _bw(style, side):
    if style.get("border-%s-style" % side, "none") in ("none", "hidden"):
        return 0
    return length(style.get("border-%s-width" % side, "medium"), style.get("-font-px", 16), None, 3) or 0


def _has_span_paint(style):
    if color_of(style.get("background-color")):
        return True
    if "gradient(" in style.get("background-image", ""):
        return True
    for side in SIDES:
        if style.get("border-%s-style" % side, "none") not in ("none", "hidden"):
            return True
    return False


def _inline_baseline_default(self):
    return None


Box.inline_baseline = _inline_baseline_default


def _block_inline_baseline(self):
    """Baseline of an inline-block, relative to its margin-box top."""
    if self.style.get("overflow", "visible") != "visible":
        return None
    b = self.first_baseline()
    if b is None:
        return None
    return b - self.margin_top_edge()


BlockBox.inline_baseline = _block_inline_baseline


# ---------------------------------------------------------------------------
# Replaced elements


class ImageBox(Box):
    is_atomic = True

    def __init__(self, node, style, ctx):
        super().__init__(node, style, ctx)
        src = node.attributes.get("src") or node.attributes.get("data-src") or ""
        srcset = node.attributes.get("srcset")
        if not src and srcset:
            src = srcset.split(",")[0].strip().split(" ")[0]
        self.url = ctx.resolve(src) if src else None
        self.image = ctx.images.get(self.url) if self.url else None
        self.alt = node.attributes.get("alt", "")

    def natural_size(self):
        if self.image is not None:
            return self.image.size
        if self.alt:
            font = get_font(self.style)
            return (font.measure(self.alt) + 4, font.linespace + 4)
        return (0, 0)

    def compute_size(self, cb_w):
        nw, nh = self.natural_size()
        w = self.content_width_from_spec(cb_w)
        h = self.specified("height", None)
        if h is not None and self.style.get("box-sizing") == "border-box":
            h -= self.vert_extra()
        ratio = _aspect_ratio(self.style)
        if ratio and w is not None and h is None:
            h = w / ratio
        elif ratio and h is not None and w is None:
            w = h * ratio
        if w is None and h is None:
            w, h = nw, nh
        elif w is None:
            w = nw * h / nh if nh else 0
        elif h is None:
            h = nh * w / nw if nw else (0 if self.image is not None else 0)
        mx = self.specified("max-width", cb_w)
        if mx is not None and w > mx and w > 0:
            h = h * mx / w
            w = mx
        mxh = self.specified("max-height", None)
        if mxh is not None and h > mxh and h > 0:
            w = w * mxh / h
            h = mxh
        return max(0.0, w), max(0.0, h)

    def intrinsic(self):
        self.compute_edges(0)
        w, _ = self.compute_size(None)
        if "%" in self.style.get("width", "") or "%" in self.style.get("max-width", ""):
            mn = 0
        else:
            mn = w
        extra = self.horiz_extra() + self.m[L] + self.m[R]
        return (mn + extra, w + extra)

    def layout(self, cb_x, y, cb_w, fc, forced_outer=None, shrink=False, positioned=None):
        self.compute_edges(cb_w)
        w, h = self.compute_size(cb_w)
        if forced_outer is not None:
            neww = forced_outer - self.horiz_extra() - self.m[L] - self.m[R]
            if w:
                h = h * neww / w
            w = neww
        if self.style.get("display") in BLOCK_LEVEL and not shrink and forced_outer is None:
            free = cb_w - w - self.horiz_extra()
            if self.m_auto[L] and self.m_auto[R]:
                self.m[L] = self.m[R] = max(0, free / 2)
        self.width, self.height = w, h
        self.x = cb_x + self.m[L] + self.b[L] + self.p[L]
        self.y = y + self.m[T] + self.b[T] + self.p[T]

    def paint(self, dl):
        if self.hidden_entirely() or _defer_positioned(self, dl) or _paint_transformed(self, dl):
            return
        if self.visible():
            paint_outline(self, dl)
        if self.visible():
            self.paint_background(dl)
            if self.image is not None and self.width >= 1 and self.height >= 1:
                fit = self.style.get("object-fit", "fill")
                if fit in ("cover", "contain") and self.image.size[0] and self.image.size[1]:
                    iw, ih = self.image.size
                    scale = (max if fit == "cover" else min)(self.width / iw, self.height / ih)
                    dw, dh = iw * scale, ih * scale
                    cmd = paint.DrawImage(self.x + (self.width - dw) / 2, self.y + (self.height - dh) / 2,
                                          dw, dh, self.image, self.url)
                    cmd = cmd.clipped((self.x, self.y, self.x + self.width, self.y + self.height))
                    if cmd:
                        dl.add(cmd)
                else:
                    dl.add(paint.DrawImage(self.x, self.y, self.width, self.height, self.image, self.url))
            elif self.alt and self.width > 0:
                font = get_font(self.style)
                dl.add(paint.DrawRect(self.x, self.y, self.x + self.width, self.y + self.height, None, outline="#bbbbbb"))
                text = paint.DrawText(self.x + 2, self.y + 2, self.alt, font, "#666666")
                c = text.clipped((self.x, self.y, self.x + self.width, self.y + self.height + 4))
                if c:
                    dl.add(c)
        link = self.style.get("-link-node")
        target = link if link is not None else (self.node if self.node.tag == "input" else None)
        if target is not None:
            x1, y1, x2, y2 = self.border_box()
            dl.hits.append((x1, y1, x2, y2, target))


class SvgBox(ImageBox):
    """Inline <svg> element, rasterized by our SVG renderer."""

    def __init__(self, node, style, ctx):
        Box.__init__(self, node, style, ctx)
        self.url = "inline-svg:%d" % id(node)
        self.alt = ""
        try:
            from .svg import SvgImage
            doc = node
            while doc.parent is not None:
                doc = doc.parent
            self.image = SvgImage(node, color_of(style.get("color")) or "#000000", doc)
        except Exception:
            self.image = None

    def natural_size(self):
        return self.image.size if self.image is not None else (0, 0)

    def _sizeless(self):
        """<svg> with neither width/height attributes nor a CSS width or height."""
        return self.image is not None and not self.image.has_explicit_size and             self.style.get("width", "auto") == "auto" and self.style.get("height", "auto") == "auto"

    def compute_size(self, cb_w):
        if self._sizeless() and cb_w:
            # CSS default sizing: as wide as the containing block, height from the viewBox
            nw, nh = self.natural_size()
            w = cb_w
            mx = self.specified("max-width", cb_w)
            if mx is not None:
                w = min(w, mx)
            return w, (w * nh / nw if nw else 150.0)
        return super().compute_size(cb_w)

    def intrinsic(self):
        if self._sizeless():
            self.compute_edges(0)
            extra = self.horiz_extra() + self.m[L] + self.m[R]
            return (extra, 300 + extra)     # the default object size is 300px wide
        return super().intrinsic()


class ControlBox(Box):
    """<input>, <select>, <textarea>: sized widgets drawn by the browser."""
    is_atomic = True

    def __init__(self, node, style, ctx):
        super().__init__(node, style, ctx)
        self.kind = node.tag
        self.type = node.attributes.get("type", "text").lower() if node.tag == "input" else node.tag

    def label(self):
        n = self.node
        if self.kind == "input":
            if self.type in ("submit", "button", "reset"):
                return n.attributes.get("value", {"submit": "Submit", "reset": "Reset"}.get(self.type, ""))
            if self.type in ("checkbox", "radio"):
                return ""
            if n.form_value is None:
                n.form_value = n.attributes.get("value", "")
            if self.type == "password":
                return "•" * len(n.form_value)
            return n.form_value
        if self.kind == "select":
            opt = selected_option(n)
            arrow = "" if _appearance_none(self.style) else "  ▾"
            return (opt.text_content().strip() if opt is not None else "") + arrow
        if self.kind == "textarea":
            if n.form_value is None:
                n.form_value = n.text_content()
            return n.form_value
        return ""

    def natural_size(self):
        font = get_font(self.style)
        if self.type in ("checkbox", "radio"):
            return 13, 13
        if self.type == "hidden":
            return 0, 0
        text_h = font.linespace
        if self.kind == "select":
            opts = [o.text_content().strip() for o in self.node.get_elements_by_tag("option")]
            w = max([font.measure(o) for o in opts] or [40]) + font.measure("  ▾") + 4
            return w, text_h
        if self.kind == "textarea":
            cols = _int(self.node.attributes.get("cols"), 30)
            rows = _int(self.node.attributes.get("rows"), 2)
            return font.measure("m") * cols * 0.6, text_h * rows
        if self.type in ("submit", "button", "reset"):
            return font.measure(self.label()), text_h
        size = _int(self.node.attributes.get("size"), 20)
        return font.measure("0") * size, text_h

    def compute_size(self, cb_w):
        nw, nh = self.natural_size()
        w = self.content_width_from_spec(cb_w)
        if w is None or self.type in ("submit", "button", "reset") and self.style.get("width") == "auto":
            w = nw
        if self.type not in ("checkbox", "radio", "submit", "button", "reset") and self.style.get("width") in ("auto",):
            w = nw
        h = self.specified("height", None)
        if h is not None and self.style.get("box-sizing") == "border-box":
            h -= self.vert_extra()
        if h is None:
            h = nh
        if self.kind == "textarea" and self.style.get("height") not in ("auto",):
            pass
        w = self.clamp_width(w, cb_w)
        return max(0.0, w), max(0.0, h)

    def intrinsic(self):
        self.compute_edges(0)
        w, _ = self.compute_size(None)
        extra = self.horiz_extra() + self.m[L] + self.m[R]
        return (w + extra, w + extra)

    def layout(self, cb_x, y, cb_w, fc, forced_outer=None, shrink=False, positioned=None):
        self.compute_edges(cb_w)
        w, h = self.compute_size(cb_w)
        if forced_outer is not None:
            w = forced_outer - self.horiz_extra() - self.m[L] - self.m[R]
        elif self.style.get("display") in BLOCK_LEVEL and not shrink and self.style.get("width") == "auto" \
                and self.type not in ("checkbox", "radio"):
            w = cb_w - self.horiz_extra() - self.m[L] - self.m[R]
        self.width, self.height = max(0.0, w), h
        self.x = cb_x + self.m[L] + self.b[L] + self.p[L]
        self.y = y + self.m[T] + self.b[T] + self.p[T]

    def inline_baseline(self):
        font = get_font(self.style)
        if self.type in ("checkbox", "radio"):
            return self.outer_height() - 3
        return self.m[T] + self.b[T] + self.p[T] + font.ascent

    def paint(self, dl):
        if self.type == "hidden" or self.hidden_entirely() or not self.visible() or _defer_positioned(self, dl)                 or _paint_transformed(self, dl):
            return
        paint_outline(self, dl)
        self.paint_background(dl)
        x1, y1, x2, y2 = self.border_box()
        font = get_font(self.style)
        color = color_of(self.style.get("color")) or "#000000"
        native = not _appearance_none(self.style)   # appearance: none -> only the CSS styling
        accent = self.style.get("accent-color", "auto")
        accent = color_of(accent) if accent not in ("auto", None) else None
        if self.type == "checkbox" and native:
            # the native box (author borders and backgrounds don't apply to it, as in Chrome)
            dl.add(paint.DrawRect(x1, y1, x2 - 1, y2 - 1, "#ffffff", outline=accent or "#555555"))
            if self.node.checked:
                mark = "#000000"
                if accent:
                    dl.add(paint.DrawRect(x1, y1, x2, y2, accent))
                    mark = "#ffffff"
                dl.add(paint.DrawLine(x1 + 2, (y1 + y2) / 2, (x1 + x2) / 2 - 1, y2 - 3, mark, 2))
                dl.add(paint.DrawLine((x1 + x2) / 2 - 1, y2 - 3, x2 - 2, y1 + 2, mark, 2))
        elif self.type in ("checkbox", "radio") and not native:
            pass
        elif self.type == "radio":
            dl.add(paint.DrawOval(x1, y1, x2, y2, "#ffffff", outline=accent or "#555555"))
            if self.node.checked:
                dl.add(paint.DrawOval(x1 + 3, y1 + 3, x2 - 3, y2 - 3, accent or "#000000"))
        else:
            label = self.label()
            clip = (self.x - self.p[L], self.y - 2, self.x + self.width + self.p[R], self.y + self.height + 4)
            if not label and self.kind in ("input", "textarea"):
                ph = self.node.attributes.get("placeholder", "")
                if ph:
                    pst = getattr(self.node, "placeholder_style", None)
                    pcol = (color_of(pst.get("color")) if pst else None) or "#8a8a8a"
                    pfont = get_font(pst) if pst else font
                    cmd = paint.DrawText(self.x, self.y, ph, pfont, pcol).clipped(clip)
                    if cmd:
                        dl.add(cmd)
            if self.kind == "textarea":
                for i, ln in enumerate(label.split("\n")):
                    cmd = paint.DrawText(self.x, self.y + i * font.linespace, ln, font, color).clipped(clip)
                    if cmd:
                        dl.add(cmd)
            elif label:
                tx, ty = self.x, self.y
                if self.type in ("submit", "button", "reset") or self.kind in ("button", "select"):
                    tx = self.x + max(0, (self.width - font.measure(label)) / 2) if self.kind != "select" else tx
                    ty = self.y + (self.height - font.linespace) / 2      # buttons centre their text
                elif self.kind == "input":
                    ty = self.y + max(0, (self.height - font.linespace) / 2)
                cmd = paint.DrawText(tx, ty, label, font, color).clipped(
                    (clip[0], min(clip[1], ty), clip[2], max(clip[3], ty + font.linespace)))
                if cmd:
                    dl.add(cmd)
            if self.ctx.focused is self.node and self.kind in ("input", "textarea"):
                last = label.split("\n")[-1]
                lines = label.count("\n")
                cx = self.x + font.measure(last) + 1
                cy = self.y + lines * font.linespace
                caret = self.style.get("caret-color", "auto")
                caret = (color_of(caret, color) if caret not in ("auto", None) else color) or "#000000"
                if caret != "transparent":
                    dl.add(paint.DrawLine(cx, cy, cx, cy + font.linespace, caret))
        dl.hits.append((x1, y1, x2, y2, self.node))
        paint_resize_grip(self, dl)


def selected_option(select):
    opts = select.get_elements_by_tag("option")
    if not opts:
        return None
    if select.form_value is not None:
        for o in opts:
            if o is select.form_value:
                return o
    for o in opts:
        if "selected" in o.attributes:
            return o
    return opts[0]


def _int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Tables


class TableBox(BlockBox):
    def __init__(self, node, style, ctx):
        Box.__init__(self, node, style, ctx)
        self.lines = []
        self.inline_items = None
        self.marker = None
        self.captions = []
        self.col_specs = []  # [(width value, <col> element)] per column from <col>/<colgroup>
        self.rows = []       # list of (row_node_or_None, [cell boxes])
        self._build()

    def _build(self):
        def add_row(row_el):
            cells = []
            for c in row_el.children:
                if isinstance(c, Element) and c.style.get("display") != "none":
                    cells.append(self._cell_box(c))
                elif isinstance(c, Text) and c.text.strip():
                    cells.append(BlockBox(None, anon_style(row_el.style), self.ctx, items=[("text", c)]))
            self.rows.append((row_el, cells))

        loose = []
        for child in self.node.children:
            if not isinstance(child, Element):
                if isinstance(child, Text) and child.text.strip():
                    loose.append(child)
                continue
            d = child.style.get("display")
            if d == "none" and child.tag not in ("col", "colgroup"):
                continue
            if d == "table-caption" or child.tag == "caption":
                self.captions.append(BlockBox(child, child.style, self.ctx))
            elif d in ("table-row-group", "table-header-group", "table-footer-group"):
                for r in child.children:
                    if isinstance(r, Element) and r.style.get("display") != "none":
                        if r.style.get("display") == "table-row" or r.tag == "tr":
                            add_row(r)
                        else:
                            self.rows.append((None, [self._cell_box(r)]))
            elif d == "table-row":
                add_row(child)
            elif d in ("table-column", "table-column-group") or child.tag in ("col", "colgroup"):
                cols = [child] if child.tag == "col" else [c for c in child.children
                                                           if isinstance(c, Element) and c.tag == "col"]
                if child.tag == "colgroup" and not cols:
                    cols = [child]
                for col in cols:
                    span = max(1, _int(col.attributes.get("span"), 1))
                    w = col.style.get("width", "auto")
                    if w == "auto" and col.attributes.get("width"):
                        w = col.attributes["width"] + ("" if col.attributes["width"].endswith("%") else "px")
                    self.col_specs += [(w, col)] * span
                continue
            else:
                # a table cell (or anything else) directly in the table: implicit row
                if self.rows and self.rows[-1][0] is None:
                    self.rows[-1][1].append(self._cell_box(child))
                else:
                    self.rows.append((None, [self._cell_box(child)]))
        self.children = [c for _, cells in self.rows for c in cells] + self.captions

    def _cell_box(self, el):
        box = build_box(el, self.ctx)
        if not isinstance(box, BlockBox):
            wrapper = BlockBox(None, anon_style(el.style), self.ctx, items=[("atom", box)])
            wrapper.table_cell = True
            return wrapper
        box.colspan = max(1, _int(el.attributes.get("colspan"), 1))
        box.rowspan = max(0, _int(el.attributes.get("rowspan"), 1))   # 0: to the end of the table
        return box

    def cell_positions(self):
        """Rows of (cell, first column, colspan, rowspan): cells skip the
        columns that rowspans from earlier rows still occupy."""
        if getattr(self, "_cellpos", None) is not None:
            return self._cellpos
        occupied = set()
        out = []
        nrows = len(self.rows)
        for r, (_, cells) in enumerate(self.rows):
            col = 0
            row = []
            for c in cells:
                while (r, col) in occupied:
                    col += 1
                span = getattr(c, "colspan", 1)
                rs = getattr(c, "rowspan", 1)
                rs = nrows - r if rs == 0 else max(1, min(rs, nrows - r))
                row.append((c, col, span, rs))
                for i in range(rs):
                    for j in range(span):
                        occupied.add((r + i, col + j))
                col += span
            out.append(row)
        self._cellpos = out
        self._ncols = max([col + 1 for _, col in occupied] or [0])
        return out

    def spacing(self):
        if self.style.get("border-collapse") == "collapse":
            return 0.0
        v = self.style.get("border-spacing", "2px").split()[0]
        return length(v, self.fs, None, 2) or 0

    def fixed_column_widths(self, n, inner):
        """table-layout: fixed: widths come from <col> elements and the first
        row's cells only; columns without a width share what is left."""
        widths = [None] * n
        for i, (w, _col) in enumerate(self.col_specs[:n]):
            widths[i] = length(w, self.fs, inner, None)
        first = self.cell_positions()[0] if self.rows else []
        for c, col, span, _ in first:
            if col < n and widths[col] is None:
                v = c.style.get("width", "auto")
                px = length(v, c.fs, inner, None)
                if px is not None:
                    c.compute_edges(inner)
                    if c.style.get("box-sizing") != "border-box":
                        px += c.horiz_extra()
                    per = px / span
                    for k in range(col, min(n, col + span)):
                        if widths[k] is None:
                            widths[k] = per
        used = sum(w for w in widths if w is not None)
        free = [i for i in range(n) if widths[i] is None]
        rest = max(0.0, inner - used)
        for i in free:
            widths[i] = rest / len(free)
        total = sum(widths)
        if total < inner and not free and total > 0:
            widths = [w * inner / total for w in widths]
        return widths

    def column_widths_intrinsic(self):
        positions = self.cell_positions()
        ncols = self._ncols
        mins = [0.0] * ncols
        maxs = [0.0] * ncols
        fixed = [None] * ncols
        pcts = [None] * ncols       # width="25%" on cells / <col>
        spans = []
        for row in positions:
            for c, col, span, _ in row:
                mn, mx = c.intrinsic()
                if span == 1:
                    mins[col] = max(mins[col], mn)
                    maxs[col] = max(maxs[col], mx)
                    spec = c.style.get("width", "auto")
                    if spec.endswith("%"):
                        try:
                            pcts[col] = max(pcts[col] or 0, float(spec[:-1]) / 100)
                        except ValueError:
                            pass
                    if spec.endswith("px"):
                        c.compute_edges(0)
                        px = length(spec, c.fs, None, None)
                        if px is not None:
                            if c.style.get("box-sizing") != "border-box":
                                px += c.horiz_extra()
                            fixed[col] = max(fixed[col] or 0, px)
                else:
                    spans.append((col, span, mn, mx))
        for i, (w, _col) in enumerate(self.col_specs[:ncols]):
            px = length(w, self.fs, None, None) if not w.endswith("%") else None
            if px is not None:
                fixed[i] = max(fixed[i] or 0, px)
            elif w.endswith("%"):
                try:
                    pcts[i] = float(w[:-1]) / 100
                except ValueError:
                    pass
        self.pcts = pcts
        for i in range(ncols):
            if fixed[i] is not None:
                maxs[i] = max(mins[i], fixed[i])
                mins[i] = max(mins[i], min(fixed[i], maxs[i]))
        sp = self.spacing()
        for col, span, mn, mx in spans:
            cur_mn = sum(mins[col:col + span]) + sp * (span - 1)
            cur_mx = sum(maxs[col:col + span]) + sp * (span - 1)
            if mn > cur_mn:
                add = (mn - cur_mn) / span
                for i in range(col, col + span):
                    mins[i] += add
            if mx > cur_mx:
                add = (mx - cur_mx) / span
                for i in range(col, col + span):
                    maxs[i] += add
        for i in range(ncols):
            maxs[i] = max(maxs[i], mins[i])
        return mins, maxs

    def intrinsic(self):
        if self._intrinsic is not None:
            return self._intrinsic
        self._intrinsic = (0, 0)
        self.compute_edges(0)
        mins, maxs = self.column_widths_intrinsic()
        sp = self.spacing() * (len(mins) + 1)
        extra = self.horiz_extra() + self.m[L] + self.m[R]
        spec = self.content_width_from_spec(None)
        if spec is not None:
            mn = max(spec, sum(mins) + sp)
            self._intrinsic = (mn + extra, mn + extra)
        else:
            self._intrinsic = (sum(mins) + sp + extra, max(sum(mins), self.preferred_width(mins, maxs)) + sp + extra)
        return self._intrinsic

    def preferred_width(self, mins, maxs):
        """Max-content width of the columns, grown so that percentage columns
        get their share (CSS 2.1 automatic table layout)."""
        pcts = getattr(self, "pcts", None) or [None] * len(maxs)
        want = sum(maxs)
        total_p = sum(p for p in pcts if p)
        for mx, p in zip(maxs, pcts):
            if p:
                want = max(want, mx / p)
        rest = sum(mx for mx, p in zip(maxs, pcts) if not p)
        if 0 < total_p < 1 and rest:
            want = max(want, rest / (1 - total_p))
        return want

    def layout(self, cb_x, y, cb_w, fc, forced_outer=None, shrink=False, positioned=None):
        self.abs_boxes = []
        self.row_bgs = []
        self.compute_edges(cb_w)
        horiz = self.horiz_extra()
        mins, maxs = self.column_widths_intrinsic()
        n = len(mins)
        sp = self.spacing()
        total_sp = sp * (n + 1) if n else 0
        spec = self.content_width_from_spec(cb_w)
        avail = cb_w - self.m[L] - self.m[R] - horiz
        if forced_outer is not None:
            tw = forced_outer - self.m[L] - self.m[R] - horiz
        elif spec is not None:
            tw = max(spec, sum(mins) + total_sp)
        else:
            tw = min(avail, self.preferred_width(mins, maxs) + total_sp)
            tw = max(tw, sum(mins) + total_sp)
        tw = self.clamp_width(tw, cb_w)
        # Distribute width to columns.
        inner = tw - total_sp
        smin, smax = sum(mins), sum(maxs)
        fixed_layout = self.style.get("table-layout", "auto") == "fixed" and \
            (spec is not None or forced_outer is not None) and n > 0
        pcts = getattr(self, "pcts", None) or [None] * n
        if fixed_layout:
            widths = self.fixed_column_widths(n, inner)
        elif n == 0:
            widths = []
        elif any(pcts) and inner >= smin:
            # percentage columns take their share, the others split the rest
            widths = [max(mn, p * inner) if p else None for mn, p in zip(mins, pcts)]
            rest = inner - sum(w for w in widths if w is not None)
            others = [i for i in range(n) if widths[i] is None]
            omax = sum(maxs[i] for i in others)
            for i in others:
                share = rest * maxs[i] / omax if omax > 0 else rest / len(others)
                widths[i] = max(mins[i], share)
            if not others and sum(widths) < inner:
                widths = [w * inner / sum(widths) for w in widths] if sum(widths) else widths
        elif inner >= smax:
            extra = inner - smax
            if smax > 0:
                widths = [mx + extra * mx / smax for mx in maxs]
            else:
                widths = [inner / n] * n
        elif inner <= smin:
            widths = list(mins)
        else:
            f = (inner - smin) / (smax - smin) if smax > smin else 0
            widths = [mn + (mx - mn) * f for mn, mx in zip(mins, maxs)]
        self.width = tw
        if forced_outer is None and not shrink:
            free = cb_w - tw - horiz
            if self.m_auto[L] and self.m_auto[R]:
                self.m[L] = self.m[R] = max(0, free / 2)
            elif self.m_auto[L]:
                self.m[L] = max(0, free - self.m[R])
            elif free > 0 and self.style.get("text-align") in CENTERING:
                self.m[L] += free / 2       # tables inside <center> or align="center"
        self.x = cb_x + self.m[L] + self.b[L] + self.p[L]
        self.y = y + self.m[T] + self.b[T] + self.p[T]
        if self.node not in self.ctx.positions:
            self.ctx.positions[self.node] = y + self.m[T]
        if self.establishes_positioning():
            positioned = self
        cursor = self.y
        bottom_caps = [c for c in self.captions if c.style.get("caption-side", "top") == "bottom"]
        for cap in self.captions:
            if cap in bottom_caps:
                continue
            cap.layout(self.x, cursor, tw, FloatContext(), positioned=positioned)
            cursor += cap.outer_height()
        cursor += sp
        col_x = []
        xx = self.x + sp
        for w in widths:
            col_x.append(xx)
            xx += w + sp
        # Lay every cell out at its width (at y=0) to learn its height.
        nrows = len(self.rows)
        row_hs = []
        for row_el, _ in self.rows:
            rh = None
            if row_el is not None:
                rh = length(row_el.style.get("height", "auto"), row_el.style.get("-font-px", 16), None, None)
            row_hs.append(rh or 0.0)
        laid = []
        for r, row in enumerate(self.cell_positions()):
            for c, col, span, rs in row:
                if col >= n:
                    continue
                span = min(span, n - col)
                cw = sum(widths[col:col + span]) + sp * (span - 1)
                c.m = [0.0] * 4
                c.layout(col_x[col], 0, cw, FloatContext(), forced_outer=cw, positioned=positioned)
                c.m = [0.0] * 4
                h = c.height + c.vert_extra()
                laid.append((r, c, rs, h))
                if rs == 1:
                    row_hs[r] = max(row_hs[r], h)
        # Cells spanning rows: grow the last spanned row if they don't fit.
        for r, c, rs, h in laid:
            if rs > 1:
                have = sum(row_hs[r:r + rs]) + sp * (rs - 1)
                if h > have:
                    row_hs[r + rs - 1] += h - have
        row_ys = []
        for r in range(nrows):
            row_ys.append(cursor)
            cursor += row_hs[r] + sp
        for r, c, rs, h in laid:
            area = sum(row_hs[r:r + rs]) + sp * (rs - 1)
            c.translate(0, row_ys[r])
            row_el = self.rows[r][0]
            va = c.style.get("vertical-align", "middle")
            if row_el is not None and va == "baseline":
                va = row_el.style.get("vertical-align", "middle")
                if va == "baseline":
                    va = "middle"
            if va == "middle":
                c.translate_contents(0, (area - h) / 2)
            elif va == "bottom":
                c.translate_contents(0, area - h)
            c.height = area - c.vert_extra()
        for cap in bottom_caps:     # caption-side: bottom
            cap.layout(self.x, cursor, tw, FloatContext(), positioned=positioned)
            cursor += cap.outer_height()
        self.row_bgs = []
        for r, (row_el, cells) in enumerate(self.rows):
            if row_el is not None and row_el not in self.ctx.positions:
                self.ctx.positions[row_el] = row_ys[r]
            if row_el is not None and cells:
                bg = color_of(row_el.style.get("background-color"))
                if bg:
                    self.row_bgs.append((self.x + sp, row_ys[r], self.x + tw - sp, row_ys[r] + row_hs[r], bg))
        self.height = self.resolve_height(cursor - self.y)
        if positioned is self:
            self.layout_abs_children()

    def translate_contents(self, dx, dy):
        super().translate_contents(dx, dy)
        self.row_bgs = [(a + dx, b + dy, c + dx, d + dy, col) for a, b, c, d, col in getattr(self, "row_bgs", [])]

    def paint_contents(self, dl, visible):
        for x1, y1, x2, y2, bg in getattr(self, "row_bgs", []):
            dl.add(paint.DrawRect(x1, y1, x2, y2, bg))
        for c in self.children:
            c.paint(dl)


# ---------------------------------------------------------------------------
# Flexbox


def _block_items(node, ctx):
    """Children of a flex/grid container, each blockified."""
    items = []
    run = []

    def flush():
        if run and not _is_blank_run(run):
            items.append(BlockBox(None, anon_style(node.style), ctx, items=list(run)))
        run.clear()

    for child in node.children:
        if isinstance(child, Text):
            run.append(("text", child))
            continue
        if not isinstance(child, Element):
            continue
        s = child.style
        if s.get("display") == "none":
            continue
        if s.get("display") == "contents":
            for kind, obj in _contents_items(child, ctx):
                if kind == "text":
                    run.append((kind, obj))
                else:
                    flush()
                    items.append(obj)
            continue
        flush()
        box = build_box(child, ctx)
        if s.get("display") in ("inline", "inline-block") and isinstance(box, BlockBox) and \
                not isinstance(box, (TableBox, FlexBox, GridBox)) and box.inline_items is None:
            pass
        items.append(box)
    flush()
    return items


def _contents_items(el, ctx):
    out = []
    for child in el.children:
        if isinstance(child, Text):
            out.append(("text", child))
        elif isinstance(child, Element) and child.style.get("display") != "none":
            out.append(("box", build_box(child, ctx)))
    return out


class FlexBox(BlockBox):
    def __init__(self, node, style, ctx):
        Box.__init__(self, node, style, ctx)
        self.lines = []
        self.inline_items = None
        self.marker = None
        self.children = _block_items(node, ctx)
        for c in self.children:
            c.flex_item = True
        self.children.sort(key=lambda c: _int(c.style.get("order", "0"), 0))

    def direction(self):
        if self.style.get("display", "").startswith("-webkit-"):
            return "column" if self.style.get("-webkit-box-orient") == "vertical" else "row"
        d = self.style.get("flex-direction", "row")
        return "column" if d.startswith("column") else "row"

    def reversed(self):
        if self.style.get("display", "").startswith("-webkit-"):
            rev = self.style.get("-webkit-box-direction", "normal") == "reverse"
        else:
            rev = self.style.get("flex-direction", "row").endswith("-reverse")
        if self.direction() == "row" and self.style.get("direction") == "rtl":
            rev = not rev            # main-start is on the right in right-to-left text
        return rev

    def gaps(self, w):
        fs = self.fs
        cg = length(self.style.get("column-gap", "0"), fs, w, 0) or 0
        rg = length(self.style.get("row-gap", "0"), fs, w, 0) or 0
        if getattr(self, "subgrid_gap", None) is not None and self.style.get("column-gap", "normal") == "normal":
            cg = self.subgrid_gap     # a subgrid uses its parent's gaps unless it sets its own
        return cg, rg

    def in_flow(self):
        return [c for c in self.children if not is_out_of_flow(c.style)]

    def intrinsic(self):
        if self._intrinsic is not None:
            return self._intrinsic
        self._intrinsic = (0, 0)
        self.compute_edges(0)
        extra = self.horiz_extra() + self.m[L] + self.m[R]
        spec = self.content_width_from_spec(None)
        if spec is not None:
            self._intrinsic = (spec + extra, spec + extra)
            return self._intrinsic
        kids = [c.intrinsic() for c in self.in_flow()]
        cg, _ = self.gaps(0)
        if self.direction() == "row":
            gap = cg * max(0, len(kids) - 1)
            mx = sum(k[1] for k in kids) + gap
            if self.style.get("flex-wrap", "nowrap") != "nowrap":
                mn = max([k[0] for k in kids] or [0])
            else:
                mn = sum(k[0] for k in kids) + gap
        else:
            mx = max([k[1] for k in kids] or [0])
            mn = max([k[0] for k in kids] or [0])
        mx_style = self.specified("max-width", None)
        if mx_style is not None:
            mx, mn = min(mx, mx_style), min(mn, mx_style)
        mn_style = self.specified("min-width", None)
        if mn_style is not None:
            mx, mn = max(mx, mn_style), max(mn, mn_style)
        self._intrinsic = (mn + extra, max(mn, mx) + extra)
        return self._intrinsic

    def layout_contents(self, fc, positioned):
        items = []
        for c in self.children:
            if is_out_of_flow(c.style):
                target = positioned if (c.style.get("position") == "absolute" and positioned is not None) else self.ctx.root_box
                queue_abs(target, c, self.x, self.y, self)
            else:
                items.append(c)
        if self.direction() == "column":
            return self.layout_column(items, positioned)
        return self.layout_row(items, positioned)

    def align_of(self, c):
        """The item's cross-axis alignment: stretch, start, center, end or baseline."""
        a = c.style.get("align-self", "auto").strip().lower()
        if a == "auto":
            a = self.style.get("align-items", "normal").strip().lower()
        a = a.replace("unsafe ", "").replace("safe ", "")
        if a in ("normal", "stretch"):
            return "stretch"
        if a == "center":
            return "center"
        if a in ("flex-end", "end", "self-end", "last baseline"):
            return "end"
        if a in ("baseline", "first baseline"):
            return "baseline"
        return "start"

    # --- row direction ------------------------------------------------
    def row_item(self, c, W):
        """Flex base size, hypothetical size and min/max for an item in a row."""
        c.compute_edges(W)
        s = c.style
        it = _FlexItem(c)
        hx = c.horiz_extra()
        bb = s.get("box-sizing") == "border-box"
        it.extra = hx + c.m[L] + c.m[R]
        it.auto_a, it.auto_b = c.m_auto[L], c.m_auto[R]
        basis = s.get("flex-basis", "auto").strip().lower()
        b = None
        if basis not in ("auto", "content", "max-content", "min-content", "fit-content"):
            b = length(basis, c.fs, W, None)
            if b is not None and bb:
                b -= hx
        if b is None and basis == "auto":
            b = c.content_width_from_spec(W)
        imn, imx = c.intrinsic()
        if b is None:
            b = (imn if basis == "min-content" else imx) - it.extra
        mx = c.specified("max-width", W)
        mx = INF if mx is None else (mx - hx if bb else mx)
        if s.get("min-width", "auto").strip() == "auto":
            if _overflow_visible(s):
                mn = imn - it.extra
                spec = c.content_width_from_spec(W)
                if spec is not None:
                    mn = min(mn, spec)
                mn = min(mn, mx)
            else:
                mn = 0
        else:
            mn = c.specified("min-width", W) or 0
            if bb:
                mn -= hx
        it.set_sizes(b, mn, mx, s)
        return it

    def layout_row(self, items, positioned):
        W = self.width
        cg, rg = self.gaps(W)
        s = self.style
        wrap = s.get("flex-wrap", "nowrap").strip().lower()
        infos = [self.row_item(c, W) for c in items]
        lines = _break_lines(infos, W, cg, wrap != "nowrap")
        for line in lines:
            _resolve_flexible(line, W, cg * (len(line) - 1))
            for it in line:
                it.box.layout(0, 0, W, FloatContext(), forced_outer=max(0.0, it.target + it.extra),
                              positioned=positioned)
        # Cross sizes of the lines.
        definite_h = _definite_height(self)
        mn_h, mx_h = _min_max_height(self)
        line_hs = []
        for line in lines:
            above = below = tallest = 0
            for it in line:
                c = it.box
                oh = c.outer_height()
                if self.align_of(c) == "baseline" and not (c.m_auto[T] or c.m_auto[B]):
                    a = _baseline_offset(c)
                    above, below = max(above, a), max(below, oh - a)
                else:
                    tallest = max(tallest, oh)
            line_hs.append(max(tallest, above + below))
        if len(lines) == 1:
            if definite_h is not None:
                line_hs[0] = definite_h
            else:
                line_hs[0] = min(max(line_hs[0], mn_h), mx_h)
        # Line positions (align-content).
        n = len(lines)
        start, between = 0.0, rg
        cross_space = definite_h if definite_h is not None else (mn_h if mn_h > 0 else None)
        if n > 1 and cross_space is not None:
            free = cross_space - sum(line_hs) - rg * (n - 1)
            ac = s.get("align-content", "normal").strip().lower()
            if free > 0 and ac in ("normal", "stretch"):
                line_hs = [h + free / n for h in line_hs]
            else:
                start, between = _distribute(ac, free, n, rg)
        order = list(range(n))
        if wrap == "wrap-reverse":
            order.reverse()
        line_y = {}
        y = start
        for k in order:
            line_y[k] = y
            y += line_hs[k] + between
        total = (y - between if n else 0.0)
        # Main-axis placement and cross-axis alignment.
        jc = s.get("justify-content", "normal").strip().lower()
        reverse = self.reversed()
        for k, line in enumerate(lines):
            lh = line_hs[k]
            base_above = max([_baseline_offset(it.box) for it in line if self.align_of(it.box) == "baseline"] or [0])
            sizes = [it.target + it.extra for it in line]
            free = W - sum(sizes) - cg * (len(line) - 1)
            autos = sum(it.auto_a + it.auto_b for it in line)
            margin_add = 0.0
            pos, between = 0.0, cg
            if free > 0 and autos:
                margin_add = free / autos
            else:
                pos, between = _distribute(jc, free, len(line), cg)
            for it, size in zip(line, sizes):
                c = it.box
                pos += margin_add * it.auto_a
                x = W - pos - size if reverse else pos
                pos += size + margin_add * it.auto_b + between
                dy = self.align_item_cross(c, lh, base_above, positioned)
                c.translate(self.x + x - c.margin_left_edge(), self.y + line_y[k] + dy - c.margin_top_edge())
        return total

    def align_item_cross(self, c, line_h, base_above, positioned):
        """Stretch or offset a row item inside its line; returns the y offset."""
        oh = c.outer_height()
        free = line_h - oh
        if c.m_auto[T] or c.m_auto[B]:
            if c.m_auto[T] and c.m_auto[B]:
                return max(0.0, free / 2)
            return max(0.0, free) if c.m_auto[T] else 0.0
        a = self.align_of(c)
        if a == "stretch":
            if c.style.get("height", "auto") == "auto":
                _stretch_height(c, line_h - c.vert_extra() - c.m[T] - c.m[B], positioned)
            return 0.0
        if a == "center":
            return free / 2
        if a == "end":
            return free
        if a == "baseline":
            return base_above - _baseline_offset(c)
        return 0.0

    # --- column direction ---------------------------------------------
    def layout_column(self, items, positioned):
        W = self.width
        cg, rg = self.gaps(W)
        s = self.style
        H = _definite_height(self)
        mn_h, mx_h = _min_max_height(self)
        infos = []
        for c in items:
            c.compute_edges(W)
            a = self.align_of(c)
            stretch = a == "stretch" and c.style.get("width", "auto") == "auto" and not (c.m_auto[L] or c.m_auto[R])
            c.layout(0, 0, W, FloatContext(), shrink=not stretch, positioned=positioned)
            cs = c.style
            it = _FlexItem(c)
            it.stretch = stretch
            vx = c.vert_extra()
            bb = cs.get("box-sizing") == "border-box"
            it.extra = vx + c.m[T] + c.m[B]
            it.auto_a, it.auto_b = c.m_auto[T], c.m_auto[B]
            content_h = c.height
            basis = cs.get("flex-basis", "auto").strip().lower()
            b = None
            if basis not in ("auto", "content", "max-content", "min-content", "fit-content"):
                b = length(basis, c.fs, H, None)
                if b is not None and bb:
                    b -= vx
            if b is None and basis == "auto" and "%" not in cs.get("height", "auto"):
                b = length(cs.get("height", "auto"), c.fs, None, None)
                if b is not None and bb:
                    b -= vx
            if b is None:
                b = content_h
            mxv = cs.get("max-height", "none")
            mx = length(mxv, c.fs, H, None) if ("%" not in mxv or H is not None) else None
            mx = INF if mx is None else (mx - vx if bb else mx)
            if cs.get("min-height", "auto").strip() == "auto":
                mn = min(content_h, mx) if _overflow_visible(cs) else 0
            else:
                mn = length(cs.get("min-height"), c.fs, H, 0) or 0
                if bb:
                    mn -= vx
            it.set_sizes(b, mn, mx, cs)
            infos.append(it)
        hyp_total = sum(it.hyp + it.extra for it in infos) + rg * max(0, len(infos) - 1)
        main = H if H is not None else min(max(hyp_total, mn_h), mx_h)
        wrap = s.get("flex-wrap", "nowrap").strip().lower()
        can_wrap = wrap != "nowrap" and (H is not None or mx_h != INF)
        lines = _break_lines(infos, main, rg, can_wrap)
        for line in lines:
            _resolve_flexible(line, main, rg * (len(line) - 1))
            for it in line:
                c = it.box
                if abs(it.target - c.height) > 0.5:
                    if isinstance(c, FlexBox):
                        _relayout_with_height(c, it.target, W, not it.stretch, positioned)
                    else:
                        c.height = it.target
        # Cross sizes (widths) of the lines.
        n = len(lines)
        if n <= 1:
            line_ws = [W]
        else:
            line_ws = [max([it.box.outer_width() for it in line] or [0]) for line in lines]
        start, between = 0.0, cg
        if n > 1:
            free = W - sum(line_ws) - cg * (n - 1)
            ac = s.get("align-content", "normal").strip().lower()
            if free > 0 and ac in ("normal", "stretch"):
                line_ws = [w + free / n for w in line_ws]
            else:
                start, between = _distribute(ac, free, n, cg)
        order = list(range(n))
        if wrap == "wrap-reverse":
            order.reverse()
        line_x = {}
        x = start
        for k in order:
            line_x[k] = x
            x += line_ws[k] + between
        jc = s.get("justify-content", "normal").strip().lower()
        reverse = self.reversed()
        for k, line in enumerate(lines):
            lw = line_ws[k]
            sizes = [it.target + it.extra for it in line]
            free = main - sum(sizes) - rg * (len(line) - 1)
            autos = sum(it.auto_a + it.auto_b for it in line)
            margin_add = 0.0
            pos, between = 0.0, rg
            if free > 0 and autos:
                margin_add = free / autos
            else:
                pos, between = _distribute(jc, free, len(line), rg)
            for it, size in zip(line, sizes):
                c = it.box
                if n > 1 and it.stretch and abs(c.outer_width() - lw) > 0.5:
                    h = c.height
                    c.layout(0, 0, lw, FloatContext(), forced_outer=lw, positioned=positioned)
                    c.height = h
                pos += margin_add * it.auto_a
                y = main - pos - size if reverse else pos
                pos += size + margin_add * it.auto_b + between
                ow = c.outer_width()
                dx = 0.0
                if c.m_auto[L] and c.m_auto[R]:
                    dx = max(0.0, (lw - ow) / 2)
                elif c.m_auto[L]:
                    dx = max(0.0, lw - ow)
                elif not it.stretch:
                    a = self.align_of(c)
                    if a == "center":
                        dx = (lw - ow) / 2
                    elif a == "end":
                        dx = lw - ow
                c.translate(self.x + line_x[k] + dx - c.margin_left_edge(), self.y + y - c.margin_top_edge())
        return main


class _FlexItem:
    __slots__ = ("box", "base", "hyp", "mn", "mx", "grow", "shrink", "extra", "target", "frozen", "viol",
                 "auto_a", "auto_b", "stretch")

    def __init__(self, box):
        self.box = box
        self.auto_a = self.auto_b = False
        self.stretch = True

    def set_sizes(self, base, mn, mx, style):
        self.base = max(0.0, base)
        self.mn = max(0.0, mn)
        self.mx = max(self.mn, mx)
        self.hyp = min(max(self.base, self.mn), self.mx)
        self.grow = max(0.0, _float(style.get("flex-grow", "0"), 0))
        self.shrink = max(0.0, _float(style.get("flex-shrink", "1"), 1))
        self.target = self.hyp


def _overflow_visible(style):
    return style.get("overflow-x", style.get("overflow", "visible")) in ("visible", "clip") and \
        style.get("overflow", "visible") in ("visible", "clip")


def _break_lines(infos, avail, gap, wrap):
    lines, cur, used = [], [], 0.0
    for it in infos:
        size = it.hyp + it.extra
        if wrap and cur and used + gap + size > avail + 0.5:
            lines.append(cur)
            cur, used = [], 0.0
        used += (gap if cur else 0) + size
        cur.append(it)
    if cur or not lines:
        lines.append(cur)
    return lines


def _resolve_flexible(line, avail, gaps):
    """CSS Flexbox 9.7: grow or shrink the items of one line to fill `avail`."""
    if not line:
        return
    space = avail - gaps
    growing = sum(it.hyp + it.extra for it in line) < space
    for it in line:
        it.target = it.hyp
        f = it.grow if growing else it.shrink
        it.frozen = f == 0 or (growing and it.base > it.hyp) or (not growing and it.base < it.hyp)
    initial_free = space - sum((it.target if it.frozen else it.base) + it.extra for it in line)
    for _ in range(len(line) + 1):
        unfrozen = [it for it in line if not it.frozen]
        if not unfrozen:
            break
        free = space - sum((it.target if it.frozen else it.base) + it.extra for it in line)
        fsum = sum(it.grow if growing else it.shrink for it in unfrozen)
        if fsum < 1 and abs(initial_free * fsum) < abs(free):
            free = initial_free * fsum
        if growing:
            for it in unfrozen:
                it.target = it.base + (free * it.grow / fsum if fsum > 0 else 0)
        else:
            scaled = sum(it.shrink * it.base for it in unfrozen)
            for it in unfrozen:
                it.target = it.base + (free * it.shrink * it.base / scaled if scaled > 0 else 0)
        total = 0.0
        for it in unfrozen:
            t = max(it.mn, min(it.target, it.mx))
            it.viol = t - it.target
            it.target = t
            total += it.viol
        for it in unfrozen:
            if abs(total) < 0.01 or (total > 0 and it.viol > 0) or (total < 0 and it.viol < 0):
                it.frozen = True


def _distribute(mode, free, n, gap):
    """justify-/align-content: (offset of the first item, spacing between items)."""
    mode = mode.replace("unsafe ", "").replace("safe ", "")
    if n <= 0:
        return 0.0, gap
    if mode == "center":
        return free / 2, gap
    if mode in ("flex-end", "end", "right"):
        return free, gap
    if free <= 0:
        return (free / 2 if mode in ("space-around", "space-evenly") else 0.0), gap
    if mode == "space-between":
        return 0.0, gap + (free / (n - 1) if n > 1 else 0)
    if mode == "space-around":
        return free / n / 2, gap + free / n
    if mode == "space-evenly":
        return free / (n + 1), gap + free / (n + 1)
    return 0.0, gap


def _baseline_offset(c):
    """Distance from an item's top margin edge to its first baseline."""
    fb = c.first_baseline() if hasattr(c, "first_baseline") else None
    if fb is None:
        return c.outer_height() - c.m[B]
    return fb - c.margin_top_edge()


def _definite_height(box):
    """The content height a box is given by its style or by a flex/grid parent, else None."""
    fh = getattr(box, "_forced_h", None)
    if fh is not None:
        return fh
    v = box.style.get("height", "auto")
    if "%" in v:
        return None
    h = length(v, box.fs, None, None)
    if h is None:
        return None
    if box.style.get("box-sizing") == "border-box":
        h -= box.vert_extra()
    mn, mx = _min_max_height(box)
    return max(0.0, min(max(h, mn), mx))


def _min_max_height(box):
    s = box.style
    bb = s.get("box-sizing") == "border-box"
    mn = length(s.get("min-height", "auto"), box.fs, None, None) if "%" not in s.get("min-height", "") else None
    mx = length(s.get("max-height", "none"), box.fs, None, None) if "%" not in s.get("max-height", "") else None
    mn = 0.0 if mn is None else max(0.0, mn - (box.vert_extra() if bb else 0))
    mx = INF if mx is None else max(0.0, mx - (box.vert_extra() if bb else 0))
    return mn, max(mn, mx)


def _relayout_with_height(c, h, cb_w, shrink, positioned, forced_outer=None):
    """Lay a flex/grid item out again with a definite height, so its own
    children can grow, shrink or align inside it."""
    c._forced_h = max(0.0, h)
    try:
        c.layout(0, 0, cb_w, FloatContext(), forced_outer=forced_outer, shrink=shrink, positioned=positioned)
    finally:
        c._forced_h = None


def _stretch_height(c, h, positioned):
    """Stretch an already laid-out item to content height h."""
    mn, mx = _min_max_height(c)
    h = max(0.0, min(max(h, mn), mx))
    if abs(h - c.height) < 0.5:
        return
    if isinstance(c, FlexBox):
        x, y = c.margin_left_edge(), c.margin_top_edge()
        outer = c.outer_width()
        _relayout_with_height(c, h, outer, False, positioned, forced_outer=outer)
        c.translate(x - c.margin_left_edge(), y - c.margin_top_edge())
    else:
        c.height = h


def _float(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


INF = float("inf")


# ---------------------------------------------------------------------------
# Grid


class GridBox(FlexBox):
    """CSS grid: explicit tracks (px, %, fr, auto, min/max-content, minmax(),
    fit-content(), repeat() incl. auto-fill/auto-fit), named areas and lines,
    line-based placement, auto-placement (row/column, dense), implicit tracks,
    gaps, and justify/align for items and tracks."""

    def direction(self):
        return "row"

    def plan(self, W, H):
        """Explicit tracks plus every in-flow item's area.
        Returns (col_tracks, row_tracks, [(box, row, col, row_span, col_span)], collapsed_cols)."""
        s = self.style
        cg, rg = self.gaps(W or 0)
        ctracks, cnames, cfit = _parse_tracks(s.get("grid-template-columns", "none"), self.fs, W, cg)
        sub = getattr(self, "subgrid_cols", None)
        if s.get("grid-template-columns", "none").startswith("subgrid") and sub:
            # subgrid: the parent grid's tracks over the columns this item spans
            ctracks = [("px", w, "px", w) for w in sub]
            cfit = [False] * len(sub)
        rspec = s.get("grid-template-rows", "none")
        rtracks, rnames, rfit = _parse_tracks("none" if rspec.strip() == "masonry" else rspec, self.fs, H, rg)
        areas, area_rows, area_cols = _parse_areas(s.get("grid-template-areas", "none"))
        for name, (r0, r1, c0, c1) in areas.items():
            rnames.setdefault(name + "-start", []).append(r0)
            rnames.setdefault(name + "-end", []).append(r1)
            cnames.setdefault(name + "-start", []).append(c0)
            cnames.setdefault(name + "-end", []).append(c1)
        n_cols = max(len(ctracks), area_cols)
        n_rows = max(len(rtracks), area_rows)
        flow = s.get("grid-auto-flow", "row").lower()
        by_col = "column" in flow
        dense = "dense" in flow
        items = []
        for c in self.in_flow():
            cs = c.style
            r = _axis_place(cs.get("grid-row-start", "auto"), cs.get("grid-row-end", "auto"), rnames, n_rows + 1)
            k = _axis_place(cs.get("grid-column-start", "auto"), cs.get("grid-column-end", "auto"), cnames,
                            n_cols + 1)
            items.append((c, r, k) if not by_col else (c, k, r))
        # Auto-placement works in (major, minor) coordinates: rows/columns for row flow.
        minor_count = n_rows if by_col else n_cols
        placed = {}
        occupied = set()

        def fits(a, b, sa, sb):
            return all((i, j) not in occupied for i in range(a, a + sa) for j in range(b, b + sb))

        def put(c, a, b, sa, sb):
            placed[c] = (a, b, sa, sb)
            for i in range(a, a + sa):
                for j in range(b, b + sb):
                    occupied.add((i, j))

        for c, (ma, sa), (mi, sb) in items:          # 1. fully placed items
            if ma is not None and mi is not None:
                put(c, ma, mi, sa, sb)
                minor_count = max(minor_count, mi + sb)
        for c, (ma, sa), (mi, sb) in items:          # 2. major position fixed
            minor_count = max(minor_count, sb)
            if ma is not None and mi is None:
                b = 0
                while not fits(ma, b, sa, sb):
                    b += 1
                put(c, ma, b, sa, sb)
                minor_count = max(minor_count, b + sb)
        ca = cb = 0                                  # 3. everything else
        for c, (ma, sa), (mi, sb) in items:
            if c in placed:
                continue
            if dense:
                ca = cb = 0
            if mi is not None:
                if mi < cb and not dense:
                    ca += 1
                cb = mi
                guard = 0
                while not fits(ca, cb, sa, sb) and guard < 100000:
                    ca += 1
                    guard += 1
                put(c, ca, cb, sa, sb)
            else:
                guard = 0
                while guard < 100000:
                    guard += 1
                    if cb + sb > minor_count:
                        ca, cb = ca + 1, 0
                        continue
                    if fits(ca, cb, sa, sb):
                        break
                    cb += 1
                put(c, ca, cb, sa, sb)
                cb += sb
        cells = []
        for c, _, _ in items:
            a, b, sa, sb = placed[c]
            cells.append((c, b, a, sb, sa) if by_col else (c, a, b, sa, sb))
        n_rows = max([n_rows] + [r + rs for _, r, _, rs, _ in cells])
        n_cols = max([n_cols] + [k + ks for _, _, k, _, ks in cells])
        ctracks = _fill_tracks(ctracks, n_cols, s.get("grid-auto-columns", "auto"), self.fs, W)
        rtracks = _fill_tracks(rtracks, n_rows, s.get("grid-auto-rows", "auto"), self.fs, H)
        used = set()
        for _, _, k, _, ks in cells:
            used.update(range(k, k + ks))
        collapsed = [i < len(cfit) and cfit[i] and i not in used for i in range(n_cols)]
        for i, col in enumerate(collapsed):
            if col:
                ctracks[i] = ("px", 0.0, "px", 0.0)
        return ctracks, rtracks, cells, collapsed

    def intrinsic(self):
        if self._intrinsic is not None:
            return self._intrinsic
        self._intrinsic = (0, 0)
        self.compute_edges(0)
        extra = self.horiz_extra() + self.m[L] + self.m[R]
        spec = self.content_width_from_spec(None)
        if spec is not None:
            self._intrinsic = (spec + extra, spec + extra)
            return self._intrinsic
        ctracks, _, cells, collapsed = self.plan(None, None)
        cg, _ = self.gaps(0)
        contrib = [(k, ks) + tuple(c.intrinsic()) for c, _, k, _, ks in cells]
        gaps = cg * max(0, len(ctracks) - 1 - sum(collapsed))
        mn = sum(_size_tracks(ctracks, contrib, None, cg, "min")) + gaps
        mx = sum(_size_tracks(ctracks, contrib, None, cg, "max")) + gaps
        mx_style = self.specified("max-width", None)
        if mx_style is not None:
            mx, mn = min(mx, mx_style), min(mn, mx_style)
        mn_style = self.specified("min-width", None)
        if mn_style is not None:
            mx, mn = max(mx, mn_style), max(mn, mn_style)
        self._intrinsic = (mn + extra, max(mn, mx) + extra)
        return self._intrinsic

    def layout_masonry(self, cells, widths, col_x, span_w, rg, positioned):
        """grid-template-rows: masonry: each item goes to the column (or run of
        columns, for spans) whose stack is currently shortest."""
        n = len(widths)
        heights = [0.0] * n
        for c, _r, k, _rs, ks in cells:
            ks = min(ks, n)
            explicit = c.style.get("grid-column-start", "auto") not in ("auto", "") and k + ks <= n
            if not explicit:
                k = min(range(n - ks + 1), key=lambda i: (max(heights[i:i + ks]), i))
            aw = span_w(k, ks)
            c.compute_edges(aw)
            c.grid_stretch_x = c.style.get("width", "auto") == "auto"
            if c.grid_stretch_x:
                c.layout(0, 0, aw, FloatContext(), forced_outer=aw, positioned=positioned)
            else:
                c.layout(0, 0, aw, FloatContext(), shrink=True, positioned=positioned)
            top = max(heights[k:k + ks])
            c.translate(self.x + col_x[k] - c.margin_left_edge(), self.y + top - c.margin_top_edge())
            for i in range(k, k + ks):
                heights[i] = top + c.outer_height() + rg
        return max(0.0, max(heights or [0.0]) - rg)

    def self_alignment(self, c, axis):
        """justify-self (axis 'x') / align-self (axis 'y') -> stretch, start, center or end."""
        if axis == "x":
            a = c.style.get("justify-self", "auto").strip().lower()
            if a == "auto":
                a = self.style.get("justify-items", "normal").strip().lower()
        else:
            a = c.style.get("align-self", "auto").strip().lower()
            if a == "auto":
                a = self.style.get("align-items", "normal").strip().lower()
        a = a.replace("unsafe ", "").replace("safe ", "").replace("legacy", "").strip() or "normal"
        if a == "normal":
            return "start" if isinstance(c, (ImageBox, ControlBox)) else "stretch"
        if a == "stretch":
            return "stretch"
        if a == "center":
            return "center"
        if a in ("end", "flex-end", "self-end", "right", "last baseline"):
            return "end"
        return "start"

    def layout_row(self, items, positioned):
        W = self.width
        cg, rg = self.gaps(W)
        H = _definite_height(self)
        ctracks, rtracks, cells, collapsed = self.plan(W, H)
        s = self.style
        # Columns.
        contrib = [(k, ks) + tuple(c.intrinsic()) for c, _, k, _, ks in cells]
        n_collapsed = sum(collapsed)
        stretch_cols = s.get("justify-content", "normal").strip().lower() in ("normal", "stretch")
        widths = _size_tracks(ctracks, contrib, W + cg * n_collapsed, cg, "layout", stretch_cols)
        col_gaps = [0.0 if collapsed[i] else cg for i in range(len(widths))]
        free = W - sum(widths) - sum(col_gaps[:-1]) if widths else 0
        start, extra_gap = _distribute(s.get("justify-content", "normal").strip().lower(), free, len(widths), 0)
        col_x, x = [], start
        for i, w in enumerate(widths):
            col_x.append(x)
            x += w + col_gaps[i] + (extra_gap if not collapsed[i] else 0)
        col_x.append(x)

        def span_w(k, ks):
            return max(0.0, col_x[k + ks] - col_x[k] - col_gaps[k + ks - 1] - extra_gap)

        if s.get("grid-template-rows", "none").strip() == "masonry":
            return self.layout_masonry(cells, widths, col_x, span_w, rg, positioned)
        # Lay items out at their area widths to find their heights.
        for c, r, k, rs, ks in cells:
            aw = span_w(k, ks)
            c.compute_edges(aw)
            if isinstance(c, GridBox) and c.style.get("grid-template-columns", "").startswith("subgrid"):
                tracks = list(widths[k:k + ks])
                if tracks:
                    tracks[0] -= c.m[L] + c.b[L] + c.p[L]
                    tracks[-1] -= c.m[R] + c.b[R] + c.p[R]
                c.subgrid_cols = [max(0.0, t) for t in tracks]
                c.subgrid_gap = cg
            if self.self_alignment(c, "x") == "stretch" and c.style.get("width", "auto") == "auto" \
                    and not (c.m_auto[L] or c.m_auto[R]):
                c.grid_stretch_x = True
                c.layout(0, 0, aw, FloatContext(), forced_outer=aw, positioned=positioned)
            else:
                c.grid_stretch_x = False
                c.layout(0, 0, aw, FloatContext(), shrink=True, positioned=positioned)
        # Rows.
        rcontrib = [(r, rs, c.outer_height(), c.outer_height()) for c, r, k, rs, ks in cells]
        stretch_rows = s.get("align-content", "normal").strip().lower() in ("normal", "stretch")
        heights = _size_tracks(rtracks, rcontrib, H, rg, "layout", stretch_rows and H is not None)
        free = (H - sum(heights) - rg * max(0, len(heights) - 1)) if H is not None else 0
        start, between = _distribute(s.get("align-content", "normal").strip().lower(), free, len(heights), rg)
        row_y, y = [], start
        for h in heights:
            row_y.append(y)
            y += h + between
        row_y.append(y)
        total = y - between if heights else 0.0
        # Place and align every item in its area.
        for c, r, k, rs, ks in cells:
            aw = span_w(k, ks)
            ah = max(0.0, row_y[r + rs] - row_y[r] - between)
            ay = self.self_alignment(c, "y")
            if c.m_auto[T] or c.m_auto[B]:
                f = ah - c.outer_height()
                dy = max(0.0, f / 2 if (c.m_auto[T] and c.m_auto[B]) else (f if c.m_auto[T] else 0))
            elif ay == "stretch":
                if c.style.get("height", "auto") == "auto":
                    _stretch_height(c, ah - c.vert_extra() - c.m[T] - c.m[B], positioned)
                dy = 0.0
            else:
                f = ah - c.outer_height()
                dy = f / 2 if ay == "center" else (f if ay == "end" else 0.0)
            dx = 0.0
            if not c.grid_stretch_x:
                f = aw - c.outer_width()
                if c.m_auto[L] and c.m_auto[R]:
                    dx = max(0.0, f / 2)
                elif c.m_auto[L]:
                    dx = max(0.0, f)
                elif not c.m_auto[R]:
                    ax = self.self_alignment(c, "x")
                    dx = f / 2 if ax == "center" else (f if ax == "end" else 0.0)
            cx = col_x[k] + dx
            if s.get("direction") == "rtl":    # grid columns run right to left
                cx = W - (col_x[k] + aw) + dx
            c.translate(self.x + cx - c.margin_left_edge(), self.y + row_y[r] + dy - c.margin_top_edge())
        return total


def _grid_tokens(spec):
    """Split a track list into sizes, functions and [line names]."""
    out, i, n = [], 0, len(spec)
    while i < n:
        ch = spec[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "[":
            j = spec.find("]", i)
            j = n - 1 if j < 0 else j
            out.append(spec[i:j + 1])
            i = j + 1
            continue
        j, depth = i, 0
        while j < n and (depth > 0 or not (spec[j].isspace() or spec[j] == "[")):
            if spec[j] == "(":
                depth += 1
            elif spec[j] == ")":
                depth -= 1
            j += 1
        out.append(spec[i:j])
        i = j
    return out


def _track_size(tok, fs, avail):
    """One track size -> (min kind, min px, max kind, max value). Kinds:
    px, auto, min (min-content), max (max-content), fr, fit (fit-content limit)."""
    t = tok.strip().lower()
    if t.startswith("minmax(") and t.endswith(")"):
        parts = _split_args(t[7:-1])
        lo = _track_size(parts[0], fs, avail)
        hi = _track_size(parts[-1], fs, avail)
        mk, mv = (lo[0], lo[1]) if lo[2] != "fr" else ("auto", 0.0)
        return (mk, mv, hi[2], hi[3])
    if t.startswith("fit-content(") and t.endswith(")"):
        v = length(t[12:-1], fs, avail, None)
        return ("auto", 0.0, "fit", INF if v is None else v)
    m = re.match(r"^(\d*\.?\d+)fr$", t)
    if m:
        return ("auto", 0.0, "fr", float(m.group(1)))
    if t == "min-content":
        return ("min", 0.0, "min", 0.0)
    if t == "max-content":
        return ("max", 0.0, "max", 0.0)
    v = length(t, fs, avail, None)
    if v is None:
        return ("auto", 0.0, "auto", 0.0)
    v = max(0.0, v)
    return ("px", v, "px", v)


def _split_args(text):
    out, depth, cur = [], 0, ""
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    out.append(cur.strip())
    return out


def _parse_tracks(spec, fs, avail, gap):
    """grid-template-columns/rows -> (tracks, {line name: [line index]}, [from auto-fit repeat])."""
    tracks, names, autofit = [], {}, []
    spec = (spec or "none").strip()
    if spec.lower() in ("none", "", "subgrid", "masonry", "initial", "auto"):
        return tracks, names, autofit

    def add_names(tok):
        for nm in tok[1:-1].split():
            names.setdefault(nm, []).append(len(tracks))

    for tok in _grid_tokens(spec):
        if tok.startswith("["):
            add_names(tok)
            continue
        if tok.lower().startswith("repeat(") and tok.endswith(")"):
            parts = _split_args(tok[7:-1])
            if len(parts) < 2:
                continue
            count, body = parts[0].strip().lower(), ",".join(parts[1:])
            body_toks = _grid_tokens(body)
            sizes = [_track_size(t, fs, avail) for t in body_toks if not t.startswith("[")]
            fit = count == "auto-fit"
            if count in ("auto-fill", "auto-fit"):
                per = 0.0
                for mk, mv, xk, xv in sizes:
                    per += xv if xk == "px" else (mv if mk == "px" else 0.0)
                if avail is None or per <= 0:
                    reps = 1
                else:
                    reps = max(1, int((avail + gap) // (per + gap * len(sizes))))
            else:
                reps = max(1, min(10000, _int(count, 1)))
            for _ in range(reps):
                for t in body_toks:
                    if t.startswith("["):
                        add_names(t)
                    else:
                        tracks.append(_track_size(t, fs, avail))
                        autofit.append(fit)
            continue
        tracks.append(_track_size(tok, fs, avail))
        autofit.append(False)
    return tracks, names, autofit


def _parse_areas(spec):
    """grid-template-areas -> ({name: (row0, row1, col0, col1)}, rows, cols)."""
    rows = [(a or b).split() for a, b in re.findall(r'"([^"]*)"|\'([^\']*)\'', spec or "")]
    areas = {}
    for r, row in enumerate(rows):
        for k, name in enumerate(row):
            if set(name) == {"."}:
                continue
            a = areas.get(name)
            if a is None:
                areas[name] = [r, r + 1, k, k + 1]
            else:
                a[0], a[1], a[2], a[3] = min(a[0], r), max(a[1], r + 1), min(a[2], k), max(a[3], k + 1)
    return {k: tuple(v) for k, v in areas.items()}, len(rows), max([len(r) for r in rows] or [0])


def _grid_line(value, names, n_lines, side):
    """One placement value -> ('line', index) | ('span', n) | None for auto."""
    v = (value or "auto").strip()
    if v.lower() in ("auto", ""):
        return None
    span, num, ident = False, None, None
    for t in v.split():
        if t.lower() == "span":
            span = True
        elif re.match(r"^-?\d+$", t):
            num = int(t)
        else:
            ident = t
    if span:
        return ("span", max(1, num or 1))
    if ident is not None:
        for key in (ident + "-" + side, ident):
            lines = names.get(key)
            if lines is None:
                lines = next((v2 for k2, v2 in names.items() if k2.lower() == key.lower()), None)
            if lines:
                n = num or 1
                return ("line", lines[min(n, len(lines)) - 1] if n > 0 else lines[max(0, len(lines) + n)])
        return None
    if not num:
        return None
    return ("line", num - 1 if num > 0 else max(0, n_lines + num))


def _axis_place(start, end, names, n_lines):
    """Resolve grid-*-start/end into (first track or None, span)."""
    a = _grid_line(start, names, n_lines, "start")
    b = _grid_line(end, names, n_lines, "end")
    if a and a[0] == "line":
        if b and b[0] == "line":
            s, e = sorted((a[1], b[1]))
            return s, max(1, e - s)
        return a[1], (b[1] if b else 1)
    if b and b[0] == "line":
        span = a[1] if a else 1
        return max(0, b[1] - span), max(1, min(span, b[1]))
    return None, (a[1] if a else (b[1] if b else 1))


def _fill_tracks(tracks, count, auto_spec, fs, avail):
    """Extend the explicit tracks with implicit ones sized by grid-auto-rows/columns."""
    auto = [t for t in (_track_size(x, fs, avail) for x in _grid_tokens(auto_spec or "auto") if not x.startswith("["))]
    auto = auto or [("auto", 0.0, "auto", 0.0)]
    out = list(tracks)
    i = 0
    while len(out) < count:
        out.append(auto[i % len(auto)])
        i += 1
    return out


def _size_tracks(tracks, items, avail, gap, mode, stretch=True):
    """The grid track sizing algorithm (simplified CSS Grid 11.3-11.8).
    items: (first track, span, min-content, max-content) outer contributions.
    mode: 'layout' (fit avail, or max-content if avail is None), 'min', 'max'."""
    n = len(tracks)
    if n == 0:
        return []
    base, limit = [], []
    for mk, mv, xk, xv in tracks:
        b = mv if mk == "px" else 0.0
        base.append(b)
        limit.append(max(xv, b) if xk == "px" else (INF if xk == "fr" else None))
    flex = [t[2] == "fr" for t in tracks]
    for st, sp, mnc, mxc in sorted(items, key=lambda it: it[1]):
        if mode == "min":
            mxc = mnc
        rng = range(st, min(n, st + sp))
        if not rng:
            continue
        gaps = gap * (len(rng) - 1)
        if any(flex[k] for k in rng):
            tg = [k for k in rng if flex[k] and tracks[k][0] != "px"]
            need = mnc - sum(base[k] for k in rng) - gaps
            if tg and need > 0:
                for k in tg:
                    base[k] += need / len(tg)
            continue
        tg = [k for k in rng if tracks[k][0] != "px"]
        if tg:
            c = mxc if all(tracks[k][0] == "max" for k in tg) else mnc
            need = c - sum(base[k] for k in rng) - gaps
            if need > 0:
                for k in tg:
                    base[k] += need / len(tg)
        tg = [k for k in rng if tracks[k][2] != "px"]
        if tg:
            c = mnc if all(tracks[k][2] == "min" for k in tg) else mxc
            for k in tg:
                limit[k] = base[k] if limit[k] is None else max(limit[k], base[k])
            need = c - sum(max(limit[k], base[k]) for k in rng) - gaps
            if need > 0:
                for k in tg:
                    limit[k] += need / len(tg)
    for k, (mk, mv, xk, xv) in enumerate(tracks):
        if limit[k] is None:
            limit[k] = base[k]
        if xk == "fit":
            limit[k] = min(limit[k], max(base[k], xv))
        limit[k] = max(limit[k], base[k])
    sizes = list(base)
    if mode == "min":
        return sizes
    if avail is None:
        for k in range(n):
            if not flex[k]:
                sizes[k] = limit[k]
        fr = 0.0
        for k in range(n):
            if flex[k]:
                fr = max(fr, base[k] / max(tracks[k][3], 1e-9) if tracks[k][3] >= 1 else base[k])
        for st, sp, mnc, mxc in items:
            rng = range(st, min(n, st + sp))
            fk = [k for k in rng if flex[k]]
            if fk:
                fixed = sum(sizes[k] for k in rng if not flex[k]) + gap * (len(rng) - 1)
                fr = max(fr, (mxc - fixed) / max(1.0, sum(tracks[k][3] for k in fk)))
        for k in range(n):
            if flex[k]:
                sizes[k] = max(base[k], fr * tracks[k][3])
        return sizes
    space = avail - gap * (n - 1)
    # Maximize tracks: grow non-flexible tracks towards their limits.
    free = space - sum(sizes)
    grow = [k for k in range(n) if not flex[k] and limit[k] > sizes[k]]
    while free > 0.01 and grow:
        share = free / len(grow)
        nxt = []
        for k in grow:
            add = min(share, limit[k] - sizes[k])
            sizes[k] += add
            free -= add
            if limit[k] - sizes[k] > 0.01:
                nxt.append(k)
        grow = nxt
    fk = [k for k in range(n) if flex[k]]
    if fk:
        leftover = space - sum(sizes[k] for k in range(n) if not flex[k])
        active = list(fk)
        hyp = 0.0
        while active:
            fsum = max(1.0, sum(tracks[k][3] for k in active))
            hyp = max(0.0, leftover) / fsum
            viol = [k for k in active if base[k] > hyp * tracks[k][3]]
            if not viol:
                break
            for k in viol:
                active.remove(k)
                leftover -= base[k]
        for k in fk:
            sizes[k] = max(base[k], hyp * tracks[k][3]) if k in active else base[k]
    elif stretch:
        free = space - sum(sizes)
        autos = [k for k in range(n) if tracks[k][2] == "auto"]
        if free > 0 and autos:
            for k in autos:
                sizes[k] += free / len(autos)
    return sizes


def css_split(value):
    out, depth, cur = [], 0, ""
    for ch in value:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch.isspace() and depth == 0:
            if cur:
                out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------------------------
# Document


def apply_offsets(box):
    stack = [box]
    while stack:
        b = stack.pop()
        b.apply_relative_offset()
        stack.extend(b.children)
        stack.extend(b.abs_boxes)
        for line in getattr(b, "lines", ()):
            for f in line.frags:
                if isinstance(f, AtomFrag):
                    stack.append(f.box)


class DocumentLayout:
    """The initial containing block. Owns the root element's box."""

    def __init__(self, document, ctx):
        self.document = document
        self.ctx = ctx
        ctx.root_box = self
        self.abs_items = []
        self.abs_boxes = []
        self.root = build_box(document, ctx)
        self.layer_boxes = []     # [(box, paint.Layer)] for fixed/sticky boxes (set by paint)
        ctx.sticky = {}
        self.width = 0
        self.height = 0
        self.background = "#ffffff"

    def layout(self, width, height):
        self.ctx.viewport_width = width
        self.ctx.viewport_height = height
        from . import style as _style
        _style.VIEWPORT["height"] = height     # for vh units
        self.ctx.positions = {}
        self.abs_items = []
        self.abs_boxes = []
        self.width = width
        fc = FloatContext()
        self.root.layout(0, 0, width, fc, positioned=None)
        # Absolute/fixed boxes with no positioned ancestor use the viewport.
        items, self.abs_items = self.abs_items, []
        for box, sx, sy in _static_positions(items):
            layout_absolute(box, 0, 0, width, height, sx, sy)
            self.abs_boxes.append(box)
        # Relative positioning and transforms are applied last, top-down, so
        # that they move a box together with everything inside it.
        apply_offsets(self.root)
        for box in self.abs_boxes:
            apply_offsets(box)
        self._record_positions()
        self._find_sticky(height)
        bottom = self.root.y + self.root.height + self.root.p[B] + self.root.b[B] + self.root.m[B]
        bottom = max(bottom, fc.bottom())
        self.height = max(bottom, height)
        self._set_canvas_background()

    def element_at(self, x, y, scroll=0):
        """The innermost element drawn at document point (x, y), for :hover.
        Fixed and sticky boxes are tested first, shifted by the scroll."""
        skip = {id(box) for box, _ in self.layer_boxes}
        for box, layer in reversed(self.layer_boxes):
            hit = self._element_in([box], x, y - layer.offset(scroll), skip - {id(box)})
            if hit is not None:
                return hit
        return self._element_in(list(reversed(self.abs_boxes)) + [self.root], x, y, skip)

    def _element_in(self, stack, x, y, skip):
        best = None
        # Pre-order walk: parents before children, so the last match is the
        # innermost (and positioned boxes, visited last, win).
        while stack:
            b = stack.pop()
            if id(b) in skip:
                continue
            x1, y1, x2, y2 = b.border_box()
            inside = x1 <= x < x2 and y1 <= y < y2
            if not inside and b.style.get("overflow", "visible") not in ("visible", "clip") and b.node is not None:
                continue   # its clipped contents are not visible here
            if inside and isinstance(b.node, Element) and b.style.get("visibility") != "hidden" and \
                    b.style.get("pointer-events") != "none":
                best = b.node
            kids = []
            for line in getattr(b, "lines", ()):
                if not (line.y <= y < line.y + line.height):
                    for f in line.frags:
                        if isinstance(f, AtomFrag):
                            kids.append(f.box)
                    continue
                for f in line.frags:
                    if isinstance(f, AtomFrag):
                        kids.append(f.box)
                    elif f.x <= x < f.x + f.width and isinstance(f.node.parent, Element) and \
                            f.style.get("pointer-events") != "none":
                        best = f.node.parent
            kids += b.children
            kids += b.abs_boxes
            stack.extend(reversed(kids))
        while best is not None and getattr(best, "is_pseudo", False):
            best = best.parent
        return best

    def _find_sticky(self, viewport_h):
        """position: sticky boxes with a `top`: where they sit and how far they
        may travel (to the bottom of their parent) -> ctx.sticky."""
        self.ctx.sticky = {}
        stack = [(self.root, None)]
        while stack:
            b, parent = stack.pop()
            if b.position == "sticky" and parent is not None:
                top = length(b.style.get("top", "auto"), b.fs, viewport_h, None)
                if top is not None:
                    x1, y1, x2, y2 = b.border_box()
                    limit = parent.y + parent.height
                    self.ctx.sticky[id(b)] = (y1, top, max(0.0, limit - y2 - b.m[B]))
            for c in b.children:
                stack.append((c, b))
            for line in getattr(b, "lines", ()):
                for f in line.frags:
                    if isinstance(f, AtomFrag):
                        stack.append((f.box, b))

    def _record_positions(self):
        """Element -> y of its first box, read from the final box tree (flex and
        grid items are laid out at the origin and moved, so the y seen during
        layout is not where they end up)."""
        layout_time = self.ctx.positions
        positions = {}
        stack = [self.root] + list(reversed(self.abs_boxes))
        while stack:
            b = stack.pop()
            if b.node is not None and b.node not in positions:
                positions[b.node] = b.y - b.p[T] - b.b[T]
            kids = []
            for line in getattr(b, "lines", ()):
                for f in line.frags:
                    if isinstance(f, AtomFrag):
                        kids.append(f.box)
                        continue
                    el = getattr(f.node, "parent", None)   # enclosing inline elements
                    while el is not None and el not in positions:
                        positions[el] = line.y
                        el = el.parent
            kids += b.children
            kids += b.abs_boxes
            stack.extend(reversed(kids))
        for node, y in layout_time.items():
            positions.setdefault(node, y)
        self.ctx.positions = positions

    def _set_canvas_background(self):
        root = self.root
        bg = color_of(root.style.get("background-color"))
        root.suppress_bg = False
        if bg:
            self.background = bg
            root.suppress_bg = True
            return
        body = next((c for c in getattr(root, "children", []) if c.node is not None and getattr(c.node, "tag", "") == "body"), None)
        if body is not None:
            bg = color_of(body.style.get("background-color"))
            if bg:
                self.background = bg
                body.suppress_bg = True
                return
        self.background = "#ffffff"

    def paint(self):
        self.ctx.scrollers = []
        self.ctx.resizers = []
        dl = paint.DisplayList()
        dl.deferred = []
        dl.negatives = []
        dl.layers = []
        self.layer_boxes = []
        # tree order of every box: ties between equal z-index values paint in this order
        n = 0
        stack = [self.root] + list(self.abs_boxes)
        stack.reverse()
        while stack:
            b = stack.pop()
            b.paint_order = n
            n += 1
            kids = list(b.children) + list(b.abs_boxes)
            for line in getattr(b, "lines", ()):
                kids += [f.box for f in line.frags if isinstance(f, AtomFrag)]
            stack.extend(reversed(kids))
        self.root.paint(dl)
        for box in self.abs_boxes:
            if box.position == "fixed":
                # Stays in place in the window while the page scrolls.
                layer = paint.Layer("fixed")
                box.paint(layer.list)
                dl.layers.append(layer)
                self.layer_boxes.append((box, layer))
            else:
                box.paint(dl)
        _drain_stacking(dl)
        dl.height = self.height
        return dl
