"""Central configuration for the GSTR-2B Downloader."""
from __future__ import annotations

import sys
import json
from pathlib import Path

def app_root() -> Path:
    """Return the directory where the running .exe (or main.py) lives."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent

APP_NAME = "GSTR-2B Pro"
APP_VERSION = "2.0.0"

ROOT_DIR = app_root()
DATA_DIR = ROOT_DIR / "data"
DOWNLOADS_DIR = ROOT_DIR / "GSTR-2B"
REPORTS_DIR = ROOT_DIR / "Reports"
LOGS_DIR = ROOT_DIR / "logs"
SCREENSHOTS_DIR = LOGS_DIR / "screenshots"
SAMPLE_EXCEL = ROOT_DIR / "sample_clients.xlsx"
SETTINGS_FILE = DATA_DIR / "settings.json"
VAULT_FILE = DATA_DIR / "vault.dat"

# GST portal endpoints
GST_LOGIN_URL = "https://services.gst.gov.in/services/login"
GST_DASHBOARD_URL = "https://services.gst.gov.in/services/auth/dashboard"
GST_RETURNS_DASHBOARD_URL = "https://return.gst.gov.in/returns/auth/dashboard"

# Polite-automation timing (seconds)
HUMAN_DELAY_MIN = 0.1
HUMAN_DELAY_MAX = 0.3
PAGE_LOAD_TIMEOUT_MS = 60_000
ELEMENT_TIMEOUT_MS = 20_000

# CAPTCHA
CAPTCHA_OCR_RETRIES = 3
CAPTCHA_LENGTH = 6

MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]

MONTH_NUMBER = {name: i + 1 for i, name in enumerate(MONTHS)}

def ensure_dirs() -> None:
    """Create all data folders if missing."""
    for d in (DATA_DIR, DOWNLOADS_DIR, REPORTS_DIR, LOGS_DIR, SCREENSHOTS_DIR):
        d.mkdir(parents=True, exist_ok=True)

def fy_string_for(year: int, month: int) -> str:
    """Indian financial year string for a given month/year, e.g. 2025-26."""
    if month >= 4:
        start = year
    else:
        start = year - 1
    return f"{start}-{str(start + 1)[-2:]}"

def month_label(year: int, month: int) -> str:
    """Folder label like 'Apr-2025'."""
    months_short = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    return f"{months_short[month - 1]}-{year}"

# --- Settings Management ---

DEFAULT_SETTINGS = {
    "threads": 3,
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 465,
    "smtp_user": "",
    "smtp_pass": "",
    "sender_name": "GST Returns Dept",
    "email_subject": "GSTR-2B Statement for {month}-{year}",
    "email_body": "Dear {client_name},\n\nPlease find attached the auto-drafted ITC statement (GSTR-2B) for the period {month}-{year}.\n\nRegards,\nYour CA Firm",
    "auto_send_email": False
}

def load_settings():
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r") as f:
                return {**DEFAULT_SETTINGS, **json.load(f)}
        except:
            return DEFAULT_SETTINGS
    return DEFAULT_SETTINGS

def save_settings(s):
    ensure_dirs()
    with open(SETTINGS_FILE, "w") as f:
        json.dump(s, f, indent=4)
