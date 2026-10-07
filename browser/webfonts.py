"""Page-local web fonts: fontTools decodes WOFF, Pillow measures/draws glyphs.

No system font installation or external rendering engine is involved.
"""
import io
import math
import re
from collections import Counter

from . import css_parser, network

MAX_FONT_BYTES = 8_000_000
MAX_FACES = 32


def family_name(value):
    return value.strip().strip("\"'").strip().lower()


def unicode_ranges(value):
    ranges = []
    for part in value.upper().split(","):
        part = part.strip()
        if not part.startswith("U+"):
            continue
        try:
            lo, _, hi = part[2:].partition("-")
            ranges.append((int(lo.replace("?", "0"), 16),
                           int((hi or lo).replace("?", "F"), 16)))
        except ValueError:
            pass
    return ranges or [(0, 0x10FFFF)]


class Face:
    def __init__(self, desc, data):
        from fontTools.ttLib import TTFont
        if len(data) > MAX_FONT_BYTES:
            raise ValueError("font exceeds size limit")
        if data[:4] in (b"wOFF", b"wOF2") and int.from_bytes(data[16:20], "big") > MAX_FONT_BYTES:
            raise ValueError("expanded font exceeds size limit")
        with TTFont(io.BytesIO(data)) as tt:
            self.cmap = set(tt.getBestCmap() or {})
            self.axes = [(a.axisTag, a.minValue, a.defaultValue, a.maxValue)
                         for a in tt["fvar"].axes] if "fvar" in tt else []
            tt.flavor = None
            output = io.BytesIO()
            tt.save(output)
            self.data = output.getvalue()
        if len(self.data) > MAX_FONT_BYTES:
            raise ValueError("expanded font exceeds size limit")
        from .fonts import numeric_weight
        weights = desc.get("font-weight", "400").split()
        self.weight = (numeric_weight(weights[0]), numeric_weight(weights[-1]))
        self.italic = desc.get("font-style", "normal") != "normal"
        self.family = family_name(desc["font-family"])
        self.ranges = unicode_ranges(desc.get("unicode-range", ""))


class FontSet:
    def __init__(self):
        self.faces = {}
        self.cache = {}

    def get(self, families, size, weight, italic):
        for family in css_parser._split_top_level(families, ","):
            faces = self.faces.get(family_name(family), [])
            if not faces:
                continue
            def rank(face):
                low, high = face.weight
                distance = max(low - weight, weight - high, 0)
                return (face.italic != italic, distance)
            face = min(faces, key=rank)
            key = (id(face), round(size, 3), weight)
            if key not in self.cache:
                self.cache[key] = WebFont(face, size, weight)
            return self.cache[key]
        return None


class WebFont:
    def __init__(self, face, size, weight):
        from PIL import ImageFont
        self.face, self.size_px, self.weight = face, max(1, size), weight
        self.scale = 2
        self.pil = ImageFont.truetype(io.BytesIO(face.data), max(1, round(self.size_px * self.scale)))
        if face.axes:
            self.pil.set_variation_by_axes([max(lo, min(hi, weight if tag == "wght" else default))
                                            for tag, lo, default, hi in face.axes])
        ascent, descent = self.pil.getmetrics()
        self.ascent, self.descent = ascent / self.scale, descent / self.scale
        self.linespace = self.ascent + self.descent
        self.space_width = self.measure(" ")
        self._fallback = None

    @property
    def tk(self):
        # Used only by UI features which require a Tk font object.
        if self._fallback is None:
            from .fonts import Font, resolve_family
            self._fallback = Font(resolve_family("sans-serif"), self.size_px, self.weight >= 600, self.face.italic)
        return self._fallback.tk

    def measure(self, text):
        return self.pil.getlength(text) / self.scale

    def rasterize(self, text, color):
        from PIL import Image, ImageDraw
        l, t, r, b = self.pil.getbbox(text, anchor="ls")
        width, height = max(1, r - l), max(1, b - t)
        # Keep malicious font sizes from creating unbounded bitmaps.
        if width * height > 16_000_000:
            raise ValueError("text bitmap exceeds size limit")
        image = Image.new("RGBA", (width, height))
        ImageDraw.Draw(image).text((-l, -t), text, font=self.pil, fill=color, anchor="ls")
        image = image.resize((max(1, math.ceil(width / self.scale)),
                              max(1, math.ceil(height / self.scale))), Image.Resampling.LANCZOS)
        return image, l / self.scale, self.ascent + t / self.scale


def load(page, descriptors, pool):
    """Fetch only font families and Unicode subsets used by this document."""
    font_set = FontSet()
    try:
        import fontTools.ttLib  # noqa: F401
    except ImportError:
        page.note("web fonts unavailable: install fonttools[woff]")
        return font_set
    used, chars = set(), Counter()
    nodes = [page.document] + list(page.document.descendants())
    for node in nodes:
        used.update(family_name(f) for f in css_parser._split_top_level(node.style.get("font-family", ""), ","))
        if hasattr(node, "text"):
            chars.update(ord(c) for c in node.text)
    candidates = []
    for desc in descriptors:
        if family_name(desc.get("font-family", "")) not in used:
            continue
        ranges = unicode_ranges(desc.get("unicode-range", ""))
        score = sum(count for cp, count in chars.items() if any(lo <= cp <= hi for lo, hi in ranges))
        urls = css_parser.css_urls(desc.get("src", ""))
        if score and urls:
            candidates.append((score, desc, urls))
    # Prefer the subset covering most document text when weights tie.
    candidates.sort(key=lambda item: -item[0])
    candidates = candidates[:MAX_FACES]
    urls = {url for _, _, sources in candidates for url in sources}
    def fetch(url):
        try:
            resolved = page.base_url.resolve(url)
            if resolved.scheme == "file" and page.url.scheme != "file":
                return None
            response = network.fetch(resolved, use_cache=True, referrer=str(page.url))
            if response.ok() and len(response.body) <= MAX_FONT_BYTES:
                return response.body
        except (network.NetworkError, ValueError):
            pass
        return None
    futures = {url: pool.submit(fetch, url) for url in urls}
    for _, desc, sources in candidates:
        for url in sources:
            data = futures[url].result()
            if data is None:
                continue
            try:
                face = Face(desc, data)
                font_set.faces.setdefault(face.family, []).append(face)
                break
            except Exception as error:
                page.note("font failed: %s (%s)" % (url, error))
    page.note("%d web font faces loaded" % sum(len(faces) for faces in font_set.faces.values()))
    return font_set
