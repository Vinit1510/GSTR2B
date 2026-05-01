"""Top-level launcher used by PyInstaller and dev runs.

Importing src.main keeps the package layout intact when frozen.
"""
from src.main import main

if __name__ == "__main__":
    raise SystemExit(main())
