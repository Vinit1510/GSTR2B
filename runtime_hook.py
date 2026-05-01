"""PyInstaller runtime hook.

Tells Playwright to look for its bundled Chromium inside the unpacked
PyInstaller bundle (sys._MEIPASS) instead of the user's AppData.
"""
import os
import sys
from pathlib import Path

if hasattr(sys, "_MEIPASS"):
    bundled = Path(sys._MEIPASS) / "ms-playwright"
    if bundled.exists():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(bundled))
