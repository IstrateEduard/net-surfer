"""Font selection and text measurement (backed by Tk's font engine)."""
import tkinter.font

_FONTS = {}
_FAMILIES = None

GENERIC = {
    "serif": ["Times New Roman", "Times", "Liberation Serif", "DejaVu Serif", "Georgia", "FreeSerif"],
    "sans-serif": ["Helvetica", "Arial", "Liberation Sans", "DejaVu Sans", "Segoe UI", "FreeSans"],
    "monospace": ["Courier New", "Menlo", "Consolas", "Liberation Mono", "DejaVu Sans Mono", "Courier"],
    "system-ui": ["Segoe UI", "SF Pro Text", "Helvetica Neue", "Helvetica", "Liberation Sans", "DejaVu Sans", "Arial"],
    "cursive": ["Comic Sans MS", "Apple Chancery", "DejaVu Serif"],
    "fantasy": ["Impact", "Papyrus", "DejaVu Sans"],
}
GENERIC["-apple-system"] = GENERIC["system-ui"]
GENERIC["blinkmacsystemfont"] = GENERIC["system-ui"]
GENERIC["ui-sans-serif"] = GENERIC["sans-serif"]
GENERIC["ui-serif"] = GENERIC["serif"]
GENERIC["ui-monospace"] = GENERIC["monospace"]


def _available():
    global _FAMILIES
    if _FAMILIES is None:
        _FAMILIES = {f.lower(): f for f in tkinter.font.families()}
    return _FAMILIES


_family_cache = {}


def resolve_family(css_value):
    """Pick the first installed family from a CSS font-family list."""
    if css_value in _family_cache:
        return _family_cache[css_value]
    avail = _available()
    result = None
    for name in css_value.split(","):
        name = name.strip().strip("\"'").strip()
        low = name.lower()
        if low in avail:
            result = avail[low]
            break
        if low in GENERIC:
            for cand in GENERIC[low]:
                if cand.lower() in avail:
                    result = avail[cand.lower()]
                    break
            if result:
                break
    if result is None:
        for cand in GENERIC["serif"]:
            if cand.lower() in avail:
                result = avail[cand.lower()]
                break
    if result is None:
        result = "TkDefaultFont"
    _family_cache[css_value] = result
    return result


class Font:
    def __init__(self, family, size_px, bold, italic):
        self.tk = tkinter.font.Font(family=family, size=-max(1, int(round(size_px))),
                                    weight="bold" if bold else "normal",
                                    slant="italic" if italic else "roman")
        m = self.tk.metrics()
        self.ascent = m["ascent"]
        self.descent = m["descent"]
        self.linespace = m["linespace"]
        self.size_px = size_px
        self._widths = {}
        self.space_width = self.tk.measure(" ")

    def measure(self, text):
        w = self._widths.get(text)
        if w is None:
            w = self.tk.measure(text)
            if len(self._widths) < 50000:
                self._widths[text] = w
        return w


def numeric_weight(weight):
    w = (weight or "normal").strip().lower()
    if w in ("normal", "initial", "unset"):
        return 400
    if w in ("bold",):
        return 700
    if w == "bolder":
        return 700
    if w == "lighter":
        return 300
    try:
        return max(1, min(1000, int(float(w))))
    except ValueError:
        return 400


def is_bold(weight):
    return numeric_weight(weight) >= 600


# Installed families named after a weight or width ("Segoe UI Light",
# "Arial Narrow"): Tk itself only knows normal and bold.
_WEIGHT_NAMES = {
    100: ("Thin", "Hairline"), 200: ("ExtraLight", "Extra Light", "UltraLight", "Ultra Light"),
    300: ("Light",), 350: ("Semilight", "SemiLight"), 500: ("Medium",),
    600: ("Semibold", "SemiBold", "Demibold", "DemiBold", "Semi Bold"), 800: ("ExtraBold", "Extra Bold", "UltraBold"),
    900: ("Black", "Heavy"),
}
_STRETCH_NAMES = {
    "condensed": ("Condensed", "Narrow", "Cond"), "semi-condensed": ("SemiCondensed", "Semi Condensed", "Narrow"),
    "extra-condensed": ("ExtraCondensed", "Extra Condensed", "Compressed", "Narrow"),
    "ultra-condensed": ("UltraCondensed", "Compressed", "Narrow"),
    "semi-expanded": ("SemiExpanded", "Wide"), "expanded": ("Expanded", "Wide", "Extended"),
    "extra-expanded": ("ExtraExpanded", "Wide"), "ultra-expanded": ("UltraExpanded", "Wide"),
}
_variant_cache = {}


def _variant_family(family, weight, stretch):
    """(family, bold) to draw a CSS weight/stretch with: a named variant
    family when one is installed, else the base family in normal or bold."""
    key = (family, weight, stretch)
    if key in _variant_cache:
        return _variant_cache[key]
    avail = _available()
    result = None
    names = []
    if stretch in _STRETCH_NAMES:
        names += [family + " " + n for n in _STRETCH_NAMES[stretch]]
    if weight not in (400, 700):
        # CSS font matching: lighter weights look for lighter faces first,
        # heavier ones for heavier faces; 500 falls back to the regular face.
        if weight < 400:
            order = sorted((w for w in _WEIGHT_NAMES if w < 400), key=lambda w: abs(w - weight))
        elif weight == 500:
            order = [500]
        else:
            order = sorted((w for w in _WEIGHT_NAMES if w > 500), key=lambda w: (abs(w - weight), -w))
        for wt in order:
            for base in names + [family]:
                for wn in _WEIGHT_NAMES[wt]:
                    cand = (base + " " + wn).lower()
                    if cand in avail:
                        result = (avail[cand], False)
                        break
                if result:
                    break
            if result:
                break
    if result is None:
        for n in names:
            if n.lower() in avail:
                result = (avail[n.lower()], weight >= 600)
                break
    if result is None:
        result = (family, weight >= 600)
    _variant_cache[key] = result
    return result


def get_font(style, scale=1.0):
    family = resolve_family(style.get("font-family", "serif"))
    size = style.get("-font-px", 16.0) * scale
    weight = numeric_weight(style.get("font-weight", "normal"))
    stretch = style.get("font-stretch", "normal").strip().lower()
    if stretch.endswith("%"):
        try:
            pct = float(stretch[:-1])
            stretch = "condensed" if pct <= 87.5 else "expanded" if pct >= 112.5 else "normal"
        except ValueError:
            stretch = "normal"
    if weight != 400 or stretch != "normal":
        family, bold = _variant_family(family, weight, stretch)
    else:
        bold = False
    italic = style.get("font-style", "normal").split()[0] in ("italic", "oblique") if style.get("font-style") else False
    key = (family, int(round(size)), bold, italic)
    f = _FONTS.get(key)
    if f is None:
        f = Font(family, size, bold, italic)
        _FONTS[key] = f
    return f


def small_caps(style):
    """font-variant: small-caps (or font-feature-settings "smcp")."""
    v = style.get("font-variant-caps") or style.get("font-variant", "normal")
    if v and "small-caps" in v or v and "all-small-caps" in v:
        return True
    ffs = style.get("font-feature-settings", "normal")
    return '"smcp"' in ffs or "'smcp'" in ffs


_SCALED = {}


def scaled_font(font, factor):
    """The same face at `factor` times the size (scale() transforms)."""
    act = font.tk.actual()
    key = (act.get("family"), int(round(font.size_px * factor)), act.get("weight") == "bold",
           act.get("slant") == "italic")
    f = _SCALED.get(key)
    if f is None:
        f = Font(key[0], max(1, font.size_px * factor), key[2], key[3])
        _SCALED[key] = f
    return f
