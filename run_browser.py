#!/usr/bin/env python3
"""Launch the browser:  python3 run_browser.py [URL]"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from browser.gui import main  # noqa: E402

if __name__ == "__main__":
    main()
