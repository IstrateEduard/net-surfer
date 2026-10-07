"""Page scripts: a sandboxed JavaScript runtime and the DOM bindings for it.

The JavaScript language itself (parsing, objects, closures, promises) comes
from QuickJS, a small standalone interpreter written in C, used through the
`quickjs` Python package. It is a library, like Tk or Pillow: it has no
built-in DOM, network, file or OS access of its own. Everything a page can
do beyond pure computation goes through the bindings in this file and in
js_prelude.js, which we wrote:

  * js_prelude.js builds `document`, `window`, elements, events, timers,
    fetch/XMLHttpRequest, storage, etc. in JavaScript on top of one native
    function. That function is deleted from the global scope before any page
    code runs, so pages only see our API.
  * ScriptHost (below) answers those native calls by reading and changing
    our own dom.Element tree, our own selector matcher and our own network
    code.

Security model -- every page script is treated as hostile:
  * Only strings, numbers, booleans and null cross the boundary. Page code
    never gets a Python object; nodes are referred to by small integer
    handles that index this page's own node table, so a page cannot reach
    another tab's document or anything else in the browser.
  * One QuickJS context (and runtime) per page, so tabs and pages share no
    JavaScript objects.
  * Memory, stack and run-time limits on every call into JavaScript; a page
    that keeps hitting the time limit has its scripts stopped.
  * Limits on the things native calls can allocate on the Python side
    (nodes, string sizes, timers, requests, storage).
  * Network: scripts load and fetch only through browser/network.py, only
    over http(s) (plus data: for <script src>), and fetch/XHR may only read
    responses from the page's own origin. Cookies are not readable from
    script, and requests cannot set headers such as Cookie or Host.
  * No file:// access from http(s) pages, no popups, no modal dialogs.
"""
import heapq
import itertools
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import css_parser, js_rewrite, network
from .dom import Comment, Element, PseudoElement, Text
from .html_parser import RAW_TEXT_ELEMENTS, VOID_ELEMENTS, parse

import importlib.util
import struct
import subprocess
import sys

AVAILABLE = importlib.util.find_spec("quickjs") is not None
ENABLED = True           # turned off by --no-js / tests

# --- limits ---------------------------------------------------------------
MEMORY_LIMIT = 64 * 1024 * 1024       # bytes of JS heap per page
STACK_LIMIT = 4 * 1024 * 1024         # bytes of stack QuickJS may use
SCRIPT_TIME_LIMIT = 5.0               # seconds for one <script> while loading
CALL_TIME_LIMIT = 2.0                 # seconds for one event handler / timer / callback
LOAD_TIME_BUDGET = 10.0               # seconds of script time while a page loads
WORKER_START_LIMIT = 15.0
MAX_SCRIPT_BYTES = 4_000_000
MAX_SCRIPTS = 200
MAX_STRING = 4_000_000                # longest string a native call accepts
MAX_NODES = 300_000                   # nodes a page's scripts may create
MAX_TIMERS = 1000
MIN_TIMER_DELAY = 0.004
MAX_FETCHES = 16                      # requests in flight per page
MAX_FETCH_BYTES = 8_000_000
STORAGE_QUOTA = 2_000_000             # characters of localStorage per origin
MAX_LOG = 500
TICK_BUDGET = 0.05                    # seconds of timers fired per UI tick

DOC = 0                               # handle of the document node

_net_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="js-net")
_storage = {}                         # origin -> {key: value} (localStorage, this session)


class _Worker:
    """One script process (js_worker.py) and the pipe to it."""

    def __init__(self):
        cmd = [sys.executable, "-B", os.path.join(os.path.dirname(os.path.abspath(__file__)), "js_worker.py"),
               str(MEMORY_LIMIT), str(STACK_LIMIT)]
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=None if os.environ.get("CS208_JS_DEBUG") else subprocess.DEVNULL,
                                     creationflags=flags)
        self.deadline = None            # watched by _watchdog while a call runs
        self.timed_out = False

    def send(self, obj):
        data = json.dumps(obj).encode("utf-8")
        self.proc.stdin.write(struct.pack("<I", len(data)) + data)
        self.proc.stdin.flush()

    def recv(self):
        """Next message, or None if the process ended (or was killed by the
        watchdog for running past its deadline)."""
        out = self.proc.stdout
        try:
            head = out.read(4)
            if len(head) < 4:
                return None
            n = struct.unpack("<I", head)[0]
            data = out.read(n)
            if len(data) < n:
                return None
            return json.loads(data.decode("utf-8"))
        except (OSError, ValueError):
            return None

    def kill(self):
        try:
            self.proc.kill()
            self.proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
        for pipe in (self.proc.stdin, self.proc.stdout):
            try:
                pipe.close()
            except OSError:
                pass


_watched = set()
_watch_lock = threading.Lock()
_watch_thread = None


def _watch(worker, limit):
    """Kill `worker` if it is still busy `limit` seconds from now."""
    global _watch_thread
    worker.deadline = time.monotonic() + limit
    with _watch_lock:
        _watched.add(worker)
        if _watch_thread is None:
            _watch_thread = threading.Thread(target=_watchdog, daemon=True)
            _watch_thread.start()


def _unwatch(worker):
    worker.deadline = None
    with _watch_lock:
        _watched.discard(worker)


def _watchdog():
    while True:
        time.sleep(0.02)
        now = time.monotonic()
        with _watch_lock:
            late = [w for w in _watched if w.deadline is not None and now > w.deadline]
        for w in late:
            w.timed_out = True
            w.kill()
            _unwatch(w)


_spare = []
_spare_lock = threading.Lock()


def _take_worker():
    """A started worker; a spare one is kept warm to hide process start-up."""
    with _spare_lock:
        w = _spare.pop() if _spare else None
    if w is None or w.proc.poll() is not None:
        w = _Worker()
    threading.Thread(target=_make_spare, daemon=True).start()
    return w


def _make_spare():
    with _spare_lock:
        if _spare:
            return
    w = _Worker()
    with _spare_lock:
        _spare.append(w)


def enabled():
    return AVAILABLE and ENABLED


class ScriptError(Exception):
    """Raised by native calls; becomes a JavaScript exception in the page."""


class _Fragment(Element):
    """A DocumentFragment: inserting it moves its children instead."""

    def __init__(self):
        super().__init__("#fragment", {}, None)


_TAG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9\-_.:]*$")
_ATTR_RE = re.compile(r"^[^\s\"'>/=\x00]+$")
_ALLOWED_REQ_HEADERS = {"content-type", "accept", "accept-language", "x-requested-with"}
_ALLOWED_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"}


def _real_children(node):
    return [c for c in node.children if not isinstance(c, PseudoElement)]


def _text_of(node):
    """textContent, ignoring ::before/::after boxes."""
    if isinstance(node, (Text, Comment)):
        return node.text
    out = []
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, PseudoElement):
            continue
        if isinstance(n, Text):
            out.append(n.text)
        elif not isinstance(n, Comment):
            stack.extend(reversed(n.children))
    return "".join(out)


def _esc_text(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\xa0", "&nbsp;")


def _esc_attr(s):
    return s.replace("&", "&amp;").replace('"', "&quot;").replace("\xa0", "&nbsp;")


def serialize(node, outer=True):
    """HTML for innerHTML/outerHTML (iterative, so deep trees are fine)."""
    out = []
    stack = [(node, outer)]
    while stack:
        n, open_tag = stack.pop()
        if isinstance(n, str):           # a pending end tag
            out.append(n)
            continue
        if isinstance(n, PseudoElement):
            continue
        if isinstance(n, Text):
            parent = n.parent
            raw = parent is not None and isinstance(parent, Element) and parent.tag in RAW_TEXT_ELEMENTS \
                and parent.tag not in ("textarea", "title")
            out.append(n.text if raw else _esc_text(n.text))
            continue
        if isinstance(n, Comment):
            out.append("<!--%s-->" % n.text)
            continue
        if open_tag and not isinstance(n, _Fragment):
            attrs = "".join(' %s="%s"' % (k, _esc_attr(str(v))) for k, v in n.attributes.items())
            out.append("<%s%s>" % (n.tag, attrs))
            if n.tag in VOID_ELEMENTS:
                continue
            stack.append(("</%s>" % n.tag, False))
        for child in reversed(n.children):
            stack.append((child, True))
    return "".join(out)


def parse_fragment(markup):
    """Parse an HTML fragment into a list of nodes (for innerHTML)."""
    root = parse("<body>" + markup)
    nodes = []
    for part in root.children:
        if isinstance(part, Element) and part.tag in ("head", "body"):
            nodes.extend(part.children)
        else:
            nodes.append(part)
    for n in nodes:
        n.parent = None
    return nodes


def _all_nodes(node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def box_rects(doc_layout):
    """element -> (x1, y1, x2, y2) border box, from a finished layout
    (read-only walk; used for getBoundingClientRect / offsetWidth)."""
    rects = {}
    if doc_layout is None or getattr(doc_layout, "root", None) is None:
        return rects
    stack = [doc_layout.root] + list(getattr(doc_layout, "abs_boxes", ()))
    seen = set()
    while stack:
        b = stack.pop()
        if id(b) in seen:
            continue
        seen.add(id(b))
        node = getattr(b, "node", None)
        try:
            x1, y1, x2, y2 = b.border_box()
        except Exception:
            x1 = y1 = x2 = y2 = None
        if isinstance(node, Element) and x1 is not None and node not in rects:
            rects[node] = (x1, y1, x2, y2)
        for line in getattr(b, "lines", ()):
            for f in getattr(line, "frags", ()):
                if hasattr(f, "box"):
                    stack.append(f.box)
                else:
                    parent = getattr(getattr(f, "node", None), "parent", None)
                    if isinstance(parent, Element) and not isinstance(parent, PseudoElement):
                        r = (f.x, line.y, f.x + f.width, line.y + line.height)
                        old = rects.get(parent)
                        if old is None:
                            rects[parent] = r
                        else:
                            rects[parent] = (min(old[0], r[0]), min(old[1], r[1]),
                                             max(old[2], r[2]), max(old[3], r[3]))
        stack.extend(getattr(b, "children", ()))
        stack.extend(getattr(b, "abs_boxes", ()))
    return rects


class _Timer:
    __slots__ = ("seq", "interval", "repeat")


class ScriptHost:
    """The JavaScript side of one page: its QuickJS context, the handle table
    for its nodes, its timers and requests, and the DOM changes waiting to be
    restyled and laid out."""

    def __init__(self, page, viewport=(1024, 768)):
        self.page = page
        self.viewport = viewport
        self.handles = {}          # node -> handle
        self.nodes = {DOC: None}   # handle -> node (None stands for the document)
        self._next = itertools.count(1)
        self.created = 0
        self.closed = False
        self.disabled_reason = None
        self.timeouts = 0
        self.lock = threading.Lock()
        self.timers = {}           # id -> _Timer
        self.timer_heap = []       # (due, seq, id)
        self._seq = itertools.count()
        self.fetches = set()
        self.done_fetches = []     # [(id, result dict)] finished on a network thread
        self.started_scripts = set()
        self.pending_scripts = []  # script elements inserted by script, waiting to run
        self.ready_scripts = []    # (element, source, name) fetched and ready to run
        self.current_script = None
        self.loading = True
        self.write_queue = []      # document.write() output during load
        # Changes for the browser to pick up after each call:
        self.dirty = set()         # nodes whose subtree needs restyling
        self.full_restyle = False  # a <style> changed
        self.images_dirty = False
        self.repaint = False
        self.pending_nav = None    # (url, replace) or ("history", n)
        self.pending_submit = None
        self.pending_focus = None
        self.active_element = None
        self.alerts = []
        self.layout = None         # the page's current DocumentLayout (for geometry)
        self.rects = None          # element -> border box, computed from it on demand
        self.scroll = 0
        self.log_count = 0
        self.worker = None
        self.call_lock = threading.RLock()   # one call into the page's JS at a time
        self._sel_cache = {}
        self.ops = {name[3:]: getattr(self, name) for name in dir(self) if name.startswith("op_")}
        self.worker = _take_worker()
        _watch(self.worker, WORKER_START_LIMIT)
        msg = self.worker.recv()
        _unwatch(self.worker)
        if msg != ["ready"]:
            self.worker.kill()
            self.worker = None
            self.disable("the script engine did not start")

    # ------------------------------------------------------------ lifecycle
    def close(self):
        """Stop the page's script process and its timers."""
        if self.closed:
            return
        self.closed = True
        with self.lock:
            self.timers.clear()
            self.timer_heap.clear()
        if self.worker is not None:
            try:
                self.worker.send(["exit"])
            except (OSError, ValueError):
                pass
            self.worker.kill()

    def disable(self, reason):
        if self.disabled_reason is None:
            self.disabled_reason = reason
            self.note("scripts stopped: %s" % reason)
        with self.lock:
            self.timers.clear()
            self.timer_heap.clear()
        if self.worker is not None:
            self.worker.kill()

    @property
    def active(self):
        return not self.closed and self.disabled_reason is None and self.worker is not None

    def note(self, msg):
        if self.log_count < MAX_LOG:
            self.log_count += 1
            self.page.note("[js] " + msg)

    # --------------------------------------------------------- calling JS
    def _invoke(self, message, limit=CALL_TIME_LIMIT):
        """Send one eval/call to the script process and answer its native
        requests until it is done. The process is killed if it runs past
        `limit` seconds. Returns the call's (primitive) result or None."""
        with self.call_lock:
            if not self.active:
                return None
            w = self.worker
            _watch(w, limit)
            try:
                w.send(message)
                while True:
                    msg = w.recv()
                    if msg is None:
                        if w.timed_out:
                            self.disable("a script ran longer than %.0f seconds" % limit)
                        else:
                            self.disable("the script process crashed")
                        return None
                    if msg[0] == "native":
                        w.send(["ret", self._native_reply(msg[1], msg[2])])
                        continue
                    if msg[0] == "done":
                        _, result, error, overflow = msg
                        if error:
                            self._report(error)
                        if overflow:
                            self.disable("promise callbacks kept scheduling more callbacks forever")
                        return result
            except (OSError, ValueError) as e:
                if w.timed_out:
                    self.disable("a script ran longer than %.0f seconds" % limit)
                else:
                    self.disable("lost the script process (%s)" % e)
                return None
            finally:
                _unwatch(w)

    def _report(self, msg):
        if "out of memory" in msg.lower():
            self.note("error: script ran out of memory (limit %d MB)" % (MEMORY_LIMIT // 1048576))
        else:
            self.note("error: %s" % msg[:1000])

    def _call(self, name, *args, limit=CALL_TIME_LIMIT):
        return self._invoke(["call", name, list(args)], limit)

    def eval(self, code, limit=CALL_TIME_LIMIT):
        """Run a classic script in the page's global scope."""
        code = js_rewrite.fix_generator_finally(code)     # dodge a QuickJS compiler bug
        return self._invoke(["eval", code], limit)

    def run_inline(self, code):
        """A javascript: URL from a clicked link."""
        self._call("runInline", code)

    def dispatch(self, node, event_type, init=None):
        """Dispatch a DOM event at `node` (an Element, or None for the
        document / "window" for the window). Returns True if a listener
        called preventDefault()."""
        if not self.active:
            return False
        if node == "window":
            h = -1
        elif node is None:
            h = DOC
        else:
            h = self._h(node)
        return self._call("dispatch", h, event_type, json.dumps(init or {})) is True

    # --------------------------------------------------------- page load
    def run_load_scripts(self, progress=None):
        """Run the page's scripts in document order, then DOMContentLoaded,
        zero-delay timers and the load event."""
        t0 = time.monotonic()
        scripts = [el for el in _all_nodes(self.page.document)
                   if isinstance(el, Element) and el.tag == "script"][:MAX_SCRIPTS]
        for el in [el for el in _all_nodes(self.page.document)
                   if isinstance(el, Element) and el.tag == "noscript"]:
            el.attributes.setdefault("hidden", "")          # scripting is on
        self.started_scripts.update(scripts)
        runnable = [el for el in scripts if self._is_classic(el)]
        # Fetch external scripts in parallel, run them in document order
        # (defer/async scripts after the ordinary ones).
        futures = {}
        for el in runnable:
            src = el.attributes.get("src")
            if src is not None:
                futures[el] = _net_pool.submit(self._fetch_script, src)
        ordered = [el for el in runnable if not self._deferred(el)] + [el for el in runnable if self._deferred(el)]
        for i, el in enumerate(ordered):
            if not self.active:
                break
            if time.monotonic() - t0 > LOAD_TIME_BUDGET:
                self.note("load-time script budget (%.0fs) used up; skipping %d scripts"
                          % (LOAD_TIME_BUDGET, len(ordered) - i))
                break
            if progress:
                progress("Running script %d of %d" % (i + 1, len(ordered)))
            if el in futures:
                code, name = futures[el].result()
            else:
                code, name = _text_of(el), "inline script"
            if code is not None:
                self._run_script_element(el, code, name)
        self.loading = False
        self._call("setReady", "interactive")
        self.dispatch(None, "DOMContentLoaded", {"bubbles": True})
        self.run_zero_timers()
        self._call("setReady", "complete")
        self.dispatch("window", "load")
        self.run_zero_timers()
        self.note("ran %d scripts in %.2fs" % (len(ordered), time.monotonic() - t0))

    def _is_classic(self, el):
        t = el.attributes.get("type", "").strip().lower()
        if t in ("module",):
            self.note("skipped module script (modules are not supported)")
            return False
        return t in ("", "text/javascript", "application/javascript", "application/ecmascript",
                     "text/ecmascript", "text/jscript", "javascript")

    @staticmethod
    def _deferred(el):
        return "src" in el.attributes and ("defer" in el.attributes or "async" in el.attributes)

    def _fetch_script(self, src):
        """Fetch an external script through our network code. Returns (code, name)."""
        try:
            url = self.page.base_url.resolve(src)
        except ValueError as e:
            self.note("bad script URL %r: %s" % (src[:200], e))
            return None, src
        allowed = ("http", "https", "data")
        if self.page.url.scheme == "file":
            allowed += ("file",)
        if url.scheme not in allowed:
            self.note("blocked script %s (scheme %s)" % (url, url.scheme))
            return None, str(url)
        try:
            r = network.fetch(url, use_cache=True, referrer=str(self.page.url))
        except network.NetworkError as e:
            self.note("script failed: %s (%s)" % (url, e))
            return None, str(url)
        if not r.ok():
            self.note("script failed: %s (HTTP %d)" % (url, r.status))
            return None, str(url)
        if len(r.body) > MAX_SCRIPT_BYTES:
            self.note("script too large: %s" % url)
            return None, str(url)
        self.note("script: %s (%d bytes)" % (url, len(r.body)))
        return r.text(), str(url)

    def _run_script_element(self, el, code, name):
        h = self._h(el)
        self.current_script = el
        self._call("setCurrent", h)
        self.eval(code, SCRIPT_TIME_LIMIT if self.loading else CALL_TIME_LIMIT)
        self._call("setCurrent", -1)
        self.current_script = None
        if self.write_queue:
            markup, self.write_queue = "".join(self.write_queue), []
            if el.parent is not None:
                idx = el.parent.children.index(el) + 1 if el in el.parent.children else len(el.parent.children)
                for n in parse_fragment(markup):
                    n.parent = el.parent
                    el.parent.children.insert(idx, n)
                    idx += 1
                    for d in _all_nodes(n):
                        if isinstance(d, Element) and d.tag == "script":
                            self.started_scripts.add(d)
                self._touch(el.parent)

    def run_zero_timers(self):
        """Fire timers that are already due (setTimeout(f, 0) during load)."""
        for _ in range(3):
            if not self.tick(budget=0.5, only_due=True):
                break

    # --------------------------------------------------------------- ticks
    def tick(self, budget=TICK_BUDGET, only_due=False):
        """Run finished requests, inserted scripts and due timers. Called
        regularly by the browser's UI loop. Returns True if any JS ran."""
        if not self.active:
            return False
        ran = False
        with self.lock:
            done, self.done_fetches = self.done_fetches, []
            ready, self.ready_scripts = self.ready_scripts, []
        for rid, result in done:
            self.fetches.discard(rid)
            data = json.dumps(result)
            self._call("fetchDone", rid, data)
            ran = True
        for el, code, name in ready:
            if code is not None:
                self._run_script_element(el, code, name)
                self.dispatch(el, "load")
            else:
                self.dispatch(el, "error")
            ran = True
        if self.pending_scripts:
            pending, self.pending_scripts = self.pending_scripts, []
            for el in pending:
                self._start_inserted_script(el)
                ran = True
        start = time.monotonic()
        now = time.monotonic()
        while self.active and time.monotonic() - start < budget:
            with self.lock:
                if not self.timer_heap or self.timer_heap[0][0] > now:
                    break
                due, seq, tid = heapq.heappop(self.timer_heap)
                t = self.timers.get(tid)
                if t is None or t.seq != seq:
                    continue
                if t.repeat:
                    t.seq = next(self._seq)
                    heapq.heappush(self.timer_heap, (max(now, due) + t.interval, t.seq, tid))
                else:
                    del self.timers[tid]
            self._call("fireTimer", tid)
            ran = True
        return ran

    def next_timer_due(self):
        with self.lock:
            return self.timer_heap[0][0] if self.timer_heap else None

    def _start_inserted_script(self, el):
        if not self._is_classic(el):
            return
        src = el.attributes.get("src")
        if src is None:
            self._run_script_element(el, _text_of(el), "inserted script")
            return

        def work():
            code, name = self._fetch_script(src)
            with self.lock:
                self.ready_scripts.append((el, code, name))
        _net_pool.submit(work)

    # ---------------------------------------------------- browser side API
    def take_changes(self):
        """The DOM changes since the last call: (subtree roots to restyle,
        whether stylesheets changed). Only nodes in the document count."""
        dirty, self.dirty = self.dirty, set()
        full, self.full_restyle = self.full_restyle, False
        roots = []
        root_el = self.page.document
        for n in dirty:
            el = n if isinstance(n, Element) else n.parent
            if el is None or not self._connected(el):
                continue
            # restyle from the parent so sibling selectors (+, ~) see the change
            if el.parent is not None and isinstance(el.parent, Element):
                el = el.parent
            roots.append(el)
        rootset = set(roots)
        out = []
        for r in roots:
            if not any(a in rootset for a in r.ancestors()) and r not in out:
                out.append(r)
        if root_el in rootset:
            out = [root_el]
        return out, full

    def set_layout(self, doc_layout, viewport, scroll):
        """Called by the browser after each layout and scroll."""
        if doc_layout is not self.layout:
            self.layout = doc_layout
            self.rects = None
        self.viewport = viewport
        self.scroll = scroll

    def title(self):
        for el in _all_nodes(self.page.document):
            if isinstance(el, Element) and el.tag == "title":
                return " ".join(_text_of(el).split())
            if isinstance(el, Element) and el.tag == "body":
                break
        return None

    # ------------------------------------------------------------- natives
    def _native_reply(self, op, args):
        """Answer one request from page code (via the prelude's `native`).
        Arguments and results are JSON primitives only; the answer is a JSON
        string {"v": value} or {"e": "ErrorName: message"}."""
        try:
            value = self._native(op, *args)
            return json.dumps({"v": value})
        except ScriptError as e:
            return json.dumps({"e": str(e)})
        except Exception as e:           # a bug in our bindings: report it, don't crash
            self.note("internal error in %s: %s: %s" % (op, type(e).__name__, e))
            return json.dumps({"e": "InternalError: %s" % type(e).__name__})

    def _native(self, op, *args):
        if self.closed:
            raise ScriptError("page is closed")
        fn = self.ops.get(op) if isinstance(op, str) else None
        if fn is None:
            raise ScriptError("unknown operation")
        for a in args:
            if a is not None and not isinstance(a, (str, int, float, bool)):
                raise ScriptError("bad argument")
            if isinstance(a, str) and len(a) > MAX_STRING:
                raise ScriptError("string too long")
        try:
            return fn(*args)
        except ScriptError:
            raise
        except (TypeError, ValueError, KeyError, IndexError) as e:
            raise ScriptError(str(e)[:300])
        except RecursionError:
            raise ScriptError("document too deep")

    def _h(self, node):
        if node is None:
            return None
        h = self.handles.get(node)
        if h is None:
            h = next(self._next)
            self.handles[node] = h
            self.nodes[h] = node
        return h

    def _ref(self, node):
        """How a node is passed to page code: [handle, nodeType, tag]."""
        if node is None:
            return None
        h = self._h(node)
        t = self.op_node_type(h)
        return [h, t, node.tag if t == 1 else None]

    def _node(self, h, allow_doc=False):
        if h is None or isinstance(h, bool) or not isinstance(h, (int, float)):
            raise ScriptError("not a node")
        h = int(h)
        if h == DOC:
            if allow_doc:
                return None
            raise ScriptError("not valid on the document")
        node = self.nodes.get(h)
        if node is None:
            raise ScriptError("unknown node")
        return node

    def _el(self, h):
        n = self._node(h)
        if not isinstance(n, Element):
            raise ScriptError("not an element")
        return n

    def _count(self, n=1):
        self.created += n
        if self.created > MAX_NODES:
            raise ScriptError("too many nodes created by script")

    def _connected(self, node):
        root = self.page.document
        while node is not None:
            if node is root:
                return True
            node = node.parent
        return False

    def _touch(self, node):
        self.dirty.add(node)
        if isinstance(node, Element) and node.tag == "style" or \
                isinstance(node, Text) and isinstance(node.parent, Element) and node.parent.tag == "style":
            self.full_restyle = True

    def _selectors(self, text):
        sels = self._sel_cache.get(text)
        if sels is None:
            sels = css_parser.parse_selector_list(text)
            if not sels or any(s.pseudo_element for s in sels):
                raise ScriptError("SyntaxError: '%s' is not a valid selector" % text[:100])
            if len(self._sel_cache) > 500:
                self._sel_cache.clear()
            self._sel_cache[text] = sels
        return sels

    def _scope(self, h):
        return self.page.document if int(h) == DOC else self._node(h)

    # --- tree reading
    def op_node_type(self, h):
        node = self._node(h, allow_doc=True)
        if node is None:
            return 9
        if isinstance(node, _Fragment):
            return 11
        if isinstance(node, Text):
            return 3
        if isinstance(node, Comment):
            return 8
        return 1

    def op_local_name(self, h):
        return self._el(h).tag

    def op_node_info(self, h):
        t = self.op_node_type(h)
        return [t, self._node(h).tag if t == 1 else None]

    def op_root(self):
        return self._ref(self.page.document)

    def op_find_tag(self, tag):
        for el in _all_nodes(self.page.document):
            if isinstance(el, Element) and el.tag == tag:
                return self._ref(el)
        return None

    def op_parent(self, h):
        node = self._node(h, allow_doc=True)
        if node is None:
            return None
        if node is self.page.document:
            return [DOC, 9, None]
        return self._ref(node.parent) if node.parent is not None else None

    def op_children(self, h, elements_only):
        node = self._node(h, allow_doc=True)
        if node is None:
            return [self._ref(self.page.document)]
        kids = _real_children(node)
        if elements_only:
            kids = [k for k in kids if isinstance(k, Element)]
        return [self._ref(k) for k in kids]

    def op_sibling(self, h, direction, elements_only):
        node = self._node(h)
        parent = node.parent
        if parent is None:
            return None
        kids = _real_children(parent)
        if node not in kids:
            return None
        i = kids.index(node)
        step = 1 if direction > 0 else -1
        i += step
        while 0 <= i < len(kids):
            if not elements_only or isinstance(kids[i], Element):
                return self._ref(kids[i])
            i += step
        return None

    def op_contains(self, a, b):
        outer = self._scope(a)
        node = self._node(b, allow_doc=True)
        if node is None:
            return int(a) == DOC
        while node is not None:
            if node is outer:
                return True
            node = node.parent
        return False

    def op_connected(self, h):
        node = self._node(h, allow_doc=True)
        return node is None or self._connected(node)

    def op_text_get(self, h):
        node = self._node(h, allow_doc=True)
        return None if node is None else _text_of(node)

    def op_text_set(self, h, value):
        node = self._node(h)
        value = "" if value is None else str(value)
        if isinstance(node, (Text, Comment)):
            node.text = value
            self._touch(node)
            return
        self._count()
        for c in node.children:
            c.parent = None
        node.children = [Text(value, node)] if value else []
        node.form_value = None if node.tag == "textarea" else node.form_value
        self._touch(node)

    # --- attributes
    def op_get_attr(self, h, name):
        v = self._el(h).attributes.get(str(name).lower())
        return None if v is None else str(v)

    def op_has_attr(self, h, name):
        return str(name).lower() in self._el(h).attributes

    def op_attr_names(self, h):
        return list(self._el(h).attributes)

    def op_set_attr(self, h, name, value):
        el = self._el(h)
        name = str(name).lower()
        if not _ATTR_RE.match(name) or len(name) > 200:
            raise ScriptError("InvalidCharacterError: bad attribute name")
        if len(el.attributes) > 500 and name not in el.attributes:
            raise ScriptError("too many attributes")
        value = "" if value is None else str(value)
        el.attributes[name] = value
        if name == "checked":
            el.checked = True
        if name in ("src", "srcset", "style", "class"):
            self.images_dirty = True
        self._touch(el)

    def op_remove_attr(self, h, name):
        el = self._el(h)
        name = str(name).lower()
        if name in el.attributes:
            del el.attributes[name]
            if name == "checked":
                el.checked = False
            self._touch(el)

    # --- creating and moving nodes
    def op_create_element(self, tag):
        tag = str(tag)
        if not _TAG_RE.match(tag) or len(tag) > 100:
            raise ScriptError("InvalidCharacterError: bad tag name")
        self._count()
        el = Element(tag.lower(), {})
        return self._ref(el)

    def op_create_text(self, text):
        self._count()
        return self._ref(Text("" if text is None else str(text)))

    def op_create_comment(self, text):
        self._count()
        return self._ref(Comment("" if text is None else str(text)))

    def op_create_fragment(self):
        self._count()
        return self._ref(_Fragment())

    def op_insert(self, ph, ch, refh):
        parent = self._node(ph, allow_doc=True)
        if parent is None:
            raise ScriptError("HierarchyRequestError: the document already has a root element")
        child = self._node(ch)
        if not isinstance(parent, Element) or isinstance(parent, PseudoElement):
            raise ScriptError("HierarchyRequestError: cannot insert into this node")
        if child is self.page.document:
            raise ScriptError("HierarchyRequestError: cannot move the root element")
        a = parent
        while a is not None:
            if a is child:
                raise ScriptError("HierarchyRequestError: would create a cycle")
            a = a.parent
        ref = None
        if refh is not None:
            ref = self._node(refh)
            if ref.parent is not parent:
                raise ScriptError("NotFoundError: reference node is not a child")
            if ref is child:
                return
        if isinstance(child, _Fragment):
            moving = _real_children(child)
            child.children = []
        else:
            moving = [child]
            if child.parent is not None:
                old = child.parent
                old.children = [c for c in old.children if c is not child]
                self._touch(old)
        for m in moving:
            m.parent = parent
        if ref is None:
            # keep a generated ::after box last
            idx = len(parent.children)
            while idx > 0 and isinstance(parent.children[idx - 1], PseudoElement) and \
                    parent.children[idx - 1].tag == "::after":
                idx -= 1
        else:
            idx = parent.children.index(ref)
        parent.children[idx:idx] = moving
        self._touch(parent)
        if self._connected(parent):
            for m in moving:
                for d in _all_nodes(m):
                    if isinstance(d, Element):
                        if d.tag == "img" or "style" in d.attributes:
                            self.images_dirty = True
                        if d.tag == "style":
                            self.full_restyle = True
                        if d.tag == "link":
                            self.full_restyle = True
                        if d.tag == "script" and d not in self.started_scripts:
                            self.started_scripts.add(d)
                            self.pending_scripts.append(d)

    def op_remove(self, ph, ch):
        parent = self._node(ph, allow_doc=True)
        child = self._node(ch)
        if parent is None or child.parent is not parent:
            raise ScriptError("NotFoundError: not a child of this node")
        if child is self.page.document:
            raise ScriptError("cannot remove the root element")
        parent.children = [c for c in parent.children if c is not child]
        child.parent = None
        self._touch(parent)
        if isinstance(child, Element) and child.tag in ("style", "link"):
            self.full_restyle = True

    def op_clone(self, h, deep):
        src = self._node(h)
        stack = [(src, None)]
        top = None
        n = 0
        while stack:
            node, new_parent = stack.pop()
            if isinstance(node, PseudoElement):
                continue
            n += 1
            if isinstance(node, Text):
                copy = Text(node.text)
            elif isinstance(node, Comment):
                copy = Comment(node.text)
            elif isinstance(node, _Fragment):
                copy = _Fragment()
            else:
                copy = Element(node.tag, dict(node.attributes))
                copy.checked = node.checked
                copy.form_value = node.form_value if isinstance(node.form_value, str) else None
            if new_parent is not None:
                copy.parent = new_parent
                new_parent.children.append(copy)
            else:
                top = copy
            if deep:
                for c in reversed(node.children):
                    stack.append((c, copy))
        self._count(n)
        return self._ref(top)

    # --- HTML
    def op_html_get(self, h, outer):
        node = self._node(h, allow_doc=True)
        if node is None:
            node = self.page.document
        return serialize(node, outer=bool(outer))

    def _parsed(self, markup):
        nodes = parse_fragment(str(markup))
        count = 0
        for n in nodes:
            for d in _all_nodes(n):
                count += 1
                if isinstance(d, Element) and d.tag == "script":
                    self.started_scripts.add(d)      # innerHTML scripts never run
                if isinstance(d, Element) and (d.tag == "img" or "style" in d.attributes):
                    self.images_dirty = True
                if isinstance(d, Element) and d.tag in ("style", "link"):
                    self.full_restyle = True
        self._count(count)
        return nodes

    def op_html_set(self, h, markup):
        el = self._el(h)
        if el.tag in RAW_TEXT_ELEMENTS:
            return self.op_text_set(h, markup)
        nodes = self._parsed(markup)
        for c in el.children:
            c.parent = None
        el.children = nodes
        for n in nodes:
            n.parent = el
        self._touch(el)

    def op_outer_set(self, h, markup):
        el = self._el(h)
        parent = el.parent
        if parent is None:
            raise ScriptError("NoModificationAllowedError: element has no parent")
        nodes = self._parsed(markup)
        i = parent.children.index(el)
        for n in nodes:
            n.parent = parent
        parent.children[i:i + 1] = nodes
        el.parent = None
        self._touch(parent)

    def op_adjacent_html(self, h, where, markup):
        el = self._el(h)
        nodes = self._parsed(markup)
        if where in ("beforebegin", "afterend"):
            parent = el.parent
            if parent is None:
                raise ScriptError("NoModificationAllowedError: element has no parent")
            i = parent.children.index(el) + (1 if where == "afterend" else 0)
        elif where in ("afterbegin", "beforeend"):
            parent = el
            i = 0 if where == "afterbegin" else len(el.children)
        else:
            raise ScriptError("SyntaxError: bad position")
        for n in nodes:
            n.parent = parent
        parent.children[i:i] = nodes
        self._touch(parent)

    def op_doc_write(self, markup):
        if self.loading and self.current_script is not None:
            self.write_queue.append(str(markup))
        else:
            self.note("document.write() after load ignored")

    # --- selectors
    def op_query(self, h, selector, all_):
        scope = self._scope(h)
        sels = self._selectors(str(selector))
        out = []
        for el in _all_nodes(scope):
            if el is scope and int(h) != DOC or not isinstance(el, Element) or isinstance(el, PseudoElement):
                continue
            if any(s.matches(el) for s in sels):
                out.append(self._ref(el))
                if not all_:
                    break
        return out

    def op_matches(self, h, selector):
        el = self._el(h)
        return any(s.matches(el) for s in self._selectors(str(selector)))

    def op_closest(self, h, selector):
        sels = self._selectors(str(selector))
        el = self._el(h)
        while isinstance(el, Element):
            if any(s.matches(el) for s in sels):
                return self._ref(el)
            el = el.parent
        return None

    def op_by_id(self, ident):
        ident = str(ident)
        for el in _all_nodes(self.page.document):
            if isinstance(el, Element) and el.attributes.get("id") == ident:
                return self._ref(el)
        return None

    def op_by_tag(self, h, tag):
        scope = self._scope(h)
        tag = str(tag).lower()
        return [self._ref(el) for el in _all_nodes(scope)
                           if isinstance(el, Element) and el is not scope and not isinstance(el, PseudoElement)
                           and (tag == "*" or el.tag == tag)]

    def op_by_class(self, h, names):
        scope = self._scope(h)
        want = str(names).split()
        if not want:
            return []
        return [self._ref(el) for el in _all_nodes(scope)
                           if isinstance(el, Element) and el is not scope
                           and all(c in el.classes for c in want)]

    # --- forms
    def op_form_get(self, h, prop):
        el = self._el(h)
        if prop == "checked":
            return bool(el.checked)
        if prop == "value":
            if el.tag == "select":
                opts = [o for o in _all_nodes(el) if isinstance(o, Element) and o.tag == "option"]
                cur = el.form_value if el.form_value in opts else None
                if cur is None:
                    cur = next((o for o in opts if "selected" in o.attributes), opts[0] if opts else None)
                return "" if cur is None else cur.attributes.get("value", _text_of(cur).strip())
            if el.tag == "option":
                return el.attributes.get("value", _text_of(el).strip())
            if el.form_value is not None and isinstance(el.form_value, str):
                return el.form_value
            if el.tag == "textarea":
                return _text_of(el)
            return el.attributes.get("value", "on" if el.attributes.get("type") in ("checkbox", "radio") else "")
        if prop == "selectedIndex":
            opts = [o for o in _all_nodes(el) if isinstance(o, Element) and o.tag == "option"]
            cur = el.form_value if el.form_value in opts else \
                next((o for o in opts if "selected" in o.attributes), opts[0] if opts else None)
            return opts.index(cur) if cur in opts else -1
        raise ScriptError("unknown property")

    def op_form_set(self, h, prop, value):
        el = self._el(h)
        if prop == "checked":
            el.checked = bool(value)
            if el.checked and el.attributes.get("type", "").lower() == "radio":
                name = el.attributes.get("name")
                root = el
                while root.parent is not None and root.tag != "form":
                    root = root.parent
                for other in _all_nodes(root):
                    if other is not el and isinstance(other, Element) and other.tag == "input" and \
                            other.attributes.get("type", "").lower() == "radio" and \
                            other.attributes.get("name") == name:
                        other.checked = False
                        self._touch(other)
        elif prop == "value":
            value = "" if value is None else str(value)
            if el.tag == "select":
                for o in _all_nodes(el):
                    if isinstance(o, Element) and o.tag == "option" and \
                            o.attributes.get("value", _text_of(o).strip()) == value:
                        el.form_value = o
                        break
            else:
                el.form_value = value
        elif prop == "selectedIndex":
            opts = [o for o in _all_nodes(el) if isinstance(o, Element) and o.tag == "option"]
            i = int(value)
            if 0 <= i < len(opts):
                el.form_value = opts[i]
        else:
            raise ScriptError("unknown property")
        self._touch(el)

    def op_submit(self, h):
        self.pending_submit = self._el(h)

    def op_focus(self, h):
        el = self._el(h)
        self.pending_focus = el
        self.active_element = el

    def op_active(self):
        return self._ref(self.active_element) if self.active_element is not None else None

    # --- style and geometry
    def op_computed(self, h):
        el = self._el(h)
        return {k: v for k, v in el.style.items() if isinstance(v, str)}

    def op_rect(self, h):
        el = self._el(h)
        if self.rects is None and self.layout is not None:
            self.rects = box_rects(self.layout)
        r = (self.rects or {}).get(el)
        if r is None:
            return None
        x1, y1, x2, y2 = r
        scroll = self.scroll
        return [x1, y1 - scroll, x2 - x1, y2 - y1, y1]

    def op_viewport(self):
        return ({"width": self.viewport[0], "height": self.viewport[1],
                           "scrollY": self.scroll})

    def op_media(self, query):
        try:
            return bool(css_parser.media_matches(str(query), self.viewport[0]))
        except Exception:
            return False

    # --- location and navigation
    def op_location(self):
        return str(self.page.url)

    def op_resolve(self, rel, base):
        try:
            b = network.URL(str(base)) if base else self.page.base_url
            return str(b.resolve(str(rel)))
        except ValueError:
            return None

    def _nav_url(self, url):
        url = str(url)
        if url.strip().lower().startswith("javascript:"):
            return None
        u = self.page.base_url.resolve(url)
        if u.scheme not in ("http", "https", "about"):
            raise ScriptError("SecurityError: navigation to %s blocked" % u.scheme)
        return str(u)

    def op_navigate(self, url, replace):
        target = self._nav_url(url)
        if target is not None:
            self.pending_nav = (target, bool(replace))

    def op_history(self, delta):
        self.pending_nav = ("history", int(delta))

    def op_alert(self, kind, message):
        message = str(message)[:500]
        self.note("%s(): %s" % (kind, message))
        self.alerts.append((kind, message))

    def op_log(self, level, message):
        self.note("console.%s: %s" % (level, str(message)[:2000]))

    def op_random(self, n):
        n = max(0, min(int(n), 65536))
        return os.urandom(n).hex()

    # --- timers
    def op_timer_set(self, tid, ms, repeat):
        with self.lock:
            if len(self.timers) >= MAX_TIMERS and int(tid) not in self.timers:
                raise ScriptError("too many timers")
            t = _Timer()
            t.seq = next(self._seq)
            t.repeat = bool(repeat)
            delay = max(MIN_TIMER_DELAY, (float(ms) if ms == ms else 0) / 1000.0)
            t.interval = max(delay, 0.01) if repeat else delay
            self.timers[int(tid)] = t
            due = time.monotonic() + (delay if delay > MIN_TIMER_DELAY else 0)
            heapq.heappush(self.timer_heap, (due, t.seq, int(tid)))

    def op_timer_clear(self, tid):
        with self.lock:
            self.timers.pop(int(tid), None)

    # --- network
    def _same_origin(self, u):
        p = self.page.url
        return (u.scheme, u.host, u.port) == (p.scheme, p.host, p.port)

    def op_fetch_start(self, rid, url, method, body, headers_json):
        if len(self.fetches) >= MAX_FETCHES:
            raise ScriptError("too many requests in flight")
        try:
            u = self.page.base_url.resolve(str(url))
        except ValueError as e:
            raise ScriptError("TypeError: bad URL: %s" % e)
        if u.scheme not in ("http", "https"):
            raise ScriptError("TypeError: only http(s) requests are allowed")
        if not self._same_origin(u):
            raise ScriptError("TypeError: cross-origin request blocked (this browser does not implement CORS)")
        method = str(method).upper()
        if method not in _ALLOWED_METHODS:
            raise ScriptError("TypeError: method not allowed")
        headers = {}
        try:
            for k, v in json.loads(headers_json or "{}").items():
                if str(k).lower() in _ALLOWED_REQ_HEADERS and "\n" not in str(v) and "\r" not in str(v):
                    headers[str(k)] = str(v)[:500]
        except (ValueError, AttributeError):
            pass
        data = None if body is None else str(body).encode("utf-8")
        rid = int(rid)
        self.fetches.add(rid)
        referrer = str(self.page.url)

        def work():
            try:
                r = network.fetch(u, method=method, body=data, headers=headers or None, referrer=referrer)
                if not self._same_origin(r.url):
                    result = {"error": "redirected to another origin"}
                elif len(r.body) > MAX_FETCH_BYTES:
                    result = {"error": "response too large"}
                else:
                    result = {"status": r.status, "statusText": r.reason, "url": str(r.url),
                              "headers": {k: v for k, v in r.headers.items()
                                          if k not in ("set-cookie", "set-cookie2")},
                              "body": r.text()}
            except network.NetworkError as e:
                result = {"error": str(e)}
            except Exception as e:
                result = {"error": "%s" % type(e).__name__}
            with self.lock:
                self.done_fetches.append((rid, result))
        _net_pool.submit(work)

    # --- storage (localStorage per origin for this browser session; sessionStorage per page)
    def _store(self, local):
        if local:
            return _storage.setdefault(self.page.url.origin + ":%s" % self.page.url.port, {})
        if not hasattr(self, "_session_store"):
            self._session_store = {}
        return self._session_store

    def op_storage_get(self, local, key):
        v = self._store(local).get(str(key))
        return v

    def op_storage_set(self, local, key, value):
        store = self._store(local)
        key, value = str(key), str(value)
        size = sum(len(k) + len(v) for k, v in store.items()) - len(store.get(key, "")) + len(value) + len(key)
        if size > STORAGE_QUOTA:
            raise ScriptError("QuotaExceededError: storage is full")
        store[key] = value

    def op_storage_remove(self, local, key):
        self._store(local).pop(str(key), None)

    def op_storage_clear(self, local):
        self._store(local).clear()

    def op_storage_keys(self, local):
        return list(self._store(local))
