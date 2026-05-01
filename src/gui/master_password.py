"""Master-password dialog: first-time setup and unlock."""
from __future__ import annotations

import logging
from typing import Optional

import customtkinter as ctk

from .. import crypto_utils
from ..crypto_utils import Vault

log = logging.getLogger("gstr2b.gui.master_password")


class MasterPasswordDialog(ctk.CTkToplevel):
    """Modal dialog. After close, .vault is set on success or None on cancel."""

    def __init__(self, parent: ctk.CTk) -> None:
        super().__init__(parent)
        self.title("GSTR-2B Downloader — Master Password")
        self.geometry("460x320")
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)

        self.vault: Optional[Vault] = None
        self._first_time = not crypto_utils.vault_exists()

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        # Center on parent
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(x, 50)}+{max(y, 50)}")

        self._password_entry.focus_set()

    def _build_ui(self) -> None:
        title_text = "Set a Master Password" if self._first_time else "Enter Master Password"
        title = ctk.CTkLabel(
            self, text=title_text,
            font=ctk.CTkFont(size=18, weight="bold"),
        )
        title.pack(pady=(20, 6))

        subtitle_text = (
            "This password protects all client credentials.\n"
            "You will need it every time you launch the tool."
            if self._first_time
            else "Enter the master password you set up earlier."
        )
        subtitle = ctk.CTkLabel(
            self, text=subtitle_text, justify="center",
            text_color=("gray30", "gray70"),
        )
        subtitle.pack(pady=(0, 16))

        form = ctk.CTkFrame(self, fg_color="transparent")
        form.pack(fill="x", padx=40)

        self._password_entry = ctk.CTkEntry(
            form, show="•", placeholder_text="Master password", height=36,
        )
        self._password_entry.pack(fill="x", pady=4)
        self._password_entry.bind("<Return>", lambda _e: self._submit())

        if self._first_time:
            self._confirm_entry = ctk.CTkEntry(
                form, show="•", placeholder_text="Confirm master password", height=36,
            )
            self._confirm_entry.pack(fill="x", pady=4)
            self._confirm_entry.bind("<Return>", lambda _e: self._submit())
        else:
            self._confirm_entry = None

        self._error_label = ctk.CTkLabel(
            self, text="", text_color="#E74C3C",
        )
        self._error_label.pack(pady=(8, 0))

        button_row = ctk.CTkFrame(self, fg_color="transparent")
        button_row.pack(pady=18)

        ctk.CTkButton(
            button_row, text="Cancel", width=120, fg_color="gray", command=self._on_cancel,
        ).pack(side="left", padx=6)
        action_text = "Create Vault" if self._first_time else "Unlock"
        ctk.CTkButton(
            button_row, text=action_text, width=140, command=self._submit,
        ).pack(side="left", padx=6)

    def _submit(self) -> None:
        pwd = self._password_entry.get()
        if not pwd:
            self._show_error("Master password cannot be empty.")
            return

        if self._first_time:
            confirm = self._confirm_entry.get() if self._confirm_entry else ""
            if pwd != confirm:
                self._show_error("Passwords do not match.")
                return
            if len(pwd) < 8:
                self._show_error("Use at least 8 characters.")
                return
            try:
                self.vault = crypto_utils.create_vault(pwd)
            except Exception as exc:  # noqa: BLE001
                log.exception("vault create failed")
                self._show_error(f"Could not create vault: {exc}")
                return
        else:
            try:
                self.vault = crypto_utils.unlock_vault(pwd)
            except ValueError:
                self._show_error("Wrong master password.")
                self._password_entry.delete(0, "end")
                self._password_entry.focus_set()
                return
            except Exception as exc:  # noqa: BLE001
                log.exception("vault unlock failed")
                self._show_error(f"Could not unlock vault: {exc}")
                return

        self.destroy()

    def _on_cancel(self) -> None:
        self.vault = None
        self.destroy()

    def _show_error(self, text: str) -> None:
        self._error_label.configure(text=text)


def prompt_master_password(parent: ctk.CTk) -> Optional[Vault]:
    """Block until the user finishes the dialog. Returns Vault or None."""
    dlg = MasterPasswordDialog(parent)
    parent.wait_window(dlg)
    return dlg.vault
