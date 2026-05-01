"""Main application window with modern Sidebar layout."""
from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, ttk, messagebox
from typing import Optional

import customtkinter as ctk

from .. import config, logger as app_logger
from ..crypto_utils import Vault
from ..excel_io import (
    Client, ClientResult, create_sample_excel, read_clients,
)
from ..orchestrator import BatchOptions, run_batch
from .captcha_dialog import prompt_manual_captcha

log = logging.getLogger("gstr2b.gui.main")

# --- Modern Theme Colors ---
CLR_SIDEBAR = "#1F2937"  # Deep grey-blue
CLR_BG = "#F3F4F6"       # Light grey background
CLR_ACCENT = "#2563EB"   # Professional Blue
CLR_TEXT_MAIN = "#111827"
CLR_TEXT_SEC = "#4B5563"

class MainWindow(ctk.CTk):
    def __init__(self, vault: Vault) -> None:
        super().__init__()
        self.vault = vault
        self.title(f"{config.APP_NAME} v{config.APP_VERSION}")
        self.geometry("1200x800")
        self.minsize(1000, 700)
        
        # Appearance
        ctk.set_appearance_mode("Light")
        
        # State
        self._clients: list[Client] = []
        self._row_to_client: dict[str, Client] = {}
        self._cancel_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        
        self._captcha_request: Optional[tuple[bytes, int, str]] = None
        self._captcha_response: Optional[str] = None
        self._captcha_event = threading.Event()
        
        self.settings = config.load_settings()
        self._gui_log_queue = app_logger.get_gui_queue()

        self._build_layout()
        self._poll_log_queue()
        self._poll_captcha_request()
        
        # Initial status
        self._log_msg("Ready to process GSTR-2B.")

    def _build_layout(self):
        # Main Grid: Sidebar (Left) + Content (Right)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # --- Sidebar ---
        self.sidebar = ctk.CTkFrame(self, width=200, corner_radius=0, fg_color=CLR_SIDEBAR)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_rowconfigure(5, weight=1)

        logo_label = ctk.CTkLabel(self.sidebar, text="GSTR-2B PRO", 
                                  font=ctk.CTkFont(size=22, weight="bold"), text_color="white")
        logo_label.grid(row=0, column=0, padx=20, pady=(20, 10))
        
        sub_label = ctk.CTkLabel(self.sidebar, text="Professional Edition", 
                                 font=ctk.CTkFont(size=12), text_color="#9CA3AF")
        sub_label.grid(row=1, column=0, padx=20, pady=(0, 20))

        self.btn_dashboard = ctk.CTkButton(self.sidebar, text="Dashboard", 
                                           command=lambda: self._show_frame("dashboard"),
                                           fg_color="transparent", anchor="w")
        self.btn_dashboard.grid(row=2, column=0, padx=10, pady=5, sticky="ew")

        self.btn_clients = ctk.CTkButton(self.sidebar, text="Client List", 
                                         command=lambda: self._show_frame("clients"),
                                         fg_color="transparent", anchor="w")
        self.btn_clients.grid(row=3, column=0, padx=10, pady=5, sticky="ew")

        self.btn_settings = ctk.CTkButton(self.sidebar, text="Settings", 
                                          command=lambda: self._show_frame("settings"),
                                          fg_color="transparent", anchor="w")
        self.btn_settings.grid(row=4, column=0, padx=10, pady=5, sticky="ew")

        # --- Content Area ---
        self.content = ctk.CTkFrame(self, fg_color=CLR_BG, corner_radius=0)
        self.content.grid(row=0, column=1, sticky="nsew")
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)

        self._frames = {}
        self._build_dashboard()
        self._build_clients()
        self._build_settings()

        self._show_frame("dashboard")

    def _show_frame(self, name: str):
        for f in self._frames.values():
            f.grid_remove()
        self._frames[name].grid(row=0, column=0, sticky="nsew")
        
        # Highlight sidebar button
        for b, n in [(self.btn_dashboard, "dashboard"), (self.btn_clients, "clients"), (self.btn_settings, "settings")]:
            b.configure(fg_color=CLR_ACCENT if n == name else "transparent")

    # --- Frame Builders ---

    def _build_dashboard(self):
        f = ctk.CTkFrame(self.content, fg_color="transparent")
        self._frames["dashboard"] = f
        f.grid_columnconfigure(0, weight=1)
        f.grid_rowconfigure(1, weight=1)

        # Header
        head = ctk.CTkFrame(f, fg_color="white", corner_radius=8)
        head.grid(row=0, column=0, padx=20, pady=20, sticky="ew")
        
        ctk.CTkLabel(head, text="Process Downloads", font=ctk.CTkFont(size=18, weight="bold")).pack(side="left", padx=20, pady=15)
        
        self.start_btn = ctk.CTkButton(head, text="Start Batch", fg_color=CLR_ACCENT, command=self._on_start)
        self.start_btn.pack(side="right", padx=10)
        
        self.stop_btn = ctk.CTkButton(head, text="Stop", fg_color="#EF4444", command=self._on_stop, state="disabled")
        self.stop_btn.pack(side="right", padx=10)

        # Middle: Log area
        log_frame = ctk.CTkFrame(f, fg_color="white", corner_radius=8)
        log_frame.grid(row=1, column=0, padx=20, pady=(0, 20), sticky="nsew")
        log_frame.grid_columnconfigure(0, weight=1)
        log_frame.grid_rowconfigure(0, weight=1)

        self.log_box = ctk.CTkTextbox(log_frame, font=ctk.CTkFont(family="Consolas", size=11))
        self.log_box.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        self.log_box.configure(state="disabled")

        # Bottom: Progress
        prog_frame = ctk.CTkFrame(f, fg_color="white", corner_radius=8)
        prog_frame.grid(row=2, column=0, padx=20, pady=(0, 20), sticky="ew")
        
        self.progress = ctk.CTkProgressBar(prog_frame, height=12)
        self.progress.set(0)
        self.progress.pack(fill="x", padx=20, pady=(15, 5))
        
        self.progress_lbl = ctk.CTkLabel(prog_frame, text="Idle", font=ctk.CTkFont(size=12))
        self.progress_lbl.pack(pady=(0, 10))

    def _build_clients(self):
        f = ctk.CTkFrame(self.content, fg_color="transparent")
        self._frames["clients"] = f
        f.grid_columnconfigure(0, weight=1)
        f.grid_rowconfigure(1, weight=1)

        # Toolbar
        tool = ctk.CTkFrame(f, fg_color="white", corner_radius=8)
        tool.grid(row=0, column=0, padx=20, pady=20, sticky="ew")
        
        ctk.CTkButton(tool, text="Load Excel", command=self._on_load_excel).pack(side="left", padx=15, pady=10)
        
        ctk.CTkLabel(tool, text="Year:").pack(side="left", padx=(20, 5))
        self.year_var = ctk.StringVar(value=str(datetime.now().year))
        ctk.CTkOptionMenu(tool, values=[str(y) for y in range(2020, 2030)], variable=self.year_var, width=100).pack(side="left")

        ctk.CTkLabel(tool, text="Month:").pack(side="left", padx=(20, 5))
        self.month_var = ctk.StringVar(value=_default_month_name())
        ctk.CTkOptionMenu(tool, values=config.MONTHS, variable=self.month_var, width=130).pack(side="left")

        # Table
        tbl_frame = ctk.CTkFrame(f, fg_color="white", corner_radius=8)
        tbl_frame.grid(row=1, column=0, padx=20, pady=(0, 20), sticky="nsew")
        tbl_frame.grid_columnconfigure(0, weight=1)
        tbl_row = 0
        
        style = ttk.Style()
        style.configure("Treeview", font=("Segoe UI", 10), rowheight=30)
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        
        cols = ("sel", "sr", "name", "gstin", "status", "email")
        self.tree = ttk.Treeview(tbl_frame, columns=cols, show="headings")
        self.tree.heading("sel", text="✓")
        self.tree.heading("sr", text="Sr")
        self.tree.heading("name", text="Client Name")
        self.tree.heading("gstin", text="GSTIN")
        self.tree.heading("status", text="Status")
        self.tree.heading("email", text="Email Status")
        
        self.tree.column("sel", width=40, anchor="center")
        self.tree.column("sr", width=40, anchor="center")
        self.tree.column("name", width=300)
        self.tree.column("gstin", width=160)
        self.tree.column("status", width=120)
        self.tree.column("email", width=120)
        
        self.tree.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        self.tree.bind("<Button-1>", self._on_tree_click)
        
        sb = ttk.Scrollbar(tbl_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.grid(row=0, column=1, sticky="ns")

    def _build_settings(self):
        f = ctk.CTkFrame(self.content, fg_color="transparent")
        self._frames["settings"] = f
        f.grid_columnconfigure(0, weight=1)

        s_box = ctk.CTkScrollableFrame(f, fg_color="white", corner_radius=8, label_text="Application & Mail Settings")
        s_box.grid(row=0, column=0, padx=20, pady=20, sticky="nsew")
        s_box.grid_columnconfigure(1, weight=1)

        # Concurrency
        row = 0
        ctk.CTkLabel(s_box, text="Simultaneous Browsers:", font=ctk.CTkFont(weight="bold")).grid(row=row, column=0, padx=20, pady=10, sticky="w")
        self.thread_var = ctk.StringVar(value=str(self.settings["threads"]))
        ctk.CTkOptionMenu(s_box, values=["1", "2", "3", "4", "5"], variable=self.thread_var).grid(row=row, column=1, padx=20, pady=10, sticky="w")

        # SMTP
        row += 1
        ctk.CTkLabel(s_box, text="Email Settings", font=ctk.CTkFont(size=16, weight="bold")).grid(row=row, column=0, columnspan=2, padx=20, pady=(20, 10), sticky="w")
        
        row += 1
        ctk.CTkLabel(s_box, text="SMTP Server:").grid(row=row, column=0, padx=20, pady=5, sticky="w")
        self.smtp_host = ctk.CTkEntry(s_box, width=300); self.smtp_host.grid(row=row, column=1, padx=20, pady=5, sticky="w")
        self.smtp_host.insert(0, self.settings["smtp_server"])

        row += 1
        ctk.CTkLabel(s_box, text="SMTP Port:").grid(row=row, column=0, padx=20, pady=5, sticky="w")
        self.smtp_port = ctk.CTkEntry(s_box, width=100); self.smtp_port.grid(row=row, column=1, padx=20, pady=5, sticky="w")
        self.smtp_port.insert(0, str(self.settings["smtp_port"]))

        row += 1
        ctk.CTkLabel(s_box, text="Your Email (User):").grid(row=row, column=0, padx=20, pady=5, sticky="w")
        self.smtp_user = ctk.CTkEntry(s_box, width=300); self.smtp_user.grid(row=row, column=1, padx=20, pady=5, sticky="w")
        self.smtp_user.insert(0, self.settings["smtp_user"])

        row += 1
        ctk.CTkLabel(s_box, text="App Password:").grid(row=row, column=0, padx=20, pady=5, sticky="w")
        self.smtp_pass = ctk.CTkEntry(s_box, width=300, show="*"); self.smtp_pass.grid(row=row, column=1, padx=20, pady=5, sticky="w")
        self.smtp_pass.insert(0, self.settings["smtp_pass"])

        row += 1
        self.auto_mail_var = ctk.BooleanVar(value=self.settings["auto_send_email"])
        ctk.CTkCheckBox(s_box, text="Automatically Email clients after successful download", variable=self.auto_mail_var).grid(row=row, column=0, columnspan=2, padx=20, pady=20, sticky="w")

        row += 1
        ctk.CTkButton(s_box, text="Save Settings", fg_color="#10B981", command=self._on_save_settings).grid(row=row, column=0, columnspan=2, padx=20, pady=20)

    # --- Handlers ---

    def _on_save_settings(self):
        self.settings["threads"] = int(self.thread_var.get())
        self.settings["smtp_server"] = self.smtp_host.get()
        self.settings["smtp_port"] = int(self.smtp_port.get() or 465)
        self.settings["smtp_user"] = self.smtp_user.get()
        self.settings["smtp_pass"] = self.smtp_pass.get()
        self.settings["auto_send_email"] = self.auto_mail_var.get()
        config.save_settings(self.settings)
        messagebox.showinfo("Saved", "Settings updated successfully!")

    def _on_load_excel(self):
        path = filedialog.askopenfilename(filetypes=[("Excel Files", "*.xlsx *.xls")])
        if not path: return
        try:
            self._clients = read_clients(Path(path))
            for r in self.tree.get_children(): self.tree.delete(r)
            self._row_to_client.clear()
            for c in self._clients:
                rid = self.tree.insert("", "end", values=("☑", c.sr_no, c.name, c.gstin, "Pending", "Skipped"))
                self._row_to_client[rid] = c
            log.info("Loaded %d clients.", len(self._clients))
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _on_start(self):
        selected = []
        for rid in self.tree.get_children():
            if self.tree.item(rid, "values")[0] == "☑":
                selected.append(self._row_to_client[rid])
        
        if not selected:
            messagebox.showwarning("Select Clients", "Please select at least one client from the 'Client List' tab.")
            return

        self._show_frame("dashboard")
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.progress.set(0)
        self._cancel_event.clear()

        opts = BatchOptions(
            year=int(self.year_var.get()),
            month=config.MONTH_NUMBER[self.month_var.get()],
            base_download_dir=config.DOWNLOADS_DIR,
            threads=int(self.thread_var.get()),
            auto_send_email=self.auto_mail_var.get(),
            settings=self.settings,
            cancel_event=self._cancel_event
        )

        self._worker = threading.Thread(target=self._run_worker, args=(selected, opts), daemon=True)
        self._worker.start()

    def _run_worker(self, clients, opts):
        total = len(clients)
        done = [0]
        
        def status_cb(res):
            done[0] += 1
            self.after(0, self._apply_result, res, done[0], total)

        def manual_cb(img, attempt, name):
            self._captcha_request = (img, attempt, name)
            self._captcha_event.clear()
            if not self._captcha_event.wait(timeout=30): return None
            return self._captcha_response

        try:
            run_batch(clients, opts, on_status=status_cb, manual_captcha=manual_cb)
            self.after(0, lambda: self._log_msg("Batch completed! Check Reports folder."))
        except Exception as e:
            log.exception("Worker failed")
            self.after(0, lambda: messagebox.showerror("Error", f"Batch failed: {e}"))
        finally:
            self.after(0, self._on_finish)

    def _apply_result(self, res, done, total):
        for rid in self.tree.get_children():
            c = self._row_to_client[rid]
            if c.gstin == res.client.gstin:
                self.tree.set(rid, "status", res.status)
                self.tree.set(rid, "email", res.email_status)
                break
        self.progress.set(done/total)
        self.progress_lbl.configure(text=f"Processed {done}/{total} clients")

    def _on_finish(self):
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")

    def _on_stop(self):
        self._cancel_event.set()
        self._log_msg("Stopping batch...")

    def _on_tree_click(self, event):
        region = self.tree.identify("region", event.x, event.y)
        if region != "cell": return
        col = self.tree.identify_column(event.x)
        if col != "#1": return
        rid = self.tree.identify_row(event.y)
        vals = list(self.tree.item(rid, "values"))
        vals[0] = "☐" if vals[0] == "☑" else "☑"
        self.tree.item(rid, values=vals)

    def _log_msg(self, msg: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _poll_log_queue(self):
        try:
            while True:
                line = self._gui_log_queue.get_nowait()
                self._log_msg(line)
        except queue.Empty: pass
        self.after(100, self._poll_log_queue)

    def _poll_captcha_request(self):
        if self._captcha_request:
            img, attempt, name = self._captcha_request
            self._captcha_request = None
            self._captcha_response = prompt_manual_captcha(self, img, attempt, name)
            self._captcha_event.set()
        self.after(200, self._poll_captcha_request)

def _default_month_name():
    m = datetime.now().month
    m = 12 if m == 1 else m - 1
    return config.MONTHS[m - 1]
