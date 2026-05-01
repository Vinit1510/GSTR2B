"""Bulk download orchestrator with Multi-threading + Resume + Mailing.

Now supports parallel processing with a staggered start to avoid portal blocks.
"""
from __future__ import annotations

import logging
import threading
import time
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from . import config
from .captcha_solver import solve_captcha
from .excel_io import Client, ClientResult, write_report, report_filename
from .mailer import send_gstr2b_email
from .gst_portal import (
    CaptchaFailedError,
    DownloadError,
    GstSession,
    LoginFailedError,
    NavigationError,
    NoDataAvailableError,
    PortalError,
    WrongPasswordError,
    playwright_session,
)

log = logging.getLogger("gstr2b.orchestrator")

# Callbacks
ManualCaptchaCb = Callable[[bytes, int, str], Optional[str]]
StatusUpdateCb = Callable[[ClientResult], None]

@dataclass
class BatchOptions:
    year: int
    month: int
    base_download_dir: Path
    headless: bool = True
    max_captcha_attempts: int = 3
    skip_existing: bool = True
    cancel_event: Optional[threading.Event] = None
    threads: int = 3
    auto_send_email: bool = False
    settings: Optional[dict] = None

def _client_target_path(opts: BatchOptions, client: Client) -> Path:
    fy = config.fy_string_for(opts.year, opts.month)
    month_lbl = config.month_label(opts.year, opts.month)
    folder = opts.base_download_dir / fy / month_lbl / client.safe_folder_name()
    filename = f"{client.safe_folder_name()}_{month_lbl}_GSTR2B.xlsx"
    return folder / filename

def _process_one(
    client: Client,
    opts: BatchOptions,
    pw,
    manual_captcha_cb: Optional[ManualCaptchaCb],
) -> ClientResult:
    result = ClientResult(client=client)
    result.started_at = datetime.now().strftime("%H:%M:%S")
    target_file = _client_target_path(opts, client)

    if opts.skip_existing and target_file.exists() and target_file.stat().st_size > 0:
        result.status = "Already Downloaded"
        result.file_path = str(target_file)
        
        # Still try to send email if requested
        if opts.auto_send_email and client.email:
            log.info("[%s] Sending email for existing file...", client.name)
            ok = send_gstr2b_email(client.name, client.email, target_file, opts.month, opts.year, opts.settings)
            result.email_status = "Sent" if ok else "Failed"
            
        result.finished_at = datetime.now().strftime("%H:%M:%S")
        return result

    log.info("[%s] starting", client.name)

    sess: Optional[GstSession] = None
    try:
        sess_cm = GstSession(
            pw,
            target_file.parent,
            headless=opts.headless,
            screenshot_dir=config.SCREENSHOTS_DIR,
            client_name=client.name,
        )
        with sess_cm as sess:
            sess.open_login_page()
            sess.enter_username(client.user_id)

            login_done = False
            last_error: Exception | None = None
            for attempt in range(1, opts.max_captcha_attempts + 1):
                if opts.cancel_event and opts.cancel_event.is_set():
                    raise RuntimeError("Cancelled by user.")

                img = sess.fetch_captcha_image()
                solved = solve_captcha(img)

                captcha_text: Optional[str] = solved
                if not captcha_text and manual_captcha_cb:
                    captcha_text = manual_captcha_cb(img, attempt, client.name)
                    if not captcha_text:
                        raise CaptchaFailedError("User cancelled manual CAPTCHA entry.")
                elif not captcha_text:
                    sess.refresh_captcha()
                    continue

                try:
                    sess.submit_login(client.password, captcha_text)
                    login_done = True
                    break
                except CaptchaFailedError as exc:
                    last_error = exc
                    sess.refresh_captcha()
                    continue
                except WrongPasswordError as exc:
                    raise exc

            if not login_done:
                raise CaptchaFailedError(f"CAPTCHA failed after {opts.max_captcha_attempts} attempts")

            sess.navigate_to_returns_dashboard()
            sess.select_period(opts.year, opts.month)
            sess.open_gstr2b_view()
            saved = sess.download_gstr2b_excel(target_file)
            
            result.status = "Success"
            result.file_path = str(saved)
            
            # --- Mailing ---
            if opts.auto_send_email and client.email:
                log.info("[%s] Sending email to %s...", client.name, client.email)
                ok = send_gstr2b_email(client.name, client.email, saved, opts.month, opts.year, opts.settings)
                result.email_status = "Sent" if ok else "Failed"

    except NoDataAvailableError as exc:
        result.status = "No Data Available"
        result.error_reason = str(exc)
    except WrongPasswordError as exc:
        result.status = "Wrong Password"
        result.error_reason = str(exc)
    except CaptchaFailedError as exc:
        result.status = "CAPTCHA Failed"
        result.error_reason = str(exc)
    except (LoginFailedError, NavigationError, DownloadError, PortalError) as exc:
        result.status = "Portal Error"
        result.error_reason = str(exc)
    except Exception as exc:
        result.status = "Portal Error"
        result.error_reason = f"Unexpected: {exc}"
        log.exception("[%s] UNEXPECTED ERROR", client.name)

    result.finished_at = datetime.now().strftime("%H:%M:%S")
    return result

def run_batch(
    clients: list[Client],
    opts: BatchOptions,
    on_status: Optional[StatusUpdateCb] = None,
    manual_captcha: Optional[ManualCaptchaCb] = None,
) -> tuple[list[ClientResult], Path]:
    """Process clients using a ThreadPoolExecutor for multi-threading."""
    config.ensure_dirs()
    opts.base_download_dir.mkdir(parents=True, exist_ok=True)

    log.info("Batch starting: %d clients, %d threads, %d/%d",
             len(clients), opts.threads, opts.month, opts.year)

    results_dict: dict[int, ClientResult] = {}
    
    with playwright_session() as pw:
        # Use ThreadPoolExecutor for parallel processing
        with ThreadPoolExecutor(max_workers=opts.threads) as executor:
            future_to_client = {}
            
            for i, client in enumerate(clients):
                if opts.cancel_event and opts.cancel_event.is_set():
                    break
                
                # STAGGERED START: Avoid burst requests to portal
                if i > 0 and opts.threads > 1:
                    wait_sec = random.uniform(2, 6)
                    log.debug("Staggered start: waiting %.1fs before next thread...", wait_sec)
                    time.sleep(wait_sec)

                future = executor.submit(_process_one, client, opts, pw, manual_captcha)
                future_to_client[future] = client

            for future in as_completed(future_to_client):
                client = future_to_client[future]
                try:
                    res = future.result()
                    results_dict[client.sr_no] = res
                    if on_status:
                        on_status(res)
                except Exception as exc:
                    log.error("[%s] Future crashed: %s", client.name, exc)

    # Sort results by sr_no to match original order
    sorted_results = [results_dict[c.sr_no] for c in clients if c.sr_no in results_dict]
    
    # Write report
    report_path = config.REPORTS_DIR / report_filename(opts.year, opts.month)
    write_report(report_path, sorted_results)
    log.info("Batch finished. Report: %s", report_path)
    return sorted_results, report_path
