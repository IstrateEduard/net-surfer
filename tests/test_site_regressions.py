"""Reduced regressions from the real Bishop's/Kadence homepage."""
import io
import unittest

from browser import css_parser, style, layout, engine, network, js
from browser.html_parser import parse
from browser.dom import Element


def styled(css, body='<div id="a"></div>'):
    doc = parse('<style>' + css + '</style><body>' + body + '</body>')
    rules, _ = css_parser.parse_stylesheet(css)
    style.StyleEngine(rules).style_tree(doc)
    return doc


class VariableRegressions(unittest.TestCase):
    def test_forward_palette_and_font_aliases(self):
        doc = styled(':root {--color:var(--palette);--font:var(--face),sans-serif}'
                     ':root {--palette:#582c83;--face:"Cooper Hewitt"}'
                     '#a {background:var(--color);font-family:var(--font)}')
        self.assertEqual(doc.find_by_id('a').style['background-color'], '#582c83')
        self.assertEqual(doc.find_by_id('a').style['font-family'], '"Cooper Hewitt",sans-serif')

    def test_invalid_alias_uses_nested_fallback(self):
        doc = styled(':root {--width:calc(100vw - var(--missing))}'
                     '#a {width:var(--width,calc(100% - var(--gap, 12px)))}')
        self.assertEqual(doc.find_by_id('a').style['width'], 'calc(100% - 12px)')

    def test_cycles_are_invalid_and_do_not_leak_partial_tokens(self):
        doc = styled(':root {--a:var(--b,red);--b:var(--a,blue)}'
                     '#a {color:var(--a,green);width:calc(100px + var(--missing))}')
        self.assertEqual(doc.find_by_id('a').style['color'], 'green')
        self.assertEqual(doc.find_by_id('a').style['width'], 'auto')

    def test_inherited_alias_is_already_computed(self):
        doc = styled(':root {--base:red;--alias:var(--base)}'
                     '#a {--base:blue;color:var(--alias)}')
        self.assertEqual(doc.find_by_id('a').style['color'], 'red')

    def test_initial_custom_property_uses_fallback(self):
        doc = styled(':root {--base:red} #a {--base:initial;color:var(--base,blue)}')
        self.assertEqual(doc.find_by_id('a').style['color'], 'blue')

    def test_font_faces_respect_media(self):
        _, parser = css_parser.parse_stylesheet_full('@font-face{font-family:Example;src:url(font.woff2)}'
                                                     '@media print{@font-face{font-family:Print;src:url(p.woff)}}')
        self.assertEqual(len(parser.font_faces), 1)
        self.assertEqual(parser.font_faces[0]['font-family'], 'Example')


class HeightRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tkinter
        cls.owns_root = tkinter._default_root is None
        try:
            cls.root = tkinter._default_root or tkinter.Tk()
            if cls.owns_root:
                cls.root.withdraw()
        except tkinter.TclError as error:
            raise unittest.SkipTest(str(error))

    @classmethod
    def tearDownClass(cls):
        if cls.owns_root:
            from browser import fonts
            fonts._FONTS.clear()
            cls.root.destroy()

    def boxes(self, css, body):
        doc = styled('body{margin:0}' + css, body)
        ctx = layout.LayoutContext(network.URL('about:test'), {}, 800, 600)
        tree = layout.DocumentLayout(doc, ctx)
        tree.layout(800, 600)
        found, stack = {}, [tree.root]
        while stack:
            box = stack.pop()
            if box.node is not None and box.node.attributes.get('id'):
                found[box.node.attributes['id']] = box
            stack.extend(box.children + box.abs_boxes)
        return found

    def test_nested_percent_height_uses_content_box(self):
        boxes = self.boxes('#p{height:240px;padding:20px;box-sizing:border-box}'
                           '#a{height:50%} #b{height:50%}',
                           '<div id=p><div id=a><div id=b></div></div></div>')
        self.assertEqual(boxes['a'].height, 100)
        self.assertEqual(boxes['b'].height, 50)

    def test_auto_height_is_not_a_percentage_basis(self):
        boxes = self.boxes('#p{min-height:240px}#a{height:50%}',
                           '<div id=p><div id=a></div></div>')
        self.assertEqual(boxes['a'].height, 0)

    def test_stretched_flex_item_relayouts_percentage_descendant(self):
        boxes = self.boxes('#p{display:flex;height:240px}#a{width:100px}#b{height:100%}',
                           '<div id=p><div id=a><div id=b></div></div></div>')
        self.assertEqual(boxes['a'].height, 240)
        self.assertEqual(boxes['b'].height, 240)


@unittest.skipUnless(js.enabled(), 'QuickJS unavailable')
class PageScriptRegressions(unittest.TestCase):
    def script(self, source):
        page = engine.Page(network.URL('https://example.test/'))
        page.document = parse('<body><script>' + source + '</script></body>')
        host = js.ScriptHost(page, (1234, 678))
        self.addCleanup(host.close)
        host.run_load_scripts()
        host.run_zero_timers()
        return page, host

    def test_document_client_size_is_viewport_before_layout(self):
        page, _ = self.script("console.log(innerWidth-document.documentElement.clientWidth, document.documentElement.clientHeight)")
        self.assertIn('console.log: 0 678', '\n'.join(page.log))

    def test_domparser_builds_inert_detached_html(self):
        page, _ = self.script("""var d=new DOMParser().parseFromString('<title>Parsed</title><button>Next</button><script>console.log("BAD")<\\/script>',"text/html");
            console.log(d.nodeType,d.title,d.body.firstElementChild.tagName,document.querySelector("button"));
            document.body.appendChild(d.body.firstElementChild);""")
        log = '\n'.join(page.log)
        self.assertIn('console.log: 9 Parsed BUTTON null', log)
        self.assertNotIn('console.log: BAD', log)
        self.assertEqual(len(page.document.get_elements_by_tag('button')), 1)

    def test_intersection_entry_feature_detection_and_callback(self):
        page, _ = self.script('console.log("intersectionRatio" in IntersectionObserverEntry.prototype);'
                              'new IntersectionObserver(function(entries){console.log(entries[0] instanceof IntersectionObserverEntry,entries[0].isIntersecting)}).observe(document.body)')
        log = '\n'.join(page.log)
        self.assertIn('console.log: true', log)
        self.assertIn('console.log: true true', log)


class WebFontRegressions(unittest.TestCase):
    def test_decoding_measurement_and_rasterization(self):
        try:
            from fontTools.fontBuilder import FontBuilder
            from fontTools.pens.ttGlyphPen import TTGlyphPen
        except ImportError:
            self.skipTest('fontTools unavailable')
        from browser.webfonts import Face, FontSet
        # An original synthetic font makes this test independent of downloads
        # and installed system fonts; the glyph advance is exactly half an em.
        builder = FontBuilder(1000, isTTF=True)
        builder.setupGlyphOrder(['.notdef', 'space', 'A'])
        glyphs = {}
        for name in ['.notdef', 'space', 'A']:
            pen = TTGlyphPen(None)
            if name == 'A':
                pen.moveTo((50, 0)); pen.lineTo((250, 700)); pen.lineTo((450, 0)); pen.closePath()
            glyphs[name] = pen.glyph()
        builder.setupGlyf(glyphs)
        builder.setupHorizontalMetrics({name: (500, 0) for name in glyphs})
        builder.setupHorizontalHeader(ascent=800, descent=-200)
        builder.setupCharacterMap({32: 'space', 65: 'A'})
        builder.setupNameTable({'familyName': 'Regression', 'styleName': 'Regular'})
        builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
        builder.setupPost()
        for flavor in [None, 'woff', 'woff2']:
            builder.font.flavor = flavor
            data = io.BytesIO(); builder.font.save(data)
            face = Face({'font-family': 'Regression'}, data.getvalue())
            faces = FontSet(); faces.faces['regression'] = [face]
            font = faces.get('Regression,sans-serif', 20, 400, False)
            self.assertAlmostEqual(font.measure('AA'), 20, places=1)
            image, _, _ = font.rasterize('AA', '#582c83')
            self.assertIsNotNone(image.getbbox())


if __name__ == '__main__':
    unittest.main()
