"""Unit and integration tests for the browser engine.

Run:  python3 -m unittest discover -s tests -v
Layout tests need a display (Tk fonts); on a headless Linux box use
`xvfb-run python3 -m unittest discover -s tests`.
"""
import gzip
import http.server
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.setrecursionlimit(20000)

from browser import css_parser, engine, network, paint, style  # noqa: E402
from browser.dom import Element, Text  # noqa: E402
from browser.html_parser import parse  # noqa: E402


def find(root, tag):
    for n in root.descendants():
        if isinstance(n, Element) and n.tag == tag:
            return n
    return None


def find_all(root, tag):
    return [n for n in root.descendants() if isinstance(n, Element) and n.tag == tag]


class HTMLParserTests(unittest.TestCase):
    def test_implied_structure(self):
        doc = parse("<title>T</title><p>Hello")
        self.assertEqual(doc.tag, "html")
        self.assertEqual([c.tag for c in doc.element_children()], ["head", "body"])
        self.assertEqual(find(doc, "title").text_content(), "T")
        self.assertEqual(find(doc, "p").parent.tag, "body")

    def test_attributes(self):
        doc = parse('<a href="x.html" class=\'a b\' data-x=1 disabled>link</a>')
        a = find(doc, "a")
        self.assertEqual(a.attributes, {"href": "x.html", "class": "a b", "data-x": "1", "disabled": ""})
        self.assertEqual(a.classes, ["a", "b"])

    def test_entities(self):
        doc = parse("<p>&lt;b&gt; &amp; &copy; &#8364; &#x41;</p>")
        self.assertEqual(find(doc, "p").text_content(), "<b> & © € A")

    def test_implied_end_tags(self):
        doc = parse("<p>one<p>two<ul><li>a<li>b</ul>")
        ps = find_all(doc, "p")
        self.assertEqual(len(ps), 2)
        self.assertEqual(ps[1].parent.tag, "body")      # second <p> is a sibling, not a child
        lis = find_all(doc, "li")
        self.assertEqual(len(lis), 2)
        self.assertEqual(lis[1].parent.tag, "ul")

    def test_void_and_self_closing(self):
        doc = parse("<p>a<br>b<img src=x.png>c</p>")
        p = find(doc, "p")
        self.assertEqual([c.tag if isinstance(c, Element) else c.text for c in p.children],
                         ["a", "br", "b", "img", "c"])

    def test_raw_text_script_style(self):
        doc = parse("<script>if (a < b && c > d) { x = '</p>'; }</script><style>p > a { color: red }</style><p>x</p>")
        self.assertIn("a < b", find(doc, "script").text_content())
        self.assertIn("p > a", find(doc, "style").text_content())
        self.assertEqual(len(find_all(doc, "p")), 1)

    def test_comments_and_doctype(self):
        doc = parse("<!DOCTYPE html><!-- hi --><p>x<!-- <b>not bold</b> --></p>")
        self.assertIsNone(find(doc, "b"))

    def test_mismatched_end_tags(self):
        doc = parse("<div><span>text</div>after")
        div = find(doc, "div")
        self.assertEqual(div.text_content(), "text")
        self.assertIn("after", find(doc, "body").text_content())

    def test_table_implied_rows(self):
        doc = parse("<table><td>1<td>2<tr><td>3</table>")
        rows = find_all(doc, "tr")
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(find_all(rows[0], "td")), 2)
        self.assertEqual(find(doc, "tbody").parent.tag, "table")


class CSSParserTests(unittest.TestCase):
    def spec(self, sel):
        return css_parser.parse_selector(sel).specificity

    def test_specificity(self):
        self.assertEqual(self.spec("p"), (0, 0, 1))
        self.assertEqual(self.spec(".a"), (0, 1, 0))
        self.assertEqual(self.spec("#x"), (1, 0, 0))
        self.assertEqual(self.spec("div p.a#x"), (1, 1, 2))
        self.assertEqual(self.spec("a:not(.b)"), (0, 1, 1))
        self.assertEqual(self.spec("ul > li + li"), (0, 0, 3))

    def test_rules_and_declarations(self):
        rules, _ = css_parser.parse_stylesheet("/* c */ h1, .x { color: red; margin: 1px 2px } p{font-weight:bold !important}")
        self.assertEqual(len(rules), 2)
        decls = rules[0].declarations
        self.assertIn(("color", "red", False), decls)
        self.assertIn(("margin-left", "2px", False), decls)
        self.assertIn(("margin-bottom", "1px", False), decls)
        self.assertEqual(rules[1].declarations, [("font-weight", "bold", True)])

    def test_media_queries(self):
        css = "@media (max-width: 600px) { p { color: red } } @media screen and (min-width: 601px) { p { color: green } } @media print { p { color: blue } }"
        wide, _ = css_parser.parse_stylesheet(css, viewport_width=1000)
        narrow, _ = css_parser.parse_stylesheet(css, viewport_width=500)
        self.assertEqual([r.declarations[0][1] for r in wide], ["green"])
        self.assertEqual([r.declarations[0][1] for r in narrow], ["red"])

    def test_bad_css_is_skipped(self):
        rules, _ = css_parser.parse_stylesheet("p { color: red; ;; bogus } }} @font-face { src: x } div { color: blue }")
        self.assertEqual([r.declarations for r in rules][-1], [("color", "blue", False)])

    def test_shorthands(self):
        d = dict((p, v) for p, v in css_parser.expand_shorthand("border", "2px solid #333"))
        self.assertEqual(d["border-left-width"], "2px")
        self.assertEqual(d["border-top-style"], "solid")
        d = dict(css_parser.expand_shorthand("font", "italic bold 12px/1.5 Georgia, serif"))
        self.assertEqual((d["font-style"], d["font-weight"], d["font-size"], d["line-height"]),
                         ("italic", "bold", "12px", "1.5"))
        self.assertEqual(d["font-family"], "Georgia, serif")
        d = dict(css_parser.expand_shorthand("background", "#fff url(a.png) no-repeat"))
        self.assertEqual((d["background-color"], d["background-image"]), ("#fff", "url(a.png)"))

    def test_selector_matching(self):
        doc = parse('<div id="m" class="c"><ul><li class="a">1</li><li>2</li><li>3</li></ul></div>')
        lis = find_all(doc, "li")
        m = lambda s, el: css_parser.parse_selector(s).matches(el)
        self.assertTrue(m("#m li", lis[0]))
        self.assertTrue(m("div.c > ul > li.a", lis[0]))
        self.assertFalse(m("div > li", lis[0]))
        self.assertTrue(m("li.a + li", lis[1]))
        self.assertTrue(m("li.a ~ li", lis[2]))
        self.assertTrue(m("li:first-child", lis[0]))
        self.assertTrue(m("li:last-child", lis[2]))
        self.assertTrue(m("li:nth-child(2n+1)", lis[2]))
        self.assertTrue(m("li:not(.a)", lis[1]))
        self.assertTrue(m("[class=a]", lis[0]))


class CascadeTests(unittest.TestCase):
    def styled(self, html, css=""):
        doc = parse(html)
        rules, _ = css_parser.parse_stylesheet(css)
        style.StyleEngine(rules).style_tree(doc)
        return doc

    def test_var_shorthand_loses_to_more_specific_longhand(self):
        # ubishops.ca: "input[type=submit] { background: var(--b) }" must not beat a more specific
        # ".search-form .search-submit[type=submit] { background: transparent }"
        doc = self.styled('<form class="search-form"><input type="submit" class="search-submit"></form>',
                          ':root { --b: #380d58 } .search-form .search-submit[type="submit"] { background: transparent }'
                          ' input[type="submit"] { background: var(--b) }')
        self.assertEqual(find(doc, "input").style["background-color"], "transparent")

    def test_specificity_and_order(self):
        doc = self.styled('<p id="x" class="y">t</p>', "#x { color: green } .y { color: red } p { color: blue }")
        self.assertEqual(find(doc, "p").style["color"], "green")
        doc = self.styled('<p class="y">t</p>', ".y { color: red } .y { color: green }")
        self.assertEqual(find(doc, "p").style["color"], "green")

    def test_inline_and_important(self):
        doc = self.styled('<p style="color: red" class="y">t</p>', ".y { color: blue }")
        self.assertEqual(find(doc, "p").style["color"], "red")
        doc = self.styled('<p style="color: red" class="y">t</p>', ".y { color: green !important }")
        self.assertEqual(find(doc, "p").style["color"], "green")

    def test_inheritance(self):
        doc = self.styled("<div><p><span>t</span></p></div>", "div { color: purple; font-size: 20px; padding: 5px }")
        span = find(doc, "span")
        self.assertEqual(span.style["color"], "purple")
        self.assertEqual(span.style["-font-px"], 20.0)
        self.assertEqual(span.style["padding-left"], "0")      # not inherited

    def test_em_and_percent_font_sizes(self):
        doc = self.styled("<div><p><span>t</span></p></div>", "div { font-size: 20px } p { font-size: 1.5em } span { font-size: 50% }")
        self.assertAlmostEqual(find(doc, "p").style["-font-px"], 30.0)
        self.assertAlmostEqual(find(doc, "span").style["-font-px"], 15.0)

    def test_user_agent_defaults(self):
        doc = self.styled("<h1>t</h1><a href=x>l</a><head><script></script></head>")
        self.assertEqual(find(doc, "h1").style["display"], "block")
        self.assertEqual(find(doc, "h1").style["-font-px"], 32.0)
        self.assertEqual(find(doc, "a").style["text-decoration"], "underline")
        self.assertEqual(find(doc, "script").style["display"], "none")

    def test_custom_properties(self):
        doc = self.styled('<div><p>t</p></div>', ":root { --c: #123456 } div { --pad: 7px } p { color: var(--c); padding: var(--pad) var(--missing, 3px) }")
        p = find(doc, "p")
        self.assertEqual(p.style["color"], "#123456")
        self.assertEqual(p.style["padding-top"], "7px")
        self.assertEqual(p.style["padding-left"], "3px")

    def test_colors(self):
        self.assertEqual(style.parse_color("red"), "#ff0000")
        self.assertEqual(style.parse_color("#abc"), "#aabbcc")
        self.assertEqual(style.parse_color("rgb(1, 2, 3)"), "#010203")
        self.assertEqual(style.parse_color("rgba(0,0,0,0)"), None)
        self.assertEqual(style.parse_color("hsl(120, 100%, 50%)"), "#00ff00")
        self.assertEqual(style.parse_color("rgba(0, 0, 0, 0.5)"), "#808080")

    def test_lengths(self):
        self.assertEqual(style.length("10px"), 10)
        self.assertEqual(style.length("2em", 10), 20)
        self.assertEqual(style.length("50%", 16, 300), 150)
        self.assertEqual(style.length("calc(100% - 20px)", 16, 300), 280)
        self.assertEqual(style.length("auto", auto="A"), "A")


class URLTests(unittest.TestCase):
    def test_parse(self):
        u = network.URL("https://Example.com:8443/a/b.html?q=1#frag")
        self.assertEqual((u.scheme, u.host, u.port, u.path, u.query, u.fragment),
                         ("https", "example.com", 8443, "/a/b.html", "q=1", "frag"))

    def test_resolve(self):
        base = network.URL("http://h.com/dir/page.html?x=1")
        r = lambda s: str(base.resolve(s))
        self.assertEqual(r("other.html"), "http://h.com/dir/other.html")
        self.assertEqual(r("../up.html"), "http://h.com/up.html")
        self.assertEqual(r("./sub/./x/../y.css"), "http://h.com/dir/sub/y.css")
        self.assertEqual(r("/abs"), "http://h.com/abs")
        self.assertEqual(r("//cdn.com/a.js"), "http://cdn.com/a.js")
        self.assertEqual(r("#top"), "http://h.com/dir/page.html?x=1#top")
        self.assertEqual(r("?y=2"), "http://h.com/dir/page.html?y=2")
        self.assertEqual(r("https://other.org/"), "https://other.org/")
        with self.assertRaises(ValueError):
            base.resolve("javascript:alert(1)")


# ---------------------------------------------------------------------------
# A local HTTP server to test networking behaviour end to end.

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        p = self.path
        if p.startswith("/redirect/"):
            n = int(p.split("/")[2])
            self.send_response(302)
            self.send_header("Location", "/redirect/%d" % (n - 1) if n > 1 else "/final")
            self.end_headers()
        elif p == "/final":
            self._send(200, b"<html><head><title>Final</title></head><body><p>arrived</p></body></html>")
        elif p == "/gzip":
            body = gzip.compress(b"<p>compressed</p>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif p == "/set-cookie":
            self.send_response(200)
            self.send_header("Set-Cookie", "session=abc123; Path=/")
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"ok")
        elif p == "/echo-cookie":
            self._send(200, ("<p>%s</p>" % self.headers.get("Cookie", "")).encode())
        elif p == "/styled":
            self._send(200, b'<html><head><link rel="stylesheet" href="/css/a.css"><link rel="stylesheet" href="/missing.css"></head>'
                            b'<body><p class="x">styled</p><img src="/nope.png"></body></html>')
        elif p == "/css/a.css":
            self._send(200, b'@import url("b.css"); .x { color: rgb(0, 128, 0) }', "text/css")
        elif p == "/css/b.css":
            self._send(200, b'.x { font-weight: bold }', "text/css")
        elif p == "/fav-page":
            self._send(200, b'<html><head><link rel="shortcut icon" href="/icon.png"></head><body>x</body></html>')
        elif p == "/fav-default":
            self._send(200, b'<html><head><title>d</title></head><body>x</body></html>')
        elif p in ("/icon.png", "/favicon.ico"):
            import io
            from PIL import Image
            buf = io.BytesIO()
            Image.new("RGBA", (32, 24), (255, 0, 0, 255)).save(buf, "PNG" if p.endswith("png") else "ICO")
            self._send(200, buf.getvalue(), "image/png" if p.endswith("png") else "image/x-icon")
        elif p == "/latin1":
            self._send(200, "<p>café</p>".encode("latin-1"), "text/html; charset=iso-8859-1")
        else:
            self._send(404, b"<h1>Not found</h1>")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(n)
        self._send(200, b"<p>posted:" + data + b"</p>")

    def _send(self, code, body, ctype="text/html"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class NetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("HTTPS_PROXY", None)
        os.environ.pop("https_proxy", None)
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_redirect_chain(self):
        r = network.fetch(self.base + "/redirect/3")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.url.path, "/final")

    def test_gzip(self):
        self.assertEqual(network.fetch(self.base + "/gzip").body, b"<p>compressed</p>")

    def test_cookies(self):
        network.fetch(self.base + "/set-cookie")
        r = network.fetch(self.base + "/echo-cookie")
        self.assertIn(b"session=abc123", r.body)

    def test_charset(self):
        page = engine.load(self.base + "/latin1", 800)
        self.assertEqual(find(page.document, "p").text_content(), "café")

    def test_404_is_rendered_not_raised(self):
        page = engine.load(self.base + "/does-not-exist", 800)
        self.assertEqual(page.status, 404)
        self.assertEqual(find(page.document, "h1").text_content(), "Not found")

    def test_connection_refused_gives_error_page(self):
        page = engine.load("http://127.0.0.1:1/", 800)
        self.assertTrue(getattr(page, "is_error", False))
        self.assertIn("could not be loaded", page.title)

    def test_stylesheets_imports_and_failures(self):
        page = engine.load(self.base + "/styled", 800)
        p = find(page.document, "p")
        self.assertEqual(p.style["color"], "rgb(0, 128, 0)")
        self.assertEqual(p.style["font-weight"], "bold")   # from @import
        self.assertTrue(any("missing.css" in line for line in page.log))
        self.assertIsNone(page.images.get(self.base + "/nope.png"))

    def test_load_progress(self):
        reports = []
        engine.load(self.base + "/styled", 800, progress=lambda f, label: reports.append((f, label)))
        fractions = [f for f, _ in reports]
        self.assertEqual(fractions, sorted(fractions))            # only ever moves forward
        self.assertEqual(fractions[-1], engine.P_IMAGES)
        labels = " | ".join(label for _, label in reports)
        for stage in ("Connecting", "Downloading", "Parsing", "stylesheet 2 of 2", "Computing styles", "image 1 of 1"):
            self.assertIn(stage, labels)

    def test_fetch_reports_bytes(self):
        seen = []
        r = network.fetch(self.base + "/styled", progress=lambda got, total: seen.append((got, total)))
        self.assertEqual(seen[0], (0, len(r.body)))
        self.assertEqual(seen[-1], (len(r.body), len(r.body)))

    def test_post(self):
        r = network.fetch(self.base + "/form", method="POST", body=b"a=1&b=2",
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertIn(b"posted:a=1&b=2", r.body)

    def test_data_url(self):
        r = network.fetch("data:text/html;base64,PHA+aGk8L3A+")
        self.assertEqual(r.body, b"<p>hi</p>")


# ---------------------------------------------------------------------------
# Layout tests (need Tk for font metrics)

try:
    import tkinter
    _root = tkinter.Tk()
    _root.withdraw()
    HAVE_TK = True
except Exception:
    HAVE_TK = False


@unittest.skipUnless(HAVE_TK, "needs a display for Tk fonts")
class LayoutTests(unittest.TestCase):
    def lay(self, html, width=800):
        from browser import layout
        page = engine.load("data:text/html," + html.replace("#", "%23"), width)
        ctx = layout.LayoutContext(page.base_url, page.images, width, 600)
        doc = layout.DocumentLayout(page.document, ctx)
        doc.layout(width, 600)
        boxes = {}
        stack = [doc.root]
        while stack:
            b = stack.pop()
            if b.node is not None:
                boxes.setdefault(b.node, b)
            stack.extend(b.children)
            stack.extend(b.abs_boxes)
            for ln in getattr(b, "lines", []):
                for f in ln.frags:
                    if isinstance(f, layout.AtomFrag):
                        stack.append(f.box)
        self.page = page
        return doc, lambda sel: boxes[next(n for n in page.document.descendants()
                                           if isinstance(n, Element) and css_parser.parse_selector(sel).matches(n))]

    def test_block_width_and_box_model(self):
        doc, box = self.lay('<body style="margin:0"><div id=a style="width:200px;padding:10px;border:5px solid;margin:7px"></div>'
                            '<div id=b style="width:200px;padding:10px;box-sizing:border-box"></div>')
        a = box("#a")
        self.assertEqual((a.x, a.width), (7 + 5 + 10, 200))
        self.assertEqual(a.border_box()[2] - a.border_box()[0], 230)
        self.assertEqual(box("#b").width, 180)

    def test_auto_margins_center(self):
        doc, box = self.lay('<body style="margin:0"><div id=a style="width:200px;margin:0 auto"></div>', 800)
        self.assertEqual(box("#a").x, 300)

    def test_margin_collapsing(self):
        doc, box = self.lay('<body style="margin:0"><p id=a style="margin:20px 0;height:10px"></p><p id=b style="margin:30px 0;height:10px"></p>')
        a, b = box("#a"), box("#b")
        self.assertEqual(b.y - (a.y + a.height), 30)

    def test_text_wraps_to_width(self):
        doc, box = self.lay('<body style="margin:0"><p id=a style="width:100px">' + "word " * 40 + "</p>")
        a = box("#a")
        self.assertGreater(len(a.lines), 5)
        for line in a.lines:
            for f in line.frags:
                self.assertLessEqual(f.x + f.width, a.x + a.width + 1)

    def test_display_none_has_no_box(self):
        doc, box = self.lay('<p id=a>x</p><p id=b style="display:none">y</p>')
        with self.assertRaises(KeyError):
            box("#b")

    def test_float_shortens_lines(self):
        doc, box = self.lay('<body style="margin:0"><div id=f style="float:left;width:100px;height:100px"></div><p id=p style="margin:0">' + "text " * 30 + "</p>")
        p = box("#p")
        self.assertGreaterEqual(p.lines[0].frags[0].x, 100)

    def test_flex_grow_and_row(self):
        doc, box = self.lay('<body style="margin:0"><div style="display:flex;width:600px"><div id=a style="width:100px"></div><div id=b style="flex:1"></div></div>')
        a, b = box("#a"), box("#b")
        self.assertEqual(a.y, b.y)
        self.assertAlmostEqual(b.x, 100)
        self.assertAlmostEqual(b.width, 500)

    def test_flex_justify_align_and_auto_margins(self):
        doc, box = self.lay('<body style="margin:0"><div style="display:flex;width:600px;height:100px;'
                            'justify-content:space-between;align-items:center">'
                            '<div id=a style="width:100px;height:20px"></div><div id=b style="width:100px;height:40px"></div>'
                            '<div id=c style="width:100px;height:20px"></div></div>'
                            '<div style="display:flex;width:600px"><div id=d style="width:50px;height:10px"></div>'
                            '<div id=e style="width:50px;height:10px;margin-left:auto"></div></div>')
        self.assertEqual([box(s).x for s in ("#a", "#b", "#c")], [0, 250, 500])
        self.assertEqual((box("#a").y, box("#b").y), (40, 30))
        self.assertEqual((box("#d").x, box("#e").x), (0, 550))

    def test_flex_shrink_wrap_and_column_grow(self):
        doc, box = self.lay('<body style="margin:0"><div style="display:flex;width:300px">'
                            '<div id=a style="flex:0 1 200px"></div><div id=b style="flex:0 3 200px"></div></div>'
                            '<div style="display:flex;flex-wrap:wrap;width:300px;gap:10px">'
                            '<div id=c style="width:200px;height:10px"></div><div id=d style="width:200px;height:10px"></div></div>'
                            '<div style="display:flex;flex-direction:column;height:300px">'
                            '<div id=e style="height:50px"></div><div id=f style="flex:1"></div></div>')
        # 100px of overflow shrinks a by 1/4 and b by 3/4
        self.assertAlmostEqual(box("#a").width, 175)
        self.assertAlmostEqual(box("#b").width, 125)
        self.assertEqual(box("#d").x, box("#c").x)
        self.assertEqual(box("#d").y, box("#c").y + 20)
        self.assertAlmostEqual(box("#f").height, 250)

    def test_grid_template_areas(self):
        doc, box = self.lay('<body style="margin:0"><div style="display:grid;width:800px;column-gap:20px;'
                            "grid-template:min-content 1fr min-content / 200px minmax(0,1fr);"
                            "grid-template-areas:'top top' 'side main' 'foot foot'\">"
                            '<div id=f style="grid-area:foot;height:30px"></div>'
                            '<div id=m style="grid-area:main;height:100px"></div>'
                            '<div id=s style="grid-area:side">side</div>'
                            '<div id=t style="grid-area:top;height:10px"></div></div>')
        t, s, m, f = box("#t"), box("#s"), box("#m"), box("#f")
        self.assertEqual((t.x, t.y, t.width), (0, 0, 800))
        self.assertEqual((s.x, s.y, s.width), (0, 10, 200))
        self.assertEqual((m.x, m.y, m.width), (220, 10, 580))
        self.assertEqual(s.height, 100)          # stretched to the row
        self.assertEqual((f.y, f.width), (110, 800))

    def test_grid_tracks_and_placement(self):
        doc, box = self.lay('<body style="margin:0"><div style="display:grid;width:640px;gap:20px;'
                            'grid-template-columns:repeat(auto-fill, minmax(200px, 1fr))">'
                            '<div id=a style="height:10px"></div><div id=b style="height:10px"></div>'
                            '<div id=c style="height:10px"></div><div id=d style="height:10px"></div></div>'
                            '<div style="display:grid;width:600px;grid-template-columns:100px 1fr 2fr">'
                            '<div id=e style="grid-column:2 / -1;height:10px"></div>'
                            '<div id=f style="grid-row:1;grid-column:1;height:10px"></div>'
                            '<div id=g style="grid-column:span 2;height:10px"></div></div>')
        a, d = box("#a"), box("#d")
        self.assertEqual(round(a.width), 200)
        self.assertEqual((box("#c").x, box("#c").y), (440, a.y))
        self.assertEqual((d.x, d.y), (0, a.y + 30))
        self.assertEqual((box("#f").x, box("#e").x, box("#e").width), (0, 100, 500))
        self.assertEqual((box("#g").x, box("#g").y, round(box("#g").width)), (0, box("#e").y + 10, 267))

    def test_hover_and_focus_restyle(self):
        doc, box = self.lay('<style>a:hover{color:red} .b:hover .c{padding:10px} input:focus{color:blue}</style>'
                            '<a id=a href=x>link</a><div class=b id=b><span class=c id=c>x</span></div><input id=i>')
        eng = self.page.style_engine
        a, b, c, i = (self.page.document.find_by_id(s) for s in "abci")
        self.assertNotEqual(a.style["color"], "red")
        a.hover_state = True
        roots = eng.dynamic_roots([a], ("hover",))
        self.assertEqual(roots, [a])
        self.assertFalse(eng.restyle_subtree(a))           # colour only: repaint, no relayout
        self.assertEqual(a.style["color"], "red")
        b.hover_state = True
        self.assertEqual(eng.dynamic_roots([b], ("hover",)), [b])
        self.assertTrue(eng.restyle_subtree(b))            # padding moves boxes
        self.assertEqual(c.style["padding-left"], "10px")
        i.focus_state = True
        self.assertEqual(eng.dynamic_roots([i], ("focus",)), [i])
        eng.restyle_subtree(i)
        self.assertEqual(i.style["color"], "blue")

    def test_checkbox_dropdown(self):
        # Wikipedia's no-JavaScript dropdowns: a checkbox and `:checked ~ .content`
        doc, box = self.lay('<style>.content{display:none} .cb:checked ~ .content{display:block}'
                            ' .menu{position:relative} .content{position:absolute;top:100%;z-index:5;width:max-content}</style>'
                            '<div class=menu id=m><input type=checkbox class=cb id=cb><label for=cb>Open</label>'
                            '<div class=content id=c>first item</div></div><p>after</p>')
        eng = self.page.style_engine
        cb, content = self.page.document.find_by_id("cb"), self.page.document.find_by_id("c")
        self.assertEqual(content.style["display"], "none")
        cb.checked = True
        roots = eng.dynamic_roots([cb], ("checked",))
        self.assertEqual(roots, [cb.parent])      # the ~ combinator reaches the siblings
        self.assertTrue(eng.restyle_subtree(roots[0]))
        self.assertEqual(content.style["display"], "block")

    def test_fixed_and_sticky_layers(self):
        doc, box = self.lay('<body style="margin:0"><div id=f style="position:fixed;top:0;height:20px;width:100px"></div>'
                            '<div id=p style="height:2000px"><div id=s style="position:sticky;top:10px;height:30px;margin-top:100px">'
                            '</div></div>', 800)
        dl = doc.paint()
        kinds = [layer.kind for layer in dl.layers]
        self.assertEqual(sorted(kinds), ["fixed", "sticky"])
        fixed = next(l for l in dl.layers if l.kind == "fixed")
        sticky = next(l for l in dl.layers if l.kind == "sticky")
        self.assertEqual(fixed.offset(500), 500)          # stays put on screen
        self.assertEqual(sticky.offset(0), 0)             # in place before reaching top: 10px
        self.assertEqual(sticky.offset(500), 410)         # 500 + 10 - 100
        self.assertEqual(sticky.offset(5000), sticky.room)  # stops at the end of its parent

    def test_multicolumn(self):
        items = "".join("<li style=height:20px>%d</li>" % i for i in range(10))
        doc, box = self.lay('<body style="margin:0"><div id=c style="width:600px;column-count:2;column-gap:20px">'
                            '<ul id=u style="margin:0;padding:0">' + items + '</ul></div>')
        c = box("#c")
        self.assertAlmostEqual(c.height, 100)             # 10 items of 20px in two columns
        lis = [b for b in box("#u").children]
        self.assertEqual(lis[0].x, 0)
        self.assertEqual(lis[5].x, 310)                   # (600 - 20) / 2 + 20
        self.assertEqual(lis[5].y, lis[0].y)

    def test_rowspan(self):
        doc, box = self.lay('<table style="border-spacing:0"><tr><td id=a rowspan=2 style="padding:0">A</td>'
                            '<td id=b style="padding:0">B</td></tr><tr><td id=c style="padding:0">C</td></tr></table>')
        a, b, c = box("#a"), box("#b"), box("#c")
        self.assertEqual(b.x, c.x)                        # C sits under B, not under A
        self.assertGreater(c.x, a.x)
        self.assertAlmostEqual(a.height, c.y + c.height - b.y)

    def test_word_breaking_and_ellipsis(self):
        doc, box = self.lay('<body style="margin:0"><div id=w style="width:100px;overflow-wrap:anywhere">'
                            + "x" * 80 + '</div><div id=e style="width:100px;white-space:nowrap;overflow:hidden;'
                            'text-overflow:ellipsis">' + "word " * 20 + '</div>')
        w = box("#w")
        self.assertGreater(len(w.lines), 2)
        for line in w.lines:
            for f in line.frags:
                self.assertLessEqual(f.x + f.width, 100.5)
        e = box("#e")
        self.assertTrue(e.lines[0].frags[-1].text.endswith("…"))
        self.assertLessEqual(e.lines[0].frags[-1].x + e.lines[0].frags[-1].width, 100.5)

    def test_overflow_auto_scrolls(self):
        doc, box = self.lay('<body style="margin:0"><div id=s style="height:50px;overflow:auto">'
                            '<div style="height:200px"></div></div>')
        doc.paint()
        self.assertEqual(len(doc.ctx.scrollers), 1)
        _, _, max_sx, max_sy = doc.ctx.scrollers[0]
        self.assertEqual((max_sx, max_sy), (0, 150))

    def test_layers_nesting_container_scope(self):
        doc, box = self.lay('<style>@layer base, theme; @layer theme { .t { color: green } } @layer base { .t { color: red } }'
                            '.card { & h3 { color: purple } .in { color: teal } }'
                            '.wrap { container-type: inline-size; width: 300px } .b { color: black }'
                            '@container (min-width: 250px) { .b { color: blue } }'
                            '@scope (.s) to (.stop) { p { color: orange } }</style>'
                            '<p class=t id=t>x</p><div class=card><h3 id=h>h</h3><span class=in id=i>i</span></div>'
                            '<div class=wrap><p class=b id=b>b</p></div>'
                            '<div class=s><p id=s1>in</p><div class=stop><p id=s2>out</p></div></div>')
        d = self.page.document.find_by_id
        self.assertEqual(d("t").style["color"], "green")
        self.assertEqual(d("h").style["color"], "purple")
        self.assertEqual(d("i").style["color"], "teal")
        self.assertEqual(d("b").style["color"], "blue")
        self.assertEqual(d("s1").style["color"], "orange")
        self.assertNotEqual(d("s2").style["color"], "orange")

    def test_math_functions_and_units(self):
        from browser.style import length
        self.assertEqual(length("round(up, 13px, 5px)", 16), 15)
        self.assertEqual(length("mod(-7px, 5px)", 16), 3)
        self.assertAlmostEqual(length("calc(100px * sin(30deg))", 16), 50)
        self.assertEqual(length("calc((100% - 30px) / 4)", 16, 400), 92.5)
        self.assertEqual(length("clamp(1rem, 2px, 2rem)", 16), 16)

    def test_modern_colors(self):
        from browser.style import parse_color
        self.assertEqual(parse_color("oklch(62.8% 0.2577 29.23)"), "#ff0000")
        self.assertEqual(parse_color("color-mix(in srgb, red 50%, blue)"), "#800080")
        self.assertEqual(parse_color("hwb(120 0% 0%)"), "#00ff00")

    def test_stacking_order(self):
        doc, box = self.lay('<body style="margin:0"><div style="position:relative">'
                            '<div id=a style="position:absolute;width:10px;height:10px;background:red;z-index:2"></div>'
                            '<div id=b style="position:absolute;width:10px;height:10px;background:lime;z-index:1"></div>'
                            '<div id=c style="position:absolute;width:10px;height:10px;background:blue;z-index:-1"></div>'
                            '<p style="margin:0;background:yellow;height:10px">in flow</p></div>')
        colors = [c.color for c in doc.paint().commands if type(c).__name__ == "DrawRect"]
        order = [colors.index(c) for c in ("#0000ff", "#ffff00", "#00ff00", "#ff0000")]
        self.assertEqual(order, sorted(order))     # z-1, in-flow, z1, z2

    def test_transforms_and_animation(self):
        doc, box = self.lay('<style>@keyframes spin { to { transform: rotate(360deg) } }'
                            '#r { animation: spin 2s linear infinite }</style>'
                            '<div id=r style="width:100px;height:50px;background:red;transform:rotate(90deg)"></div>')
        polys = [c for c in doc.paint().commands if type(c).__name__ == "DrawPolygon"]
        self.assertTrue(polys)
        xs = [p[0] for p in polys[0].pts]
        self.assertAlmostEqual(max(xs) - min(xs), 50, delta=1)    # turned a quarter: 50 wide
        from browser import animation
        an = animation.Animator(self.page)
        an.scan(0.0)
        an.tick(0.5)
        # the missing "from" frame starts at the element's own transform (90deg): 90 + 270 * 0.25
        self.assertEqual(self.page.document.find_by_id("r").style["transform"], "rotate(157.5deg)")

    def test_rtl_and_vertical(self):
        doc, box = self.lay('<body style="margin:0"><p id=p dir=rtl style="margin:0;width:300px">abc</p>'
                            '<div id=v style="writing-mode:vertical-rl">vertical words</div>')
        p = box("#p")
        f = p.lines[0].frags[0]
        self.assertAlmostEqual(f.x + f.width, 300, delta=1)     # right-aligned
        v = box("#v")
        self.assertGreater(v.height, v.width)

    def test_filters_and_clip_path(self):
        doc, box = self.lay('<body style="margin:0"><div style="width:20px;height:20px;background:#0000ff;'
                            'filter:invert(1)"></div><div style="width:20px;height:20px;background:red;'
                            'clip-path:inset(0 10px 0 0)"></div>')
        cmds = doc.paint().commands
        self.assertTrue(any(getattr(c, "color", None) == "#ffff00" for c in cmds))
        red = [c for c in cmds if getattr(c, "color", None) == "#ff0000"]
        self.assertEqual(red[0].right - red[0].left, 10)

    def test_center_and_percent_columns(self):
        # Google's home page: a table inside <center> with 25% / content / 25% columns
        doc, box = self.lay('<body style="margin:0"><center><table cellpadding=0 cellspacing=0><tr>'
                            '<td width="25%" id=l>&nbsp;</td><td id=m><div style="width:400px;height:10px"></div></td>'
                            '<td width="25%" id=r>x</td></tr></table></center>', 1000)
        l, m, r = box("#l"), box("#m"), box("#r")
        self.assertAlmostEqual(l.width, r.width, delta=1)
        self.assertAlmostEqual(m.x + m.width / 2, 500, delta=2)     # the middle cell is centred

    def test_svg_without_size_fills_its_container(self):
        # Google's icons: <svg viewBox="0 -960 960 960"> in a 24px box, no width/height
        doc, box = self.lay('<div style="width:24px"><svg id=s viewBox="0 -960 960 960">'
                            '<path d="M0 0h10v10z"/></svg></div>')
        s = box("#s")
        self.assertEqual((s.width, s.height), (24, 24))

    def test_button_centres_its_contents(self):
        # Google's "AI Mode" button: a 36px-high button around a 20px row
        doc, box = self.lay('<button id=b style="height:36px;padding:0;border:0">'
                            '<div id=c style="height:20px">AI Mode</div></button>')
        b, c = box("#b"), box("#c")
        self.assertAlmostEqual(c.y - b.y, 8, delta=1)

    def test_lines_in_clipped_positioned_boxes(self):
        # ubishops.ca crashed: an underline inside a positioned box under overflow:hidden had no clipped()
        doc, box = self.lay('<div style="overflow:hidden;width:60px;height:30px">'
                            '<div style="position:relative;z-index:1"><a href="#" style="text-decoration:underline">'
                            'a long underlined link that overflows</a><hr></div></div>')
        dl = doc.paint()
        lines = [c for c in dl.commands if type(c).__name__ == "DrawLine"]
        self.assertTrue(lines)
        for c in lines:
            self.assertLessEqual(max(c.x1, c.x2), 60.5)
        cut = paint.DrawLine(0, 5, 100, 5, "#000").clipped((10, 0, 50, 10))
        self.assertEqual((cut.x1, cut.x2), (10, 50))
        self.assertIsNone(paint.DrawLine(0, 50, 100, 50, "#000").clipped((10, 0, 50, 10)))

    def test_z_index_inside_z_auto_positioned_box(self):
        # ubishops.ca: header z-index:21 must cover a z-index:4 section nested in a position:relative (z auto) div
        doc, box = self.lay('<body style="margin:0"><header style="position:relative;z-index:21;height:100px;'
                            'background:red"></header><main style="margin-top:-40px"><div style="position:relative">'
                            '<section style="position:relative;z-index:4;height:100px;background:blue"></section>'
                            '</div></main>')
        colors = [c.color for c in doc.paint().commands if getattr(c, "color", None) in ("#ff0000", "#0000ff")]
        self.assertEqual(colors, ["#0000ff", "#ff0000"])    # blue first, red painted on top

    def test_absolute_percent_height_and_top_bottom_stretch(self):
        # ubishops.ca search icon: position:absolute; top:0; height:100% inside a 40px form
        doc, box = self.lay('<form style="position:relative;width:300px"><div style="height:40px"></div>'
                            '<div id=p style="position:absolute;top:0;right:0;width:50px;height:100%"></div>'
                            '<div id=s style="position:absolute;top:5px;bottom:5px;left:0;width:10px"></div></form>')
        self.assertEqual(box("#p").height, 40)
        self.assertEqual(box("#s").height, 30)

    def test_mask_shorthand_with_quoted_data_url(self):
        # Google's checkbox tick: -webkit-mask: url('data:...<svg xmlns="...">') center/cover no-repeat
        decls = dict(css_parser.expand_shorthand(
            "-webkit-mask", "url('data:image/svg+xml,\\00003csvg xmlns=\"http://www.w3.org/2000/svg\"/>') "
                            "center/cover no-repeat"))
        self.assertEqual(decls["mask-size"], "cover")
        self.assertEqual(decls["mask-repeat"], "no-repeat")
        self.assertEqual(css_parser.css_urls(decls["mask-image"]),
                         ['data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg"/>'])

    def test_supports_conditions(self):
        self.assertTrue(css_parser.supports_matches("(display: grid)"))
        self.assertTrue(css_parser.supports_matches("(display:flex) and (gap:1px)"))
        self.assertTrue(css_parser.supports_matches("(position: sticky)"))
        self.assertFalse(css_parser.supports_matches("(anchor-name: --a)"))
        self.assertTrue(css_parser.supports_matches("(-webkit-mask-image:none) or (mask-image:none)"))
        self.assertFalse(css_parser.supports_matches("not ((-webkit-mask-image:none) or (mask-image:none))"))

    def test_table_columns_align(self):
        doc, box = self.lay('<table><tr><td id=a>short</td><td>x</td></tr><tr><td id=b>much longer cell text</td><td>y</td></tr></table>')
        self.assertEqual(box("#a").width, box("#b").width)
        self.assertEqual(box("#a").x, box("#b").x)

    def test_absolute_position(self):
        doc, box = self.lay('<body style="margin:0"><div style="position:relative;height:200px;width:400px"><span id=a style="position:absolute;right:10px;bottom:10px;width:50px;height:20px"></span></div>')
        a = box("#a")
        self.assertEqual((a.x, a.y), (340, 170))

    def test_images_get_intrinsic_size(self):
        import base64
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (40, 30), "red").save(buf, "PNG")
        src = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        doc, box = self.lay('<img id=a src="%s"><img id=b src="%s" width=80>' % (src, src))
        self.assertEqual((box("#a").width, box("#a").height), (40, 30))
        self.assertEqual((box("#b").width, box("#b").height), (80, 60))

    def test_link_hit_testing(self):
        doc, box = self.lay('<body style="margin:0"><a href="/next" id=a>click me</a></body>')
        dl = doc.paint()
        node = dl.hit_test(5, 8)
        self.assertIsNotNone(node)
        self.assertEqual(node.attributes["href"], "/next")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# End-to-end tests driving the real window with synthetic mouse/keyboard events.

class SiteHandler(Handler):
    PAGES = {
        "/a": b'<html><head><title>Page A</title></head><body style="margin:0"><p><a id=l href="/b">go to b</a></p>'
              b'<form action="/search" method="get"><input name=q id=q><input type=submit value=Go id=s></form>'
              + b"<p>filler</p>" * 200 + b'<p id="end">end</p></body></html>',
        "/b": b'<html><head><title>Page B</title></head><body><a href="/a#end">back to a, end</a></body></html>',
    }

    def do_GET(self):
        if self.path in self.PAGES:
            self._send(200, self.PAGES[self.path])
        elif self.path.startswith("/search"):
            self._send(200, ("<title>Results</title><p>you searched %s</p>" % self.path).encode())
        else:
            Handler.do_GET(self)


@unittest.skipUnless(HAVE_TK, "needs a display")
class BrowserWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SiteHandler)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        from browser.gui import Browser
        self.b = Browser(self.base + "/a", 800, 600)
        self.wait()

    def tearDown(self):
        self.b.close()

    def wait(self, timeout=10):
        import time
        end = time.time() + timeout
        while time.time() < end:
            self.b.root.update()
            if not self.b.current.loading and self.b.current.display_list is not None:
                self.b.root.update()
                return
            time.sleep(0.02)
        self.fail("page did not load")

    def click_node(self, node_id):
        tab = self.b.current
        for x1, y1, x2, y2, n in tab.display_list.hits:
            if n.attributes.get("id") == node_id or (n.tag == "a" and n.attributes.get("id") == node_id):
                cx, cy = (x1 + x2) / 2, (y1 + y2) / 2 - tab.scroll
                self.b.canvas.event_generate("<Button-1>", x=int(cx), y=int(cy))
                self.b.root.update()
                return
        self.fail("no clickable %s" % node_id)

    def test_click_link_back_forward_reload(self):
        b = self.b
        self.assertEqual(b.current.title, "Page A")
        self.click_node("l")
        self.wait()
        self.assertEqual(b.current.title, "Page B")
        b.go_back()
        self.wait()
        self.assertEqual(b.current.title, "Page A")
        b.go_forward()
        self.wait()
        self.assertEqual(b.current.title, "Page B")
        b.reload()
        self.wait()
        self.assertEqual(b.current.title, "Page B")
        self.assertEqual(b.address.get(), self.base + "/b")

    def test_fragment_link_scrolls(self):
        b = self.b
        b.navigate(self.base + "/b")
        self.wait()
        b.navigate(self.base + "/a#end")
        self.wait()
        self.assertGreater(b.current.scroll, 1000)

    def test_scrolling_keys(self):
        b = self.b
        b.canvas.focus_force()
        b.canvas.event_generate("<Key>", keysym="Next")
        b.root.update()
        self.assertGreater(b.current.scroll, 100)
        b.canvas.event_generate("<Key>", keysym="End")
        b.root.update()
        self.assertEqual(b.current.scroll, b.max_scroll())
        b.canvas.event_generate("<Key>", keysym="Home")
        b.root.update()
        self.assertEqual(b.current.scroll, 0)

    def test_type_and_submit_form(self):
        b = self.b
        self.click_node("q")
        for ch in "hi there":
            b.canvas.event_generate("<Key>", keysym="space" if ch == " " else ch)
        b.root.update()
        self.assertEqual(b.current.focused.form_value, "hi there")
        self.click_node("s")
        self.wait()
        self.assertEqual(b.current.title, "Results")
        self.assertIn("q=hi+there", b.address.get())

    def test_address_bar_and_error_page(self):
        b = self.b
        b.address.delete(0, "end")
        b.address.insert(0, "http://127.0.0.1:1/")
        b._on_address_enter()
        self.wait()
        self.assertIn("could not be loaded", b.current.title)

    def test_resize_relayouts(self):
        b = self.b
        before = b.current.doc_layout.height
        b.root.geometry("400x600")
        import time
        for _ in range(50):
            b.root.update()
            time.sleep(0.02)
        self.assertNotEqual(b.current.ctx.viewport_width, 800 - 20)
        self.assertGreaterEqual(b.current.doc_layout.height, before)


class FaviconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def icon(self, path):
        from browser import engine, favicon
        return favicon.load(engine.load(self.base + path, 800))

    def test_link_rel_icon_is_fetched_and_shrunk(self):
        img = self.icon("/fav-page")
        self.assertEqual(img.size, (16, 16))
        self.assertEqual(img.getpixel((8, 8))[:3], (255, 0, 0))

    def test_falls_back_to_favicon_ico(self):
        self.assertEqual(self.icon("/fav-default").size, (16, 16))

    def test_no_icon_gives_none(self):
        from browser import engine, favicon
        page = engine.load(self.base + "/final", 800)
        page.url = network.URL("http://127.0.0.1:1/x")      # nothing listens here
        page.base_url = page.url
        self.assertIsNone(favicon.load(page))
