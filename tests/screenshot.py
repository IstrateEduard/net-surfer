"""Headless screenshot harness: python3 tests/screenshot.py URL out.png [width height] [scroll]

Runs the real browser window (under Xvfb in CI), waits for the page to
load, then captures the window. Used to check rendering on test pages.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.setrecursionlimit(20000)

from PIL import ImageGrab  # noqa: E402

from browser.gui import Browser  # noqa: E402


def main():
    url, out = sys.argv[1], sys.argv[2]
    w = int(sys.argv[3]) if len(sys.argv) > 3 else 1024
    h = int(sys.argv[4]) if len(sys.argv) > 4 else 768
    scroll = float(sys.argv[5]) if len(sys.argv) > 5 else 0
    b = Browser(url, w, h)
    state = {"ticks": 0}

    def check():
        state["ticks"] += 1
        tab = b.current
        if (not tab.loading and tab.display_list is not None) or state["ticks"] > 600:
            if scroll:
                b.scroll_to(scroll)
            b.root.after(400, grab)
        else:
            b.root.after(100, check)

    def grab():
        b.root.update()
        x, y = b.root.winfo_rootx(), b.root.winfo_rooty()
        img = ImageGrab.grab(bbox=(x, y, x + b.root.winfo_width(), y + b.root.winfo_height()),
                             xdisplay=os.environ.get("DISPLAY"))
        img.save(out)
        print("status:", b.status.cget("text"))
        print("height:", b.current.doc_layout.height if b.current.doc_layout else None)
        b.close()

    b.root.after(300, check)
    b.run()


if __name__ == "__main__":
    main()
