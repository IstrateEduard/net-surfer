"""Page loading: URL -> response -> DOM -> stylesheets -> styles -> images.

Everything here runs on a background thread (it does no Tk calls), so the
window stays responsive while a page loads. Layout and painting happen on
the UI thread afterwards because they measure text with Tk fonts.
"""
import html
import io
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import css_parser, js, network, style
from .dom import Element, Text
from .html_parser import parse

try:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 60_000_000
except ImportError:  # images are optional
    Image = None

MAX_STYLESHEETS = 300     # <style> and <link> sheets per page (Google alone uses 50+)
MAX_IMAGES = 150
MAX_IMAGE_BYTES = 12_000_000
POOL = ThreadPoolExecutor(max_workers=8)

# Where each load stage sits on the progress bar (fractions of the whole load).
# Layout and painting, done afterwards on the UI thread, fill the rest up to 1.0.
P_CONNECT = 0.03
P_HEADERS = 0.10
P_DOWNLOADED = 0.50
P_PARSED = 0.55
P_STYLESHEETS = 0.70
P_STYLED = 0.72
P_IMAGES = 0.85


def _no_progress(fraction, label):
    pass


def _size(n):
    return "%d B" % n if n < 1024 else "%.0f KB" % (n / 1024) if n < 1024 * 1024 else "%.1f MB" % (n / 1048576)


def _download_progress(progress, host):
    """Map (bytes received, Content-Length) onto the HEADERS..DOWNLOADED span."""
    def report(received, total):
        span = P_DOWNLOADED - P_HEADERS
        if total:
            frac = min(1.0, received / total)
            label = "Downloading %s of %s" % (_size(received), _size(total))
        else:
            # Unknown length: approach the end of the span without reaching it.
            frac = 0.9 * (1 - math.exp(-received / 150000))
            label = "Downloading %s" % _size(received) if received else "Waiting for %s" % host
        progress(P_HEADERS + span * frac, label)
    return report


class Page:
    def __init__(self, url):
        self.url = url                  # network.URL (final, after redirects)
        self.base_url = url
        self.document = None
        self.source = ""
        self.title = ""
        self.css_sources = []           # [(css text, URL it came from)] in document order
        self.images = {}                # absolute url -> PIL image or None
        self.log = []                   # human-readable network/debug log
        self.status = 200
        self.styled_width = None
        self.rule_count = 0
        self.timings = {}
        self.style_engine = None
        self.js = None                  # js.ScriptHost when the page runs scripts

    def note(self, msg):
        self.log.append(msg)

    # ------------------------------------------------------------------
    def restyle(self, viewport_width):
        """(Re)parse stylesheets for this viewport width and compute styles."""
        t0 = time.time()
        style.VIEWPORT["width"] = viewport_width
        rules = []
        order = 0
        layers = {}
        self.keyframes, counter_styles, registered = {}, {}, {}
        self.font_faces = []
        for text, _src in self.css_sources:
            parsed, parser = css_parser.parse_stylesheet_full(text, viewport_width, order, layers)
            order += len(parsed) + 1
            rules.extend(parsed)
            self.font_faces.extend(parser.font_faces)
            self.keyframes.update(parser.keyframes)
            counter_styles.update(parser.counter_styles)
            registered.update(parser.properties)
        style.COUNTER_STYLES.clear()
        style.COUNTER_STYLES.update(counter_styles)
        self.has_containers = any(getattr(r, "container", None) for r in rules) or \
            any("cq" in t and "container-type" in t for t, _ in self.css_sources)
        engine = style.StyleEngine(rules, registered)
        engine.font_faces = getattr(self, "web_fonts", None)
        engine.style_tree(self.document, {"-font-faces": getattr(self, "web_fonts", None)})
        self.style_engine = engine      # kept for :hover/:focus restyles
        self.rule_count = len(rules)
        self.styled_width = viewport_width
        self.has_media = any("@media" in t for t, _ in self.css_sources)
        self.timings["style"] = time.time() - t0


def containers_changed(page):
    """True if a query container's laid-out size differs from the size its
    @container rules were matched against."""
    stack = [page.document]
    while stack:
        n = stack.pop()
        used = getattr(n, "container_size_used", None)
        if used is not None:
            now = getattr(n, "container_size", None)
            if now is not None and (abs(now[0] - used[0]) > 0.5 or abs(now[1] - used[1]) > 0.5):
                return True
        stack.extend(c for c in n.children if hasattr(c, "children"))
    return False


def load(url, viewport_width, method="GET", body=None, headers=None, referrer=None, progress=None):
    """Load a page. Never raises: failures become an error page.

    progress(fraction, label) is called from this (background) thread as the
    load moves through its stages; fraction runs from 0 up to P_IMAGES."""
    progress = progress or _no_progress
    t0 = time.time()
    if js.enabled() and network.USER_AGENT != network.BROWSER_USER_AGENT and             network.USER_AGENT.startswith("NetSurfer/"):
        # scripts run, so ask sites for the pages they send real browsers
        network.USER_AGENT = network.BROWSER_USER_AGENT
    view_source = False
    if isinstance(url, str) and url.startswith("view-source:"):
        view_source = True
        url = url[len("view-source:"):]
    try:
        if isinstance(url, str):
            url = network.URL(url)
    except ValueError as e:
        return error_page(str(url), "Invalid address", str(e), viewport_width)

    if url.scheme == "about":
        name = url.path
        return about_page(name, url, viewport_width, progress)

    progress(P_CONNECT, "Connecting to %s" % (url.host or url.scheme))
    try:
        resp = network.fetch(url, method=method, body=body, headers=headers, referrer=referrer,
                             progress=_download_progress(progress, url.host))
    except network.NetworkError as e:
        return error_page(str(url), "This page could not be loaded", str(e), viewport_width)
    t_net = time.time() - t0

    page = Page(resp.url)
    page.status = resp.status
    page.note("%s %s -> %d %s (%d bytes, %s)" % (method, url, resp.status, resp.reason,
                                              len(resp.body), resp.content_type or "?"))
    if str(resp.url) != str(url):
        page.note("redirected to %s" % resp.url)
    if resp.url.fragment == "" and url.fragment:
        resp.url.fragment = url.fragment
    ctype = resp.content_type

    if view_source:
        source = resp.text()
        page.url = network.URL(str(resp.url))
        markup = "<html><head><title>Source of %s</title></head><body><pre style='font-size:13px'>%s</pre></body></html>" % (
            html.escape(str(resp.url)), html.escape(source))
        _finish(page, markup, viewport_width, fetch_subresources=False, progress=progress)
        page.view_source_of = str(resp.url)
        return page

    if ctype.startswith("image/"):
        markup = '<html><body style="margin:0;background:#222;text-align:center"><img src="%s"></body></html>' % html.escape(str(resp.url))
        network.CACHE.put(str(resp.url), resp)
    elif ctype in ("text/plain", "text/css", "application/javascript", "text/javascript", "application/json") or \
            (ctype and not ctype.startswith("text/html") and not ctype.startswith("application/xhtml") and ctype.startswith("text/")):
        markup = "<html><body><pre>%s</pre></body></html>" % html.escape(resp.text())
    elif ctype and not (ctype.startswith("text/") or "html" in ctype or "xml" in ctype):
        return error_page(str(url), "Cannot display this file",
                          "The server sent %s (%d bytes), which this browser does not render." % (ctype, len(resp.body)),
                          viewport_width)
    else:
        markup = resp.text()
    page.timings["network"] = t_net
    _finish(page, markup, viewport_width, progress=progress)
    page.timings["total"] = time.time() - t0
    return page


def _finish(page, markup, viewport_width, fetch_subresources=True, progress=None):
    progress = progress or _no_progress
    t0 = time.time()
    progress(P_DOWNLOADED, "Parsing HTML")
    page.source = markup
    page.document = parse(markup)
    page.timings["parse"] = time.time() - t0
    progress(P_PARSED, "Parsed HTML")
    for el in _elements(page.document):
        if el.tag == "title" and not page.title:
            page.title = " ".join(el.text_content().split())
        if el.tag == "base" and "href" in el.attributes:
            try:
                page.base_url = page.url.resolve(el.attributes["href"])
            except ValueError:
                pass
    if fetch_subresources and js.enabled():
        _run_scripts(page, viewport_width, progress)
    t1 = time.time()
    if fetch_subresources:
        _load_stylesheets(page, viewport_width, progress)
    page.timings["css"] = time.time() - t1
    progress(P_STYLESHEETS, "Computing styles")
    page.restyle(viewport_width)
    if fetch_subresources and page.font_faces:
        from . import webfonts
        page.web_fonts = webfonts.load(page, page.font_faces, POOL)
        page.style_engine.font_faces = page.web_fonts
        for node in [page.document] + list(page.document.descendants()):
            node.style["-font-faces"] = page.web_fonts
    progress(P_STYLED, "Styled")
    t2 = time.time()
    if fetch_subresources:
        _load_images(page, progress)
    page.timings["images"] = time.time() - t2
    progress(P_IMAGES, "Laying out page")


def _run_scripts(page, viewport_width, progress):
    """Run the page's <script>s (in a separate, sandboxed process; see js.py)
    before stylesheets are collected, so scripts can add styles and content."""
    if not any(el.tag == "script" for el in _elements(page.document)):
        return
    t0 = time.time()
    progress(P_PARSED, "Running scripts")
    try:
        page.js = js.ScriptHost(page, (viewport_width, style.VIEWPORT.get("height") or 768))
        page.js.run_load_scripts(lambda label: progress(P_PARSED, label))
        page.js.take_changes()       # styles are computed from scratch next anyway
        page.js.images_dirty = False
        title = page.js.title()
        if title is not None:
            page.title = title
    except Exception as e:           # a bug in the bindings must not lose the page
        import traceback
        traceback.print_exc()
        page.note("scripts failed: %s: %s" % (type(e).__name__, e))
    page.timings["scripts"] = time.time() - t0


def _elements(root):
    stack = [root]
    while stack:
        n = stack.pop()
        if isinstance(n, Element):
            yield n
            stack.extend(reversed(n.children))


def _load_stylesheets(page, viewport_width, progress=_no_progress):
    """Find <link rel=stylesheet> and <style> in document order and fetch
    external sheets (and their @imports) in parallel."""
    entries = []  # ("inline", text) | ("link", url)
    for el in _elements(page.document):
        if el.tag == "style":
            media = el.attributes.get("media", "")
            if media and not css_parser.media_matches(media, viewport_width):
                continue
            entries.append(("inline", el.text_content(), page.base_url))
        elif el.tag == "link":
            rel = el.attributes.get("rel", "").lower().split()
            if "stylesheet" not in rel or "alternate" in rel or "href" not in el.attributes:
                continue
            media = el.attributes.get("media", "")
            if media and media != "all" and not css_parser.media_matches(media, viewport_width):
                continue
            try:
                u = page.base_url.resolve(el.attributes["href"])
            except ValueError:
                continue
            entries.append(("link", u, None))
        if len(entries) >= MAX_STYLESHEETS:
            break

    def get(u):
        try:
            r = network.fetch(u, use_cache=True, referrer=str(page.url))
            if not r.ok():
                return u, None, "HTTP %d" % r.status
            return u, r.text(), None
        except network.NetworkError as e:
            return u, None, str(e)

    futures = {i: POOL.submit(get, e[1]) for i, e in enumerate(entries) if e[0] == "link"}
    n_links = len(futures)
    if n_links:
        progress(P_PARSED, "Loading %d stylesheet%s" % (n_links, "s" if n_links > 1 else ""))
    done = 0
    for i, entry in enumerate(entries):
        if entry[0] == "inline":
            text, base = entry[1], entry[2]
            page.css_sources.extend(_with_imports(page, text, base, viewport_width, 0))
        else:
            u, text, err = futures[i].result()
            done += 1
            progress(P_PARSED + (P_STYLESHEETS - P_PARSED) * done / n_links,
                     "Loaded stylesheet %d of %d" % (done, n_links))
            if text is None:
                page.note("stylesheet failed: %s (%s)" % (u, err))
                continue
            page.note("stylesheet: %s (%d chars)" % (u, len(text)))
            page.css_sources.extend(_with_imports(page, text, u, viewport_width, 0))


def _with_imports(page, text, base, viewport_width, depth):
    """Return [(css, base)] with @import-ed sheets (fetched) placed before `text`."""
    text = _absolutize_urls(text, base)
    out = []
    if depth < 3 and "@import" in text:
        _, imports = css_parser.parse_stylesheet(text, viewport_width)
        for href in imports:
            try:
                u = base.resolve(href)
                r = network.fetch(u, use_cache=True)
                if r.ok():
                    page.note("@import: %s" % u)
                    out.extend(_with_imports(page, r.text(), u, viewport_width, depth + 1))
            except (ValueError, network.NetworkError) as e:
                page.note("@import failed: %s (%s)" % (href, e))
    out.append((text, base))
    return out




def _absolutize_urls(css, base):
    """Rewrite url(...) in a stylesheet to absolute URLs, since the sheet's
    own location (not the page's) is the base for relative references."""
    def repl(m):
        ref = css_parser.url_of(m)
        if ref.startswith("data:"):
            return m.group(0)
        try:
            return 'url("%s")' % str(base.resolve(ref)).replace('"', "%22")
        except ValueError:
            return m.group(0)
    return css_parser.URL_RE.sub(repl, css)


def _load_images(page, progress=_no_progress):
    if Image is None:
        return
    urls = []
    seen = set()

    def want(src):
        if not src:
            return
        try:
            u = str(page.base_url.resolve(src))
        except ValueError:
            return
        if u not in seen:
            seen.add(u)
            urls.append(u)

    for el in _elements(page.document):
        disp = el.style.get("display")
        if disp == "none":
            continue
        if el.tag == "img" or (el.tag == "input" and el.attributes.get("type", "").lower() == "image"):
            src = el.attributes.get("src") or el.attributes.get("data-src")
            if not src and el.attributes.get("srcset"):
                src = el.attributes["srcset"].split(",")[0].strip().split(" ")[0]
            want(src)
        for prop in ("background-image", "mask-image", "-webkit-mask-image", "border-image-source", "list-style-image"):
            bg = el.style.get(prop, "none")
            if "url(" in bg:
                for u in css_parser.css_urls(bg):
                    want(u)
        if len(urls) >= MAX_IMAGES:
            break

    def get(u):
        try:
            r = network.fetch(u, use_cache=True, referrer=str(page.url))
            if not r.ok():
                return u, None, "HTTP %d" % r.status
            if len(r.body) > MAX_IMAGE_BYTES:
                return u, None, "too large"
            return u, decode_image(r.body), None
        except network.NetworkError as e:
            return u, None, str(e)
        except Exception as e:  # corrupt / unsupported image data
            return u, None, "%s: %s" % (type(e).__name__, e)

    if urls:
        progress(P_STYLED, "Loading %d image%s" % (len(urls), "s" if len(urls) > 1 else ""))
    for done, future in enumerate(as_completed([POOL.submit(get, u) for u in urls]), 1):
        u, img, err = future.result()
        page.images[u] = img
        if img is None:
            page.note("image failed: %s (%s)" % (u[:120], err))
        progress(P_STYLED + (P_IMAGES - P_STYLED) * done / len(urls),
                 "Loaded image %d of %d" % (done, len(urls)))
    page.note("%d images loaded, %d failed" % (sum(1 for v in page.images.values() if v is not None),
                                               sum(1 for v in page.images.values() if v is None)))


def decode_image(data):
    head = data[:512].lstrip().lower()
    if head.startswith((b"<svg", b"<?xml", b"<!doctype svg", b"<!--")) and b"<svg" in data[:4096].lower():
        from . import svg
        return svg.from_bytes(data)
    img = Image.open(io.BytesIO(data))
    img.seek(0)  # first frame of animated images
    img.load()
    if img.mode not in ("RGBA", "RGB"):
        img = img.convert("RGBA")
    if max(img.size) > 3000:
        img.thumbnail((3000, 3000))
    return img


# ---------------------------------------------------------------------------
# Built-in pages


def error_page(url, heading, detail, viewport_width):
    markup = """<html><head><title>%s</title></head>
<body style="font-family: sans-serif; background: #f6f6f6; color: #333; margin: 0">
<div style="max-width: 560px; margin: 80px auto; padding: 24px 32px; background: white; border: 1px solid #ddd">
<h1 style="font-size: 22px; color: #b00020">%s</h1>
<p>%s</p>
<p style="color:#777; font-size: 13px">%s</p>
<p><a href="%s">Try again</a></p>
</div></body></html>""" % (html.escape(heading), html.escape(heading), html.escape(detail),
                           html.escape(url), html.escape(url))
    try:
        page_url = network.URL(url)
    except ValueError:
        page_url = network.URL("about:error")
    page = Page(page_url)
    page.status = 0
    page.note("error: %s" % detail)
    page.is_error = True
    _finish(page, markup, viewport_width, fetch_subresources=False)
    return page


HOME = """<html><head><title>New Tab</title>
<style>
body { font-family: sans-serif; background: #f4f6fb; color: #222; margin: 0; }
.wrap { max-width: 720px; margin: 0 auto; padding: 40px 20px; }
h1 { font-size: 30px; margin: 0 0 4px; }
.brand { display: flex; align-items: center; gap: 16px; }
.brand img { width: 72px; height: 72px; }
.sub { color: #666; margin-top: 0; }
.grid { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 24px; }
.card { display: block; width: 210px; padding: 12px 14px; background: white; border: 1px solid #dde2ee;
        border-radius: 6px; text-decoration: none; color: #1a3d8f; }
.card small { display: block; color: #777; margin-top: 4px; }
</style></head>
<body><div class="wrap">
<div class="brand"><img src="{logo}" alt=""><div>
<h1>Net Surfer</h1>
<p class="sub">Type an address above, or try one of these.</p>
</div></div>
<div class="grid">
<a class="card" href="https://example.com/">example.com<small>The smallest real page</small></a>
<a class="card" href="https://en.wikipedia.org/wiki/Web_browser">Wikipedia<small>Long article, tables, images</small></a>
<a class="card" href="https://info.cern.ch/hypertext/WWW/TheProject.html">The first website<small>CERN, 1991</small></a>
<a class="card" href="https://news.ycombinator.com/">Hacker News<small>Table layout, forms</small></a>
<a class="card" href="https://browser.engineering/">Web Browser Engineering<small>A book about this assignment</small></a>
<a class="card" href="https://html.duckduckgo.com/html/">DuckDuckGo (HTML)<small>Search form (GET/POST)</small></a>
<a class="card" href="http://neverssl.com/">neverssl.com<small>Plain HTTP</small></a>
<a class="card" href="https://httpbin.org/redirect/3">httpbin redirects<small>Follows 3 redirects</small></a>
<a class="card" href="about:test">Built-in test page<small>CSS features checklist</small></a>
</div></div></body></html>"""


def about_page(name, url, viewport_width, progress=None):
    page = Page(url)
    if name in ("", "blank"):
        markup = "<html><body></body></html>"
    elif name == "home":
        import os
        import pathlib
        logo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "netsurfer-256.png")
        markup = HOME.replace("{logo}", pathlib.Path(logo).as_uri())
    elif name in ("test", "js"):
        import os
        path = os.path.join(os.path.dirname(__file__), "..", "tests", "pages",
                            "features.html" if name == "test" else "js.html")
        try:
            with open(path, encoding="utf-8") as f:
                markup = f.read()
            import pathlib     # as_uri() gives file:///G:/... on Windows too
            page.url = page.base_url = network.URL(pathlib.Path(os.path.abspath(path)).as_uri())
        except OSError:
            markup = "<p>Test page missing.</p>"
    else:
        return error_page(str(url), "Unknown page", "about:%s does not exist" % name, viewport_width)
    _finish(page, markup, viewport_width, fetch_subresources=True, progress=progress)
    page.url = url if name not in ("test", "js") else page.url
    return page
