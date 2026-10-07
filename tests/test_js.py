"""Tests for page scripts (browser/js.py, js_prelude.js, js_worker.py).

Run:  python -m unittest tests.test_js
The window tests (JSWindowTests) drive the real Tk window; on Windows run
them one at a time, like the other window tests.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.setrecursionlimit(20000)

from browser import engine, js, network  # noqa: E402
from browser.dom import Element  # noqa: E402
from browser.html_parser import parse  # noqa: E402

try:
    import tkinter
    tkinter.Tk().destroy()
    HAVE_TK = True
except Exception:
    HAVE_TK = False


def by_id(root, ident):
    return root.find_by_id(ident)


def run_page(body, url="https://example.com/dir/page.html"):
    """Parse a page, run its scripts, return (page, host)."""
    page = engine.Page(network.URL(url))
    page.document = parse("<html><head><title>T</title></head><body>%s</body></html>" % body)
    host = js.ScriptHost(page)
    host.run_load_scripts()
    return page, host


def logs(page):
    return "\n".join(page.log)


@unittest.skipUnless(js.enabled(), "quickjs is not installed")
class DOMTests(unittest.TestCase):
    def tearDown(self):
        if getattr(self, "host", None) is not None:
            self.host.close()

    def run_js(self, body, **kw):
        self.page, self.host = run_page(body, **kw)
        return self.page

    def test_read_and_change_the_dom(self):
        page = self.run_js("""<div id=a class="x y">hi <b>there</b></div><script>
            var a = document.getElementById('a');
            a.classList.add('z'); a.classList.remove('x');
            a.style.color = 'red'; a.dataset.fooBar = '1';
            var s = document.createElement('span'); s.textContent = '<new>'; a.appendChild(s);
            document.title = 'Changed';
            console.log(a.tagName, a.children.length, a.querySelector('b').textContent,
                        document.querySelectorAll('div b, span').length);
            </script>""")
        a = by_id(page.document, "a")
        self.assertEqual(a.attributes["class"], "y z")
        self.assertEqual(a.attributes["style"], "color: red")
        self.assertEqual(a.attributes["data-foo-bar"], "1")
        self.assertEqual(a.children[-1].tag, "span")
        self.assertEqual(a.children[-1].text_content(), "<new>")
        self.assertEqual(self.host.title(), "Changed")
        self.assertIn("console.log: DIV 2 there 2", logs(page))

    def test_inner_html_and_serialization(self):
        page = self.run_js("""<div id=a></div><script>
            var a = document.getElementById('a');
            a.innerHTML = '<p class="q">one &amp; two</p><img src="x.png"><script>console.log("must not run")<\\/script>';
            console.log(a.innerHTML);
            a.insertAdjacentHTML('beforeend', '<i>end</i>');
            </script>""")
        a = by_id(page.document, "a")
        self.assertEqual([c.tag for c in a.children], ["p", "img", "script", "i"])
        self.assertIn('console.log: <p class="q">one &amp; two</p><img src="x.png">', logs(page))
        self.assertNotIn("must not run", logs(page).replace('console.log("must not run")', ""))

    def test_document_write_during_load(self):
        page = self.run_js("<p>before</p><script>document.write('<em id=w>written</em>')</script><p>after</p>")
        body = page.document.children[-1]
        tags = [c.tag for c in body.children if isinstance(c, Element)]
        self.assertEqual(tags, ["p", "script", "em", "p"])

    def test_events_bubble_and_prevent_default(self):
        page = self.run_js("""<div id=outer><a id=link href="/x">x</a></div><script>
            var order = [];
            document.getElementById('outer').addEventListener('click', function (e) { order.push('outer'); });
            document.getElementById('outer').addEventListener('click', function (e) { order.push('capture'); }, true);
            document.getElementById('link').onclick = function (e) { order.push('link'); return false; };
            document.addEventListener('click', function (e) { console.log(order.join(','), e.target.id, e.isTrusted); });
            </script>""")
        link = by_id(page.document, "link")
        self.assertTrue(self.host.dispatch(link, "click", {"bubbles": True, "cancelable": True}))
        self.assertIn("console.log: capture,link,outer link true", logs(page))

    def test_inline_handler_attribute(self):
        page = self.run_js("""<button id=b onclick="this.textContent = 'clicked'">press</button>""")
        b = by_id(page.document, "b")
        self.host.dispatch(b, "click", {"bubbles": True})
        self.assertEqual(b.text_content(), "clicked")

    def test_timers_promises_and_load_events(self):
        page = self.run_js("""<p id=p>0</p><script>
            var log = [];
            document.addEventListener('DOMContentLoaded', function () { log.push('dcl:' + document.readyState); });
            window.addEventListener('load', function () { log.push('load:' + document.readyState); console.log(log.join(' ')); });
            Promise.resolve().then(function () { log.push('promise'); });
            setTimeout(function () { log.push('timeout'); }, 0);
            var n = 0, id = setInterval(function () { n++; document.getElementById('p').textContent = n;
                                                       if (n == 3) clearInterval(id); }, 10);
            </script>""")
        self.assertIn("console.log: promise dcl:interactive", logs(page))
        self.assertIn("load:complete", logs(page))
        end = time.time() + 3
        while time.time() < end and by_id(page.document, "p").text_content() != "3":
            self.host.tick()
            time.sleep(0.01)
        self.assertEqual(by_id(page.document, "p").text_content(), "3")
        self.assertEqual(self.host.timers, {})

    def test_form_values(self):
        page = self.run_js("""<form id=f><input id=t name=t value=a><input type=checkbox id=c>
            <select id=s><option>x</option><option value=yy>y</option></select></form><script>
            var t = document.getElementById('t'), c = document.getElementById('c'), s = document.getElementById('s');
            console.log(t.value, c.checked, s.value, s.selectedIndex);
            t.value = 'typed'; c.checked = true; s.value = 'yy';
            console.log(new URLSearchParams(new FormData(document.getElementById('f'))).toString());
            </script>""")
        self.assertIn("console.log: a false x 0", logs(page))
        self.assertIn("console.log: t=typed", logs(page))
        self.assertEqual(by_id(page.document, "t").form_value, "typed")
        self.assertTrue(by_id(page.document, "c").checked)

    def test_external_and_inserted_scripts(self):
        src = "data:text/javascript,document.getElementById('a').textContent%20=%20'external'"
        page, host = run_page("""<p id=a></p><script src="%s"></script><script>
            var s = document.createElement('script');
            s.textContent = "document.getElementById('a').title = 'inserted'";
            document.body.appendChild(s);
            </script>""" % src)
        self.host = host
        host.tick()
        a = by_id(page.document, "a")
        self.assertEqual(a.text_content(), "external")
        self.assertEqual(a.attributes.get("title"), "inserted")

    def test_location_and_url(self):
        page = self.run_js("""<script>
            console.log(location.pathname, location.host, new URL('../x?a=1#h', location.href).href,
                        new URLSearchParams('a=1&b=two+words').get('b'));
            location.href = 'other.html';
            </script>""")
        self.assertIn("console.log: /dir/page.html example.com https://example.com/x?a=1#h two words", logs(page))
        self.assertEqual(self.host.pending_nav, ("https://example.com/dir/other.html", False))

    def test_generator_yield_inside_try_finally(self):
        # QuickJS can't compile this as written (Google's main script has it); js_rewrite works around it
        page = self.run_js("""<script>
            var out = [];
            function* g() { try { out.push('got ' + (yield 1)); } finally { out.push('finally'); } }
            var it = g(); it.next(); it.next('x');
            var it2 = g(); it2.next(); it2.return();
            console.log(out.join(','));
            </script>""")
        self.assertIn("console.log: got x,finally,finally", logs(page))

    def test_modern_globals(self):
        page = self.run_js("""<script>
            var c = new AbortController(), seen = [];
            c.signal.addEventListener('abort', () => seen.push('abort'));
            c.abort();
            var r = new WeakRef(document.body);
            document.fonts.load('10pt Foo').then(f => console.log('fonts ' + f.length + ' ' + seen + ' ' +
                c.signal.aborted + ' ' + (r.deref() === document.body)));
            </script>""")
        self.assertIn("console.log: fonts 0 abort true true", logs(page))

    def test_script_errors_are_logged_not_fatal(self):
        page = self.run_js("<script>undefinedFunction()</script><script>console.log('second ran')</script>")
        self.assertIn("ReferenceError", logs(page))
        self.assertIn("second ran", logs(page))
        self.assertTrue(self.host.active)


@unittest.skipUnless(js.enabled(), "quickjs is not installed")
class SandboxTests(unittest.TestCase):
    """Page scripts are hostile: none of these may escape or hang the browser."""

    def setUp(self):
        self.old = (js.SCRIPT_TIME_LIMIT, js.CALL_TIME_LIMIT)
        js.SCRIPT_TIME_LIMIT = js.CALL_TIME_LIMIT = 1.0
        self.host = None

    def tearDown(self):
        js.SCRIPT_TIME_LIMIT, js.CALL_TIME_LIMIT = self.old
        if self.host is not None:
            self.host.close()

    def run_js(self, body, url="https://example.com/"):
        page, self.host = run_page(body, url)
        return page

    def test_no_python_os_or_bridge_objects(self):
        page = self.run_js("""<script>console.log([typeof require, typeof process, typeof std, typeof os,
            typeof __native, typeof Python, typeof importScripts].join(','))</script>""")
        self.assertIn("console.log: " + ",".join(["undefined"] * 7), logs(page))

    def test_infinite_loop_is_killed(self):
        t0 = time.time()
        page = self.run_js("<script>while (true) {}</script><script>console.log('never')</script>")
        self.assertLess(time.time() - t0, 5)
        self.assertFalse(self.host.active)
        self.assertIn("ran longer than", logs(page))
        self.assertNotIn("never", logs(page))

    def test_infinite_loop_in_event_handler_is_killed(self):
        page = self.run_js("<button id=b onclick='for(;;){}'>x</button>")
        t0 = time.time()
        self.host.dispatch(by_id(page.document, "b"), "click", {"bubbles": True})
        self.assertLess(time.time() - t0, 5)
        self.assertFalse(self.host.active)

    def test_memory_limit(self):
        page = self.run_js("<script>var a = []; while (true) a.push(new Array(100000).fill(1));</script>"
                           "<script>console.log('still alive')</script>")
        self.assertIn("out of memory", logs(page))
        self.assertIn("still alive", logs(page))

    def test_deep_recursion(self):
        page = self.run_js("<script>function f() { f() } try { f() } catch (e) { console.log('caught', e) }</script>")
        self.assertIn("caught InternalError: stack overflow", logs(page))

    def test_endless_promise_chain(self):
        self.run_js("<script>function f() { Promise.resolve().then(f) } f()</script>")
        self.assertFalse(self.host.active)

    def test_network_is_same_origin_http_only(self):
        page = self.run_js("""<script>
            fetch('https://other.example/').catch(function (e) { console.log('cross', e.message) });
            fetch('file:///C:/Windows/win.ini').catch(function (e) { console.log('file', e.message) });
            try { location.href = 'file:///C:/' } catch (e) { console.log('nav', e.name) }
            console.log('cookie[' + document.cookie + ']', window.open('https://x.example/'));
            </script>""")
        text = logs(page)
        self.assertIn("console.log: cross", text)
        self.assertIn("cross-origin request blocked", text)
        self.assertIn("console.log: file", text)
        self.assertIn("console.log: nav SecurityError", text)
        self.assertIn("console.log: cookie[] null", text)
        self.assertIsNone(self.host.pending_nav)

    def test_file_scripts_blocked_on_web_pages(self):
        page = self.run_js('<script src="file:///C:/Windows/win.ini"></script>')
        self.assertIn("blocked script file:", logs(page))

    def test_resource_limits(self):
        page = self.run_js("""<script>
            try { for (var i = 0; i < 5000; i++) setInterval(function () {}, 0) } catch (e) { console.log('timers', e.message) }
            try { document.body.innerHTML = 'x'.repeat(5000000) } catch (e) { console.log('string', e.message) }
            </script>""")
        self.assertIn("console.log: timers too many timers", logs(page))
        self.assertIn("console.log: string string too long", logs(page))

    def test_pages_are_isolated(self):
        p1, h1 = run_page("<p id=a>one</p><script>window.secret = 42; localStorage.setItem('k', 'v')</script>")
        p2, h2 = run_page("<script>console.log(typeof secret, localStorage.getItem('k'))</script>",
                          url="https://another.example/")
        try:
            self.assertIn("console.log: undefined null", logs(p2))
        finally:
            h1.close()
            h2.close()

    def test_closed_page_stops_its_process(self):
        page = self.run_js("<script>setInterval(function () {}, 10)</script>")
        proc = self.host.worker.proc
        self.host.close()
        proc.wait(timeout=5)
        self.assertIsNotNone(proc.poll())


@unittest.skipUnless(HAVE_TK and js.enabled(), "needs a display and quickjs")
class JSWindowTests(unittest.TestCase):
    def setUp(self):
        from browser.gui import Browser
        self.b = Browser("about:js", 900, 700)
        self.wait()

    def tearDown(self):
        self.b.close()

    def wait(self, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            self.b.root.update()
            if not self.b.current.loading and self.b.current.display_list is not None:
                self.b.root.update()
                return
            time.sleep(0.02)
        self.fail("page did not load")

    def click(self, ident):
        tab = self.b.current
        for n, (x1, y1, x2, y2) in js.box_rects(tab.doc_layout).items():
            if n.attributes.get("id") == ident:
                self.b.canvas.event_generate("<Button-1>", x=int((x1 + x2) / 2), y=int((y1 + y2) / 2 - tab.scroll))
                self.b.root.update()
                return
        self.fail("no box for %s" % ident)

    def test_click_handler_and_form(self):
        doc = self.b.current.page.document
        self.click("counter")
        self.click("counter")
        self.assertEqual(by_id(doc, "counter").text_content(), "Clicked 2 times")
        self.click("toggle")
        self.assertIn("hidden", by_id(doc, "box").attributes["class"])
        self.click("name")
        for ch in "Ed":
            self.b.canvas.event_generate("<Key>", keysym=ch)
        self.b.root.update()
        self.assertEqual(by_id(doc, "echo").text_content(), "Ed")
        self.click("go")
        self.b.root.update()
        self.assertEqual(self.b.current.page.document, doc)        # submit was cancelled by the page
        self.assertEqual(by_id(doc, "greeting").text_content(), "Hello, Ed!")
        checks = by_id(doc, "checks").text_content()
        self.assertNotIn("NOT BLOCKED", checks)
        self.assertEqual(checks.count("blocked"), 7)


if __name__ == "__main__":
    unittest.main()
