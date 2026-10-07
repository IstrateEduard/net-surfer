"""Toolbar icons drawn from Google's Material Icons font (Apache 2.0).

The font lives next to this file in assets/ so the browser works offline.
Tk cannot load a font file on its own, so each glyph is rendered with
Pillow into a small transparent image.  If the font or Pillow is missing,
icon() returns None and the caller falls back to a plain text symbol.
"""
import os

try:
    from PIL import Image, ImageDraw, ImageFont, ImageTk
except ImportError:
    Image = None

FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "MaterialIcons-Regular.ttf")

# Codepoints from the font's codepoints file.
CODEPOINTS = {
    "arrow_back": 0xE5C4,
    "arrow_forward": 0xE5C8,
    "refresh": 0xE5D5,
    "home": 0xE88A,
    "star_border": 0xE83A,
    "star": 0xE838,
    "menu": 0xE5D2,
    "close": 0xE5CD,
    "add": 0xE145,
    "public": 0xE80B,
}

ICON_COLOR = "#3c4043"
DISABLED_COLOR = "#b4b8bf"

_fonts = {}


def available():
    return Image is not None and os.path.exists(FONT_PATH)


def icon(name, master, size=20, color=ICON_COLOR, box=None):
    """A PhotoImage of the named icon for master's window, or None if icons are unavailable."""
    root = master._root()
    # Tk drops an image nothing in Python refers to, so each window keeps its own.
    photos = root.__dict__.setdefault("_icon_photos", {})
    box = box or (size, size)   # the glyph is centred in a transparent box of this size
    key = (name, size, color, box)
    if key in photos:
        return photos[key]
    if not available() or name not in CODEPOINTS:
        return None
    try:
        font = _fonts.get(size) or _fonts.setdefault(size, ImageFont.truetype(FONT_PATH, size))
        img = Image.new("RGBA", box, (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        # textbbox() reports the glyph's advance box, not its ink, so measure the
        # drawn pixels and centre those.
        ch = chr(CODEPOINTS[name])
        glyph = Image.new("L", (size * 2, size * 2), 0)
        ImageDraw.Draw(glyph).text((size // 2, size // 2), ch, font=font, fill=255)
        gx0, gy0, gx1, gy1 = glyph.point(lambda v: 255 if v > 40 else 0).getbbox()
        draw.text(((box[0] - (gx1 - gx0)) / 2 - (gx0 - size // 2), (box[1] - (gy1 - gy0)) / 2 - (gy0 - size // 2)),
                  ch, font=font, fill=color)
        photo = ImageTk.PhotoImage(img, master=root)
    except Exception:
        return None
    photos[key] = photo
    return photo
