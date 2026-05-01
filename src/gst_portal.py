"""GST portal automation via Playwright (sync API).

This module is the only place that should know about the portal's HTML.
If GSTN changes the page, only the constants and helpers here need updating.

Real-world flow (verified from live portal April 2026):
    1. open_login_page()
    2. enter_username(user_id)            -> typing username triggers CAPTCHA load
    3. fetch_captcha_image() -> bytes
    4. submit_login(password, captcha_text)
    5. navigate_to_returns_dashboard()    -> CLICK-based: either welcome-page
                                             "RETURN DASHBOARD" button or
                                             top-nav Services > Returns Dashboard.
                                             Direct URL jumps break the GST
                                             portal session, so we always click.
    6. select_period(year, month)         -> FY + Quarter + Period
    7. open_gstr2b_view()                 -> click VIEW on GSTR-2B tile
    8. download_gstr2b_excel(save_path)   -> on summary page click "Download
                                             GSTR-2B details Excel"; if portal
                                             says "no data" raise NoDataAvailableError
"""
from __future__ import annotations

import logging
import random
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from playwright.sync_api import (
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PWTimeout,
    sync_playwright,
)

from . import config

log = logging.getLogger("gstr2b.portal")


# ---------------------------------------------------------------------------
# Selectors. Multiple fallbacks per element so a small portal change doesn't
# break us. Order matters: most specific first.
# ---------------------------------------------------------------------------

SEL_USERNAME = [
    "#username",
    "input[name='username']",
    "input[placeholder*='Username' i]",
]
SEL_PASSWORD = [
    "#user_pass",
    "input[name='user_pass']",
    "input[type='password']",
]
SEL_CAPTCHA_IMAGE = [
    "#imgCaptcha",
    "img[src*='captcha' i]",
    "img.captcha-image",
    "img[alt*='captcha' i]",
    "img[ng-src*='captcha' i]",
]
SEL_CAPTCHA_INPUT = [
    "#captcha",
    "input[name='captcha']",
    "input[placeholder*='captcha' i]",
]
SEL_LOGIN_BUTTON = [
    "button.btn-primary:has-text('LOGIN')",
    "button[type='submit']:has-text('LOGIN')",
    "button:has-text('LOGIN')",
    "input[type='submit'][value='LOGIN' i]",
]
SEL_LOGIN_ERROR = [
    ".alert-danger",
    ".error-msg",
    "#errorMsg",
    "span.err",
    "div[role='alert']",
]
SEL_LOGGED_IN_MARKER = [
    "a:has-text('Logout')",
    "a:has-text('Sign Out')",
    ":text('Welcome')",
    ":text-matches('Last logged in on', 'i')",
]

# Welcome page (services.gst.gov.in/services/auth/fowelcome)
# This is the BIG button on the welcome page that takes us straight to the
# Returns Dashboard. We prefer it because it's a single direct click.
SEL_WELCOME_RETURN_DASHBOARD_BTN = [
    "button:has-text('RETURN DASHBOARD')",
    "a:has-text('RETURN DASHBOARD')",
    "button:has-text('Return Dashboard')",
    "a:has-text('Return Dashboard')",
]

# Top-nav menu fallback: Services dropdown -> Returns Dashboard link
SEL_TOPNAV_SERVICES = [
    "a.dropdown-toggle:has-text('Services')",
    "li.dropdown:has-text('Services') > a",
    "a:has-text('Services')",
]
SEL_TOPNAV_RETURNS_DASHBOARD = [
    "a:has-text('Returns Dashboard')",
    "a:has-text('Return Dashboard')",
]

# Returns dashboard (return.gst.gov.in/returns/auth/dashboard)
SEL_FY_DROPDOWN = [
    "select#fin",
    "select[name='fin']",
]
SEL_QUARTER_DROPDOWN = [
    "select#quarter",
    "select[name='quarter']",
]
SEL_PERIOD_DROPDOWN = [
    "select#mon",
    "select[name='mon']",
]
SEL_SEARCH_BUTTON = [
    "button.btn-primary:has-text('SEARCH')",
    "button:has-text('SEARCH')",
    "input[type='submit'][value='SEARCH' i]",
]

# GSTR-2B tile and its VIEW button.  The portal renders each return form as
# a card with the form name (e.g. "GSTR2B") inside.  We want the VIEW button
# inside that specific card.
SEL_GSTR2B_TILE = [
    "div.panel:has-text('GSTR2B')",
    "div.panel:has-text('Auto - drafted ITC Statement')",
    "div:has(> :text-matches('^GSTR.?2B$', 'i'))",
]
SEL_GSTR2B_VIEW_BUTTON = [
    "button:has-text('VIEW')",
    "a:has-text('VIEW')",
    "input[type='button'][value='VIEW' i]",
]

# GSTR-2B summary page (gstr2b.gst.gov.in/gstr2b/auth/...)
SEL_NO_DATA_MARKERS = [
    "text=/could not be generated/i",
    "text=/no records to generate/i",
    "text=/GSTR-?2B has not been generated/i",
    "text=/data is not available/i",
]
SEL_DOWNLOAD_GSTR2B_EXCEL = [
    "button:has-text('Download GSTR-2B details Excel')",
    "a:has-text('Download GSTR-2B details Excel')",
    "button:has-text('Download GSTR2B details Excel')",
    "a:has-text('Download GSTR2B details Excel')",
    "button:has-text('DOWNLOAD GSTR-2B DETAILS')",
    "button:has-text('Download Excel')",
    "a:has-text('Download Excel')",
    # generic fallback: any button containing both Download and Excel
    "button:text-matches('download.*excel', 'i')",
    "a:text-matches('download.*excel', 'i')",
]

# A second-stage popup that some portal versions use:
#   click "Download Excel" -> popup with "Generate Excel file" -> wait ->
#   "Download Excel File" becomes enabled.
SEL_GENERATE_EXCEL = [
    "button:has-text('GENERATE EXCEL FILE TO DOWNLOAD')",
    "button:has-text('Generate Excel')",
]
SEL_DOWNLOAD_EXCEL_READY = [
    "button:has-text('DOWNLOAD EXCEL FILE')",
    "a:has-text('DOWNLOAD EXCEL FILE')",
    "button:has-text('Click here to download')",
]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PortalError(Exception):
    """Base for all portal failures the orchestrator should map to a status."""


class WrongPasswordError(PortalError):
    pass


class CaptchaFailedError(PortalError):
    pass


class LoginFailedError(PortalError):
    pass


class NavigationError(PortalError):
    pass


class DownloadError(PortalError):
    pass


class NoDataAvailableError(PortalError):
    """GSTR-2B not generated for the period (no purchases, or before 14th)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _SelectorHit:
    selector: str
    locator: any  # playwright Locator


def _human_pause():
    time.sleep(random.uniform(config.HUMAN_DELAY_MIN, config.HUMAN_DELAY_MAX))


def _first_visible(page: Page, selectors: list[str], timeout_ms: int) -> _SelectorHit:
    """Return the first selector whose locator becomes visible."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=400):
                    return _SelectorHit(selector=sel, locator=loc)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        time.sleep(0.25)
    raise PWTimeout(
        f"None of the selectors became visible: {selectors}. Last error: {last_error}"
    )


def _any_visible(page: Page, selectors: list[str], timeout_ms: int = 1500) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        for sel in selectors:
            try:
                if page.locator(sel).first.is_visible(timeout=300):
                    return True
            except Exception:
                pass
        time.sleep(0.2)
    return False


def _select_option_robust(locator, label_candidates: list[str]) -> str:
    """Try each candidate label until one of them works. Returns chosen label."""
    last_err: Exception | None = None
    for label in label_candidates:
        try:
            locator.select_option(label=label)
            return label
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    raise PortalError(
        f"None of the dropdown options matched: {label_candidates}. Last err: {last_err}"
    )


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class GstSession:
    """One browser context per client. Use as a context manager."""

    def __init__(
        self,
        playwright: Playwright,
        download_dir: Path,
        headless: bool = True,
        screenshot_dir: Optional[Path] = None,
        client_name: str = "client",
    ) -> None:
        self._pw = playwright
        self._download_dir = download_dir
        self._headless = headless
        self._screenshot_dir = screenshot_dir
        self._client_name = client_name
        self._browser = None
        self._context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    def __enter__(self) -> "GstSession":
        self._browser = self._pw.chromium.launch(headless=self._headless)
        self._context = self._browser.new_context(
            accept_downloads=True,
            viewport={"width": 1366, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
        )
        self._context.set_default_timeout(config.ELEMENT_TIMEOUT_MS)
        self.page = self._context.new_page()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._context:
                self._context.close()
        finally:
            if self._browser:
                self._browser.close()

    # ------------------ login flow ------------------

    def open_login_page(self) -> None:
        assert self.page is not None
        log.info("Opening GST login page...")
        self.page.goto(config.GST_LOGIN_URL, timeout=config.PAGE_LOAD_TIMEOUT_MS)
        _first_visible(self.page, SEL_USERNAME, config.ELEMENT_TIMEOUT_MS)

    def enter_username(self, user_id: str) -> None:
        """Click username field, type the username, wait for CAPTCHA to render.

        On the GST portal the CAPTCHA image is lazily rendered after the
        username field is focused and filled.  Do this BEFORE trying to
        fetch the CAPTCHA image.
        """
        assert self.page is not None
        u_hit = _first_visible(self.page, SEL_USERNAME, config.ELEMENT_TIMEOUT_MS)
        u_hit.locator.click()
        u_hit.locator.fill("")  # clear in case of pre-fill
        u_hit.locator.type(user_id, delay=40)
        # Push focus away so portal triggers CAPTCHA load
        try:
            self.page.keyboard.press("Tab")
        except Exception:
            pass
        # Wait for CAPTCHA image to actually appear
        try:
            _first_visible(self.page, SEL_CAPTCHA_IMAGE, 12_000)
        except PWTimeout as exc:
            raise PortalError(
                "CAPTCHA image did not appear after username entry."
            ) from exc

    def fetch_captcha_image(self) -> bytes:
        """Return the current CAPTCHA image bytes (PNG)."""
        assert self.page is not None
        hit = _first_visible(self.page, SEL_CAPTCHA_IMAGE, config.ELEMENT_TIMEOUT_MS)
        return hit.locator.screenshot()

    def refresh_captcha(self) -> None:
        """Click the CAPTCHA image to refresh it."""
        assert self.page is not None
        try:
            hit = _first_visible(self.page, SEL_CAPTCHA_IMAGE, 5000)
            hit.locator.click()
            time.sleep(1.2)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not refresh CAPTCHA: %s", exc)

    def submit_login(self, password: str, captcha_text: str) -> None:
        """Fill password + CAPTCHA and click LOGIN.

        Username must already have been entered via enter_username().
        """
        assert self.page is not None
        page = self.page

        p_hit = _first_visible(page, SEL_PASSWORD, config.ELEMENT_TIMEOUT_MS)
        # Clear the password field first (in case of retry)
        p_hit.locator.fill("")
        p_hit.locator.type(password, delay=30)
        _human_pause()

        c_hit = _first_visible(page, SEL_CAPTCHA_INPUT, config.ELEMENT_TIMEOUT_MS)
        c_hit.locator.fill("")
        c_hit.locator.type(captcha_text, delay=30)
        _human_pause()

        btn = _first_visible(page, SEL_LOGIN_BUTTON, config.ELEMENT_TIMEOUT_MS)
        btn.locator.click()

        # Wait for either logged-in marker OR a known error
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            # Success: URL moved to authenticated area
            cur_url = page.url or ""
            if "/auth/" in cur_url or "/services/auth/" in cur_url:
                log.info("Login successful (URL=%s).", cur_url)
                return
            if _any_visible(page, SEL_LOGGED_IN_MARKER, timeout_ms=400):
                log.info("Login successful (marker visible).")
                return
            err_text = self._read_first_visible_text(SEL_LOGIN_ERROR)
            if err_text:
                err_lc = err_text.lower()
                log.warning("Login error message: %s", err_text)
                if "captcha" in err_lc:
                    raise CaptchaFailedError(err_text)
                if ("invalid" in err_lc and ("user" in err_lc or "password" in err_lc)) \
                        or "incorrect" in err_lc:
                    raise WrongPasswordError(err_text)
                if "locked" in err_lc or "blocked" in err_lc:
                    raise LoginFailedError(err_text)
                # Unknown error -> treat as login failed
                raise LoginFailedError(err_text)
            time.sleep(0.5)

        raise LoginFailedError(
            "Login did not complete in time (no auth URL, no error message)."
        )

    # ------------------ post-login navigation ------------------

    def navigate_to_returns_dashboard(self) -> None:
        """Click our way to the File Returns page (NO direct URL jumps).

        The GST portal session is fragile across direct URL navigations to
        a different sub-domain (return.gst.gov.in). It expects a real-user
        navigation: either click the 'RETURN DASHBOARD' button on the
        welcome page, or use the top-nav 'Services > Returns Dashboard'.
        """
        assert self.page is not None
        page = self.page
        log.info("Navigating to Returns Dashboard via clicks...")

        # Strategy 1: welcome-page big button "RETURN DASHBOARD"
        try:
            hit = _first_visible(page, SEL_WELCOME_RETURN_DASHBOARD_BTN, 4000)
            log.info("Clicking welcome-page button: %s", hit.selector)
            hit.locator.click()
            self._wait_for_returns_dashboard()
            return
        except PWTimeout:
            log.info("Welcome-page 'RETURN DASHBOARD' not visible; "
                     "falling back to top-nav menu.")

        # Strategy 2: top-nav menu Services > Returns Dashboard
        try:
            services = _first_visible(page, SEL_TOPNAV_SERVICES,
                                      config.ELEMENT_TIMEOUT_MS)
            # Try hover first (dropdown may open on hover); if that doesn't
            # reveal the link, click the menu to expand it.
            try:
                services.locator.hover()
                time.sleep(0.5)
            except Exception:
                pass

            try:
                rd = _first_visible(page, SEL_TOPNAV_RETURNS_DASHBOARD, 2000)
            except PWTimeout:
                # Hover didn't reveal link; click to expand
                services.locator.click()
                time.sleep(0.5)
                rd = _first_visible(page, SEL_TOPNAV_RETURNS_DASHBOARD,
                                    config.ELEMENT_TIMEOUT_MS)

            log.info("Clicking menu link: %s", rd.selector)
            rd.locator.click()
            self._wait_for_returns_dashboard()
            return
        except PWTimeout as exc:
            raise NavigationError(
                "Could not navigate to Returns Dashboard via clicks "
                "(welcome-page button missing AND Services menu unreachable)."
            ) from exc

    def _wait_for_returns_dashboard(self) -> None:
        """Wait for the File Returns page (FY dropdown visible).

        Some portal versions open the dashboard in a new tab — handle that.
        """
        assert self.page is not None
        try:
            ctx = self.page.context
            if len(ctx.pages) > 1:
                self.page = ctx.pages[-1]
                log.info("Switched to new tab for Returns Dashboard.")
        except Exception:
            pass
        try:
            _first_visible(self.page, SEL_FY_DROPDOWN, config.ELEMENT_TIMEOUT_MS)
        except PWTimeout as exc:
            raise NavigationError(
                "Returns Dashboard FY dropdown did not appear after click."
            ) from exc

    def select_period(self, year: int, month: int) -> None:
        """Select Financial Year, Quarter, Period (month), then click SEARCH."""
        assert self.page is not None
        page = self.page

        fy = config.fy_string_for(year, month)
        log.info("Selecting period FY=%s month=%d/%d", fy, month, year)

        # Financial Year - try multiple label variants
        fy_hit = _first_visible(page, SEL_FY_DROPDOWN, config.ELEMENT_TIMEOUT_MS)
        _select_option_robust(fy_hit.locator, [fy, fy.replace("-", "-")])
        _human_pause()

        # Quarter (mandatory on current portal)
        try:
            q_hit = _first_visible(page, SEL_QUARTER_DROPDOWN, 4000)
            quarter_labels = _quarter_label_candidates(month)
            chosen = _select_option_robust(q_hit.locator, quarter_labels)
            log.info("Quarter selected: %s", chosen)
            _human_pause()
        except PWTimeout:
            log.info("No Quarter dropdown on this layout (skipping).")

        # Period (month)
        m_hit = _first_visible(page, SEL_PERIOD_DROPDOWN, config.ELEMENT_TIMEOUT_MS)
        month_labels = _month_label_candidates(month)
        chosen = _select_option_robust(m_hit.locator, month_labels)
        log.info("Period selected: %s", chosen)
        _human_pause()

        search = _first_visible(page, SEL_SEARCH_BUTTON, config.ELEMENT_TIMEOUT_MS)
        search.locator.click()

        # Wait for tiles to render. We assume the GSTR2B tile will appear.
        time.sleep(2)
        try:
            _first_visible(page, SEL_GSTR2B_TILE + ["text=/GSTR.?2B/i"],
                           config.ELEMENT_TIMEOUT_MS)
        except PWTimeout as exc:
            raise NavigationError(
                "Tiles (including GSTR-2B) did not appear after SEARCH."
            ) from exc

    def open_gstr2b_view(self) -> None:
        """Click the View button inside the GSTR-2B card.

        The File Returns page shows several cards in a grid:
            - GSTR1   : Details of outward supplies
            - GSTR1A  : Amendment of outward supplies
            - GSTR2B  : Auto - drafted ITC Statement   <-- WE WANT THIS
            - GSTR3B  : Monthly Return
            - GSTR2A  : Auto Drafted details (For view only)

        Important DOM facts (from real portal inspection):
          * The button HTML text is literally  "View"  (mixed case).
            The all-caps look comes from CSS `text-transform: uppercase`.
            Therefore string comparisons MUST be case-insensitive.
          * Button class is `btn btn-primary` and has the attribute
            `data-ng-click="page_rtp(x.return_ty,x.due_dt,x.status)"`.
          * The "GSTR2B" sub-label sits in a SIBLING div, not the button's
            parent -- so we must walk several ancestors up to find a card
            that contains BOTH the "GSTR2B" text and the "View" button.
        """
        assert self.page is not None
        page = self.page
        import re as _re
        log.info("Clicking View under GSTR-2B card (Auto-drafted ITC Statement)...")

        last_err: Exception | None = None

        # Selector matching the View button across the portal (case-insensitive).
        view_btn_selector = (
            "button.btn-primary[data-ng-click*='page_rtp'], "
            "button.btn.btn-primary, "
            "button:has-text('View'), "
            "a:has-text('View')"
        )
        view_text_re = _re.compile(r"^\s*view\s*$", _re.I)

        # ------------------------------------------------------------------
        # Strategy A (preferred): use Playwright filter chain to find the
        # smallest div that contains BOTH the text "GSTR2B" and a View
        # button.  `.last` returns the deepest (innermost) match -- i.e. the
        # actual card, not its outer wrapper.
        # ------------------------------------------------------------------
        try:
            card = page.locator("div").filter(
                has_text=_re.compile(r"GSTR\s*-?\s*2B", _re.I),
            ).filter(
                has=page.locator(view_btn_selector).filter(has_text=view_text_re),
            ).last

            if card.count() > 0:
                btn = card.locator(view_btn_selector).filter(
                    has_text=view_text_re,
                ).first
                if btn.is_visible(timeout=3000):
                    btn.scroll_into_view_if_needed(timeout=2000)
                    btn.click()
                    log.info("View clicked via Strategy A (filter chain on GSTR2B card)")
                    self._wait_for_gstr2b_summary()
                    return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            log.debug("Strategy A failed: %s", exc)

        # ------------------------------------------------------------------
        # Strategy B: enumerate every "View" button on the page; for each,
        # walk UP the DOM ancestors one by one until we find one whose
        # text contains "GSTR2B" (and NOT GSTR1/GSTR3B/GSTR2A in the same
        # immediate sub-text).  That ancestor is the card -> click that
        # button.
        # ------------------------------------------------------------------
        try:
            view_btns = page.locator(view_btn_selector).filter(has_text=view_text_re)
            count = view_btns.count()
            log.info("Strategy B: found %d 'View' button(s) on page; "
                     "looking for the one inside the GSTR-2B card", count)

            for i in range(count):
                btn = view_btns.nth(i)
                try:
                    # Walk up to 8 ancestors and inspect their text content.
                    matched = False
                    for depth in range(1, 9):
                        anc = btn.locator(f"xpath=ancestor::*[{depth}]")
                        if anc.count() == 0:
                            break
                        try:
                            text = (anc.inner_text(timeout=1500) or "").upper()
                        except Exception:
                            continue
                        compact = text.replace(" ", "").replace("-", "")
                        if "GSTR2B" not in compact:
                            continue
                        # Reject if this same ancestor also contains another
                        # return type's label -- means we've gone too far up
                        # (we're now at a wrapper that holds multiple cards).
                        other_returns = ("GSTR1", "GSTR1A", "GSTR3B", "GSTR2A")
                        # Strip GSTR2B occurrences before checking siblings
                        stripped = compact.replace("GSTR2B", "")
                        if any(o in stripped for o in other_returns):
                            continue
                        # Found the GSTR-2B card.
                        matched = True
                        break

                    if matched:
                        btn.scroll_into_view_if_needed(timeout=2000)
                        btn.click()
                        log.info(
                            "View clicked via Strategy B "
                            "(button index %d, ancestor depth %d)",
                            i, depth,
                        )
                        self._wait_for_gstr2b_summary()
                        return
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    continue
        except Exception as exc:  # noqa: BLE001
            last_err = exc

        # ------------------------------------------------------------------
        # Strategy C: anchor on "GSTR2B" sub-label and walk up to nearest
        # ancestor that contains ANY View button (case-insensitive XPath).
        # ------------------------------------------------------------------
        try:
            anchor = page.locator(
                "xpath=//*[contains(translate(normalize-space(.),"
                "'abcdefghijklmnopqrstuvwxyz',"
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ'),"
                "'GSTR2B') or contains(translate(normalize-space(.),"
                "'abcdefghijklmnopqrstuvwxyz',"
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'GSTR-2B')]"
            ).last  # the deepest such element is the GSTR2B sub-label

            if anchor.count() > 0:
                card = anchor.locator(
                    "xpath=ancestor::*["
                    ".//button[contains(translate(normalize-space(.),"
                    "'abcdefghijklmnopqrstuvwxyz',"
                    "'ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'VIEW')]"
                    " or .//a[contains(translate(normalize-space(.),"
                    "'abcdefghijklmnopqrstuvwxyz',"
                    "'ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'VIEW')]"
                    "][1]"
                )
                btn = card.locator(view_btn_selector).filter(
                    has_text=view_text_re,
                ).first
                if btn.is_visible(timeout=2000):
                    btn.scroll_into_view_if_needed(timeout=2000)
                    btn.click()
                    log.info("View clicked via Strategy C (XPath anchor walk-up)")
                    self._wait_for_gstr2b_summary()
                    return
        except Exception as exc:  # noqa: BLE001
            last_err = exc

        raise NavigationError(
            "Could not click View under the GSTR-2B (Auto-drafted ITC Statement) "
            f"card. Last error: {last_err}"
        )

    def _wait_for_gstr2b_summary(self) -> None:
        """Wait for either the GSTR-2B summary header or a 'no data' message."""
        assert self.page is not None
        page = self.page
        # The summary page may open as a new tab in some portal versions.
        # Switch to the latest tab if so.
        try:
            ctx = page.context
            if len(ctx.pages) > 1:
                self.page = ctx.pages[-1]
                page = self.page
                log.info("Switched to new tab for GSTR-2B summary.")
        except Exception:
            pass

        # Wait for any of: summary header, no-data message, download button
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if _any_visible(page,
                            SEL_NO_DATA_MARKERS
                            + SEL_DOWNLOAD_GSTR2B_EXCEL
                            + ["text=/AUTO.?DRAFTED ITC STATEMENT/i",
                               "text=/GSTR.?2B/i"],
                            timeout_ms=600):
                return
            time.sleep(0.4)
        raise NavigationError(
            "GSTR-2B summary page did not load (no header, no download, no error)."
        )

    # ------------------ download ------------------

    def download_gstr2b_excel(self, save_path: Path) -> Path:
        """Click 'Download GSTR-2B details Excel' on the summary page.

        Raises NoDataAvailableError if the portal shows the
        'GSTR-2B could not be generated' message.
        """
        assert self.page is not None
        page = self.page

        # First check for "no data" — this is a normal/expected case for
        # current month before 14th, or clients with no purchases.
        if _any_visible(page, SEL_NO_DATA_MARKERS, timeout_ms=2000):
            err_text = self._read_first_visible_text(SEL_NO_DATA_MARKERS)
            log.info("No GSTR-2B data for this period: %s", err_text)
            raise NoDataAvailableError(
                err_text or "GSTR-2B not generated for this period."
            )

        # Click the Download button
        try:
            btn_hit = _first_visible(page, SEL_DOWNLOAD_GSTR2B_EXCEL,
                                     config.ELEMENT_TIMEOUT_MS)
        except PWTimeout as exc:
            # Re-check no-data with a longer timeout in case it appeared late
            if _any_visible(page, SEL_NO_DATA_MARKERS, timeout_ms=3000):
                err_text = self._read_first_visible_text(SEL_NO_DATA_MARKERS)
                raise NoDataAvailableError(
                    err_text or "GSTR-2B not generated for this period."
                ) from exc
            raise DownloadError(
                "'Download GSTR-2B details Excel' button not found on summary page."
            ) from exc

        save_path.parent.mkdir(parents=True, exist_ok=True)

        # Some portal versions stream the file straight to a download.
        # Others first show a popup with "Generate Excel" then "Download".
        # Try the streaming path first, then fall back to two-step popup.
        try:
            with page.expect_download(timeout=15_000) as dl_info:
                btn_hit.locator.click()
            download = dl_info.value
            download.save_as(str(save_path))
            log.info("Saved (direct): %s", save_path)
            return save_path
        except PWTimeout:
            log.info("Direct download did not start; trying generate-then-download flow.")

        # Two-step popup: GENERATE then DOWNLOAD
        try:
            gen = _first_visible(page, SEL_GENERATE_EXCEL, 8000)
            gen.locator.click()
            log.info("Clicked GENERATE EXCEL; waiting for portal to prepare file...")
        except PWTimeout:
            log.info("No GENERATE button visible; will wait for ready button.")

        deadline = time.monotonic() + 120
        ready_btn = None
        while time.monotonic() < deadline:
            try:
                ready_btn = _first_visible(page, SEL_DOWNLOAD_EXCEL_READY, 2000)
                break
            except PWTimeout:
                if _any_visible(page, SEL_NO_DATA_MARKERS, timeout_ms=500):
                    err_text = self._read_first_visible_text(SEL_NO_DATA_MARKERS)
                    raise NoDataAvailableError(
                        err_text or "GSTR-2B not generated."
                    )
        if ready_btn is None:
            raise DownloadError(
                "Portal did not produce a downloadable file in time."
            )

        with page.expect_download(timeout=120_000) as dl_info:
            ready_btn.locator.click()
        download = dl_info.value
        download.save_as(str(save_path))
        log.info("Saved (two-step): %s", save_path)
        return save_path

    def logout(self) -> None:
        """Best-effort logout to release the GST session quickly."""
        assert self.page is not None
        try:
            self.page.locator(
                "a:has-text('Logout'), a:has-text('Sign Out')"
            ).first.click(timeout=3000)
        except Exception:
            pass

    # ------------------ debug helpers ------------------

    def take_screenshot(self, label: str) -> Optional[Path]:
        """Save a screenshot of the current page for debugging.

        Returns the path or None if screenshot dir not configured.
        """
        if not self._screenshot_dir or self.page is None:
            return None
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe = re.sub(r"[^A-Za-z0-9_-]", "_", self._client_name)[:40]
            self._screenshot_dir.mkdir(parents=True, exist_ok=True)
            p = self._screenshot_dir / f"{safe}_{label}_{ts}.png"
            self.page.screenshot(path=str(p), full_page=True)
            log.info("Screenshot saved: %s", p)
            return p
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not save screenshot: %s", exc)
            return None

    # ------------------ small helpers ------------------

    def _read_first_visible_text(self, selectors: list[str]) -> str:
        assert self.page is not None
        for sel in selectors:
            try:
                loc = self.page.locator(sel).first
                if loc.is_visible(timeout=300):
                    return (loc.inner_text(timeout=1000) or "").strip()
            except Exception:
                continue
        return ""


# ---------------------------------------------------------------------------
# Module-level Playwright lifecycle helper
# ---------------------------------------------------------------------------


@contextmanager
def playwright_session() -> Iterator[Playwright]:
    pw = sync_playwright().start()
    try:
        yield pw
    finally:
        pw.stop()


# ---------------------------------------------------------------------------
# Period helpers (portal-specific labels)
# ---------------------------------------------------------------------------


def _month_label_candidates(month: int) -> list[str]:
    full = [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ][month - 1]
    short = full[:3]
    return [full, short, full.upper(), short.upper()]


def _quarter_label_candidates(month: int) -> list[str]:
    """Return all label variants the portal might use for a given quarter.

    GSTN's wording has historically varied across:
        'Quarter 1 (Apr - Jun)' / 'Apr-Jun' / 'Q1 (Apr-Jun)' etc.
    """
    if month in (4, 5, 6):
        return [
            "Quarter 1 (Apr - Jun)", "Quarter 1 (Apr-Jun)",
            "Q1 (Apr - Jun)", "Q1 (Apr-Jun)", "Apr - Jun", "Apr-Jun",
        ]
    if month in (7, 8, 9):
        return [
            "Quarter 2 (Jul - Sep)", "Quarter 2 (Jul-Sep)",
            "Q2 (Jul - Sep)", "Q2 (Jul-Sep)", "Jul - Sep", "Jul-Sep",
        ]
    if month in (10, 11, 12):
        return [
            "Quarter 3 (Oct - Dec)", "Quarter 3 (Oct-Dec)",
            "Q3 (Oct - Dec)", "Q3 (Oct-Dec)", "Oct - Dec", "Oct-Dec",
        ]
    return [
        "Quarter 4 (Jan - Mar)", "Quarter 4 (Jan-Mar)",
        "Q4 (Jan - Mar)", "Q4 (Jan-Mar)", "Jan - Mar", "Jan-Mar",
    ]
