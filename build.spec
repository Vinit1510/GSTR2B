# PyInstaller spec for GSTR-2B Downloader
# Build with:  pyinstaller build.spec --clean --noconfirm
#
# Bundles Playwright's Chromium browser inside the .exe so the end-user
# does NOT need to run `playwright install` separately.
# -*- mode: python ; coding: utf-8 -*-

import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

# ---------------------------------------------------------------------------
# Resolve Playwright browser path so we can bundle it.
# ---------------------------------------------------------------------------
def _playwright_browsers_path() -> str:
    # When `playwright install chromium` runs (in CI we do this before build),
    # browsers live under the Playwright registry. We grab that and copy it
    # into the bundle.
    import playwright
    pkg_dir = Path(playwright.__file__).parent
    # Default Windows registry path
    candidates = [
        Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")) if os.environ.get("PLAYWRIGHT_BROWSERS_PATH") else None,
        Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright",
        Path.home() / "AppData" / "Local" / "ms-playwright",
        Path.home() / ".cache" / "ms-playwright",
    ]
    for cand in candidates:
        if cand and cand.exists():
            return str(cand)
    raise FileNotFoundError(
        "Could not locate ms-playwright browsers. Run "
        "`python -m playwright install chromium` before building."
    )


browsers_path = _playwright_browsers_path()

datas = []

# Bundle the entire ms-playwright folder under _internal/ms-playwright
for entry in Path(browsers_path).iterdir():
    datas.append((str(entry), f"ms-playwright/{entry.name}"))

# Bundle ddddocr ONNX models
datas.extend(collect_data_files('ddddocr'))

hiddenimports = [
    "playwright",
    "playwright.sync_api",
    "ddddocr",
    "customtkinter",
    "openpyxl",
    "cryptography",
    "PIL",
    "PIL.Image",
    "cv2",
    "numpy",
]

a = Analysis(
    ["run.py"],
    pathex=[str(Path(SPECPATH))],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=["runtime_hook.py"],
    excludes=[
        # Trim very heavy modules we never use to keep the .exe smaller
        "matplotlib",
        "scipy",
        "tkinter.test",
        "torch.distributions",
        "torch.testing",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="GSTR2B_Pro",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # GUI app — no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join("assets", "icon.ico") if os.path.exists(os.path.join("assets", "icon.ico")) else None,
)
