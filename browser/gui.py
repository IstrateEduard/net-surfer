"""The browser window: tabs, address bar, viewport, scrolling, links, forms.

Tk supplies the window, widgets, fonts and a canvas to draw primitives on.
Everything shown inside the viewport comes from our own display list.
"""
import copy
import os
import queue
import threading
import time
import tkinter
import tkinter.font
import tkinter.ttk as ttk
import urllib.parse

from . import animation, engine, favicon, icons, layout, network
from .dom import Element, dump

try:
    from PIL import ImageTk
except ImportError:
    ImageTk = None

APP_NAME = "Net Surfer"
ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
HOME_URL = "about:home"
SCROLL_STEP = 60
PROGRESS_COLOR = "#1a73e8"
PROGRESS_HIDE_DELAY = 0.35   # seconds the full bar stays visible after a load
TOOLBAR_BG = "#f3f4f7"
TOOLBAR_HOVER = "#e2e5eb"     # icon button under the mouse
TOOLBAR_PRESSED = "#d3d7df"   # icon button while clicked
TOOLBAR_BORDER = "#c9ced6"    # line between the browser chrome and the page
ADDRESS_HOVER = "#eef0f3"     # address bar under the mouse
TOOLBAR_ICON_BOX = (36, 30)   # button size in pixels when it shows an icon
TABBAR_BG = "#dfe3ea"
TABBAR_HOVER = "#cdd2db"


def _add_hover(widget, normal, hover):
    """Swap the widget's background while the mouse is over it."""
    def enter(event):
        if str(widget.cget("state")) != "disabled":
            widget.configure(bg=hover)

    def leave(event):
        widget.configure(bg=normal)
    widget.bind("<Enter>", enter, add="+")
    widget.bind("<Leave>", leave, add="+")


def _set_icon(widget, name, fallback, color=icons.ICON_COLOR):
    """Show a Material icon on a toolbar button, or the text symbol without one."""
    photo = icons.icon(name, widget, color=color, box=TOOLBAR_ICON_BOX)
    if photo is not None:
        # The image fills the whole button, so the glyph sits exactly in the middle of it.
        widget.configure(image=photo, text="", width=TOOLBAR_ICON_BOX[0], height=TOOLBAR_ICON_BOX[1],
                         padx=0, pady=0, bd=0, highlightthickness=0)
    else:
        widget.configure(image="", text=fallback, width=3, height=1)


class ImageCache:
    """PhotoImages created from PIL images at the size they are drawn."""

    def __init__(self):
        self.photos = {}

    def get(self, cmd):
        if ImageTk is None:
            return None
        key = cmd.cache_key()
        photo = self.photos.get(key)
        if photo is None:
            try:
                img = cmd.image
                fw, fh = cmd.full_size
                if hasattr(img, "render"):          # vector (SVG): rasterize at the drawn size
                    img = img.render(max(1, fw), max(1, fh))
                elif (fw, fh) != img.size and fw > 0 and fh > 0:
                    img = img.resize((max(1, fw), max(1, fh)))
                if cmd.crop:
                    x, y, w, h = cmd.crop
                    img = img.crop((x, y, x + max(1, w), y + max(1, h)))
                photo = ImageTk.PhotoImage(img)
            except Exception:
                return None
            self.photos[key] = photo
        return photo


def length_of(value, box):
    from .style import length
    return length(value or "0", box.fs, None, 0) or 0.0


def _root_styles(doc):
    body = next((c for c in doc.children if getattr(c, "tag", "") == "body"), None)
    return [doc.style] + ([body.style] if body is not None else [])


def _smooth(doc):
    return any(st.get("scroll-behavior") == "smooth" for st in _root_styles(doc))


def _scroll_padding(doc):
    from .style import length
    for st in _root_styles(doc):
        v = st.get("scroll-padding-top") or (st.get("scroll-padding", "").split() or [None])[0]
        if v and v != "auto":
            return length(v, st.get("-font-px", 16), None, 0) or 0.0
    return 0.0


def _scroll_offset(el, doc):
    """How far above a #fragment target to stop: its scroll-margin-top plus
    the page's scroll-padding-top."""
    from .style import length
    st = el.style
    m = st.get("scroll-margin-top") or (st.get("scroll-margin", "").split() or ["0"])[0]
    return (length(m, st.get("-font-px", 16), None, 0) or 0.0) + _scroll_padding(doc)


HOVER_KINDS = ("hover",)
FOCUS_KINDS = ("focus", "focus-visible", "focus-within")
CHECKED_KINDS = ("checked",)
TARGET_KINDS = ("target",)
FORM_KINDS = ("valid", "invalid", "user-valid", "user-invalid", "placeholder-shown", "in-range",
              "out-of-range", "indeterminate", "checked")


class Tab:
    def __init__(self, browser):
        self.browser = browser
        self.history = []        # list of dicts: url, method, body, scroll
        self.index = -1
        self.page = None
        self.doc_layout = None
        self.display_list = None
        self.ctx = None
        self.title = "New Tab"
        self.favicon = None      # PIL image once the page's icon has loaded
        self.loading = False
        self.progress = 0.0      # how far the current load has really got (0..1)
        self.shown_progress = 0.0  # what the bar shows; eases toward progress
        self.progress_label = ""
        self.done_at = None      # when the last load finished, to hide the bar
        self.generation = 0
        self.scroll = 0
        self.focused = None
        self.pending_scroll = None
        self.pending_fragment = None
        self.box_scroll = {}     # element -> scroll offset of overflow: auto boxes
        self.hover_set = set()   # elements currently matching :hover
        self.focus_el = None     # element currently matching :focus

    @property
    def url(self):
        return self.history[self.index]["url"] if self.index >= 0 else ""


class Browser:
    def __init__(self, start_url=None, width=1024, height=768):
        self.root = tkinter.Tk()
        self.root.title(APP_NAME)
        _set_app_icon(self.root)
        self.root.geometry("%dx%d" % (width, height))
        self.results = queue.Queue()
        self.images = ImageCache()
        self.tabs = []
        self.current = None
        self.bookmarks = []
        self._resize_job = None
        self._last_size = None
        self._hover_job = None
        self._hover_point = None
        self._anim_job = None
        self._snap_job = None
        self._selection = []
        self._drag = None
        self._last_anim_layout = 0.0
        self._anim_layout_pending = False
        self._layer_offsets = []
        self._build_ui()
        self.new_tab(start_url or HOME_URL)
        self._poll_job = self.root.after(30, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def close(self):
        if self._poll_job:
            self.root.after_cancel(self._poll_job)
            self._poll_job = None
        for tab in self.tabs:
            _close_scripts(tab.page)
        self.root.destroy()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = self.root
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tkinter.TclError:
            pass
        ui_font = tkinter.font.nametofont("TkDefaultFont")
        ui_font.configure(size=10)

        self.tabbar = tkinter.Frame(root, bg=TABBAR_BG)
        self.tabbar.pack(side="top", fill="x")

        bar = tkinter.Frame(root, bg=TOOLBAR_BG, padx=4, pady=4)
        bar.pack(side="top", fill="x")
        def btn(icon, text, cmd, tip):
            b = tkinter.Button(bar, command=cmd, relief="flat", bd=0, bg=TOOLBAR_BG,
                               activebackground=TOOLBAR_PRESSED, font=("DejaVu Sans", 11), cursor="hand2")
            _set_icon(b, icon, text)
            b.pack(side="left", padx=1)
            _add_hover(b, TOOLBAR_BG, TOOLBAR_HOVER)
            b.tip = tip
            return b
        self.back_btn = btn("arrow_back", "←", self.go_back, "Back (Alt+Left)")
        self.fwd_btn = btn("arrow_forward", "→", self.go_forward, "Forward (Alt+Right)")
        self.reload_btn = btn("refresh", "↻", self.reload, "Reload (F5)")
        btn("home", "⌂", lambda: self.navigate(HOME_URL), "Home")
        self.address = tkinter.Entry(bar, font=("DejaVu Sans", 11), relief="solid", bd=1, bg="white")
        self.address.pack(side="left", fill="x", expand=True, padx=6, ipady=3)
        self.address.bind("<Return>", self._on_address_enter)
        _add_hover(self.address, "white", ADDRESS_HOVER)
        self.bookmark_btn = btn("star_border", "☆", self.add_bookmark, "Bookmark this page")
        self.menu_btn = tkinter.Menubutton(bar, relief="flat", bd=0, bg=TOOLBAR_BG,
                                           activebackground=TOOLBAR_PRESSED, font=("DejaVu Sans", 11),
                                           cursor="hand2")
        _set_icon(self.menu_btn, "menu", "☰")
        _add_hover(self.menu_btn, TOOLBAR_BG, TOOLBAR_HOVER)
        self.menu = tkinter.Menu(self.menu_btn, tearoff=0)
        self.menu.add_command(label="New tab   Ctrl+T", command=lambda: self.new_tab(HOME_URL))
        self.menu.add_command(label="Close tab   Ctrl+W", command=self.close_tab)
        self.menu.add_separator()
        self.menu.add_command(label="View source   Ctrl+U", command=self.view_source)
        self.menu.add_command(label="DOM & style inspector   F12", command=self.open_inspector)
        self.menu.add_command(label="Network log", command=self.show_log)
        self.menu.add_separator()
        self.bookmark_menu = tkinter.Menu(self.menu, tearoff=0)
        self.menu.add_cascade(label="Bookmarks", menu=self.bookmark_menu)
        self.menu_btn.configure(menu=self.menu)
        self.menu_btn.pack(side="left", padx=1)

        # Load progress: a thin strip under the toolbar, filled left to right.
        # The strip keeps its height when idle so the page never shifts.
        self.progress_track = tkinter.Frame(root, height=3, bg=TOOLBAR_BG)
        self.progress_track.pack(side="top", fill="x")
        self.progress_fill = tkinter.Frame(self.progress_track, bg=PROGRESS_COLOR)
        # A 1px line under the chrome (below the progress strip) separates it from the page.
        self.toolbar_border = tkinter.Frame(root, height=1, bg=TOOLBAR_BORDER)
        self.toolbar_border.pack(side="top", fill="x")

        body = tkinter.Frame(root)
        body.pack(side="top", fill="both", expand=True)
        self.canvas = tkinter.Canvas(body, bg="white", highlightthickness=0, takefocus=1)
        self.scrollbar = ttk.Scrollbar(body, orient="vertical", command=self._on_scrollbar)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.browser_images = self.images

        self.status = tkinter.Label(root, text="", anchor="w", bg="#f3f4f7", fg="#444",
                                    font=("DejaVu Sans", 9), padx=6)
        self.status.pack(side="bottom", fill="x")

        c = self.canvas
        c.bind("<Configure>", self._on_configure)
        c.bind("<Button-1>", self._on_click)
        c.bind("<B1-Motion>", self._on_drag)
        c.bind("<ButtonRelease-1>", self._on_release)
        root.bind("<Control-c>", lambda e: self._copy_selection())
        root.bind("<Control-a>", lambda e: self._select_all())
        c.bind("<Motion>", self._on_motion)
        c.bind("<Leave>", lambda e: self._schedule_hover(None))
        c.bind("<MouseWheel>", self._on_wheel)
        c.bind("<Button-4>", lambda e: self.scroll_by(-SCROLL_STEP))
        c.bind("<Button-5>", lambda e: self.scroll_by(SCROLL_STEP))
        c.bind("<Key>", self._on_key)
        root.bind("<Alt-Left>", lambda e: self.go_back())
        root.bind("<Alt-Right>", lambda e: self.go_forward())
        root.bind("<F5>", lambda e: self.reload())
        root.bind("<Control-r>", lambda e: self.reload())
        root.bind("<Control-l>", lambda e: self._focus_address())
        root.bind("<Control-t>", lambda e: self.new_tab(HOME_URL))
        root.bind("<Control-w>", lambda e: self.close_tab())
        root.bind("<Control-u>", lambda e: self.view_source())
        root.bind("<F12>", lambda e: self.open_inspector())
        root.bind("<Control-d>", lambda e: self.add_bookmark())

    def _focus_address(self):
        self.address.focus_set()
        self.address.select_range(0, "end")
        return "break"

    # ---------------------------------------------------------------- tabs
    def new_tab(self, url):
        tab = Tab(self)
        self.tabs.append(tab)
        self.switch_tab(tab)
        self.navigate(url)

    def close_tab(self):
        if len(self.tabs) <= 1:
            self.close()
            return
        idx = self.tabs.index(self.current)
        self.tabs.remove(self.current)
        _close_scripts(self.current.page)
        self.switch_tab(self.tabs[max(0, idx - 1)])

    def switch_tab(self, tab):
        self.current = tab
        self.root.after_idle(self._schedule_anim)
        self._render_tabbar()
        self._update_chrome()
        self._draw_progress(snap=True)
        if tab.display_list is not None and tab.ctx is not None and \
                tab.ctx.viewport_width != self.canvas.winfo_width():
            self.relayout()
        else:
            self.redraw()

    def _render_tabbar(self):
        for w in self.tabbar.winfo_children():
            w.destroy()
        for tab in self.tabs:
            active = tab is self.current
            f = tkinter.Frame(self.tabbar, bg="#ffffff" if active else TABBAR_BG, padx=8, pady=3)
            f.pack(side="left", padx=(4, 0), pady=(4, 0))
            title = (tab.title or tab.url or "New Tab")
            if len(title) > 24:
                title = title[:23] + "…"
            if tab.loading:
                title = "⌛ " + title
            ico = self._tab_icon(tab)
            if ico is not None:
                tkinter.Label(f, image=ico, bg=f["bg"], bd=0, padx=0, pady=0).pack(side="left", padx=(0, 5))
            lbl = tkinter.Label(f, text=title, bg=f["bg"], font=("DejaVu Sans", 9))
            lbl.pack(side="left")
            lbl.bind("<Button-1>", lambda e, t=tab: self.switch_tab(t))
            x = tkinter.Label(f, text="✕", bg=f["bg"], fg="#777", font=("DejaVu Sans", 8), cursor="hand2")
            close_icon = icons.icon("close", x, size=14, color="#5f6368")
            if close_icon is not None:
                x.configure(image=close_icon, text="")
            x.pack(side="left", padx=(6, 0))
            x.bind("<Button-1>", lambda e, t=tab: self._close_specific(t))
            _add_hover(x, f["bg"], TOOLBAR_HOVER if active else TABBAR_HOVER)
        plus = tkinter.Label(self.tabbar, text=" + ", bg=TABBAR_BG, font=("DejaVu Sans", 11), cursor="hand2")
        add_icon = icons.icon("add", plus, size=18)
        if add_icon is not None:
            plus.configure(image=add_icon, text="", width=24, height=22)
        plus.pack(side="left", padx=4, pady=(4, 0))
        _add_hover(plus, TABBAR_BG, TABBAR_HOVER)
        plus.bind("<Button-1>", lambda e: self.new_tab(HOME_URL))

    def _tab_icon(self, tab):
        """The tab's favicon, or the default globe while there is none."""
        if tab.favicon is not None and ImageTk is not None:
            photos = self.root.__dict__.setdefault("_favicon_photos", {})
            photo = photos.get(id(tab.favicon))
            if photo is None:
                photo = photos[id(tab.favicon)] = ImageTk.PhotoImage(tab.favicon, master=self.root)
            return photo
        return icons.icon("public", self.root, size=16, color="#8a8f98")

    def _close_specific(self, tab):
        self.current = tab
        self.close_tab()

    # ---------------------------------------------------------- navigation
    def _on_address_enter(self, event=None):
        text = self.address.get().strip()
        if not text:
            return
        if "://" not in text and not text.startswith(("about:", "view-source:", "data:")):
            if " " in text or "." not in text:
                text = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(text)
            else:
                text = "https://" + text
        self.navigate(text)
        self.canvas.focus_set()

    def navigate(self, url, method="GET", body=None, push=True, scroll=0, tab=None):
        tab = tab or self.current
        url = str(url)
        # Same-document fragment navigation: just scroll.
        if method == "GET" and tab.page is not None and "#" in url and push and \
                url.split("#")[0] == str(tab.page.url).split("#")[0] and not url.startswith("view-source:"):
            self._push_history(tab, url, method, body)
            self.scroll_to_fragment(url.split("#", 1)[1])
            self._update_chrome()
            return
        if push:
            self._push_history(tab, url, method, body)
        tab.generation += 1
        gen = tab.generation
        tab.loading = True
        tab.progress = tab.shown_progress = 0.0
        tab.done_at = None
        tab.pending_scroll = scroll
        tab.focused = None
        self._update_chrome()
        self._render_tabbar()
        self.set_status("Loading %s …" % url)
        width = max(200, self.canvas.winfo_width())
        referrer = str(tab.page.url) if tab.page is not None else None
        headers = {"Content-Type": "application/x-www-form-urlencoded"} if method == "POST" else None

        def progress(fraction, label):
            self.results.put(("progress", tab, gen, fraction, label))

        def work():
            try:
                page = engine.load(url, width, method=method, body=body, headers=headers, referrer=referrer,
                                   progress=progress)
            except Exception as e:  # a bug in our engine must not kill the window
                import traceback
                traceback.print_exc()
                page = engine.error_page(url, "The browser hit an internal error", "%s: %s" % (type(e).__name__, e), width)
            self.results.put(("page", tab, gen, page))
        threading.Thread(target=work, daemon=True).start()

    def _push_history(self, tab, url, method, body):
        if tab.index >= 0:
            tab.history[tab.index]["scroll"] = tab.scroll
        del tab.history[tab.index + 1:]
        tab.history.append({"url": url, "method": method, "body": body, "scroll": 0})
        tab.index = len(tab.history) - 1

    def go_back(self):
        tab = self.current
        if tab.index > 0:
            tab.history[tab.index]["scroll"] = tab.scroll
            tab.index -= 1
            self._go_to_history(tab)

    def go_forward(self):
        tab = self.current
        if tab.index < len(tab.history) - 1:
            tab.history[tab.index]["scroll"] = tab.scroll
            tab.index += 1
            self._go_to_history(tab)

    def _go_to_history(self, tab):
        entry = tab.history[tab.index]
        self.navigate(entry["url"], entry["method"], entry["body"], push=False, scroll=entry["scroll"])

    def reload(self):
        tab = self.current
        if tab.index >= 0:
            entry = tab.history[tab.index]
            network.CACHE.entries.clear()
            self.navigate(entry["url"], entry["method"], entry["body"], push=False, scroll=tab.scroll)

    def view_source(self):
        tab = self.current
        if tab.page is not None and not tab.url.startswith(("view-source:", "about:")):
            self.new_tab("view-source:" + tab.url.split("#")[0])

    # ------------------------------------------------------------- loading
    def _poll(self):
        try:
            while True:
                item = self.results.get_nowait()
                kind, tab, gen = item[:3]
                if gen != tab.generation:
                    if kind == "page" and item[3] is not tab.page:
                        _close_scripts(item[3])     # a load that was superseded
                    continue
                if kind == "favicon":
                    tab.favicon = item[3]
                    self._render_tabbar()
                    continue
                if kind == "js-images":
                    if tab.page is item[3]:
                        self._relayout_tab(tab)
                    continue
                if kind == "progress":
                    fraction, label = item[3:]
                    tab.progress = max(tab.progress, fraction)
                    if label != tab.progress_label:
                        tab.progress_label = label
                        if tab is self.current:
                            self.set_status(label + " …")
                else:
                    self._page_loaded(tab, item[3])
        except queue.Empty:
            pass
        self._tick_scripts()
        self._draw_progress()
        self._poll_job = self.root.after(30, self._poll)

    def _draw_progress(self, snap=False):
        """Ease the bar toward the tab's real progress; hide it once done."""
        tab = self.current
        if tab is None:
            return
        if snap:
            tab.shown_progress = tab.progress
        else:
            tab.shown_progress += (tab.progress - tab.shown_progress) * 0.35
            if tab.progress - tab.shown_progress < 0.002:
                tab.shown_progress = tab.progress
        idle = not tab.loading and (tab.done_at is None or time.time() - tab.done_at > PROGRESS_HIDE_DELAY)
        if idle or tab.shown_progress <= 0:
            self.progress_fill.place_forget()
        else:
            self.progress_fill.place(x=0, y=0, relheight=1, relwidth=tab.shown_progress)

    def _page_loaded(self, tab, page):
        t0 = time.time()
        # Layout runs right here on the UI thread, so paint the bar first.
        tab.progress = max(tab.progress, engine.P_IMAGES)
        if tab is self.current:
            self.set_status("Laying out page …")
            self._draw_progress(snap=True)
            self.root.update_idletasks()
        tab.loading = False
        if tab.page is not page:
            _close_scripts(tab.page)
        tab.page = page
        tab.hover_set = set()
        tab.focus_el = None
        tab.box_scroll = {}
        tab.target_el = None
        tab.title = page.title or str(page.url)
        tab.favicon = None
        self._load_favicon(tab, page)
        entry = tab.history[tab.index] if tab.index >= 0 else None
        if entry is not None and not entry["url"].startswith(("about:", "view-source:")) and \
                entry["method"] == "GET" and not getattr(page, "is_error", False):
            final = str(page.url)
            if "#" in entry["url"] and "#" not in final:
                final += "#" + entry["url"].split("#", 1)[1]
            entry["url"] = final
        self._layout_tab(tab)
        page.timings["layout+paint"] = time.time() - t0
        self._start_animations(tab)
        tab.progress = 1.0
        tab.done_at = time.time()
        frag = page.url.fragment if hasattr(page.url, "fragment") else ""
        if tab is self.current:
            if tab.pending_scroll:
                self.scroll_to(tab.pending_scroll)
            elif frag:
                self.scroll_to_fragment(frag)
            else:
                self.scroll_to(0)
            self.redraw()
            self._update_chrome()
            t = page.timings
            self.set_status("Done in %.2fs  (network %.2f, parse %.2f, css %.2f, style %.2f, images %.2f, layout+paint %.2f)" % (
                t.get("total", 0) + t.get("layout+paint", 0), t.get("network", 0), t.get("parse", 0),
                t.get("css", 0), t.get("style", 0), t.get("images", 0), t.get("layout+paint", 0)))
        self._render_tabbar()

    def _load_favicon(self, tab, page):
        """Fetch the page's icon in the background; the tab shows a globe until it arrives."""
        gen = tab.generation

        def work():
            try:
                img = favicon.load(page)
            except Exception:
                img = None
            if img is not None:
                self.results.put(("favicon", tab, gen, img))
        threading.Thread(target=work, daemon=True).start()

    def _layout_tab(self, tab):
        page = tab.page
        width = max(200, self.canvas.winfo_width())
        height = max(100, self.canvas.winfo_height())
        from . import style as _style
        old_h = _style.VIEWPORT.get("height")
        _style.VIEWPORT["height"] = height
        height_queries = getattr(page, "has_media", False) and old_h != height and             any(k in t for t, _ in page.css_sources for k in ("height", "orientation", "aspect-ratio"))
        if page.styled_width is not None and getattr(page, "has_media", False) and                 (page.styled_width != width or height_queries):
            page.restyle(width)
        tab.ctx = layout.LayoutContext(page.base_url, page.images, width, height)
        tab.ctx.box_scroll = tab.box_scroll
        tab.ctx.focused = tab.focused
        try:
            tab.doc_layout = layout.DocumentLayout(page.document, tab.ctx)
            tab.doc_layout.layout(width, height)
            # @container rules and cq units were matched against the sizes of
            # the previous layout: if a container's size changed, style and
            # lay out again (at most twice).
            for _ in range(2):
                if not getattr(page, "has_containers", False) or not engine.containers_changed(page):
                    break
                page.restyle(width)
                tab.ctx = layout.LayoutContext(page.base_url, page.images, width, height)
                tab.ctx.box_scroll = tab.box_scroll
                tab.ctx.focused = tab.focused
                tab.doc_layout = layout.DocumentLayout(page.document, tab.ctx)
                tab.doc_layout.layout(width, height)
            tab.display_list = tab.doc_layout.paint()
            if getattr(page, "js", None) is not None:
                page.js.set_layout(tab.doc_layout, (width, height), tab.scroll)
        except RecursionError:
            err = engine.error_page(str(page.url), "Page too deeply nested", "The document tree was too deep to lay out.", width)
            tab.page = err
            return self._layout_tab(tab)

    def relayout(self):
        tab = self.current
        if tab.page is None:
            return
        frac = tab.scroll / max(1, tab.doc_layout.height) if tab.doc_layout else 0
        self._layout_tab(tab)
        self.scroll_to(frac * tab.doc_layout.height)
        self.redraw()

    def repaint(self):
        """Rebuild the display list without relayout (form edits, focus)."""
        tab = self.current
        if tab.doc_layout is None:
            return
        if self._apply_states(tab, focus=tab.focused):
            self.relayout()   # a :focus rule changed box sizes
            return
        tab.ctx.focused = tab.focused
        tab.display_list = tab.doc_layout.paint()
        self.redraw()

    # ------------------------------------------- :hover / :focus restyling
    _KEEP = object()

    def _apply_states(self, tab, hover=_KEEP, focus=_KEEP, checked=(), targets=(), forms=()):
        """Move the :hover / :focus states to new elements and restyle what
        they affect. Returns None if no style changed, False if only paint
        properties changed, True if the page needs a new layout."""
        eng = getattr(tab.page, "style_engine", None) if tab.page is not None else None
        if eng is None or tab.doc_layout is None:
            return None
        changed = [(el, CHECKED_KINDS) for el in checked]    # [(element, kinds of state it changed)]
        changed += [(el, TARGET_KINDS) for el in targets]
        for el in forms:      # a field's value changed: it and its form/fieldsets revalidate
            changed.append((el, FORM_KINDS))
            changed += [(a, FORM_KINDS) for a in el.ancestors()
                        if isinstance(a, Element) and a.tag in ("form", "fieldset")]
        if hover is not Browser._KEEP:
            new = set()
            el = hover
            while isinstance(el, Element):
                new.add(el)
                el = el.parent
            for el in tab.hover_set - new:
                el.hover_state = False
                changed.append((el, HOVER_KINDS))
            for el in new - tab.hover_set:
                el.hover_state = True
                changed.append((el, HOVER_KINDS))
            tab.hover_set = new
        if focus is not Browser._KEEP and focus is not tab.focus_el:
            for el, flag in ((tab.focus_el, False), (focus, True)):
                if el is None:
                    continue
                el.focus_state = flag
                changed.append((el, FOCUS_KINDS))
                for a in el.ancestors():
                    if isinstance(a, Element):
                        a.focus_within_state = flag
                        changed.append((a, FOCUS_KINDS))
            tab.focus_el = focus
        if not changed:
            return None
        roots = []
        for kinds in (HOVER_KINDS, FOCUS_KINDS, CHECKED_KINDS, TARGET_KINDS, FORM_KINDS):
            els = [el for el, k in changed if k is kinds]
            if els:
                roots += [r for r in eng.dynamic_roots(els, kinds) if r not in roots]
        roots = [r for r in roots if not any(a in roots for a in r.ancestors())]
        if not roots:
            return None
        needs_layout = False
        changes = []
        for root in roots:
            needs_layout = eng.restyle_subtree(root) or needs_layout
            changes += getattr(eng, "last_changes", [])
        self._animate_changes(tab, changes)
        return needs_layout

    # ------------------------------------------------- transitions / animations
    def _animator(self, tab):
        page = tab.page
        if page is None or page.document is None:
            return None
        an = getattr(page, "animator", None)
        if an is None:
            an = page.animator = animation.Animator(page)
        return an

    def _start_animations(self, tab):
        """Start @keyframes animations found on the page."""
        an = self._animator(tab)
        if an is None:
            return
        an.scan(time.monotonic())
        self._schedule_anim()

    def _animate_changes(self, tab, changes):
        """A restyle changed property values: run `transition`s for them."""
        an = self._animator(tab)
        if an is None:
            return
        now = time.monotonic()
        if changes:
            an.on_restyle(changes, now)
        an.scan(now)
        self._schedule_anim()

    def _schedule_anim(self):
        an = getattr(self.current.page, "animator", None) if self.current and self.current.page else None
        if an is not None and an.active() and self._anim_job is None:
            self._anim_job = self.root.after(16, self._anim_tick)

    def _anim_tick(self):
        self._anim_job = None
        tab = self.current
        an = getattr(tab.page, "animator", None) if tab.page is not None else None
        if an is None or tab.doc_layout is None or tab.loading:
            return
        t0 = time.monotonic()
        changed, needs_layout = an.tick(t0)
        if changed:
            if needs_layout:
                if t0 - self._last_anim_layout >= 0.1 or not an.active():
                    self._last_anim_layout = t0
                    self.relayout()
                else:
                    self._anim_layout_pending = True
            else:
                tab.ctx.focused = tab.focused
                tab.display_list = tab.doc_layout.paint()
                self.redraw()
        elif getattr(self, "_anim_layout_pending", False):
            self._anim_layout_pending = False
            self.relayout()
        if an.active() or getattr(self, "_anim_layout_pending", False):
            # frame rate adapts to how long a frame takes to draw
            cost = time.monotonic() - t0
            self._anim_job = self.root.after(max(16, int(cost * 1500)), self._anim_tick)

    def _checked_changed(self, tab, elements):
        """Checkboxes/radios toggled: restyle :checked rules (CSS-only
        dropdowns and menus such as Wikipedia's are built on them)."""
        if self._apply_states(tab, checked=elements, forms=elements):
            self.relayout()
        else:
            self.repaint()

    def _label_control(self, tab, x, y):
        """The form control activated by clicking at (x, y) outside any
        painted control: a <label>'s control, or an invisible input."""
        el = tab.doc_layout.element_at(x, y, tab.scroll) if tab.doc_layout is not None else None
        while isinstance(el, Element):
            if el.tag in ("input", "select", "textarea", "button"):
                return el
            if el.tag == "label":
                target = el.attributes.get("for")
                if target:
                    return tab.page.document.find_by_id(target)
                return next((d for d in el.descendants() if isinstance(d, Element)
                             and d.tag in ("input", "select", "textarea")), None)
            el = el.parent
        return None

    def _scroll_inner_box(self, event):
        """Mouse wheel over an overflow: auto/scroll box scrolls that box
        (Shift+wheel scrolls sideways) until it reaches its end."""
        tab = self.current
        if tab.ctx is None or not getattr(tab.ctx, "scrollers", None):
            return False
        x, y = self._doc_point(event)
        step = -SCROLL_STEP if event.delta > 0 else SCROLL_STEP
        horizontal = bool(event.state & 0x1)
        for box, _clip, max_sx, max_sy in reversed(tab.ctx.scrollers):
            cx1, cy1, cx2, cy2 = box.border_box()
            if not (cx1 <= x <= cx2 and cy1 <= y <= cy2):
                continue
            sx, sy = tab.box_scroll.get(box.node, (0.0, 0.0))
            if horizontal:
                if not max_sx:
                    continue
                nsx, nsy = min(max(0.0, sx + step), max_sx), sy
            elif not max_sy:
                continue
            else:
                nsx, nsy = sx, min(max(0.0, sy + step), max_sy)
            if (nsx, nsy) == (sx, sy):
                continue    # at its end: let an outer box or the page scroll
            tab.box_scroll[box.node] = (nsx, nsy)
            tab.display_list = tab.doc_layout.paint()
            self.redraw()
            return True
        return False

    def _schedule_hover(self, point):
        self._hover_point = point
        if self._hover_job is None:
            self._hover_job = self.root.after(25, self._update_hover)

    def _update_hover(self):
        self._hover_job = None
        tab = self.current
        if tab is None or tab.doc_layout is None or tab.loading:
            return
        el = tab.doc_layout.element_at(*self._hover_point, tab.scroll) if self._hover_point else None
        result = self._apply_states(tab, hover=el)
        if result is None:
            return
        if result:
            self.relayout()
        else:
            tab.ctx.focused = tab.focused
            tab.display_list = tab.doc_layout.paint()
            self.redraw()

    # -------------------------------------------------------------- drawing
    def redraw(self):
        tab = self.current
        c = self.canvas
        c.delete("all")
        if tab is None or tab.display_list is None:
            return
        c.configure(bg=tab.doc_layout.background)
        w = c.winfo_width()
        h = tab.doc_layout.height
        c.configure(scrollregion=(0, 0, w, h))
        for cmd in tab.display_list.commands:
            cmd.execute(c, 0, 0)
        # Fixed and sticky boxes: each layer gets its own tag so scrolling can
        # move it with one canvas.move instead of a full redraw.
        self._layer_offsets = []
        for i, layer in enumerate(tab.display_list.layers or ()):
            before = (c.find_all() or (0,))[-1]
            if layer.kind == "fixedbg":
                c.create_line(0, 0, 0, 0, tags=("layer%d" % i,))   # placeholder until the first scroll update
                self._layer_offsets.append(None)
                continue
            for cmd in layer.list.commands:
                cmd.execute(c, 0, 0)
            for item in c.find_all():
                if item > before:
                    c.addtag_withtag("layer%d" % i, item)
            self._layer_offsets.append(0.0)
        if self._selection:
            live = set(map(id, tab.display_list.commands))
            self._selection = [t for t in self._selection if id(t[0]) in live]
            self._draw_selection()
        self._apply_scroll()

    def _apply_scroll(self):
        tab = self.current
        h = tab.doc_layout.height if tab.doc_layout else 1
        self.canvas.yview_moveto(tab.scroll / max(1, h))
        layers = tab.display_list.layers if tab.display_list is not None else None
        for i, layer in enumerate(layers or ()):
            if i < len(self._layer_offsets):
                off = layer.offset(tab.scroll)
                if off == self._layer_offsets[i]:
                    continue
                if layer.kind == "fixedbg":
                    # background-attachment: fixed -- the image stays put but
                    # is only visible inside its box, so redraw it clipped.
                    self.canvas.delete("layer%d" % i)
                    before = (self.canvas.find_all() or (0,))[-1]
                    for cmd in layer.list.commands:
                        c2 = copy.copy(cmd)
                        c2.top, c2.bottom = c2.top + off, c2.bottom + off
                        c2 = c2.clipped(layer.clip)
                        if c2 is not None:
                            c2.execute(self.canvas, 0, 0)
                    for item in self.canvas.find_all():
                        if item > before:
                            self.canvas.addtag_withtag("layer%d" % i, item)
                    anchor = "anchor%d" % id(layer)
                    if self.canvas.find_withtag(anchor):
                        self.canvas.tag_raise("layer%d" % i, anchor)   # just above the box's background
                else:
                    self.canvas.move("layer%d" % i, 0, off - self._layer_offsets[i])
                self._layer_offsets[i] = off
        view_h = self.canvas.winfo_height()
        self.scrollbar.set(tab.scroll / max(1, h), min(1.0, (tab.scroll + view_h) / max(1, h)))

    def max_scroll(self):
        tab = self.current
        if tab.doc_layout is None:
            return 0
        return max(0, tab.doc_layout.height - self.canvas.winfo_height())

    def scroll_to(self, y):
        tab = self.current
        tab.scroll = max(0, min(y, self.max_scroll()))
        if tab.doc_layout is not None:
            self._apply_scroll()

    def scroll_by(self, dy):
        self.scroll_to(self.current.scroll + dy)

    def scroll_to_fragment(self, frag):
        tab = self.current
        if tab.page is None or tab.ctx is None:
            return
        frag = urllib.parse.unquote(frag)
        el = tab.page.document.find_by_id(frag)
        if el is None:
            if frag in ("", "top"):
                self.scroll_to(0)
            return
        old = getattr(tab, "target_el", None)
        if old is not el:
            # :target styles (Wikipedia highlights the reference you jumped to)
            if old is not None:
                old.target_state = False
            el.target_state = True
            tab.target_el = el
            res = self._apply_states(tab, targets=[e for e in (old, el) if e is not None])
            if res:
                self._layout_tab(tab)
                self.redraw()
            elif res is False:
                tab.display_list = tab.doc_layout.paint()
                self.redraw()
        node = el
        while node is not None and node not in tab.ctx.positions:
            # use the first descendant that got a box, else an ancestor
            nxt = next((d for d in node.descendants() if d in tab.ctx.positions), None)
            if nxt is not None:
                node = nxt
                break
            node = node.parent
        if node is not None and node in tab.ctx.positions:
            target = tab.ctx.positions[node] - _scroll_offset(el, tab.page.document)
            self.scroll_smoothly(target) if _smooth(tab.page.document) else self.scroll_to(target)

    def _on_scrollbar(self, *args):
        tab = self.current
        if tab.doc_layout is None:
            return
        if args[0] == "moveto":
            self.scroll_to(float(args[1]) * tab.doc_layout.height)
        elif args[0] == "scroll":
            n = int(args[1])
            step = self.canvas.winfo_height() * 0.9 if args[2] == "pages" else SCROLL_STEP
            self.scroll_by(n * step)

    def _on_wheel(self, event):
        if self._scroll_inner_box(event):
            return
        delta = event.delta
        if abs(delta) >= 120:
            self.scroll_by(-delta / 120 * SCROLL_STEP)
        else:
            self.scroll_by(-delta * 2)
        if self._snap_job is not None:
            self.root.after_cancel(self._snap_job)
        self._snap_job = self.root.after(150, self._snap_page)

    # ------------------------------------------- scrolling extras
    def scroll_smoothly(self, target, duration=0.3):
        """scroll-behavior: smooth -- ease the page to `target`."""
        tab = self.current
        start, t0 = tab.scroll, time.monotonic()
        target = max(0, min(target, self.max_scroll()))

        def step():
            if self.current is not tab:
                return
            f = min(1.0, (time.monotonic() - t0) / duration)
            f = 1 - (1 - f) ** 3
            self.scroll_to(start + (target - start) * f)
            if f < 1:
                self.root.after(16, step)
        step()

    def _snap_page(self):
        """scroll-snap-type on the page: settle on the nearest snap position."""
        self._snap_job = None
        tab = self.current
        if tab.doc_layout is None or tab.page is None:
            return
        root = tab.page.document
        body = next((c for c in root.children if getattr(c, "tag", "") == "body"), None)
        snap = root.style.get("scroll-snap-type", "none")
        if snap == "none" and body is not None:
            snap = body.style.get("scroll-snap-type", "none")
        if "y" not in snap and "block" not in snap and "both" not in snap:
            return
        view = self.canvas.winfo_height()
        pad = _scroll_padding(root)
        cands = []
        stack = [tab.doc_layout.root]
        while stack:
            b = stack.pop()
            align = b.style.get("scroll-snap-align", "none").split()[0] if b.style.get("scroll-snap-align") else "none"
            if align != "none" and b.node is not None:
                x1, y1, x2, y2 = b.border_box()
                m = length_of(b.style.get("scroll-margin-top", "0"), b)
                if align == "start":
                    cands.append(y1 - pad - m)
                elif align == "center":
                    cands.append((y1 + y2) / 2 - view / 2)
                elif align == "end":
                    cands.append(y2 - view)
            stack.extend(b.children)
        if not cands:
            return
        best = min(cands, key=lambda c: abs(c - tab.scroll))
        if "mandatory" in snap or abs(best - tab.scroll) < view * 0.3:
            if abs(best - tab.scroll) > 1:
                self.scroll_smoothly(best, 0.2)

    # ------------------------------------------- selection and resizing
    def _on_drag(self, event):
        tab = self.current
        drag = getattr(self, "_drag", None)
        if not drag or tab.display_list is None:
            return
        x, y = self._doc_point(event)
        if drag[0] == "resize":
            _, node, mode, sx, sy, w0, h0 = drag
            w = max(10, w0 + (x - sx)) if mode in ("both", "horizontal", "inline") else w0
            h = max(10, h0 + (y - sy)) if mode in ("both", "vertical", "block") else h0
            node.resize_override = (w, h)
            node.style["width"], node.style["height"] = "%gpx" % w, "%gpx" % h
            node.style["box-sizing"] = "content-box"
            now = time.monotonic()
            if now - getattr(self, "_last_resize", 0) > 0.05:
                self._last_resize = now
                self.relayout()
            return
        _, ax, ay = drag
        if abs(x - ax) + abs(y - ay) < 4 and not self._selection:
            return
        self._selection = self._select_between(tab, (ax, ay), (x, y))
        self._draw_selection()

    def _on_release(self, event):
        drag = getattr(self, "_drag", None)
        self._drag = None
        if drag and drag[0] == "resize":
            self.relayout()

    def _text_commands(self, tab):
        cmds = [c for c in tab.display_list.commands if getattr(c, "node", None) is not None
                and hasattr(c, "text") and not getattr(c, "angle", 0)]
        cmds = [c for c in cmds if c.node.style.get("user-select", "auto") not in ("none",)]
        cmds.sort(key=lambda c: (round((c.top + c.bottom) / 2 / 4), c.left))
        return cmds

    def _select_between(self, tab, a, b):
        """Text runs (and character ranges) between two points, in reading order."""
        cmds = self._text_commands(tab)
        if not cmds:
            return []

        def pos(pt):
            x, y = pt
            for i, c in enumerate(cmds):
                if c.top <= y <= c.bottom and x < c.right:
                    if x <= c.left:
                        return (i, 0)
                    k = 0
                    while k < len(c.text) and c.left + c.font.measure(c.text[:k + 1]) <= x:
                        k += 1
                    return (i, k)
                if c.top > y:
                    return (i, 0)
            return (len(cmds) - 1, len(cmds[-1].text))
        p1, p2 = sorted([pos(a), pos(b)])
        out = []
        for i in range(p1[0], p2[0] + 1):
            c = cmds[i]
            k1 = p1[1] if i == p1[0] else 0
            k2 = p2[1] if i == p2[0] else len(c.text)
            if k2 > k1:
                out.append((c, k1, k2))
        return out

    def _select_all(self):
        tab = self.current
        if tab.display_list is None or self.root.focus_get() is self.address:
            return
        cmds = self._text_commands(tab)
        self._selection = [(c, 0, len(c.text)) for c in cmds]
        self._draw_selection()
        return "break"

    def _draw_selection(self):
        """Highlight selected text with the element's ::selection colours."""
        c = self.canvas
        c.delete("selection")
        for cmd, k1, k2 in self._selection:
            x1 = cmd.left + cmd.font.measure(cmd.text[:k1])
            x2 = cmd.left + cmd.font.measure(cmd.text[:k2])
            el = cmd.node.parent if cmd.node is not None else None
            st = None
            while el is not None and st is None:
                st = getattr(el, "selection_style", None)
                el = getattr(el, "parent", None)
            bg = layout.color_of(st.get("background-color")) if st else None
            fg = layout.color_of(st.get("color")) if st and "color" in st.get("-declared", ()) else None
            c.create_rectangle(x1, cmd.top, x2, cmd.bottom, fill=bg or "#b3d4fc", width=0, tags=("selection",))
            c.create_text(x1, cmd.top, text=cmd.text[k1:k2], font=cmd.font.tk, anchor="nw",
                          fill=fg or cmd.color, tags=("selection",))

    def _clear_selection(self):
        self._selection = []
        self.canvas.delete("selection")

    def _copy_selection(self):
        if self.root.focus_get() is self.address or not self._selection:
            return
        parts, last = [], None
        for cmd, k1, k2 in self._selection:
            if last is not None:
                parts.append("\n" if cmd.top >= last.bottom - 2 else
                             (" " if cmd.left > last.right + 1 else ""))
            parts.append(cmd.text[k1:k2])
            last = cmd
        self.root.clipboard_clear()
        self.root.clipboard_append("".join(parts))
        self.set_status("Copied %d characters" % sum(len(p) for p in parts))
        return "break"

    def _on_configure(self, event):
        size = (event.width, event.height)
        if size == self._last_size:
            return
        width_changed = self._last_size is None or self._last_size[0] != event.width
        self._last_size = size
        if self._resize_job:
            self.root.after_cancel(self._resize_job)
        # Lay out again on any size change: vh units, fixed boxes and
        # height media queries depend on the window height too.
        self._resize_job = self.root.after(120 if width_changed else 150, self.relayout)

    # --------------------------------------------------------- interaction
    def _doc_point(self, event):
        return self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)

    def _on_motion(self, event):
        tab = self.current
        if tab.display_list is None:
            return
        x, y = self._doc_point(event)
        self._schedule_hover((x, y))
        node = tab.display_list.hit_test_at(x, y, tab.scroll)
        if node is not None and node.tag == "a":
            self.canvas.configure(cursor="hand2")
            href = node.attributes.get("href", "")
            try:
                href = str(tab.page.base_url.resolve(href))
            except ValueError:
                pass
            self.set_status(href)
        elif node is not None:
            self.canvas.configure(cursor="xterm" if node.tag in ("input", "textarea") and
                                  node.attributes.get("type", "text") not in ("submit", "button", "checkbox", "radio", "image")
                                  else "hand2")
        else:
            self.canvas.configure(cursor="")
            if not tab.loading:
                self.set_status("")

    def _on_click(self, event):
        self.canvas.focus_set()
        tab = self.current
        if tab.display_list is None:
            return
        x, y = self._doc_point(event)
        self._clear_selection()
        self._drag = None
        for box, mode, (gx1, gy1, gx2, gy2) in reversed(getattr(tab.ctx, "resizers", []) or []):
            if gx1 <= x <= gx2 + 2 and gy1 <= y <= gy2 + 2:
                # start dragging a resize: handle
                self._drag = ("resize", box.node, mode, x, y, box.width, box.height)
                return
        self._drag = ("select", x, y)
        node = tab.display_list.hit_test_at(x, y, tab.scroll)
        if node is None:
            node = self._label_control(tab, x, y)
        if self._scripts_active(tab):
            # Page scripts see the click first and may cancel the default action.
            target = tab.doc_layout.element_at(x, y, tab.scroll) if tab.doc_layout is not None else None
            if target is None or (node is not None and target is not node and node not in target.ancestors()):
                target = node
            if target is not None:
                mouse = {"clientX": event.x, "clientY": event.y, "pageX": x, "pageY": y, "button": 0,
                         "ctrlKey": bool(event.state & 0x4), "shiftKey": bool(event.state & 0x1)}
                # the same sequence a browser sends for a mouse click
                down_cancelled = self._js_event(tab, target, "pointerdown", mouse)
                if not down_cancelled:
                    self._js_event(tab, target, "mousedown", mouse)
                up_cancelled = self._js_event(tab, target, "pointerup", mouse)
                if not up_cancelled:
                    self._js_event(tab, target, "mouseup", mouse)
                if self._js_event(tab, target, "click", mouse) or tab.loading or tab is not self.current:
                    return
        old_focus = tab.focused
        tab.focused = None
        if node is None:
            if old_focus is not None:
                self.repaint()
            return
        if node.tag == "a":
            href = node.attributes.get("href")
            if href is None:
                return
            if href.strip().lower().startswith("javascript:"):
                if self._scripts_active(tab):
                    tab.page.js.run_inline(urllib.parse.unquote(href.strip()[len("javascript:"):]))
                    self._apply_js(tab)
                return
            try:
                target = tab.page.base_url.resolve(href)
            except ValueError as e:
                self.set_status("Cannot follow link: %s" % e)
                return
            if node.attributes.get("target") == "_blank" or (event.state & 0x4):
                self.new_tab(str(target))
            else:
                self.navigate(str(target))
            return
        self._activate_control(node)

    def _activate_control(self, node):
        tab = self.current
        kind = node.attributes.get("type", "text").lower() if node.tag == "input" else node.tag
        if node.tag == "input" and kind in ("submit", "image"):
            self.submit_form(node)
        elif node.tag == "input" and kind == "checkbox":
            node.checked = not node.checked
            self._checked_changed(tab, [node])
            self._js_event(tab, node, "input", {"cancelable": False})
            self._js_event(tab, node, "change", {"cancelable": False})
        elif node.tag == "input" and kind == "radio":
            name = node.attributes.get("name")
            form = _form_of(node) or tab.page.document
            changed = []
            for other in form.get_elements_by_tag("input"):
                if other.attributes.get("type", "").lower() == "radio" and other.attributes.get("name") == name:
                    if other.checked:
                        changed.append(other)
                    other.checked = False
            node.checked = True
            self._checked_changed(tab, changed + [node])
            if node not in changed:
                self._js_event(tab, node, "input", {"cancelable": False})
                self._js_event(tab, node, "change", {"cancelable": False})
        elif node.tag == "input" and kind == "reset":
            form = _form_of(node)
            if form is not None:
                for el in form.descendants():
                    if isinstance(el, Element):
                        el.form_value = None
                        el.checked = "checked" in el.attributes
            self.repaint()
        elif node.tag == "select":
            opts = node.get_elements_by_tag("option")
            if opts:
                cur = layout.selected_option(node)
                i = opts.index(cur) if cur in opts else -1
                node.form_value = opts[(i + 1) % len(opts)]
                self.relayout()
                self._js_event(tab, node, "input", {"cancelable": False})
                self._js_event(tab, node, "change", {"cancelable": False})
        elif node.tag in ("input", "textarea"):
            if kind not in ("button", "hidden", "file"):
                tab.focused = node
                self.repaint()
        elif node.tag == "button":
            if node.attributes.get("type", "submit").lower() == "submit":
                self.submit_form(node)

    def _on_key(self, event):
        tab = self.current
        node = tab.focused
        if self._scripts_active(tab):
            key = event.char if event.char and event.char.isprintable() else _DOM_KEYS.get(event.keysym, event.keysym)
            init = {"key": key, "code": event.keysym, "keyCode": event.keycode,
                    "ctrlKey": bool(event.state & 0x4), "shiftKey": bool(event.state & 0x1)}
            if self._js_event(tab, node, "keydown", init) or tab.loading or tab is not self.current:
                return "break"
            node = tab.focused
        if node is not None:
            old_value = node.form_value
            if node.form_value is None:
                node.form_value = node.attributes.get("value", "") if node.tag == "input" else node.text_content()
            if event.keysym == "BackSpace":
                node.form_value = node.form_value[:-1]
            elif event.keysym in ("Return", "KP_Enter"):
                if node.tag == "textarea":
                    node.form_value += "\n"
                else:
                    self.submit_form(node)
                    return "break"
            elif event.keysym == "Escape":
                tab.focused = None
            elif event.keysym == "Tab":
                self._focus_next(node)
                return "break"
            elif event.char and event.char.isprintable() and not (event.state & 0x4):
                maxlen = node.attributes.get("maxlength")
                if not (maxlen and maxlen.isdigit() and len(node.form_value) >= int(maxlen)):
                    node.form_value += event.char
            else:
                return
            if node.form_value != old_value and event.keysym != "Escape":
                self._js_event(tab, node, "input", {"cancelable": False, "data": event.char or None})
                if tab.loading or tab is not self.current:
                    return "break"
            if self._apply_states(tab, forms=[node]):
                self.relayout()
                return "break"
            self.repaint()
            return "break"
        keys = {"Down": SCROLL_STEP, "Up": -SCROLL_STEP, "Next": self.canvas.winfo_height() * 0.9,
                "Prior": -self.canvas.winfo_height() * 0.9, "space": self.canvas.winfo_height() * 0.9}
        if event.keysym in keys:
            self.scroll_by(keys[event.keysym])
        elif event.keysym == "Home":
            self.scroll_to(0)
        elif event.keysym == "End":
            self.scroll_to(self.max_scroll())
        elif event.keysym == "BackSpace":
            self.go_back()

    def _focus_next(self, node):
        tab = self.current
        fields = [e for e in tab.page.document.descendants() if isinstance(e, Element) and
                  (e.tag == "textarea" or e.tag == "input" and e.attributes.get("type", "text").lower()
                   in ("text", "search", "email", "password", "url", "tel", "number"))]
        if node in fields:
            tab.focused = fields[(fields.index(node) + 1) % len(fields)]
            self.repaint()

    def submit_form(self, submitter, from_script=False):
        tab = self.current
        form = submitter if submitter.tag == "form" else _form_of(submitter)
        if form is None:
            return
        if not from_script and self._scripts_active(tab):
            init = {"submitter": tab.page.js._h(submitter) if submitter is not form else None}
            if self._js_event(tab, form, "submit", init) or tab.loading or tab is not self.current:
                return
        fields = []
        for el in form.descendants():
            if not isinstance(el, Element) or "name" not in el.attributes or "disabled" in el.attributes:
                continue
            name = el.attributes["name"]
            if el.tag == "input":
                t = el.attributes.get("type", "text").lower()
                if t in ("submit", "button", "reset", "image"):
                    if el is submitter:
                        fields.append((name, el.attributes.get("value", "")))
                    continue
                if t in ("checkbox", "radio"):
                    if el.checked:
                        fields.append((name, el.attributes.get("value", "on")))
                    continue
                if t == "file":
                    continue
                value = el.form_value if el.form_value is not None else el.attributes.get("value", "")
                fields.append((name, value))
            elif el.tag == "select":
                opt = layout.selected_option(el)
                if opt is not None:
                    fields.append((name, opt.attributes.get("value", opt.text_content().strip())))
            elif el.tag == "textarea":
                fields.append((name, el.form_value if el.form_value is not None else el.text_content()))
            elif el.tag == "button" and el is submitter:
                fields.append((name, el.attributes.get("value", "")))
        encoded = urllib.parse.urlencode(fields)
        action = form.attributes.get("action", "")
        try:
            target = tab.page.base_url.resolve(action) if action else network.URL(str(tab.page.url))
        except ValueError as e:
            self.set_status("Cannot submit form: %s" % e)
            return
        method = form.attributes.get("method", "get").lower()
        tab.focused = None
        if method == "post":
            target.fragment = ""
            self.navigate(str(target), method="POST", body=encoded.encode())
        else:
            target.fragment = ""
            target.query = encoded
            target.has_query = True
            self.navigate(str(target))

    # ------------------------------------------------------------ scripts
    def _scripts_active(self, tab):
        page = tab.page
        host = getattr(page, "js", None)
        return host is not None and host.active and not tab.loading

    def _js_event(self, tab, node, kind, init=None):
        """Dispatch a DOM event to the page's scripts (node None = the
        document) and apply whatever they changed. True if cancelled."""
        if not self._scripts_active(tab):
            return False
        init = dict({"bubbles": kind not in ("focus", "blur", "load"), "cancelable": True}, **(init or {}))
        tab.page.js.scroll = tab.scroll
        prevented = tab.page.js.dispatch(node, kind, init)
        self._apply_js(tab)
        return prevented

    def _tick_scripts(self):
        """Run due timers and finished requests for every tab's page."""
        for tab in self.tabs:
            host = getattr(tab.page, "js", None)
            if host is None or not host.active:
                continue
            host.scroll = tab.scroll
            host.tick()
            self._apply_js(tab)

    def _apply_js(self, tab):
        """Pick up what scripts asked for or changed: navigation, form
        submission, focus, title, and DOM changes (restyle + relayout)."""
        page = tab.page
        host = getattr(page, "js", None)
        if host is None:
            return
        if host.disabled_reason and not getattr(host, "reported", False):
            host.reported = True
            if tab is self.current:
                self.set_status("Scripts stopped on this page: %s" % host.disabled_reason)
        if host.alerts:
            kind, msg = host.alerts[-1]
            host.alerts.clear()
            if tab is self.current:
                self.set_status("Page %s: %s" % (kind, msg))
        nav, host.pending_nav = host.pending_nav, None
        if nav is not None:
            if nav[0] == "history":
                if tab is self.current:
                    self.go_back() if nav[1] < 0 else self.go_forward()
            elif nav[1] and tab.index >= 0:          # location.replace()
                tab.history[tab.index]["url"] = nav[0]
                self.navigate(nav[0], push=False, tab=tab)
            else:
                self.navigate(nav[0], tab=tab)
            return
        submit, host.pending_submit = host.pending_submit, None
        if submit is not None and tab is self.current:
            self.submit_form(submit, from_script=True)
            return
        focus, host.pending_focus = host.pending_focus, None
        if focus is not None and focus.tag in ("input", "textarea") and tab is self.current:
            tab.focused = focus
            if not host.dirty:
                self.repaint()
        if not host.dirty and not host.full_restyle and not host.images_dirty:
            return
        if time.monotonic() < getattr(tab, "js_layout_after", 0):
            return                                   # throttled: picked up on a later tick
        roots, full = host.take_changes()
        title = host.title()
        if title and title != tab.title:
            tab.title = page.title = title
            self._render_tabbar()
        width = max(200, self.canvas.winfo_width())
        t0 = time.monotonic()
        if full or page.style_engine is None:
            page.css_sources = []
            engine._load_stylesheets(page, width)
            page.restyle(width)
        else:
            changes = []
            for root in roots:
                page.style_engine.restyle_subtree(root)
                changes += getattr(page.style_engine, "last_changes", [])
            self._animate_changes(tab, changes)
        if host.images_dirty:
            host.images_dirty = False
            gen = tab.generation

            def load_images():
                engine._load_images(page)
                self.results.put(("js-images", tab, gen, page))
            threading.Thread(target=load_images, daemon=True).start()
        if roots or full:
            self._relayout_tab(tab)
        # A page that changes itself constantly must not keep the window busy
        # restyling: batch its next changes for 3x as long as this update took.
        tab.js_layout_after = time.monotonic() + min(3.0, 3 * (time.monotonic() - t0))

    def _relayout_tab(self, tab):
        if tab.page is None:
            return
        if tab is self.current:
            self.relayout()
        else:
            self._layout_tab(tab)

    # ------------------------------------------------------------- chrome
    def _update_chrome(self):
        tab = self.current
        self.address.delete(0, "end")
        self.address.insert(0, tab.url)
        for b, icon, text, enabled in ((self.back_btn, "arrow_back", "←", tab.index > 0),
                                       (self.fwd_btn, "arrow_forward", "→", tab.index < len(tab.history) - 1)):
            b.configure(state="normal" if enabled else "disabled", cursor="hand2" if enabled else "")
            _set_icon(b, icon, text, icons.ICON_COLOR if enabled else icons.DISABLED_COLOR)
            if not enabled:
                b.configure(bg=TOOLBAR_BG)   # drop a hover highlight left from the click that disabled it
        self._update_bookmark_icon()
        self.root.title("%s — %s" % (tab.title or "New Tab", APP_NAME))

    def set_status(self, text):
        self.status.configure(text=text)

    def add_bookmark(self):
        tab = self.current
        if not tab.url or any(b[1] == tab.url for b in self.bookmarks):
            return
        self.bookmarks.append((tab.title or tab.url, tab.url))
        title, url = self.bookmarks[-1]
        self.bookmark_menu.add_command(label=title[:60], command=lambda u=url: self.navigate(u))
        self._update_bookmark_icon()
        self.set_status("Bookmarked %s" % url)

    def _update_bookmark_icon(self):
        """A filled star when the current page is bookmarked, an outline otherwise."""
        marked = any(b[1] == self.current.url for b in self.bookmarks)
        if marked:
            _set_icon(self.bookmark_btn, "star", "★", PROGRESS_COLOR)
        else:
            _set_icon(self.bookmark_btn, "star_border", "☆")

    def show_log(self):
        tab = self.current
        if tab.page is None:
            return
        win = tkinter.Toplevel(self.root)
        win.title("Network log — %s" % tab.url)
        text = tkinter.Text(win, width=110, height=30, font=("DejaVu Sans Mono", 9))
        text.pack(fill="both", expand=True)
        text.insert("end", "\n".join(tab.page.log))
        text.insert("end", "\n\nTimings: %r\nCSS rules: %d\n" % (tab.page.timings, tab.page.rule_count))

    def open_inspector(self):
        tab = self.current
        if tab.page is None:
            return
        Inspector(self, tab)

    def run(self):
        self.root.mainloop()


_DOM_KEYS = {"Return": "Enter", "KP_Enter": "Enter", "BackSpace": "Backspace", "Escape": "Escape",
             "Tab": "Tab", "Up": "ArrowUp", "Down": "ArrowDown", "Left": "ArrowLeft", "Right": "ArrowRight",
             "Prior": "PageUp", "Next": "PageDown", "Home": "Home", "End": "End", "Delete": "Delete",
             "space": " ", "Shift_L": "Shift", "Shift_R": "Shift", "Control_L": "Control",
             "Control_R": "Control", "Alt_L": "Alt", "Alt_R": "Alt"}


def _close_scripts(page):
    """Stop a page's script process when the page goes away."""
    host = getattr(page, "js", None)
    if host is not None:
        host.close()


def _form_of(node):
    for a in node.ancestors():
        if isinstance(a, Element) and a.tag == "form":
            return a
    form_id = node.attributes.get("form")
    if form_id:
        root = node
        while root.parent is not None:
            root = root.parent
        return root.find_by_id(form_id)
    return None


class Inspector:
    """A small DevTools: the DOM tree on the left, computed style and box on the right."""

    SHOWN = ("display", "position", "float", "width", "height", "margin-top", "margin-right", "margin-bottom",
             "margin-left", "padding-top", "padding-right", "padding-bottom", "padding-left", "color",
             "background-color", "font-family", "font-size", "font-weight", "line-height", "text-align",
             "flex-direction", "justify-content", "align-items", "white-space", "-link-href")

    def __init__(self, browser, tab):
        self.browser = browser
        self.tab = tab
        win = self.win = tkinter.Toplevel(browser.root)
        win.title("Inspector — %s" % (tab.title or tab.url))
        win.geometry("900x600")
        pane = ttk.PanedWindow(win, orient="horizontal")
        pane.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(pane, show="tree")
        pane.add(self.tree, weight=3)
        self.info = tkinter.Text(pane, width=45, font=("DejaVu Sans Mono", 9))
        pane.add(self.info, weight=2)
        self.nodes = {}
        self._insert("", tab.page.document)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

    def _insert(self, parent_id, node, depth=0):
        if depth > 60:
            return
        from .dom import Text
        if isinstance(node, Element):
            attrs = " ".join('%s="%s"' % (k, v[:40]) for k, v in list(node.attributes.items())[:4])
            label = "<%s%s>" % (node.tag, " " + attrs if attrs else "")
        elif isinstance(node, Text):
            t = " ".join(node.text.split())
            if not t:
                return
            label = '"%s"' % t[:80]
        else:
            return
        iid = self.tree.insert(parent_id, "end", text=label, open=depth < 2)
        self.nodes[iid] = node
        for c in node.children:
            self._insert(iid, c, depth + 1)

    def _on_select(self, event):
        sel = self.tree.selection()
        if not sel:
            return
        node = self.nodes.get(sel[0])
        self.info.delete("1.0", "end")
        if node is None:
            return
        lines = []
        if isinstance(node, Element):
            lines.append("<%s>" % node.tag)
            for k, v in node.attributes.items():
                lines.append("  %s = %s" % (k, v[:60]))
            lines.append("")
        lines.append("Computed style:")
        for prop in self.SHOWN:
            if prop in node.style:
                lines.append("  %-18s %s" % (prop, node.style[prop]))
        pos = self.tab.ctx.positions.get(node) if self.tab.ctx else None
        if pos is not None:
            lines.append("")
            lines.append("Laid out at y = %.0f px" % pos)
            self.browser.scroll_to(max(0, pos - 40))
        self.info.insert("end", "\n".join(lines))


def _set_app_icon(root):
    """Window and taskbar icon: the surfing penguin. A missing file is not fatal."""
    try:
        if os.name == "nt":
            # .ico carries every size, so Windows picks a sharp one per place
            root.iconbitmap(default=os.path.join(ICON_DIR, "netsurfer.ico"))
        photos = [tkinter.PhotoImage(master=root, file=os.path.join(ICON_DIR, "netsurfer-%d.png" % n))
                  for n in (256, 64, 32, 16)]
        root.iconphoto(True, *photos)
        root._app_icons = photos   # keep them alive
    except (tkinter.TclError, OSError):
        pass


def main(argv=None):
    import sys
    if sys.platform == "win32":
        try:   # group our windows under our own taskbar icon, not python's
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("NetSurfer.Browser")
        except (AttributeError, OSError):
            pass
    argv = argv if argv is not None else sys.argv[1:]
    sys.setrecursionlimit(20000)
    url = argv[0] if argv else None
    Browser(url).run()
