# Double-click launcher for Windows (.pyw runs without a console window).
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from browser.gui import main  # noqa: E402

main([])
