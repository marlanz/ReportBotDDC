# -*- coding: utf-8 -*-
"""
evnspc-crawl.py
===============
Playwright crawler for EVN SPC (Long An) Customer Portal.

Workflow:
    1. Login with username / password + CAPTCHA (solved via PaddleOCR cloud API).
    2. Navigate to the bill lookup page.
    3. Select target Month + Year.
    4. Iterate every row in the billing table (all clients for the period).
    5. For each row, open the bill viewer and download the "Xem chi tiết" PDF.
    6. Return a list of downloaded PDF paths for the downstream OCR pipeline.

Usage (standalone):
    python evnspc-crawl.py
    python evnspc-crawl.py --month 7 --year 2026

Public API (for pipeline integration):
    from evnspc-crawl import crawl
    pdf_paths = crawl(target_month=7, target_year=2026)

Environment variables (from .env):
    EVN_LA_USERNAME   – portal username
    EVN_LA_PASSWORD   – portal password
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import zipfile
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

# ── Reuse the shared PaddleOCR cloud API from the existing infrastructure ──────
# ocr_image_bytes(img_bytes) → submits raw PNG bytes to the same API used by
# crawlpaddleocr.py and returns the recognised text string.
from crawlpaddleocr import ocr_image_bytes

load_dotenv()

# ─── Constants ────────────────────────────────────────────────────────────────
LOGIN_URL      = "https://www.cskh.evnspc.vn/TaiKhoan/DangNhap?previousLink=%2FTraCuu%2FHoaDonTienDien"
BILL_URL       = "https://www.cskh.evnspc.vn/TraCuu/HoaDonTienDien"
CAPTCHA_URL    = "https://www.cskh.evnspc.vn/TaiKhoan/GetCaptchaImage"
VERIFY_URL     = "https://www.cskh.evnspc.vn/TaiKhoan/CustomCaptcha"
LOGIN_POST_URL = "https://www.cskh.evnspc.vn/TaiKhoan/Login"

USERNAME = os.getenv("EVN_LA_USERNAME", "").strip()
PASSWORD = os.getenv("EVN_LA_PASSWORD", "").strip()

SAVED_DIR       = Path("la_raw_bill")   # where PDFs are stored
MAX_CAPTCHA_ATTEMPTS = 5

MONTH_OF_BILL=1
YEAR_OF_BILL=2025


# ─── Logging helper ───────────────────────────────────────────────────────────
def log(msg: str) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((msg + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()


# ─── CAPTCHA helpers ──────────────────────────────────────────────────────────

# The EVN SPC CAPTCHA is 6 alphanumeric characters (regex from page JS).
_CAPTCHA_PATTERN = re.compile(r"^[A-Z0-9]{6}$")


def _dismiss_error_modal(page: Page) -> None:
    """
    If the error modal (#FinishModalClose) is visible, click the
    "Kết thúc" button and wait until the modal is fully hidden.
    """
    modal = page.locator("#FinishModalClose")
    try:
        if not modal.is_visible(timeout=500):
            return
    except Exception:
        return

    log("  [MODAL] Error modal detected — dismissing…")

    # Click the "Kết thúc" or close button inside the modal
    dismiss_candidates = [
        "input[value*='Kết thúc']",
        "input[value*='Ket thuc']",
        ".btn-close",
        "button.btn-primary",
    ]
    clicked = False
    for candidate in dismiss_candidates:
        try:
            btn = modal.locator(candidate).first
            if btn.is_visible(timeout=1000):
                btn.click()
                clicked = True
                break
        except Exception:
            continue

    if not clicked:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass

    # Wait until the modal is fully hidden
    try:
        modal.wait_for(state="hidden", timeout=5000)
    except PlaywrightTimeoutError:
        pass

    # Extra safety: wait for any lingering .modal-backdrop to disappear
    try:
        page.locator(".modal-backdrop").wait_for(state="hidden", timeout=3000)
    except (PlaywrightTimeoutError, Exception):
        pass

    log("  [MODAL] Error modal dismissed.")


def _ensure_no_modal_visible(page: Page) -> None:
    """
    Make sure no Bootstrap modal is covering the page.
    Dismiss any that are visible before proceeding.
    """
    _dismiss_error_modal(page)

    # Generic safety: close any other .modal.show
    try:
        any_modal = page.locator(".modal.show")
        if any_modal.count() > 0 and any_modal.first.is_visible(timeout=300):
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
    except Exception:
        pass


def _get_captcha_src(page: Page) -> str:
    """Return the current `src` attribute of #imgCaptcha."""
    try:
        return page.locator("#imgCaptcha").get_attribute("src") or ""
    except Exception:
        return ""


def _click_refresh_captcha(page: Page) -> None:
    """
    Click the "Lấy lại captcha" link to force the server to generate a
    fresh CAPTCHA image, then wait until the #imgCaptcha src actually changes.
    """
    old_src = _get_captcha_src(page)
    log(f"  [CAPTCHA] Old src: {old_src[:80]}…")

    # Click the refresh link using same selectors as crawlexample.py
    refresh_candidates = [
        "a:has-text('Lấy lại captcha')",
        "a:has-text('Lay lai captcha')",
        "a[href*='RefreshCaptcha']",
        "a[onclick*='RefreshCaptcha']",
    ]
    clicked = False
    for candidate in refresh_candidates:
        try:
            link = page.locator(candidate).first
            if link.is_visible(timeout=1000):
                link.click()
                clicked = True
                break
        except Exception:
            continue

    if not clicked:
        # Fallback: invoke the JS function directly
        log("  [CAPTCHA] Refresh link not found, calling RefreshCaptcha() via JS…")
        page.evaluate("""() => {
            if (typeof RefreshCaptcha === 'function') {
                RefreshCaptcha();
            } else {
                const img = document.getElementById('imgCaptcha');
                if (img) img.src = '/TaiKhoan/GetCaptchaImage?' + Date.now();
                const inp = document.getElementById('clientCaptcha');
                if (inp) { inp.value = ''; inp.focus(); }
            }
        }""")

    # Wait for the CAPTCHA <img> src to change (the timestamp query param will differ)
    for _ in range(20):  # up to ~4 seconds
        page.wait_for_timeout(200)
        new_src = _get_captcha_src(page)
        if new_src and new_src != old_src:
            log(f"  [CAPTCHA] New src: {new_src[:80]}…")
            return
    log("  [CAPTCHA] Warning: src did not change after refresh")


def _capture_captcha_element(page: Page) -> bytes:
    """
    Screenshot ONLY the #imgCaptcha element and return the raw PNG bytes.
    Verifies no modal is covering it first.
    """
    _ensure_no_modal_visible(page)

    captcha_el = page.locator("#imgCaptcha")
    captcha_el.wait_for(state="visible", timeout=8000)

    # Scroll into view just in case
    captcha_el.scroll_into_view_if_needed()
    page.wait_for_timeout(300)

    img_bytes = captcha_el.screenshot()
    log(f"  [CAPTCHA] Captured element screenshot: {len(img_bytes)} bytes")
    return img_bytes


def solve_captcha_from_bytes(img_bytes: bytes) -> str:
    """
    Send CAPTCHA image bytes to the PaddleOCR cloud API and return the
    cleaned, UPPERCASED result containing only letters (A-Z) and digits (0-9).

    Reuses ocr_image_bytes() from crawlpaddleocr — the same API endpoint,
    token, and model already used for PDF invoice OCR.
    """
    raw = ocr_image_bytes(img_bytes, filename="captcha.png")

    # Clean: remove non-alphanumeric characters, whitespace/newlines, and uppercase
    cleaned = re.sub(r"[^A-Z0-9]", "", raw.upper())
    log(f"  [CAPTCHA] Raw OCR: {repr(raw)}  →  Cleaned: {repr(cleaned)}")
    return cleaned


# ─── Login ────────────────────────────────────────────────────────────────────

def do_login(page: Page) -> bool:
    """
    Perform the full login flow with automatic CAPTCHA retry.

    Workflow per attempt:
        1. Click "Lấy lại captcha" to get a FRESH CAPTCHA
        2. Wait for the CAPTCHA image src to change
        3. Screenshot only the #imgCaptcha element
        4. OCR via PaddleOCR cloud API → uppercase
        5. Fill Username + Password + CAPTCHA → click "Đăng nhập"
        6. On success → return True
        7. On failure → dismiss error modal → loop back to step 1

    Returns True on success.
    Raises RuntimeError if all attempts are exhausted.
    """
    if not USERNAME or not PASSWORD:
        raise ValueError("Missing EVN_LA_USERNAME or EVN_LA_PASSWORD in .env")

    log(f"\n[LOGIN] Opening {LOGIN_URL}")
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        pass

    # Wait for the login form to be ready
    page.locator("#Username").wait_for(state="visible", timeout=10000)

    for attempt in range(1, MAX_CAPTCHA_ATTEMPTS + 1):
        log(f"\n  ────────── CAPTCHA attempt {attempt}/{MAX_CAPTCHA_ATTEMPTS} ──────────")

        # ── Step 1: Make sure no error modal is covering the page ─────────
        _ensure_no_modal_visible(page)

        # ── Step 2: Fill Username + Password ──────────────────────────────
        page.locator("#Username").fill(USERNAME)
        page.locator("#Password").fill(PASSWORD)

        # ── Step 3: ALWAYS refresh CAPTCHA before OCR ─────────────────────
        # The EVN SPC CAPTCHA expires very quickly, so we never OCR
        # whatever was initially loaded or left over from a failed attempt.
        _click_refresh_captcha(page)

        # ── Step 4: Capture the fresh CAPTCHA image element ───────────────
        try:
            img_bytes = _capture_captcha_element(page)
        except Exception as exc:
            log(f"  [WARN] Could not capture CAPTCHA image: {exc}")
            continue

        if not img_bytes or len(img_bytes) < 100:
            log("  [WARN] CAPTCHA image appears empty/corrupt, retrying…")
            continue

        # ── Step 5: OCR the CAPTCHA image ─────────────────────────────────
        try:
            captcha_text = solve_captcha_from_bytes(img_bytes)
        except Exception as exc:
            log(f"  [WARN] OCR failed: {exc}")
            continue

        if not captcha_text:
            log("  [WARN] OCR returned empty string, retrying…")
            continue

        # Validate against known CAPTCHA format (6 alphanumeric chars)
        if not _CAPTCHA_PATTERN.match(captcha_text):
            log(f"  [WARN] OCR result '{captcha_text}' doesn't match expected "
                f"6-char alphanumeric pattern — retrying…")
            continue

        # ── Step 6: Fill the CAPTCHA and submit ───────────────────────────
        page.locator("#clientCaptcha").fill(captcha_text)
        log(f"  [LOGIN] Submitting: user={USERNAME}, captcha={captcha_text}")

        # Intercept the AJAX login response
        login_result_code = None
        login_redirect_url = None

        def _on_response(response):
            nonlocal login_result_code, login_redirect_url
            try:
                if "/TaiKhoan/Login" in response.url and response.request.method == "POST":
                    data = response.json()
                    login_result_code = str(data.get("KetQua", ""))
                    login_redirect_url = data.get("RedirectUrl", "")
                    log(f"  [LOGIN] Server response: KetQua={login_result_code}")
            except Exception:
                pass

        page.on("response", _on_response)
        page.locator("#btnDangNhap").click()

        # Wait for the AJAX round-trip
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(1000)

        page.remove_listener("response", _on_response)

        # ── Step 7: Evaluate the result ───────────────────────────────────

        # Check for URL redirect (means login succeeded and page navigated)
        current_url = page.url.lower()
        if "dangnhap" not in current_url and "login" not in current_url:
            log(f"  [LOGIN] ✓ Redirected to: {page.url}")
            return True

        # Check the AJAX response code
        if login_result_code == "3" and login_redirect_url:
            log(f"  [LOGIN] ✓ Server says redirect to: {login_redirect_url}")
            page.goto(login_redirect_url, wait_until="domcontentloaded", timeout=30000)
            return True

        if login_result_code and login_result_code not in ("0", "44"):
            log(f"  [LOGIN] ✓ Login API success (KetQua={login_result_code})")
            return True

        # ── Step 8: Login failed — handle error modal ─────────────────────
        log("  [LOGIN] ✗ Login failed. Checking for error modal…")

        # Wait a moment for the error modal to appear
        try:
            page.locator("#FinishModalClose").wait_for(state="visible", timeout=5000)
        except PlaywrightTimeoutError:
            pass

        # Read the error message for diagnostics
        try:
            err_msg = page.locator("#idNoiDungThongBaoModalClose").inner_text(timeout=1000).strip()
            if err_msg:
                log(f"  [LOGIN] Error message: {err_msg}")
        except Exception:
            pass

        # Dismiss the error modal
        _dismiss_error_modal(page)

        # Loop continues → step 1 will refresh CAPTCHA again

    # All attempts exhausted
    raise RuntimeError(
        f"CAPTCHA login failed after {MAX_CAPTCHA_ATTEMPTS} attempts. "
        "The CAPTCHA OCR may be inaccurate or the credentials are wrong."
    )


# ─── Bill lookup helpers ───────────────────────────────────────────────────────

def navigate_to_bills(page: Page) -> None:
    """Navigate to the bill lookup page and wait until it loads."""
    log(f"\n[NAV] Going to {BILL_URL}")
    page.goto(BILL_URL, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        pass

    # Verify we are on the right page (not redirected back to login)
    if "dangnhap" in page.url.lower() or "login" in page.url.lower():
        raise RuntimeError(f"Redirected back to login after navigating to bills. URL: {page.url}")

    log(f"  [NAV] Current URL: {page.url}")


def _select_dropdown_by_value(page: Page, selector: str, value: str, label: str) -> bool:
    """Select a <select> element option by its value attribute."""
    el = page.locator(selector)
    try:
        el.wait_for(state="visible", timeout=8000)
        el.select_option(value=value)
        log(f"  [FILTER] Selected {label} = {value}")
        return True
    except Exception as exc:
        log(f"  [FILTER] Could not select {label}: {exc}")
        return False


def _select_dropdown_by_text(page: Page, selector: str, text: str, label: str) -> bool:
    """Select a <select> element option by its visible text."""
    el = page.locator(selector)
    try:
        el.wait_for(state="visible", timeout=8000)
        el.select_option(label=text)
        log(f"  [FILTER] Selected {label} = {text!r}")
        return True
    except Exception as exc:
        log(f"  [FILTER] Could not select {label}: {exc}")
        return False


def _click_search_button(page: Page) -> None:
    """Click the search / filter submit button on the billing page."""
    candidates = [
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Tìm kiếm')",
        "button:has-text('Tra cứu')",
        ".btn-search",
        "#btnSearch",
        "#btnTraCuu",
        "#btnTimKiem",
    ]
    for sel in candidates:
        try:
            btn = page.locator(sel).first
            if btn.is_visible():
                btn.click()
                log(f"  [FILTER] Clicked search button ({sel})")
                return
        except Exception:
            continue
    log("  [WARN] Could not find a search button. Data may auto-refresh.")


def apply_month_year_filter(page: Page, target_month: int, target_year: int) -> None:
    """
    Select the billing month and year in the filter dropdowns, then trigger search.

    The EVN SPC bill page displays ALL clients for a chosen billing period.
    We select Month + Year (and optionally Kỳ/period) to narrow the dataset.
    """
    log(f"\n[FILTER] Applying filter: month={target_month}, year={target_year}")

    # Wait until the filter area is present
    try:
        page.wait_for_selector("select, .frm-group, form", timeout=10000)
    except PlaywrightTimeoutError:
        log("  [WARN] Could not find filter selectors within timeout.")

    # ── Inspect available <select> elements ──────────────────────────────────
    selects_info = page.evaluate("""() => {
        const selects = Array.from(document.querySelectorAll('select'));
        return selects.map(s => ({
            id:       s.id,
            name:     s.name,
            className: s.className,
            options:  Array.from(s.options).map(o => ({value: o.value, text: o.text.trim()}))
        }));
    }""")

    log(f"  [INSPECT] Found {len(selects_info)} <select> element(s) on bill page:")
    for s in selects_info:
        log(f"    id={s['id']!r}  name={s['name']!r}  class={s['className']!r}  options={s['options'][:5]}…")

    # ── Identify month/year/ky dropdowns by id / name / class ────────────────
    month_sel = None
    year_sel  = None
    ky_sel    = None

    MONTH_KEYS = ("thang", "month", "thángn", "thang_hd", "thang_hoa_don")
    YEAR_KEYS  = ("nam",   "year",  "nam_hd",  "nam_hoa_don")
    KY_KEYS    = ("ky",    "ki",    "period",  "dot")

    def _matches(info: dict, keywords: tuple) -> bool:
        combined = f"{info['id']} {info['name']} {info['className']}".lower()
        return any(k in combined for k in keywords)

    for s in selects_info:
        if month_sel is None and _matches(s, MONTH_KEYS):
            month_sel = f"#{s['id']}" if s['id'] else f"[name='{s['name']}']"
        elif year_sel is None and _matches(s, YEAR_KEYS):
            year_sel = f"#{s['id']}" if s['id'] else f"[name='{s['name']}']"
        elif ky_sel is None and _matches(s, KY_KEYS):
            ky_sel = f"#{s['id']}" if s['id'] else f"[name='{s['name']}']"

    log(f"  [INSPECT] month_sel={month_sel}  year_sel={year_sel}  ky_sel={ky_sel}")

    changed = False

    if month_sel:
        # Try numeric value first, then text match
        ok = _select_dropdown_by_value(page, month_sel, str(target_month), "Tháng")
        if not ok:
            ok = _select_dropdown_by_value(page, month_sel, f"{target_month:02d}", "Tháng")
        if not ok:
            _select_dropdown_by_text(page, month_sel, str(target_month), "Tháng")
        changed = True

    if year_sel:
        ok = _select_dropdown_by_value(page, year_sel, str(target_year), "Năm")
        if not ok:
            _select_dropdown_by_text(page, year_sel, str(target_year), "Năm")
        changed = True

    if changed:
        # Give the page a moment for any onchange handlers to fire
        page.wait_for_timeout(500)
        _click_search_button(page)
        # Wait for the table to reload
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(800)


# ─── Table inspection ─────────────────────────────────────────────────────────

def _find_billing_table(page: Page) -> str | None:
    """
    Locate the billing results table and return its CSS selector.
    Returns None if not found.
    """
    candidates = [
        "#idThongTinHoaDonTienDien table:visible",
        "#idThongTinHoaDonTienDien table",
        "table.table",
        "table",
        ".tbl-data",
        "#tblHoaDon",
        "#tableHoaDon",
        ".bill-table",
    ]
    for sel in candidates:
        try:
            tbl = page.locator(sel).first
            if tbl.is_visible(timeout=2000):
                row_count = page.locator(f"{sel} tbody tr").count()
                if row_count == 0:
                    row_count = page.locator(sel).locator("tr").count()
                if row_count > 0:
                    log(f"  [TABLE] Found billing table: {sel!r} with {row_count} rows")
                    return sel
        except Exception:
            continue
    return None


def get_table_rows(page: Page, table_sel: str) -> list:
    """Return all visible data rows from the billing table."""
    tr_sel = f"{table_sel} tbody tr"
    rows = page.locator(tr_sel).all()
    if not rows:
        tr_sel = f"{table_sel} tr"
        rows = page.locator(tr_sel).all()

    # Filter out empty / header-style rows
    data_rows = []
    for row in rows:
        try:
            # Skip if it is part of the thead or contains only th
            if row.locator("th").count() > 0 and row.locator("td").count() == 0:
                continue
            cell_count = row.locator("td").count()
            if cell_count >= 2:
                data_rows.append(row)
        except Exception:
            continue
    return data_rows


# ─── Bill download ────────────────────────────────────────────────────────────

# ─── ZIP Extraction Helper ───────────────────────────────────────────────────

def extract_td_ct_pdf(zip_path: Path, destination_dir: Path) -> Path | None:
    """
    Extracts only the file ending with '_TD_CT.pdf'
    from the downloaded ZIP archive.

    Returns:
        Path to the extracted PDF, or None if no matching file exists.
    """
    try:
        if not zip_path.exists():
            log(f"    [ZIP] Error: File {zip_path} does not exist.")
            return None
        
        if not zipfile.is_zipfile(zip_path):
            log(f"    [ZIP] Error: File {zip_path} is not a valid zip archive.")
            return None

        destination_dir.mkdir(parents=True, exist_ok=True)
        
        with zipfile.ZipFile(zip_path, "r") as z:
            # Find the file ending with _TD_CT.pdf
            matching_files = [f for f in z.namelist() if f.endswith("_TD_CT.pdf")]
            if not matching_files:
                log(f"    [ZIP] Warning: No file ending with '_TD_CT.pdf' found in {zip_path.name}")
                return None
                
            target_name = matching_files[0]
            log(f"    [ZIP] Found matching file: {target_name}")
            
            # Extract only that file and write directly to destination_dir to avoid subfolders
            base_filename = os.path.basename(target_name)
            out_path = destination_dir / base_filename
            
            with z.open(target_name) as source, open(out_path, "wb") as target:
                target.write(source.read())
                
            log(f"    [ZIP] Extracted successfully to: {out_path}")
            return out_path
    except Exception as exc:
        log(f"    [ZIP] Error during extraction of {zip_path.name}: {exc}")
        return None
    finally:
        # ZIP Cleanup: delete the downloaded ZIP archive unless KEEP_ZIP is set
        if os.getenv("KEEP_ZIP", "false").lower() != "true":
            try:
                if zip_path.exists():
                    zip_path.unlink()
                    log(f"    [ZIP] Cleaned up temporary archive: {zip_path.name}")
            except Exception as exc:
                log(f"    [ZIP] Warning: Could not delete temporary archive {zip_path}: {exc}")


def _download_row_zip(page: Page, row, save_dir: Path, filename: str) -> Path | None:
    """
    Locates the download button in the row, triggers ZIP download,
    and returns the downloaded Path or None.
    """
    save_dir.mkdir(parents=True, exist_ok=True)
    temp_zip_path = save_dir / filename

    # Candidates for the download button inside the row
    dl_candidates = [
        "a.download-btn",
        "a:has-text('Tải hóa đơn')",
        "button:has-text('Tải hóa đơn')",
        "a:has-text('Tải')",
        "button:has-text('Tải')",
        "a[title*='Tải']",
        "a[title*='tải']",
        "a[href*='Download']",
    ]

    for sel in dl_candidates:
        try:
            btn = row.locator(sel).first
            if btn.count() > 0 and btn.is_visible(timeout=500):
                # Trigger the download
                with page.expect_download(timeout=30000) as dl_info:
                    btn.click()
                download = dl_info.value
                download.save_as(temp_zip_path)
                log(f"    [DL] Downloaded ZIP via {sel} to: {temp_zip_path.name}")
                return temp_zip_path
        except Exception:
            continue

    # Generic: click the last visible link/button in the row if no candidates match
    try:
        links = row.locator("a, button").all()
        for link in reversed(links):
            if link.is_visible(timeout=300):
                with page.expect_download(timeout=30000) as dl_info:
                    link.click()
                download = dl_info.value
                download.save_as(temp_zip_path)
                log(f"    [DL] Downloaded ZIP via generic link to: {temp_zip_path.name}")
                return temp_zip_path
    except Exception:
        pass

    return None


# ─── Row metadata extraction ──────────────────────────────────────────────────

def _extract_row_meta(row, row_idx: int) -> dict:
    """
    Extract customer code, period, and other identifiers from a table row.
    Returns a dict with at minimum 'filename' key.
    """
    try:
        cells = row.locator("td").all()
        texts = [c.inner_text().strip() for c in cells]
    except Exception:
        texts = []

    # Build a safe filename from available cell data
    # Typical columns: STT | Mã KH | Tên KH | Kỳ | Tháng | Năm | Tiền | Xem
    customer_code = ""
    period = ""
    for t in texts[:6]:
        clean = re.sub(r"\s+", "_", t.strip())
        clean = re.sub(r"[^\w\-]", "", clean)
        if re.match(r"^[A-Z0-9]{5,}", clean):   # looks like a customer code
            customer_code = clean
            break

    for t in texts:
        m = re.search(r"(\d+)", t)
        if m and not period:
            period = m.group(1)

    filename = f"row{row_idx:03d}"
    if customer_code:
        filename = f"{customer_code}_row{row_idx:03d}"
    elif texts:
        safe = re.sub(r"[^\w\-]", "_", "_".join(texts[:3]))[:60]
        filename = f"{safe}_row{row_idx:03d}"

    return {
        "customer_code": customer_code,
        "period": period,
        "raw_texts": texts,
        "filename": filename + ".pdf",
    }


# ─── Pagination ───────────────────────────────────────────────────────────────

def _get_next_page(page: Page, table_sel: str) -> bool:
    """
    Click the "next page" button if pagination exists.
    Returns True if a next page was navigated to.
    """
    next_candidates = [
        "a[aria-label='Next']",
        "a.page-next",
        ".pagination .next a",
        ".pagination li:last-child a",
        "a:has-text('Trang sau')",
        "a:has-text('Sau')",
        "a:has-text('>')",
        "li.next a",
    ]
    for sel in next_candidates:
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible(timeout=500):
                is_disabled = btn.evaluate(
                    "(el) => el.classList.contains('disabled') || el.parentElement.classList.contains('disabled')"
                )
                if not is_disabled:
                    btn.click()
                    try:
                        page.wait_for_load_state("networkidle", timeout=15000)
                    except PlaywrightTimeoutError:
                        pass
                    page.wait_for_timeout(500)
                    log("  [PAGE] Moved to next page")
                    return True
        except Exception:
            continue
    return False


# ─── Main crawl logic ─────────────────────────────────────────────────────────

def _process_all_rows(
    page: Page,
    table_sel: str,
    save_dir: Path,
) -> list[Path]:
    """
    Iterate all rows (with pagination), download the ZIP archive for each,
    extract only the '_TD_CT.pdf' file to the customer's directory, and
    clean up the ZIP file. Returns a list of successfully extracted PDF paths.
    """
    downloaded: list[Path] = []
    page_num = 1

    while True:
        log(f"\n  [PAGE {page_num}] Processing rows…")
        rows = get_table_rows(page, table_sel)
        log(f"  Found {len(rows)} data rows on this page.")

        if not rows:
            break

        for row_idx, row in enumerate(rows, start=1):
            meta = _extract_row_meta(row, row_idx + (page_num - 1) * 100)
            customer_code = meta.get("customer_code") or "unknown"
            log(f"\n  [{row_idx}/{len(rows)}] Row: {meta['raw_texts'][:4]}")
            
            # Destination folder corresponding to the current client
            client_dir = save_dir / customer_code
            
            # Define temporary ZIP path
            zip_filename = f"temp_{customer_code}_{row_idx}.zip"
            
            # Step 1: Download the ZIP
            log(f"    [DL] Downloading ZIP archive for client {customer_code}…")
            zip_path = _download_row_zip(page, row, save_dir, zip_filename)
            if not zip_path or not zip_path.exists():
                log(f"    [WARN] Failed to download ZIP for row {row_idx} (client {customer_code}). Skipping.")
                continue
                
            # Step 2: Extract the '_TD_CT.pdf' file from ZIP & clean up ZIP
            log(f"    [ZIP] Extracting '_TD_CT.pdf' to {client_dir}…")
            pdf_path = extract_td_ct_pdf(zip_path, client_dir)
            if pdf_path:
                downloaded.append(pdf_path)
            else:
                log(f"    [WARN] ZIP extraction failed or no matching '_TD_CT.pdf' found for client {customer_code}. Skipping.")

        # Try next page
        has_next = _get_next_page(page, table_sel)
        if not has_next:
            break
        page_num += 1

    return downloaded


def get_available_parts(page: Page) -> list[str]:
    """Get all values of the '#part' select dropdown."""
    try:
        loc = page.locator("#part").first
        if loc.count() > 0:
            options = loc.locator("option").all_attribute_values("value")
            valid_parts = [o for o in options if o and o.strip().isdigit()]
            if valid_parts:
                return sorted(list(set(valid_parts)))
    except Exception:
        pass
    return ["1", "2", "3"]


# ─── Public API ───────────────────────────────────────────────────────────────

def crawl(target_month: int, target_year: int) -> list[Path]:
    """
    Downloads all detailed PDFs for the specified month/year from EVN SPC portal.

    Args:
        target_month: Calendar month (1 = Jan … 12 = Dec).
        target_year:  4-digit year (e.g. 2026).

    Returns:
        List of downloaded PDF file paths.
    """
    save_dir = SAVED_DIR / f"{target_year}_{target_month:02d}"
    save_dir.mkdir(parents=True, exist_ok=True)

    downloaded: list[Path] = []

    with sync_playwright() as p:
        browser: Browser = p.chromium.launch(
            headless=False,
            slow_mo=80,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context: BrowserContext = browser.new_context(
            locale="vi-VN",
            viewport={"width": 1366, "height": 768},
            accept_downloads=True,
        )
        page: Page = context.new_page()

        try:
            # ── Step 1: Login ─────────────────────────────────────────────────
            try:
                do_login(page)
            except RuntimeError as err:
                log(f"[ERROR] Login failed: {err}")
                return []

            log(f"\n[LOGIN] ✓ Logged in successfully. URL: {page.url}")

            # ── Step 2: Navigate to bill lookup ───────────────────────────────
            navigate_to_bills(page)

            # ── Step 3: Iterate through all available Parts (Kỳ) ───────────────
            parts = get_available_parts(page)
            log(f"[INFO] Found billing periods (Kỳ) to query: {parts}")
            
            for part in parts:
                log(f"\n[INFO] Querying period (Kỳ): {part} for {target_month:02d}/{target_year}")
                
                # Apply filter for this specific part/month/year
                try:
                    page.locator("#part").first.select_option(value=str(part))
                except Exception:
                    pass
                try:
                    page.locator("#month").first.select_option(value=str(target_month))
                except Exception:
                    pass
                try:
                    page.locator("#year").first.select_option(value=str(target_year))
                except Exception:
                    pass
                
                page.wait_for_timeout(500)
                _click_search_button(page)
                
                # Wait for the table to reload
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except PlaywrightTimeoutError:
                    pass
                page.wait_for_timeout(1000)

                # ── Step 4: Find billing table ────────────────────────────────────
                table_sel = _find_billing_table(page)
                if not table_sel:
                    log(f"[WARN] Billing table not found for period {part}. Skipping.")
                    continue

                # ── Step 5: Process all rows for this period ──────────────────────
                downloaded_part = _process_all_rows(page, table_sel, save_dir)
                downloaded.extend(downloaded_part)

        except Exception as exc:
            log(f"[ERROR] Unexpected error: {exc}")
            import traceback
            traceback.print_exc()
        finally:
            browser.close()

    log(f"\n✓ Done. Downloaded {len(downloaded)} PDF(s) to {save_dir}")
    for p in downloaded:
        log(f"  • {p}")
    return downloaded


# ─── CLI entry point ──────────────────────────────────────────────────────────

def main() -> None:
    # Force UTF-8 output on Windows to avoid cp1252 encode errors
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="EVN SPC (Long An) Bill Crawler - downloads chi tiet PDFs"
    )
    parser.add_argument(
        "--month",
        type=int,
        default=MONTH_OF_BILL,
        help="Target month (1-12). Default: current month.",
    )
    parser.add_argument(
        "--year",
        type=int,
        default=YEAR_OF_BILL,
        help="Target year (e.g. 2026). Default: current year.",
    )
    args = parser.parse_args()

    if not 1 <= args.month <= 12:
        parser.error("--month must be between 1 and 12")

    log("=" * 60)
    log("  EVN SPC (Long An) Bill Crawler")
    log(f"  Target: {args.month:02d}/{args.year}")
    log("=" * 60)

    paths = crawl(args.month, args.year)
    log(f"\nTotal PDFs downloaded: {len(paths)}")
    sys.exit(0 if paths else 1)


if __name__ == "__main__":
    main()
