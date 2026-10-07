"""Favicons: find a page's icon, fetch it and shrink it to tab size."""
import threading

from . import engine, network

try:
    from PIL import Image
except ImportError:
    Image = None

SIZE = 16
MAX_BYTES = 512 * 1024

_cache = {}                 # icon url -> PIL image or None, so tabs on one site share a fetch
_lock = threading.Lock()


def candidate_urls(page):
    """Icon URLs to try, best first: <link rel="icon"> tags, then /favicon.ico."""
    found = []
    for el in page.document.get_elements_by_tag("link"):
        rel = el.attributes.get("rel", "").lower().split()
        href = el.attributes.get("href")
        if href and "icon" in rel and "mask-icon" not in rel:
            try:
                found.append(str(page.base_url.resolve(href)))
            except ValueError:
                pass
    try:
        default = str(page.url.resolve("/favicon.ico"))
    except ValueError:
        default = None
    if default and default not in found:
        found.append(default)
    return found


def load(page):
    """The page's favicon as a 16x16 RGBA PIL image, or None. Runs on a worker thread."""
    if Image is None or page.document is None or str(page.url).startswith(("about:", "view-source:", "data:")):
        return None
    for url in candidate_urls(page):
        with _lock:
            if url in _cache:
                if _cache[url] is not None:
                    return _cache[url]
                continue
        img = _fetch(url, str(page.url))
        with _lock:
            _cache[url] = img
        if img is not None:
            return img
    return None


def _fetch(url, referrer):
    try:
        r = network.fetch(url, use_cache=True, referrer=referrer)
        if not r.ok() or not r.body or len(r.body) > MAX_BYTES or r.content_type.startswith("text/html"):
            return None
        img = engine.decode_image(r.body)
        if hasattr(img, "render"):          # SVG
            img = img.render(SIZE * 2, SIZE * 2)
        img = img.convert("RGBA")
        img.thumbnail((SIZE, SIZE), Image.LANCZOS)
        out = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
        out.paste(img, ((SIZE - img.width) // 2, (SIZE - img.height) // 2))
        return out
    except Exception:       # missing, corrupt or unsupported icon: just use the default
        return None
