"""Application entry point."""
from __future__ import annotations

import sys

import customtkinter as ctk

from . import config
from .crypto_utils import vault_exists
from .gui.main_window import MainWindow
from .gui.master_password import prompt_master_password
from .logger import setup_logging


def main() -> int:
    config.ensure_dirs()
    log = setup_logging()
    log.info("Starting %s v%s", config.APP_NAME, config.APP_VERSION)

    ctk.set_appearance_mode("light")
    ctk.set_default_color_theme("blue")

    # Master password gate. We need a Tk root to host the dialog.
    root = ctk.CTk()
    root.withdraw()
    root.title(config.APP_NAME)
    # Center the (still hidden) root so the dialog has a known anchor
    root.geometry("400x300+400+200")

    vault = prompt_master_password(root)
    if vault is None:
        log.info("Master password dialog cancelled — exiting.")
        root.destroy()
        return 0

    # Hand off to the main window. We destroy the placeholder root and
    # create the actual app window.
    root.destroy()

    app = MainWindow(vault=vault)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
