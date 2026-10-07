"""Style engine: cascade, specificity, inheritance and computed values.

For every element we compute a dict of property -> value. Values stay as
CSS strings, except font-size which is resolved to pixels here (since em
units everywhere depend on it). Lengths are resolved against the
containing block during layout by `length()`.
"""
import math
import re

from . import css_parser
from .dom import Element, PseudoElement, Text

INHERITED = {
    "color", "font-family", "font-size", "font-style", "font-weight", "font-variant",
    "line-height", "text-align", "white-space", "list-style-type", "list-style-position",
    "visibility", "text-transform", "letter-spacing", "word-spacing", "cursor",
    "text-indent", "direction", "border-collapse", "border-spacing", "quotes",
    "-link-href", "-link-node",  # internal: the nearest enclosing hyperlink
    "-font-px",
    "overflow-wrap", "word-wrap", "word-break", "hyphens", "text-align-last",
    "text-shadow", "tab-size", "text-wrap-style", "text-wrap-mode", "text-underline-position",
    "text-underline-offset", "font-variant-caps", "font-stretch", "font-feature-settings", "line-clamp",
    "-webkit-text-fill-color", "text-decoration-skip-ink", "direction", "writing-mode", "text-orientation",
    "pointer-events", "user-select", "caret-color", "accent-color", "quotes",
}

INITIAL = {
    "display": "inline",
    "color": "black",
    "background-color": "transparent",
    "background-image": "none",
    "font-family": "serif",
    "font-size": "16px",
    "font-style": "normal",
    "font-weight": "normal",
    "line-height": "normal",
    "text-align": "start",
    "text-decoration": "none",
    "text-transform": "none",
    "white-space": "normal",
    "width": "auto",
    "height": "auto",
    "min-width": "auto",
    "max-width": "none",
    "min-height": "auto",
    "max-height": "none",
    "position": "static",
    "float": "none",
    "clear": "none",
    "visibility": "visible",
    "list-style-type": "disc",
    "list-style-position": "outside",
    "vertical-align": "baseline",
    "box-sizing": "content-box",
    "flex-direction": "row",
    "flex-wrap": "nowrap",
    "flex-grow": "0",
    "flex-shrink": "1",
    "flex-basis": "auto",
    "justify-content": "normal",
    "align-items": "stretch",
    "row-gap": "normal",
    "column-gap": "normal",
    "top": "auto", "right": "auto", "bottom": "auto", "left": "auto",
    "overflow": "visible",
    "opacity": "1",
    "text-indent": "0",
    "border-spacing": "2px",
    "border-collapse": "separate",
    "letter-spacing": "normal",
    "word-spacing": "normal",
    "z-index": "auto",
    "order": "0",
}
for _side in ("top", "right", "bottom", "left"):
    INITIAL["margin-" + _side] = "0"
    INITIAL["padding-" + _side] = "0"
    INITIAL["border-%s-width" % _side] = "medium"
    INITIAL["border-%s-style" % _side] = "none"
    INITIAL["border-%s-color" % _side] = "currentcolor"


USER_AGENT_CSS = """
html, address, blockquote, body, center, dialog, div, figure, figcaption, footer, form, header, hr,
legend, listing, main, p, plaintext, pre, xmp, article, aside, h1, h2, h3, h4, h5, h6, hgroup, nav,
section, dl, dd, dt, ol, ul, menu, dir, fieldset, details, summary, optgroup { display: block; }
head, script, style, link, meta, title, template, noembed, iframe, datalist, param, area, map, base,
[hidden], input[type=hidden], dialog:not([open]), noscript link, noscript img[width="1"], select option,
video, audio, canvas, object, embed { display: none; }
svg { overflow: hidden; }
li { display: list-item; }
table { display: table; border-spacing: 2px; border-collapse: separate; }
caption { display: block; text-align: center; }
thead, tbody, tfoot { display: table-row-group; }
tr { display: table-row; }
td, th { display: table-cell; padding: 1px; vertical-align: middle; }
th { font-weight: bold; text-align: center; }
colgroup, col { display: none; }
body { margin: 8px; }
[dir=rtl i] { direction: rtl; } [dir=ltr i] { direction: ltr; }
bdo[dir=rtl i] { unicode-bidi: bidi-override; } bdi { unicode-bidi: isolate; }
ul:dir(rtl), ol:dir(rtl), menu:dir(rtl), dir:dir(rtl) { padding-left: 0; padding-right: 40px; }
dd:dir(rtl) { margin-left: 0; margin-right: 40px; }
p, dl, multicol, figure { margin-top: 1em; margin-bottom: 1em; }
figure { margin-left: 40px; margin-right: 40px; }
blockquote { margin: 1em 40px; }
dd { margin-left: 40px; }
h1 { font-size: 2em; margin: 0.67em 0; font-weight: bold; }
h2 { font-size: 1.5em; margin: 0.83em 0; font-weight: bold; }
h3 { font-size: 1.17em; margin: 1em 0; font-weight: bold; }
h4 { font-size: 1em; margin: 1.33em 0; font-weight: bold; }
h5 { font-size: 0.83em; margin: 1.67em 0; font-weight: bold; }
h6 { font-size: 0.67em; margin: 2.33em 0; font-weight: bold; }
ul, ol, menu, dir { margin-top: 1em; margin-bottom: 1em; padding-left: 40px; }
ul ul, ol ul, ul ol, ol ol { margin-top: 0; margin-bottom: 0; }
ul { list-style-type: disc; }
ol { list-style-type: decimal; }
ul ul { list-style-type: circle; }
ul ul ul { list-style-type: square; }
b, strong, dt { font-weight: bold; }
i, em, cite, var, dfn, address { font-style: italic; }
u, ins { text-decoration: underline; }
s, strike, del { text-decoration: line-through; }
small { font-size: smaller; }
big { font-size: larger; }
sub, sup { font-size: smaller; }
sub { vertical-align: sub; }
sup { vertical-align: super; }
pre, xmp, listing, plaintext { white-space: pre; margin: 1em 0; }
pre, code, kbd, samp, tt, xmp, listing, plaintext { font-family: monospace; font-size: 0.8125em; }
pre code, pre tt, pre kbd, pre samp { font-size: 1em; }
textarea { white-space: pre-wrap; font-family: monospace; }
a:link, a:any-link { color: #0000ee; text-decoration: underline; cursor: pointer; }
center { text-align: -webkit-center; }
hr { border: 1px inset #888; margin: 0.5em 0; height: 0; }
img { display: inline; }
input, button, select, textarea { display: inline-block; font-size: 13px; font-family: sans-serif; }
input, textarea { border: 2px inset #999; padding: 1px 2px; background-color: white; }
input { width: 150px; }
input[type=checkbox], input[type=radio] { width: 13px; height: 13px; padding: 0; border: none; background-color: transparent; margin: 3px 3px 3px 4px; }
input[type=submit], input[type=button], input[type=reset], button { width: auto; border: 1px solid #767676;
  padding: 1px 6px; background-color: #efefef; border-radius: 2px; text-align: center; }
input[type=image] { border: none; padding: 0; width: auto; }
select { border: 1px solid #767676; padding: 1px 4px; background-color: #fff; }
textarea { resize: both; overflow: auto; }     /* size comes from cols/rows */
fieldset { margin: 0 2px; padding: 0.35em 0.75em 0.625em; border: 2px groove #ccc; }
legend { padding: 0 2px; }
mark { background-color: yellow; color: black; }
q::before { content: open-quote; } q::after { content: close-quote; }
summary { font-weight: bold; }
details:not([open]) > :not(summary) { display: none; }
abbr[title] { text-decoration: underline; }
"""

NAMED_COLORS = {
    "black": "#000000", "silver": "#c0c0c0", "gray": "#808080", "grey": "#808080", "white": "#ffffff",
    "maroon": "#800000", "red": "#ff0000", "purple": "#800080", "fuchsia": "#ff00ff", "green": "#008000",
    "lime": "#00ff00", "olive": "#808000", "yellow": "#ffff00", "navy": "#000080", "blue": "#0000ff",
    "teal": "#008080", "aqua": "#00ffff", "orange": "#ffa500", "aliceblue": "#f0f8ff",
    "antiquewhite": "#faebd7", "aquamarine": "#7fffd4", "azure": "#f0ffff", "beige": "#f5f5dc",
    "bisque": "#ffe4c4", "blanchedalmond": "#ffebcd", "blueviolet": "#8a2be2", "brown": "#a52a2a",
    "burlywood": "#deb887", "cadetblue": "#5f9ea0", "chartreuse": "#7fff00", "chocolate": "#d2691e",
    "coral": "#ff7f50", "cornflowerblue": "#6495ed", "cornsilk": "#fff8dc", "crimson": "#dc143c",
    "cyan": "#00ffff", "darkblue": "#00008b", "darkcyan": "#008b8b", "darkgoldenrod": "#b8860b",
    "darkgray": "#a9a9a9", "darkgrey": "#a9a9a9", "darkgreen": "#006400", "darkkhaki": "#bdb76b",
    "darkmagenta": "#8b008b", "darkolivegreen": "#556b2f", "darkorange": "#ff8c00", "darkorchid": "#9932cc",
    "darkred": "#8b0000", "darksalmon": "#e9967a", "darkseagreen": "#8fbc8f", "darkslateblue": "#483d8b",
    "darkslategray": "#2f4f4f", "darkslategrey": "#2f4f4f", "darkturquoise": "#00ced1",
    "darkviolet": "#9400d3", "deeppink": "#ff1493", "deepskyblue": "#00bfff", "dimgray": "#696969",
    "dimgrey": "#696969", "dodgerblue": "#1e90ff", "firebrick": "#b22222", "floralwhite": "#fffaf0",
    "forestgreen": "#228b22", "gainsboro": "#dcdcdc", "ghostwhite": "#f8f8ff", "gold": "#ffd700",
    "goldenrod": "#daa520", "greenyellow": "#adff2f", "honeydew": "#f0fff0", "hotpink": "#ff69b4",
    "indianred": "#cd5c5c", "indigo": "#4b0082", "ivory": "#fffff0", "khaki": "#f0e68c",
    "lavender": "#e6e6fa", "lavenderblush": "#fff0f5", "lawngreen": "#7cfc00", "lemonchiffon": "#fffacd",
    "lightblue": "#add8e6", "lightcoral": "#f08080", "lightcyan": "#e0ffff",
    "lightgoldenrodyellow": "#fafad2", "lightgray": "#d3d3d3", "lightgrey": "#d3d3d3",
    "lightgreen": "#90ee90", "lightpink": "#ffb6c1", "lightsalmon": "#ffa07a", "lightseagreen": "#20b2aa",
    "lightskyblue": "#87cefa", "lightslategray": "#778899", "lightslategrey": "#778899",
    "lightsteelblue": "#b0c4de", "lightyellow": "#ffffe0", "limegreen": "#32cd32", "linen": "#faf0e6",
    "magenta": "#ff00ff", "mediumaquamarine": "#66cdaa", "mediumblue": "#0000cd",
    "mediumorchid": "#ba55d3", "mediumpurple": "#9370db", "mediumseagreen": "#3cb371",
    "mediumslateblue": "#7b68ee", "mediumspringgreen": "#00fa9a", "mediumturquoise": "#48d1cc",
    "mediumvioletred": "#c71585", "midnightblue": "#191970", "mintcream": "#f5fffa",
    "mistyrose": "#ffe4e1", "moccasin": "#ffe4b5", "navajowhite": "#ffdead", "oldlace": "#fdf5e6",
    "olivedrab": "#6b8e23", "orangered": "#ff4500", "orchid": "#da70d6", "palegoldenrod": "#eee8aa",
    "palegreen": "#98fb98", "paleturquoise": "#afeeee", "palevioletred": "#db7093",
    "papayawhip": "#ffefd5", "peachpuff": "#ffdab9", "peru": "#cd853f", "pink": "#ffc0cb",
    "plum": "#dda0dd", "powderblue": "#b0e0e6", "rebeccapurple": "#663399", "rosybrown": "#bc8f8f",
    "royalblue": "#4169e1", "saddlebrown": "#8b4513", "salmon": "#fa8072", "sandybrown": "#f4a460",
    "seagreen": "#2e8b57", "seashell": "#fff5ee", "sienna": "#a0522d", "skyblue": "#87ceeb",
    "slateblue": "#6a5acd", "slategray": "#708090", "slategrey": "#708090", "snow": "#fffafa",
    "springgreen": "#00ff7f", "steelblue": "#4682b4", "tan": "#d2b48c", "thistle": "#d8bfd8",
    "tomato": "#ff6347", "turquoise": "#40e0d0", "violet": "#ee82ee", "wheat": "#f5deb3",
    "whitesmoke": "#f5f5f5", "yellowgreen": "#9acd32",
    # system colours
    "canvas": "#ffffff", "canvastext": "#000000", "linktext": "#0000ee", "buttonface": "#efefef",
    "buttontext": "#000000", "field": "#ffffff", "fieldtext": "#000000", "graytext": "#808080",
    "highlight": "#3399ff", "highlighttext": "#ffffff", "window": "#ffffff", "windowtext": "#000000",
}


# ---------------------------------------------------------------------------
# Value parsing helpers


def parse_color(value, current="#000000"):
    """Return '#rrggbb', or None for transparent / unparseable.

    Tk has no alpha channel, so translucent colours are blended onto white.
    """
    if not value:
        return None
    v = value.strip().lower()
    if v in ("transparent", "none", "initial", "unset", "inherit"):
        return None
    if v == "currentcolor":
        return current
    if v in NAMED_COLORS:
        return NAMED_COLORS[v]
    if v.startswith("#"):
        h = v[1:]
        if not re.fullmatch(r"[0-9a-f]+", h):
            return None
        if len(h) in (3, 4):
            r, g, b = (int(c * 2, 16) for c in h[:3])
            a = int(h[3] * 2, 16) / 255 if len(h) == 4 else 1
        elif len(h) in (6, 8):
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            a = int(h[6:8], 16) / 255 if len(h) == 8 else 1
        else:
            return None
        return _blend(r, g, b, a)
    if v.startswith(("hwb(", "lab(", "lch(", "oklab(", "oklch(", "color(", "color-mix(", "light-dark(")):
        rgba = to_rgba(v, current)
        return _blend(*rgba) if rgba else None
    m = re.match(r"(rgba?|hsla?)\((.*)\)$", v)
    if m:
        fn, args = m.group(1), m.group(2)
        parts = [p for p in re.split(r"[\s,/]+", args.strip()) if p]
        if len(parts) < 3:
            return None
        try:
            alpha = 1.0
            if len(parts) >= 4:
                alpha = float(parts[3][:-1]) / 100 if parts[3].endswith("%") else float(parts[3])
            if fn.startswith("rgb"):
                rgb = []
                for p in parts[:3]:
                    if p.endswith("%"):
                        rgb.append(float(p[:-1]) * 2.55)
                    else:
                        rgb.append(float(p))
                return _blend(*rgb, alpha)
            h = float(re.sub(r"deg$", "", parts[0])) % 360 / 360
            s = float(parts[1].rstrip("%")) / 100
            li = float(parts[2].rstrip("%")) / 100
            import colorsys
            r, g, b = colorsys.hls_to_rgb(h, li, s)
            return _blend(r * 255, g * 255, b * 255, alpha)
        except ValueError:
            return None
    return None


def to_rgba(value, current="#000000"):
    """Any CSS colour -> (r, g, b, alpha) with r/g/b in 0..255, unblended.
    Covers the modern functions: hwb(), lab(), lch(), oklab(), oklch(),
    color(srgb ...), color-mix() and light-dark()."""
    v = value.strip().lower()
    if v in ("transparent",):
        return (0.0, 0.0, 0.0, 0.0)
    m = re.match(r"([a-z-]+)\((.*)\)$", v, re.S)
    if not m:
        c = parse_color(v, current)
        if c is None:
            return None
        return (int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16), 1.0)
    fn, args = m.group(1), m.group(2).strip()
    if fn == "light-dark":
        from .css_parser import _split_top_level
        return to_rgba(_split_top_level(args, ",")[0], current)
    if fn == "color-mix":
        return _color_mix(args, current)
    if fn in ("rgb", "rgba", "hsl", "hsla"):
        alpha = 1.0
        parts = [p for p in re.split(r"[\s,/]+", args) if p]
        if len(parts) >= 4:
            alpha = _num_or_pct(parts[3], 1.0)
        c = parse_color("%s(%s)" % (fn.rstrip("a"), " ".join(parts[:3])), current)
        if c is None:
            return None
        return (int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16), alpha)
    main, _, alpha_s = args.partition("/")
    alpha = _num_or_pct(alpha_s.strip(), 1.0) if alpha_s.strip() else 1.0
    parts = [p for p in re.split(r"[\s,]+", main.strip()) if p]
    if fn == "color":
        space, parts = parts[0], parts[1:]
        if len(parts) < 3:
            return None
        r, g, b = (_num_or_pct(p, 1.0) for p in parts[:3])
        if space == "srgb-linear":
            return tuple(_gamma(c) * 255 for c in (r, g, b)) + (alpha,)
        if space in ("xyz", "xyz-d65"):
            return _xyz_to_rgb(r, g, b) + (alpha,)
        return (r * 255, g * 255, b * 255, alpha)   # srgb, display-p3 (close enough)
    if len(parts) < 3:
        return None
    try:
        if fn == "hwb":
            h = _hue(parts[0])
            w, bl = _num_or_pct(parts[1], 1.0, 100), _num_or_pct(parts[2], 1.0, 100)
            if w + bl >= 1:
                g = w / (w + bl) * 255
                return (g, g, g, alpha)
            import colorsys
            r, g, b = colorsys.hls_to_rgb(h / 360, 0.5, 1.0)
            f = 1 - w - bl
            return (255 * (r * f + w), 255 * (g * f + w), 255 * (b * f + w), alpha)
        if fn in ("oklab", "oklch"):
            L = _num_or_pct(parts[0], 1.0)
            if fn == "oklab":
                A, B = _num_or_pct(parts[1], 0.4), _num_or_pct(parts[2], 0.4)
            else:
                C, h = _num_or_pct(parts[1], 0.4), math.radians(_hue(parts[2]))
                A, B = C * math.cos(h), C * math.sin(h)
            return _oklab_to_rgb(L, A, B) + (alpha,)
        if fn in ("lab", "lch"):
            L = _num_or_pct(parts[0], 100.0)
            if fn == "lab":
                A, B = _num_or_pct(parts[1], 125.0), _num_or_pct(parts[2], 125.0)
            else:
                C, h = _num_or_pct(parts[1], 150.0), math.radians(_hue(parts[2]))
                A, B = C * math.cos(h), C * math.sin(h)
            return _lab_to_rgb(L, A, B) + (alpha,)
    except (ValueError, IndexError):
        return None
    return None


def _num_or_pct(tok, pct_scale, plain_scale=1.0):
    """'50%' -> 0.5 * pct_scale ; '0.3' -> 0.3 / plain_scale ; 'none' -> 0."""
    tok = tok.strip()
    if tok in ("none", ""):
        return 0.0
    if tok.endswith("%"):
        return float(tok[:-1]) / 100 * pct_scale
    return float(tok) / plain_scale


def _hue(tok):
    tok = tok.strip()
    if tok == "none":
        return 0.0
    if tok.endswith("deg"):
        return float(tok[:-3])
    if tok.endswith("turn"):
        return float(tok[:-4]) * 360
    if tok.endswith("rad"):
        return math.degrees(float(tok[:-3]))
    return float(tok)


def _gamma(c):
    c = max(0.0, min(1.0, c))
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def _linear(c):
    c = c / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _oklab_to_rgb(L, A, B):
    l_ = L + 0.3963377774 * A + 0.2158037573 * B
    m_ = L - 0.1055613458 * A - 0.0638541728 * B
    s_ = L - 0.0894841775 * A - 1.2914855480 * B
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    r = 4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
    g = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
    b = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s
    return tuple(_gamma(c) * 255 for c in (r, g, b))


def _rgb_to_oklab(r, g, b):
    r, g, b = _linear(r), _linear(g), _linear(b)
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = (math.copysign(abs(x) ** (1 / 3), x) for x in (l, m, s))
    return (0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
            1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
            0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_)


def _xyz_to_rgb(x, y, z):
    r = 3.2409699419 * x - 1.5373831776 * y - 0.4986107603 * z
    g = -0.9692436363 * x + 1.8759675015 * y + 0.0415550574 * z
    b = 0.0556300797 * x - 0.2039769589 * y + 1.0569715142 * z
    return tuple(_gamma(c) * 255 for c in (r, g, b))


def _lab_to_rgb(L, A, B):
    # CIE Lab (D50) -> XYZ D50 -> XYZ D65 (Bradford) -> sRGB
    fy = (L + 16) / 116
    fx, fz = fy + A / 500, fy - B / 200
    e, k = 216 / 24389, 24389 / 27
    xr = fx ** 3 if fx ** 3 > e else (116 * fx - 16) / k
    yr = ((L + 16) / 116) ** 3 if L > k * e else L / k
    zr = fz ** 3 if fz ** 3 > e else (116 * fz - 16) / k
    x, y, z = xr * 0.96422, yr * 1.0, zr * 0.82521
    x2 = 0.9554734527 * x - 0.0230985368 * y + 0.0632593086 * z
    y2 = -0.0283697093 * x + 1.0099954580 * y + 0.0210413990 * z
    z2 = 0.0123140016 * x - 0.0205076964 * y + 1.3303659366 * z
    return _xyz_to_rgb(x2, y2, z2)


def _color_mix(args, current):
    from .css_parser import _split_top_level
    parts = [p.strip() for p in _split_top_level(args, ",")]
    if len(parts) < 3:
        return None
    space = parts[0].replace("in", "", 1).strip().split()[0] if parts[0].startswith("in") else "srgb"
    cols = []
    for p in parts[1:3]:
        m = re.search(r"\s(\d*\.?\d+)%\s*$", " " + p)
        pct = float(m.group(1)) / 100 if m else None
        ctext = p[:m.start() - 1].strip() if m else p
        m2 = re.match(r"^(\d*\.?\d+)%\s+(.*)$", p)
        if m2:
            pct, ctext = float(m2.group(1)) / 100, m2.group(2)
        rgba = to_rgba(ctext, current)
        if rgba is None:
            return None
        cols.append((rgba, pct))
    (c1, p1), (c2, p2) = cols
    if p1 is None and p2 is None:
        p1 = p2 = 0.5
    elif p1 is None:
        p1 = 1 - p2
    elif p2 is None:
        p2 = 1 - p1
    total = p1 + p2
    if total <= 0:
        return None
    w2 = p2 / total
    alpha = c1[3] * (1 - w2) + c2[3] * w2
    if space in ("oklab", "oklch"):
        a, b = _rgb_to_oklab(*c1[:3]), _rgb_to_oklab(*c2[:3])
        mixed = _oklab_to_rgb(*(x * (1 - w2) + y * w2 for x, y in zip(a, b)))
    else:
        mixed = tuple(x * (1 - w2) + y * w2 for x, y in zip(c1[:3], c2[:3]))
    return tuple(mixed) + (alpha * min(1.0, total),)


def _blend(r, g, b, a):
    if a <= 0.02:
        return None
    a = min(1.0, a)
    r = r * a + 255 * (1 - a)
    g = g * a + 255 * (1 - a)
    b = b * a + 255 * (1 - a)
    clamp = lambda x: max(0, min(255, int(round(x))))
    return "#%02x%02x%02x" % (clamp(r), clamp(g), clamp(b))


FONT_SIZE_KEYWORDS = {
    "xx-small": 9, "x-small": 10, "small": 13, "medium": 16, "large": 18,
    "x-large": 24, "xx-large": 32, "xxx-large": 48,
}

VIEWPORT = {"width": 1000, "height": 700}


def _eval_calc(expr, font_size, percent_base):
    """Evaluate calc() and the other CSS math functions (min, max, clamp,
    round, mod, rem, abs, sign, sin, cos, tan, asin, acos, atan, atan2, pow,
    sqrt, hypot, log, exp) to pixels (or a plain number)."""
    try:
        p = _CalcParser(expr, font_size, percent_base)
        v = p.expr()
        if p.i != len(p.toks):
            return None
        return float(v[0])
    except (ValueError, IndexError, ZeroDivisionError, OverflowError, TypeError):
        return None


_CALC_TOKEN = re.compile(r"\s*(?:(-?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)([a-z%]*)|([a-z][a-z0-9-]*)\s*(\()?|(\*|/|\+|-|\(|\)|,))",
                         re.I)
_ANGLES = {"deg": math.pi / 180, "rad": 1.0, "grad": math.pi / 200, "turn": 2 * math.pi}


class _CalcParser:
    """Recursive-descent evaluator. Values are (number, kind) with kind
    'num', 'len' (pixels) or 'angle' (radians)."""

    def __init__(self, text, font_size, percent_base):
        self.fs, self.pb = font_size, percent_base
        self.toks = []
        i = 0
        text = text.strip()
        while i < len(text):
            m = _CALC_TOKEN.match(text, i)
            if not m or m.end() == i:
                if text[i:].strip() == "":
                    break
                raise ValueError(text[i:])
            i = m.end()
            if m.group(1) is not None:
                self.toks.append(("n", m.group(1), m.group(2).lower()))
            elif m.group(3) is not None:
                self.toks.append(("f" if m.group(4) else "id", m.group(3).lower(), None))
            else:
                self.toks.append(("op", m.group(5), None))
        self.i = 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None, None)

    def take(self, op=None):
        t = self.toks[self.i]
        if op is not None and t[1] != op:
            raise ValueError(op)
        self.i += 1
        return t

    def expr(self):
        v = self.term()
        while self.peek()[0] == "op" and self.peek()[1] in "+-":
            op = self.take()[1]
            w = self.term()
            kind = v[1] if v[1] != "num" else w[1]
            v = (v[0] + w[0] if op == "+" else v[0] - w[0], kind)
        return v

    def term(self):
        v = self.factor()
        while self.peek()[0] == "op" and self.peek()[1] in "*/":
            op = self.take()[1]
            w = self.factor()
            if op == "*":
                v = (v[0] * w[0], v[1] if v[1] != "num" else w[1])
            else:
                v = (v[0] / w[0], v[1] if w[1] == "num" else "num")
        return v

    def factor(self):
        t = self.peek()
        if t[0] == "op" and t[1] in "+-":
            self.take()
            v = self.factor()
            return (-v[0], v[1]) if t[1] == "-" else v
        if t[0] == "op" and t[1] == "(":
            self.take()
            v = self.expr()
            self.take(")")
            return v
        if t[0] == "n":
            self.take()
            num, unit = float(t[1]), t[2]
            if not unit:
                return (num, "num")
            if unit in _ANGLES:
                return (num * _ANGLES[unit], "angle")
            px = length(t[1] + unit, self.fs, self.pb)
            if px is None:
                raise ValueError(unit)
            return (px, "len")
        if t[0] == "id":
            self.take()
            consts = {"pi": math.pi, "e": math.e, "infinity": 1e300, "-infinity": -1e300}
            if t[1] in consts:
                return (consts[t[1]], "num")
            raise ValueError(t[1])
        if t[0] == "f":
            self.take()
            name = t[1]
            args = []
            if not (self.peek()[0] == "op" and self.peek()[1] == ")"):
                while True:
                    if name == "round" and not args and self.peek()[0] == "id" and \
                            self.peek()[1] in ("nearest", "up", "down", "to-zero"):
                        args.append((self.take()[1], "strategy"))
                    else:
                        args.append(self.expr())
                    if self.peek()[1] == ",":
                        self.take()
                        continue
                    break
            self.take(")")
            return self.call(name, args)
        raise ValueError(t)

    def call(self, name, args):
        vals = [a for a in args if a[1] != "strategy"]
        kind = next((v[1] for v in vals if v[1] != "num"), "num")
        nums = [v[0] for v in vals]
        if name == "calc":
            return vals[0]
        if name == "min":
            return (min(nums), kind)
        if name == "max":
            return (max(nums), kind)
        if name == "clamp":
            return (max(nums[0], min(nums[1], nums[2])), kind)
        if name == "round":
            strategy = args[0][0] if args and args[0][1] == "strategy" else "nearest"
            a = nums[0]
            b = nums[1] if len(nums) > 1 else 1.0
            if b == 0:
                return (a, kind)
            q = a / b
            r = {"nearest": math.floor(q + 0.5), "up": math.ceil(q), "down": math.floor(q),
                 "to-zero": math.trunc(q)}[strategy]
            return (r * b, kind)
        if name == "mod":
            return (nums[0] - nums[1] * math.floor(nums[0] / nums[1]), kind)
        if name == "rem":
            return (math.fmod(nums[0], nums[1]), kind)
        if name == "abs":
            return (abs(nums[0]), kind)
        if name == "sign":
            return ((nums[0] > 0) - (nums[0] < 0), "num")
        if name in ("sin", "cos", "tan"):
            return (getattr(math, name)(nums[0]), "num")
        if name in ("asin", "acos", "atan"):
            return (getattr(math, name)(nums[0]), "angle")
        if name == "atan2":
            return (math.atan2(nums[0], nums[1]), "angle")
        if name == "pow":
            return (nums[0] ** nums[1], "num")
        if name == "sqrt":
            return (math.sqrt(nums[0]), "num")
        if name == "hypot":
            return (math.hypot(*nums), kind)
        if name == "log":
            return (math.log(nums[0]) if len(nums) == 1 else math.log(nums[0], nums[1]), "num")
        if name == "exp":
            return (math.exp(nums[0]), "num")
        if name == "var":
            raise ValueError("var")
        raise ValueError(name)


MATH_FUNCTIONS = ("calc(", "min(", "max(", "clamp(", "round(", "mod(", "rem(", "abs(", "sign(", "sin(", "cos(",
                  "tan(", "asin(", "acos(", "atan(", "atan2(", "pow(", "sqrt(", "hypot(", "log(", "exp(")
_LENGTH_CACHE = {}


def length(value, font_size=16, percent_base=None, auto=None):
    """Convert a CSS length to pixels. Returns `auto` for 'auto'/unknown and
    None if a percentage has no base. (Memoised: layout calls this a lot.)"""
    key = (value, font_size, percent_base, auto, ROOT_FONT_SIZE[0], VIEWPORT["width"], VIEWPORT["height"])
    r = _LENGTH_CACHE.get(key, _MISSING)
    if r is _MISSING:
        r = _length(value, font_size, percent_base, auto)
        if len(_LENGTH_CACHE) > 200000:
            _LENGTH_CACHE.clear()
        _LENGTH_CACHE[key] = r
    return r


_MISSING = object()


def _length(value, font_size, percent_base, auto):
    if value is None:
        return auto
    v = value.strip().lower()
    if v in ("auto", "none", "normal", "", "initial", "unset", "inherit", "fit-content", "max-content", "min-content"):
        return auto
    if v.startswith(MATH_FUNCTIONS):
        r = _eval_calc(v, font_size, percent_base)
        return auto if r is None else r
    if v in ("thin",):
        return 1
    if v in ("medium",):
        return 3
    if v in ("thick",):
        return 5
    m = re.match(r"^(-?\d*\.?\d+(?:e-?\d+)?)(px|em|rem|pt|%|ex|ch|ic|lh|rlh|cap|[dsl]?vw|[dsl]?vh|[dsl]?vmin|[dsl]?vmax|vi|vb|cm|mm|in|pc|q)?$", v)
    if not m:
        return auto
    num = float(m.group(1))
    unit = m.group(2)
    if unit and unit[0] in "dsl" and unit[1:] in ("vw", "vh", "vmin", "vmax"):
        unit = unit[1:]      # dynamic/small/large viewport units: the window has one size
    elif unit == "vi":
        unit = "vw"
    elif unit == "vb":
        unit = "vh"
    if unit in (None, "px"):
        return num
    if unit == "em":
        return num * font_size
    if unit == "rem":
        return num * ROOT_FONT_SIZE[0]
    if unit == "pt":
        return num * 4 / 3
    if unit == "pc":
        return num * 16
    if unit == "ex":
        return num * font_size * 0.519    # x-height of Arial/Helvetica-like faces
    if unit == "ch":
        return num * font_size * 0.556    # advance of "0" in the same faces
    if unit == "ic":
        return num * font_size
    if unit == "lh":
        return num * font_size * 1.2
    if unit == "rlh":
        return num * ROOT_FONT_SIZE[0] * 1.2
    if unit == "cap":
        return num * font_size * 0.716
    if unit == "%":
        if percent_base is None:
            return auto
        return num * percent_base / 100
    if unit == "vw":
        return num * VIEWPORT["width"] / 100
    if unit == "vh":
        return num * VIEWPORT["height"] / 100
    if unit == "vmin":
        return num * min(VIEWPORT["width"], VIEWPORT["height"]) / 100
    if unit == "vmax":
        return num * max(VIEWPORT["width"], VIEWPORT["height"]) / 100
    if unit == "in":
        return num * 96
    if unit == "cm":
        return num * 96 / 2.54
    if unit == "mm":
        return num * 96 / 25.4
    if unit == "q":
        return num * 96 / 101.6
    return auto


ROOT_FONT_SIZE = [16.0]


def compute_font_size(value, parent_px):
    v = value.strip().lower()
    if v in FONT_SIZE_KEYWORDS:
        return float(FONT_SIZE_KEYWORDS[v])
    if v == "smaller":
        return parent_px / 1.2
    if v == "larger":
        return parent_px * 1.2
    if v.endswith("%"):
        try:
            return float(v[:-1]) * parent_px / 100
        except ValueError:
            return parent_px
    px = length(v, parent_px, parent_px)
    if px is None or px <= 0:
        return parent_px
    return px


# ---------------------------------------------------------------------------
# Cascade

_VAR_RE = re.compile(r"var\(\s*(--[\w-]+)\s*(?:,\s*((?:[^()]|\([^()]*\))*))?\)")


def _substitute(value, variables, depth=0):
    if "var(" not in value or depth > 10:
        return value

    def repl(m):
        name, fallback = m.group(1), m.group(2)
        v = variables.get(name)
        if v is None:
            v = fallback if fallback is not None else ""
        return _substitute(v, variables, depth + 1)
    out = _VAR_RE.sub(repl, value)
    if "var(" in out and out != value:
        out = _substitute(out, variables, depth + 1)
    return out


# Properties that never change box positions or sizes: a hover/focus change
# that only touches these is repainted without a new layout.
PAINT_ONLY = {
    "transform", "translate", "rotate", "scale", "transform-origin", "filter", "backdrop-filter",
    "mix-blend-mode", "clip-path", "box-shadow", "text-shadow", "animation-play-state",
    "color", "background-color", "background-image", "background-position", "background-size",
    "background-repeat", "text-decoration", "text-decoration-line", "text-decoration-color",
    "text-decoration-style", "text-decoration-thickness", "text-underline-offset", "outline",
    "outline-color", "outline-style", "outline-width", "outline-offset", "opacity", "visibility",
    "cursor", "box-shadow", "text-shadow", "filter", "fill", "stroke", "caret-color", "transition",
    "transition-property", "transition-duration", "transition-delay", "transition-timing-function",
    "mask-image", "-webkit-mask-image", "mask-position", "mask-size", "mask-repeat", "-vars",
    "border-top-color", "border-right-color", "border-bottom-color", "border-left-color",
    "border-radius", "border-top-left-radius", "border-top-right-radius", "border-bottom-left-radius",
    "border-bottom-right-radius", "pointer-events", "user-select", "will-change",
}


class StyleEngine:
    def __init__(self, author_rules, registered=None):
        self.registered = registered or {}    # @property --name -> (inherits, initial value)
        ua_rules, _ = css_parser.parse_stylesheet(USER_AGENT_CSS, order_start=-100000)
        # Index rules by the key selector's id / class / tag so that each
        # element only checks rules that could possibly match it.
        self.index_id = {}
        self.index_class = {}
        self.index_tag = {}
        self.universal = []
        self.pseudo_rules = {}
        self.dynamic = []        # [(compound with :hover etc., sibling combinator follows)]
        self.rule_count = 0
        for origin, rules in ((0, ua_rules), (1, author_rules)):
            for rule in rules:
                for sel in rule.selectors:
                    self._index(origin, sel, rule)
        self.rule_count = len(ua_rules) + len(author_rules)
        self.ancestors = None

    def _index_dynamic(self, sel):
        """Remember selectors that test :hover/:focus/... and, for each
        dynamic compound, whether a sibling combinator follows it."""
        for i, (_, comp) in enumerate(sel.parts):
            if css_parser.is_dynamic_compound(comp):
                siblings = any(comb in ("+", "~") for comb, _ in sel.parts[i + 1:])
                self.dynamic.append((comp, siblings))

    def _index(self, origin, sel, rule):
        entry = (origin, sel, rule)
        self._index_dynamic(sel)
        if sel.pseudo_element:
            self.pseudo_rules.setdefault(sel.pseudo_element, []).append(entry)
            return
        key = sel.parts[-1][1]
        if key.ids:
            self.index_id.setdefault(key.ids[0], []).append(entry)
        elif key.classes:
            self.index_class.setdefault(key.classes[0], []).append(entry)
        elif key.tag and key.tag != "*":
            self.index_tag.setdefault(key.tag, []).append(entry)
        else:
            self.universal.append(entry)

    def candidate_rules(self, el):
        cands = list(self.universal)
        cands += self.index_tag.get(el.tag, ())
        ident = el.attributes.get("id")
        if ident:
            cands += self.index_id.get(ident, ())
        for c in el.attributes.get("class", "").split():
            cands += self.index_class.get(c, ())
        return cands

    def matched_declarations(self, el, pseudo=None):
        """Return declarations sorted by cascade precedence (lowest first)."""
        matched = []
        seen = set()
        cands = self.pseudo_rules.get(pseudo, ()) if pseudo else self.candidate_rules(el)
        for origin, sel, rule in cands:
            key = (id(sel), id(rule))
            if key in seen:
                continue
            seen.add(key)
            req = sel.ancestor_requirements
            if req and self.ancestors is not None:
                anc = self.ancestors
                if any(r not in anc for r in req):
                    continue
            try:
                ok = sel.matches(el)
            except RecursionError:
                ok = False
            if ok and getattr(rule, "container", None):
                ok = css_parser.container_matches(rule.container, el)
            if ok:
                lyr = getattr(rule, "layer", None)
                for prop, value, important in rule.declarations:
                    matched.append((origin, important, sel.specificity, rule.order, prop, value, lyr))
        inline = el.attributes.get("style") if not pseudo else None
        if inline:
            for prop, value, important in css_parser.parse_declarations(inline):
                matched.append((1, important, (1000, 0, 0), 10 ** 9, prop, value, "inline"))
        # Precedence: UA normal < author normal < author !important < UA !important.
        # Within author rules, @layer order: layered < unlayered for normal
        # declarations, and the reverse (earlier layers win) for !important.
        def key(d):
            origin, important, spec, order = d[0], d[1], d[2], d[3]
            lyr = d[6] if len(d) > 6 else None
            if important:
                layer = 2 if origin == 1 else 3
                rank = 1e9 if lyr == "inline" else (-1e9 if lyr is None else -lyr)
            else:
                layer = origin
                rank = 1e9 if lyr is None or lyr == "inline" else lyr
            return (layer, rank, spec, order)
        matched.sort(key=key)
        return matched

    def style_tree(self, node, parent_style=None, ancestors=None):
        """Compute styles for the whole tree (iteratively, to cope with very deep documents).

        While walking we keep a multiset of the tags/ids/classes of the current
        element's ancestors, so selectors like `.nav li a` can be rejected
        without walking up the tree (the "ancestor filter" real engines use)."""
        self.ancestors = ancestors if ancestors is not None else {}
        # CSS counters run in document order, so only a full pass updates them;
        # a partial restyle (hover etc.) reuses the values saved on elements.
        self.full_pass = ancestors is None
        if self.full_pass:
            self.counters = {}
            self.quote_depth = [0]
        self.pending_first_letter = []
        EXIT = object()
        stack = [(node, parent_style)]
        while stack:
            n, ps = stack.pop()
            if n is EXIT:
                feats, owner = ps
                self._pop_features(feats)
                if self.full_pass and self.counters:
                    for st in self.counters.values():
                        while st and st[-1][1] is owner:
                            st.pop()
                continue
            self._style_node(n, ps)
            pending = getattr(n, "pending_content", None)
            if pending is not None:
                content, owner = pending
                key = "quote_at_" + n.tag
                if self.full_pass:
                    setattr(owner, key, self.quote_depth[0])
                    qstate = self.quote_depth
                else:
                    qstate = [getattr(owner, key, 0)]
                text = generated_text(content, owner, getattr(owner, "counters_at", {}), qstate)
                n.pending_content = None
                if text:
                    n.children.append(Text(text, n))
            if isinstance(n, Element) and not isinstance(n, PseudoElement) and self.full_pass:
                self._apply_counters(n)
            if isinstance(n, Element) and self.pseudo_rules and not isinstance(n, PseudoElement):
                self._generate_pseudos(n)
            if n.children:
                if isinstance(n, Element):
                    feats = self._features(n)
                    self._push_features(feats)
                    stack.append((EXIT, (feats, n)))
                for child in reversed(n.children):
                    stack.append((child, n.style))
        # ::first-letter needs the ::before content in place, so it runs last.
        for el, decls in self.pending_first_letter:
            _apply_first_letter(self, el, decls)
        self.pending_first_letter = []

    def _apply_counters(self, el):
        """counter-reset / counter-set / counter-increment on an element."""
        s = el.style
        reset, cset, inc = s.get("counter-reset"), s.get("counter-set"), s.get("counter-increment")
        if not (reset or cset or inc):
            return
        if reset and reset != "none":
            for name, val in _counter_pairs(reset, 0):
                st = self.counters.setdefault(name, [])
                if st and st[-1][1] is el.parent:
                    st[-1][0] = val
                else:
                    st.append([val, el.parent])
        for value, add in ((cset, False), (inc, True)):
            if value and value != "none":
                for name, val in _counter_pairs(value, 1 if add else 0):
                    st = self.counters.setdefault(name, [])
                    if not st:
                        st.append([0, None])
                    st[-1][0] = st[-1][0] + val if add else val

    # --- dynamic states (:hover, :focus, ...) ------------------------
    def dynamic_roots(self, changed, kinds):
        """Subtrees that must be restyled after the elements in `changed`
        gained or lost the pseudo-class states named in `kinds`
        (e.g. ("hover",) or ("focus", "focus-visible", "focus-within"))."""
        if not self.dynamic:
            return []
        roots = []
        css_parser.IGNORE_DYNAMIC[0] = kinds
        try:
            for el in changed:
                for comp, siblings in self.dynamic:
                    if comp.matches(el):
                        root = el.parent if (siblings and isinstance(el.parent, Element)) else el
                        if root not in roots:
                            roots.append(root)
        finally:
            css_parser.IGNORE_DYNAMIC[0] = None
        # drop roots that sit inside another root
        out = []
        for r in roots:
            if not any(a in roots for a in r.ancestors()):
                out.append(r)
        return out

    def restyle_subtree(self, root):
        """Recompute styles below `root` in place. Returns True when the
        change can move boxes (needs a new layout), False when only
        paint properties such as colours changed."""
        nodes = [root] + list(root.descendants())
        old_styles = [(n, n.style) for n in nodes if not isinstance(n, PseudoElement)
                      and not isinstance(n.parent, PseudoElement)]
        old_pseudos = {n: [c for c in n.children if isinstance(c, PseudoElement)]
                       for n in nodes if isinstance(n, Element)}
        chain = [a for a in root.ancestors() if isinstance(a, Element)]
        ancestors = {}
        for a in reversed(chain):
            for f in self._features(a):
                ancestors[f] = ancestors.get(f, 0) + 1
        parent = root.parent
        self.style_tree(root, parent.style if parent is not None else None, ancestors)
        needs_layout = False
        updates = [(n, old, n.style) for n, old in old_styles]
        for el, olds in old_pseudos.items():
            news = [c for c in el.children if isinstance(c, PseudoElement)]
            if [(p.tag, p.text_content()) for p in olds] != [(p.tag, p.text_content()) for p in news]:
                needs_layout = True
                continue
            # Same generated content: keep the old pseudo nodes (the layout
            # boxes point at them) and give them the new styles.
            el.children = [olds[news.index(c)] if isinstance(c, PseudoElement) else c for c in el.children]
            for o, n in zip(olds, news):
                o.pseudo_decls = n.pseudo_decls
                updates.append((o, o.style, n.style))
                for oc, nc in zip(o.children, n.children):
                    updates.append((oc, oc.style, nc.style))
        self.last_changes = []      # (node, prop, old, new) for transitions
        for node, old, new in updates:
            if old is new:
                continue
            if old != new:
                for k in set(old) | set(new):
                    if old.get(k) != new.get(k):
                        if not k.startswith("-"):
                            self.last_changes.append((node, k, old.get(k), new.get(k)))
                        if k not in PAINT_ONLY:
                            needs_layout = True
            old.clear()
            old.update(new)
            node.style = old
        return needs_layout

    def _pseudo_style(self, el, which):
        """Style dict for a pseudo-element that is not a box of its own
        (::marker, ::placeholder, ::first-line, ::selection), or None."""
        decls = self.matched_declarations(el, which)
        if not decls:
            return None
        pe = PseudoElement(which, el)
        pe.pseudo_decls = decls
        self._style_node(pe, el.style)
        pe.style["-declared"] = {d[4] for d in decls}
        return pe.style

    def _generate_pseudos(self, el):
        """Create ::before/::after children for rules with a `content` value,
        and the styles of ::marker, ::placeholder, ::first-line, ::first-letter
        and ::selection."""
        _undo_first_letter(el)
        el.children = [c for c in el.children if not isinstance(c, PseudoElement)]
        for which, attr in (("marker", "marker_style"), ("placeholder", "placeholder_style"),
                            ("first-line", "first_line_style"), ("selection", "selection_style")):
            if which in self.pseudo_rules:
                setattr(el, attr, self._pseudo_style(el, which))
        if el.style.get("display") == "none" or el.tag in ("img", "input", "br", "hr", "svg", "select", "textarea", "iframe"):
            return
        if "first-letter" in self.pseudo_rules and el.style.get("display") not in ("inline", "none"):
            decls = self.matched_declarations(el, "first-letter")
            if decls:
                self.pending_first_letter.append((el, decls))
        for which in ("before", "after"):
            if which not in self.pseudo_rules:
                continue
            decls = self.matched_declarations(el, which)
            if not decls:
                continue
            content = None
            for d in decls:
                if d[4] == "content":
                    content = d[5]
            if content is None or content.strip() in ("none", "normal", ""):
                continue
            if "counter" in content and self.full_pass:
                el.counters_at = {k: [v for v, _ in st] for k, st in self.counters.items() if st}
            pe = PseudoElement(which, el)
            pe.pseudo_decls = decls
            if "quote" in content:
                # open/close-quote depend on the quotes before them in document
                # order: the text is made when the pseudo-element is styled.
                pe.pending_content = (content, el)
                text = ""
            else:
                text = generated_text(content, el, getattr(el, "counters_at", {}))
            for um in css_parser.URL_RE.finditer(content):
                # content: url(): an image inside the generated box
                img = Element("img", {"src": css_parser.url_of(um)}, pe)
                pe.children.append(img)
            if text:
                pe.children.append(Text(text, pe))
            if which == "before":
                el.children.insert(0, pe)
            else:
                el.children.append(pe)

    @staticmethod
    def _features(el):
        f = [el.tag]
        ident = el.attributes.get("id")
        if ident:
            f.append("#" + ident)
        for c in el.attributes.get("class", "").split():
            f.append("." + c)
        return f

    def _push_features(self, feats):
        a = self.ancestors
        for f in feats:
            a[f] = a.get(f, 0) + 1

    def _pop_features(self, feats):
        a = self.ancestors
        for f in feats:
            a[f] -= 1
            if not a[f]:
                del a[f]

    def _style_node(self, node, parent_style):
        parent_style = parent_style or {}
        style = {}
        # Inherit.
        for prop in INHERITED:
            if prop in parent_style:
                style[prop] = parent_style[prop]
        variables = parent_style.get("-vars", {})
        if self.registered:
            # @property: initial values, and no inheritance for inherits: false
            variables = dict(variables)
            for name, (inherits, initial) in self.registered.items():
                if not inherits or name not in variables:
                    if initial is not None:
                        variables[name] = initial
                    else:
                        variables.pop(name, None)
        font_size_set = False
        if isinstance(node, Element):
            decls = node.pseudo_decls if isinstance(node, PseudoElement) else self.matched_declarations(node)
            # Cascade order: UA rules < presentational attributes < author rules.
            raw = {}
            i = 0
            while i < len(decls) and decls[i][0] == 0 and not decls[i][1]:
                raw[decls[i][4]] = decls[i][5]
                i += 1
            raw.update(_presentational_hints(node))
            for d in decls[i:]:
                raw[d[4]] = d[5]
            custom = [(p, v) for p, v in raw.items() if p.startswith("--")]
            if custom:
                variables = dict(variables)
                for prop, value in custom:
                    variables[prop] = _substitute(value, variables)
            for prop, value in raw.items():
                if prop.startswith("--"):
                    continue
                if "cq" in value:
                    value = _container_units(value, node)
                if "attr(" in value and prop != "content":
                    value = _attr_values(value, node)
                if "var(" in value:
                    value = _substitute(value, variables).strip()
                    if not value:
                        continue
                    for p2, v2 in css_parser.expand_shorthand(prop, value):
                        self._apply(style, p2, v2, parent_style)
                        font_size_set = font_size_set or p2 == "font-size"
                else:
                    self._apply(style, prop, value, parent_style)
                    font_size_set = font_size_set or prop == "font-size"
            if node.tag == "a" and "href" in node.attributes:
                style["-link-href"] = node.attributes["href"]
                style["-link-node"] = node
        style["-vars"] = variables
        size = getattr(node, "resize_override", None)
        if size is not None:          # the user dragged the resize handle
            style["width"], style["height"] = "%gpx" % size[0], "%gpx" % size[1]
            style["box-sizing"] = "content-box"
        # Fill initial values for non-inherited properties.
        for prop, value in INITIAL.items():
            if prop not in style:
                style[prop] = value
        # Resolve font size to px.
        parent_px = parent_style.get("-font-px", 16.0)
        px = compute_font_size(style["font-size"], parent_px) if font_size_set else parent_px
        style["-font-px"] = px
        style["font-size"] = "%.3fpx" % px
        if isinstance(node, Element) and node.tag == "html":
            ROOT_FONT_SIZE[0] = px
        node.style = style

    def _apply(self, style, prop, value, parent_style):
        v = value.strip()
        lv = v.lower()
        if prop == "font-weight" and lv in ("bolder", "lighter"):
            from .fonts import numeric_weight
            pw = numeric_weight(parent_style.get("font-weight", "normal"))
            if lv == "bolder":
                v = lv = "400" if pw < 350 else "700" if pw < 550 else "900"
            else:
                v = lv = "100" if pw < 550 else "400" if pw < 750 else "700"
        if prop == "font-variant" and "small-caps" in lv:
            style["font-variant-caps"] = "small-caps"
        if lv == "inherit":
            if prop in parent_style:
                style[prop] = parent_style[prop]
            elif prop in INITIAL:
                style[prop] = INITIAL[prop]
            return
        if lv in ("initial", "unset", "revert", "revert-layer"):
            if lv == "unset" and prop in INHERITED and prop in parent_style:
                style[prop] = parent_style[prop]
            elif prop in INITIAL:
                style[prop] = INITIAL[prop]
            return
        if prop == "font-size" and ("em" in lv or "%" in lv or lv in ("smaller", "larger")):
            # resolve relative sizes now against the parent
            style[prop] = "%.3fpx" % compute_font_size(lv, parent_style.get("-font-px", 16.0))
            return
        if prop == "font-weight":
            pw = parent_style.get("font-weight", "normal")
            if lv == "bolder":
                v = "bold"
            elif lv == "lighter":
                v = "normal"
        style[prop] = v


def _first_text(el):
    """The first non-blank text node in an element's own inline content."""
    for c in el.children:
        if isinstance(c, Text):
            if c.text.strip():
                return c
        elif isinstance(c, Element):
            d = c.style.get("display", "inline")
            if d in ("none",) or getattr(c, "tag", "") in ("img", "br", "svg", "input"):
                continue
            if d not in ("inline", "inline-block", "list-item", "block", "flow-root") or \
                    c.style.get("float", "none") != "none" or c.style.get("position") in ("absolute", "fixed"):
                continue
            t = _first_text(c)
            if t is not None:
                return t
            if d not in ("inline",):
                return None
    return None


def _apply_first_letter(engine, el, decls):
    """Split the first letter (with leading punctuation) of el's text into a
    ::first-letter pseudo-element with its own style (drop caps etc.)."""
    t = _first_text(el)
    if t is None:
        return
    text = t.text
    lead = len(text) - len(text.lstrip())
    m = re.match(r"[\"'\u201c\u2018(\[\u00ab]*\w", text[lead:], re.U)
    if not m:
        return
    cut = lead + m.end()
    parent = t.parent
    pe = PseudoElement("first-letter", parent)
    pe.pseudo_decls = decls
    pe.split_from = t
    letter = Text(text[lead:cut], pe)
    pe.children.append(letter)
    i = parent.children.index(t)
    rest = Text(text[cut:], parent)
    rest.style = t.style
    parent.children[i:i + 1] = ([Text(text[:lead], parent)] if lead else []) + [pe, rest]
    for n in parent.children[i:i + 3]:
        if isinstance(n, Text) and n is not rest:
            n.style = t.style
    pe.original = (t, parent.children[i:i + (3 if lead else 2)])
    el.first_letter_pe = pe
    engine._style_node(pe, parent.style)
    engine._style_node(letter, pe.style)


def _undo_first_letter(el):
    pe = getattr(el, "first_letter_pe", None)
    if pe is None:
        return
    el.first_letter_pe = None
    t, pieces = pe.original
    parent = pe.parent
    if not pieces or pieces[0] not in parent.children:
        return
    i = parent.children.index(pieces[0])
    parent.children[i:i + len(pieces)] = [t]


_ATTR_RE = re.compile(r"attr\(\s*([\w:-]+)(?:\s+(type\([^)]*\)|[a-z%]+|string|raw-string))?\s*(?:,\s*((?:[^()]|\([^()]*\))*))?\)",
                      re.I)


def _attr_values(value, node):
    """attr(name [unit | type(...)], fallback) outside `content`."""
    def repl(m):
        raw = node.attributes.get(m.group(1).lower()) if hasattr(node, "attributes") else None
        if raw is None:
            return (m.group(3) or "").strip()
        unit = (m.group(2) or "").lower()
        if unit in ("string", "raw-string"):
            return '"%s"' % raw
        if unit and not unit.startswith("type("):
            return raw.strip() + ("" if unit == "number" else unit)
        return raw.strip()
    return _ATTR_RE.sub(repl, value)


_CQ_RE = re.compile(r"(-?\d*\.?\d+)(cqw|cqh|cqi|cqb|cqmin|cqmax)\b")


def _container_units(value, node):
    """cqw/cqh/cqi/cqb/cqmin/cqmax: percentages of the nearest query
    container's size from the last layout (the viewport before the first)."""
    if not _CQ_RE.search(value):
        return value
    found = None
    n = node.parent
    while n is not None and hasattr(n, "style"):
        if n.style.get("container-type", "normal") in ("inline-size", "size"):
            found = n
            break
        n = n.parent
    if found is not None:
        w, h = getattr(found, "container_size", None) or (VIEWPORT["width"], VIEWPORT["height"])
        found.container_size_used = (w, h)
    else:
        w, h = VIEWPORT["width"], VIEWPORT["height"]

    def repl(m):
        num, unit = float(m.group(1)), m.group(2)
        base = {"cqw": w, "cqi": w, "cqh": h, "cqb": h, "cqmin": min(w, h), "cqmax": max(w, h)}[unit]
        return "%gpx" % (num * base / 100)
    return _CQ_RE.sub(repl, value)


COUNTER_STYLES = {}    # @counter-style name -> descriptors (set by the page's restyle)


def _counter_pairs(value, default):
    toks = value.split()
    out = []
    for i, t in enumerate(toks):
        if re.match(r"^-?\d+$", t):
            continue
        n = default
        if i + 1 < len(toks) and re.match(r"^-?\d+$", toks[i + 1]):
            n = int(toks[i + 1])
        out.append((t, n))
    return out


def format_counter(n, kind, _depth=0):
    kind = (kind or "decimal").strip().lower()
    if kind == "none":
        return ""
    if kind in COUNTER_STYLES and _depth < 5:
        return _custom_counter(n, COUNTER_STYLES[kind], _depth)
    if kind == "lower-greek" and n > 0:
        out = ""
        while n > 0:
            n, r = divmod(n - 1, 24)
            out = "αβγδεζηθικλμνξοπρστυφχψω"[r] + out
        return out
    if kind in ("disclosure-open", "disclosure-closed"):
        return "\u25be" if kind == "disclosure-open" else "\u25b8"
    if kind == "decimal-leading-zero":
        return "%02d" % n
    if kind in ("lower-alpha", "lower-latin", "upper-alpha", "upper-latin") and n > 0:
        out = ""
        while n > 0:
            n, r = divmod(n - 1, 26)
            out = chr(97 + r) + out
        return out.upper() if kind.startswith("upper") else out
    if kind in ("lower-roman", "upper-roman") and 0 < n < 4000:
        vals = [(1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"), (50, "l"), (40, "xl"),
                (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]
        out = ""
        for v, sym in vals:
            while n >= v:
                out += sym
                n -= v
        return out.upper() if kind.startswith("upper") else out
    if kind in ("disc", "circle", "square"):
        return {"disc": "\u2022", "circle": "\u25e6", "square": "\u25aa"}[kind]
    return str(n)


def _symbols(text):
    return [m.group(1) if m.group(1) is not None else (m.group(2) if m.group(2) is not None else m.group(3))
            for m in re.finditer(r'"([^"]*)"|\'([^\']*)\'|(\S+)', text or "")]


def _custom_counter(n, desc, depth):
    """Render n with an @counter-style rule (cyclic, fixed, symbolic,
    alphabetic, numeric, additive; extends; prefix/suffix and negative)."""
    system = desc.get("system", "symbolic").strip().split()
    kind = system[0] if system else "symbolic"
    if kind == "extends" and len(system) > 1:
        base = dict(COUNTER_STYLES.get(system[1], {}))
        base.update({k: v for k, v in desc.items() if k != "system"})
        if system[1] not in COUNTER_STYLES:
            return format_counter(n, system[1], depth + 1)
        return _custom_counter(n, base, depth + 1)
    syms = _symbols(desc.get("symbols"))
    neg = n < 0
    v = abs(n)
    text = None
    if kind == "cyclic" and syms:
        text = syms[(n - 1) % len(syms)]
    elif kind == "fixed" and syms:
        first = int(system[1]) if len(system) > 1 and system[1].lstrip("-").isdigit() else 1
        i = n - first
        text = syms[i] if 0 <= i < len(syms) else None
    elif kind == "symbolic" and syms and v > 0:
        text = syms[(v - 1) % len(syms)] * ((v - 1) // len(syms) + 1)
    elif kind == "alphabetic" and len(syms) > 1 and v > 0:
        text = ""
        while v > 0:
            v, r = divmod(v - 1, len(syms))
            text = syms[r] + text
    elif kind == "numeric" and len(syms) > 1:
        text = "" if v else syms[0]
        while v > 0:
            v, r = divmod(v, len(syms))
            text = syms[r] + text
    elif kind == "additive":
        pairs = []
        for part in (desc.get("additive-symbols") or "").split(","):
            toks = _symbols(part)
            if len(toks) >= 2 and toks[0].isdigit():
                pairs.append((int(toks[0]), toks[1]))
        pairs.sort(reverse=True)
        text = ""
        for weight, sym in pairs:
            if weight == 0:
                if v == 0:
                    text = sym
                continue
            while v >= weight:
                text += sym
                v -= weight
        if v:
            text = None
    if text is None:
        return format_counter(n, desc.get("fallback", "decimal").strip(), depth + 1)
    if neg:
        nsym = _symbols(desc.get("negative", '"-"'))
        text = (nsym[0] if nsym else "-") + text + (nsym[1] if len(nsym) > 1 else "")
    pad = _symbols(desc.get("pad"))
    if len(pad) >= 2 and pad[0].isdigit():
        while len(text) < int(pad[0]):
            text = pad[1] + text
    return text


def counter_marker(n, kind):
    """List-marker text for list-style-type (including @counter-style names)."""
    kind = kind.strip().lower()
    if kind in COUNTER_STYLES:
        desc = COUNTER_STYLES[kind]
        pre = _symbols(desc.get("prefix", '""'))
        suf = _symbols(desc.get("suffix", '". "'))
        return (pre[0] if pre else "") + format_counter(n, kind) + (suf[0] if suf else "")
    return None


def _quote_pairs(value):
    toks = re.findall(r'"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'', value or "")
    flat = [a if a or not b else b for a, b in toks]
    return [(flat[i], flat[i + 1]) for i in range(0, len(flat) - 1, 2)]


def generated_text(content, el, counters=None, quotes=None):
    """Evaluate a `content` value: strings, attr(), quotes, counter(),
    counters(). Icon-font glyphs (Unicode private use area) are dropped since
    we can't draw web fonts."""
    out = []
    counters = counters or {}
    for m in re.finditer(r'"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'|attr\(\s*([\w-]+)\s*\)|(open-quote|close-quote)'
                         r'|(counters?)\(\s*([\w-]+)\s*(?:,\s*("[^"]*"|\'[^\']*\'|[\w-]+))?\s*(?:,\s*([\w-]+))?\s*\)', content):
        if m.group(5):
            vals = counters.get(m.group(6), [0])
            if m.group(5) == "counter":
                out.append(format_counter(vals[-1], m.group(7)))
            else:
                sep = (m.group(7) or '"."')[1:-1]
                out.append(sep.join(format_counter(v, m.group(8)) for v in vals))
            continue
        if m.group(3):
            out.append(el.attributes.get(m.group(3).lower(), ""))
        elif m.group(4):
            # quotes: depth-dependent pairs from the `quotes` property
            pairs = _quote_pairs(el.style.get("quotes", "auto")) if el.style.get("quotes", "auto") not in \
                ("auto", "none", None) else [("\u201c", "\u201d"), ("\u2018", "\u2019")]
            if el.style.get("quotes") == "none":
                pairs = []
            depth = quotes[0] if quotes else 0
            if m.group(4) == "open-quote":
                if pairs:
                    out.append(pairs[min(depth, len(pairs) - 1)][0])
                if quotes:
                    quotes[0] += 1
            else:
                if quotes:
                    quotes[0] = max(0, quotes[0] - 1)
                    depth = quotes[0]
                if pairs:
                    out.append(pairs[min(depth, len(pairs) - 1)][1])
        else:
            raw = m.group(1) if m.group(1) is not None else m.group(2)
            raw = re.sub(r"\\([0-9a-fA-F]{1,6})\s?", lambda h: chr(int(h.group(1), 16)) if int(h.group(1), 16) < 0x110000 else "", raw)
            raw = re.sub(r"\\(.)", r"\1", raw)
            out.append(raw)
    text = "".join(out)
    return "".join(ch for ch in text if not (0xE000 <= ord(ch) <= 0xF8FF or ord(ch) >= 0xF0000))


def _presentational_hints(el):
    """Old-school HTML attributes like bgcolor, width, align, <font color>."""
    style = {}
    a = el.attributes
    if not a and el.tag != "td" and el.tag != "th":
        return style
    if "bgcolor" in a:
        style["background-color"] = a["bgcolor"]
    if el.tag in ("img", "table", "td", "th", "iframe", "canvas", "video", "embed", "object", "input", "hr", "col"):
        for dim in ("width", "height"):
            if dim in a:
                val = a[dim].strip()
                if re.fullmatch(r"\d+(\.\d+)?", val):
                    style[dim] = val + "px"
                elif re.fullmatch(r"\d+(\.\d+)?%", val):
                    style[dim] = val
    if el.tag == "font":
        if "color" in a:
            style["color"] = a["color"]
        if "face" in a:
            style["font-family"] = a["face"]
        if "size" in a:
            sizes = {"1": "x-small", "2": "small", "3": "medium", "4": "large", "5": "x-large", "6": "xx-large", "7": "xxx-large"}
            s = a["size"].strip()
            if s.startswith(("+", "-")):
                try:
                    s = str(max(1, min(7, 3 + int(s))))
                except ValueError:
                    s = "3"
            if s in sizes:
                style["font-size"] = sizes[s]
    if "align" in a and el.tag in ("p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "td", "th", "tr", "center"):
        v = a["align"].lower()
        if v in ("left", "right", "center", "justify"):
            style["text-align"] = v
    if el.tag == "table":
        if "border" in a:
            try:
                bw = int(a["border"] or "1")
            except ValueError:
                bw = 1
            if bw > 0:
                for s in ("top", "right", "bottom", "left"):
                    style["border-%s-width" % s] = "%dpx" % bw
                    style["border-%s-style" % s] = "outset"
                    style["border-%s-color" % s] = "#888"
        if "cellspacing" in a:
            style["border-spacing"] = a["cellspacing"] + "px"
        if a.get("align", "").lower() == "center":
            style["margin-left"] = style["margin-right"] = "auto"
    if el.tag in ("td", "th"):
        table = next((p for p in el.ancestors() if getattr(p, "tag", None) == "table"), None)
        if table is not None:
            if table.attributes.get("border", "0").strip() not in ("0", ""):
                for s in ("top", "right", "bottom", "left"):
                    style["border-%s-width" % s] = "1px"
                    style["border-%s-style" % s] = "inset"
                    style["border-%s-color" % s] = "#888"
            elif "border" in table.attributes and table.attributes["border"] == "":
                pass
            if "cellpadding" in table.attributes:
                p = table.attributes["cellpadding"].strip() + "px"
                for s in ("top", "right", "bottom", "left"):
                    style["padding-" + s] = p
        if "valign" in a:
            style["vertical-align"] = a["valign"].lower()
        if "nowrap" in a:
            style["white-space"] = "nowrap"
    if el.tag == "body":
        if "text" in a:
            style["color"] = a["text"]
    if el.tag == "img" and "align" in a:
        v = a["align"].lower()
        if v in ("left", "right"):
            style["float"] = v
    if el.tag in ("ol", "ul") and "type" in a:
        t = a["type"]
        style["list-style-type"] = {"1": "decimal", "a": "lower-alpha", "A": "upper-alpha",
                                    "i": "lower-roman", "I": "upper-roman"}.get(t, t.lower())
    return style
