# -*- coding: utf-8 -*-
"""
evnhcm-crawl.py
===============
Playwright crawler for EVNHCMC Customer Portal.

Usage:
    python evnhcm-crawl.py                        # defaults to current month/year
    python evnhcm-crawl.py --month 6 --year 2026

Environment variables (from .env):
    EVN_AH_USERNAME  – portal phone/username
    EVN_AH_PASSWORD  – portal password
"""
import argparse
import os
import re
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

load_dotenv()

PHONE    = os.getenv("EVN_AH_USERNAME")
PASSWORD = os.getenv("EVN_AH_PASSWORD")

TARGET_MONTH = 1
TARGET_YEAR = 2025

CRAWL_PAGE = "https://www.evnhcmc.vn/Tracuu/HDDT"
SAVED_DIR  = Path("ah_raw_bill")   # root save directory

if not PHONE or not PASSWORD:
    raise ValueError("Thiếu EVN_AH_USERNAME hoặc EVN_AH_PASSWORD trong file .env")


# ─── Helpers ──────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        import sys
        sys.stdout.buffer.write((msg + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()


def try_click(page, selectors, timeout=1000):
    """Try clicking the first visible element from a list of CSS/text selectors."""
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if loc.is_visible():
                loc.click()
                log(f"  [OK] Clicked (immediate): {sel}")
                return True
        except Exception:
            continue

    for sel in selectors:
        loc = page.locator(sel).first
        try:
            loc.wait_for(state="visible", timeout=timeout)
            loc.click()
            log(f"  [OK] Clicked (waited): {sel}")
            return True
        except Exception:
            continue
    return False


def open_makh_modal(page):
    """
    Open the 'Chọn Mã KH' modal on the HDDT page.
    Returns True when modal is visible, False otherwise.
    """
    trigger_selectors = [
        "input.openChonMaKH",
        "i.openChonMaKH",
        "input.input-maKH",
        ".openChonMaKH",
        "label:has-text('Mã khách hàng')",
    ]

    log("\n[INFO] Opening 'Mã khách hàng' modal...")
    clicked = try_click(page, trigger_selectors, timeout=2000)
    if not clicked:
        return False

    modal_selectors = [
        "#modalChonMAKH",
        ".modal.show",
        "div.modal:visible",
    ]

    for sel in modal_selectors:
        try:
            page.wait_for_selector(sel, state="visible", timeout=3000)
            return True
        except PlaywrightTimeoutError:
            continue

    return False


def extract_customer_codes(page):
    """Extract all customer codes from the open modal."""
    code_selectors = [
        "#modalChonMAKH .ma_kh",
        ".modal.show .ma_kh",
        "#modalChonMAKH td:first-child",
    ]

    for sel in code_selectors:
        items = page.locator(sel)
        count = items.count()
        if count > 0:
            codes = []
            for i in range(count):
                text = items.nth(i).inner_text().strip()
                if text:
                    codes.append(text)
            if codes:
                return codes
    return []


# ─── Filter helpers ───────────────────────────────────────────────────────────

def _click_search_button(page) -> None:
    """
    Click the search / filter submit button on the billing page.
    Tries a broad set of candidates so it works regardless of label language.
    """
    candidates = [
        "button:has-text('Tra cứu')",
        "button:has-text('Tìm kiếm')",
        "button:has-text('Search')",
        "input[type='submit']",
        "button[type='submit']",
        ".btn-search",
        "#btnSearch",
        "#btnTraCuu",
        "#btnTimKiem",
    ]
    for sel in candidates:
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible(timeout=800):
                btn.click()
                log(f"  [FILTER] Clicked search button ({sel})")
                return
        except Exception:
            continue
    log("  [FILTER] No explicit search button found – table may auto-refresh on filter change.")


def _select_select2(page, label_hint: str, option_text: str) -> bool:
    """
    Interact with a Select2 (or compatible) custom dropdown widget.

    Strategy:
      1. Find a <select> element whose label / aria-label / surrounding text
         contains `label_hint` (e.g. 'Tháng' or 'Năm').
      2. Try to drive it as a native <select> first (cheapest path).
      3. If that fails (hidden / replaced by Select2 widget), locate the
         sibling .select2-container, click it to open, then click the
         matching option in the dropdown list.
      4. Absolute fallback: inject the value via jQuery/Select2 JS API.

    Returns True if the value was successfully set.
    """
    log(f"  [SELECT2] Trying to set '{label_hint}' → '{option_text}'")

    # ── 1. Find the underlying <select> elements and score them ───────────
    selects_info = page.evaluate("""
        (hint) => {
            const results = [];
            document.querySelectorAll('select').forEach(sel => {
                const id   = (sel.id   || '').toLowerCase();
                const name = (sel.name || '').toLowerCase();
                // look at surrounding label text too
                let labelText = '';
                try {
                    const lbl = document.querySelector(`label[for='${sel.id}']`);
                    if (lbl) labelText = lbl.textContent.toLowerCase();
                } catch {}
                // also walk up to find a wrapping label or form-group
                let parentText = '';
                try {
                    let p = sel.parentElement;
                    for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
                        parentText += ' ' + (p.textContent || '').toLowerCase().slice(0, 60);
                    }
                } catch {}
                const combined = id + ' ' + name + ' ' + labelText + ' ' + parentText;
                results.push({
                    id, name, combined,
                    options: Array.from(sel.options).map(o => ({value: o.value, text: o.text.trim()}))
                });
            });
            return results;
        }
    """, label_hint.lower())

    hint_lower = label_hint.lower()
    # Vietnamese month/year keywords
    if 'th' in hint_lower:  # Tháng
        keywords = ['thang', 'month', 'tháng']
    else:  # Năm
        keywords = ['nam', 'year', 'năm']

    target_id = None
    for s in selects_info:
        if any(k in s['combined'] for k in keywords):
            target_id = s['id']
            break

    # ── 2. Native <select> path ───────────────────────────────────────────
    if target_id:
        sel_css = f"#{target_id}"
        try:
            loc = page.locator(sel_css).first
            # Try by value, then by label text
            for method, val in [("value", option_text), ("label", option_text)]:
                try:
                    if method == "value":
                        loc.select_option(value=val, timeout=3000)
                    else:
                        loc.select_option(label=val, timeout=3000)
                    log(f"  [SELECT2] Native select succeeded: #{target_id} = {option_text!r}")
                    return True
                except Exception:
                    pass
        except Exception:
            pass

    # ── 3. Select2 widget path ────────────────────────────────────────────
    # Find all Select2 containers on the page and pick the right one
    # by inspecting their aria-label or position relative to a label.
    s2_containers = page.locator(".select2-container").all()
    log(f"  [SELECT2] Found {len(s2_containers)} .select2-container widgets on page")

    # Determine which container index corresponds to our label_hint.
    # Heuristic: inspect each container's preceding sibling or parent text.
    target_container = None
    for i, container in enumerate(s2_containers):
        try:
            # The select2 container usually sits right after the <select>
            # or is the next sibling. We look at parent element's text.
            parent_text = container.evaluate(
                """(el) => {
                    let p = el.parentElement;
                    for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
                        const t = (p.textContent || '').toLowerCase();
                        if (t.length < 200) return t;
                    }
                    return '';
                }"""
            ).lower()
            if any(k in parent_text for k in keywords):
                target_container = container
                log(f"  [SELECT2] Container #{i} matches label_hint={label_hint!r}")
                break
        except Exception:
            continue

    # Position-based fallback: first container = Month, second = Year
    if target_container is None and len(s2_containers) >= 2:
        idx = 0 if 'th' in hint_lower else 1
        target_container = s2_containers[idx]
        log(f"  [SELECT2] Using position-based fallback: container index {idx}")
    elif target_container is None and len(s2_containers) == 1:
        target_container = s2_containers[0]

    if target_container is None:
        log(f"  [SELECT2] Could not locate a Select2 container for {label_hint!r}")
        return False

    # Click to open the dropdown
    try:
        selection_span = target_container.locator(".select2-selection").first
        selection_span.click(timeout=5000)
        log(f"  [SELECT2] Opened dropdown for {label_hint!r}")
    except Exception as exc:
        log(f"  [SELECT2] Could not click .select2-selection: {exc}")
        return False

    # Wait for the dropdown list to appear
    try:
        page.wait_for_selector(".select2-dropdown", state="visible", timeout=5000)
    except Exception:
        log("  [SELECT2] Warning: .select2-dropdown did not appear")

    # If there is a search box, type the option text to filter
    try:
        search_box = page.locator(".select2-search__field").first
        if search_box.is_visible(timeout=1000):
            search_box.fill(option_text)
            page.wait_for_timeout(400)
    except Exception:
        pass

    # Click the matching option
    try:
        # Try exact text match first, then partial
        option_loc = page.locator(
            f".select2-results__option:has-text('{option_text}')"
        ).first
        option_loc.click(timeout=5000)
        log(f"  [SELECT2] Clicked option '{option_text}' for {label_hint!r}")
        return True
    except Exception as exc:
        log(f"  [SELECT2] Could not click option '{option_text}': {exc}")

    # ── 4. JS / jQuery fallback ───────────────────────────────────────────
    if target_id:
        try:
            injected = page.evaluate(
                """
                ([selId, val]) => {
                    const el = document.getElementById(selId);
                    if (!el) return false;
                    // Try to find the option by text
                    const opt = Array.from(el.options).find(
                        o => o.text.trim() === val || o.value === val
                    );
                    if (!opt) return false;
                    el.value = opt.value;
                    // Fire change events for Select2 / Vue / React listeners
                    ['change', 'input'].forEach(ev =>
                        el.dispatchEvent(new Event(ev, {bubbles: true}))
                    );
                    if (window.jQuery) {
                        jQuery(el).trigger('change');
                    }
                    return true;
                }
                """,
                [target_id, option_text],
            )
            if injected:
                log(f"  [SELECT2] JS fallback set #{target_id} = {option_text!r}")
                return True
        except Exception as exc:
            log(f"  [SELECT2] JS fallback failed: {exc}")

    return False


def _apply_period_filter(page, target_month: int, target_year: int) -> None:
    """
    Set the billing Month and Year filters on the EVNHCMC HDDT page.

    Supports:
      - Native <select> elements (tried first — cheapest)
      - Select2 custom widgets (detected via <span class="selection">)
      - jQuery/JS direct injection as a last resort

    After setting both filters, clicks the search button (if any) to trigger
    a table refresh.
    """
    log(f"\n  [FILTER] Applying period filter: {target_month:02d}/{target_year}")

    month_str = str(target_month)
    year_str  = str(target_year)

    # Try to set Month
    month_ok = _select_select2(page, "Tháng", month_str)
    if not month_ok:
        # Also try zero-padded value (e.g. "01")
        month_ok = _select_select2(page, "Tháng", f"{target_month:02d}")

    # Try to set Year
    year_ok = _select_select2(page, "Năm", year_str)

    if not month_ok:
        log("  [FILTER] WARNING: Could not set Month filter. Table may show wrong period.")
    if not year_ok:
        log("  [FILTER] WARNING: Could not set Year filter. Table may show wrong period.")

    # Small pause for any onchange handlers to fire before we click search
    page.wait_for_timeout(400)

    # Click the search / tra cứu button
    _click_search_button(page)


def _wait_for_table_refresh(
    page,
    old_html: str,
    table_sel: str = "table.table-custom",
    timeout_ms: int = 15000,
) -> None:
    """
    Wait until the billing table DOM changes from `old_html`.

    Uses page.wait_for_function() with the captured HTML as a sentinel so we
    don't rely on arbitrary sleep() durations.

    Falls back to networkidle if wait_for_function times out.
    """
    log("  [WAIT] Waiting for billing table to refresh…")
    try:
        page.wait_for_function(
            """
            ([sel, old]) => {
                const tbl = document.querySelector(sel);
                if (!tbl) return false;
                // Accept once content is different OR a loading indicator disappears
                const spinner = document.querySelector(
                    '.loading, .spinner, [class*="loading"], [class*="spinner"]'
                );
                if (spinner && spinner.offsetParent !== null) return false;
                return tbl.innerHTML !== old;
            }
            """,
            arg=[table_sel, old_html],
            timeout=timeout_ms,
        )
        log("  [WAIT] Table content changed — refresh complete.")
    except Exception:
        log("  [WAIT] wait_for_function timed out — falling back to networkidle.")
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        # Extra safety buffer
        page.wait_for_timeout(800)


# ─── Main crawl function ──────────────────────────────────────────────────────

def crawl(target_month: int, target_year: int) -> list[Path]:
    """
    Download all detailed PDFs for the given month/year from EVNHCMC portal.

    Returns:
        List of downloaded PDF file paths.
    """
    # Save directory: ah_raw_bill/YYYY_MM/
    save_root = SAVED_DIR / f"{target_year}_{target_month:02d}"
    save_root.mkdir(parents=True, exist_ok=True)

    # The billing table shows dates as "Đợt X Tháng MM/YYYY"
    target_pattern = f"{target_month:02d}/{target_year}"
    log(f"[INFO] Target period: {target_pattern}")

    downloaded: list[Path] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=100)
        context = browser.new_context(
            locale="vi-VN",
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        try:
            # ── Step 1: Login ─────────────────────────────────────────────────
            log("Đang mở website EVNHCMC...")
            page.goto("https://www.evnhcmc.vn/", wait_until="domcontentloaded", timeout=60000)

            log("Đang chờ form đăng nhập...")
            page.wait_for_selector("form.form-dangnhap-trangchu", timeout=30000)

            login_form = page.locator("form.form-dangnhap-trangchu")
            login_form.locator(".input-user").fill(PHONE)
            login_form.locator(".input-pass").fill(PASSWORD)
            login_form.locator("button[type='submit']").click()

            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except PlaywrightTimeoutError:
                pass

            # ── Step 2: Navigate to HDDT page ────────────────────────────────
            log(f"\nĐang mở trang tra cứu: {CRAWL_PAGE}")
            page.goto(CRAWL_PAGE, wait_until="domcontentloaded", timeout=60000)

            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeoutError:
                pass

            if "dangnhap" in page.url.lower() or "login" in page.url.lower():
                log("[ERROR] Bị chuyển hướng sang trang đăng nhập!")
                return []

            # ── Step 3: Get customer codes ────────────────────────────────────
            if not open_makh_modal(page):
                log("[ERROR] Không thể mở modal chọn khách hàng!")
                return []

            codes = extract_customer_codes(page)

            # Close modal
            close_btn = page.locator(
                "#modalChonMAKH .close, #modalChonMAKH [data-dismiss='modal']"
            ).first
            if close_btn.count() > 0 and close_btn.is_visible():
                close_btn.click()
                page.locator("#modalChonMAKH").wait_for(state="hidden", timeout=3000)

            if not codes:
                log("[WARN] Không tìm thấy mã khách hàng nào!")
                return []

            log(f"\n✓ Lấy được {len(codes)} mã khách hàng: {codes}")
            log(f"Mục tiêu tìm kiếm hóa đơn tháng/năm: {target_pattern}")

            # ── Step 4: Loop through all clients ──────────────────────────────
            for idx, client_code in enumerate(codes, 1):
                log(f"\n[{idx}/{len(codes)}] Bắt đầu xử lý khách hàng: {client_code}")

                try:
                    # Open modal and select client
                    if not open_makh_modal(page):
                        log(f"  [ERROR] Không thể mở modal cho {client_code}. Bỏ qua.")
                        continue

                    log(f"Selecting client: {client_code}")
                    client_item = page.locator(
                        f"#modalChonMAKH div.item[ma_pe='{client_code}']"
                    ).first
                    client_item.click()
                    page.locator("#modalChonMAKH").wait_for(state="hidden", timeout=5000)

                    # Confirm client changed
                    confirmed = False
                    for _ in range(10):
                        displayed_code = page.locator("input.input-maKH").first.input_value().strip()
                        if displayed_code == client_code:
                            confirmed = True
                            break
                        page.wait_for_timeout(500)

                    if not confirmed:
                        log(f"  [WARN] Mã trên trang ({displayed_code}) ≠ ({client_code}). Đang thử lại...")
                        if open_makh_modal(page):
                            page.locator(
                                f"#modalChonMAKH div.item[ma_pe='{client_code}']"
                            ).first.click()
                            page.locator("#modalChonMAKH").wait_for(state="hidden", timeout=5000)
                            displayed_code = page.locator("input.input-maKH").first.input_value().strip()
                            confirmed = displayed_code == client_code

                    if not confirmed:
                        log(f"  [ERROR] Xác nhận khách hàng thất bại ({displayed_code}). Bỏ qua.")
                        continue

                    displayed_name = page.locator(
                        ".thongTin_maKH_dangchon .ten"
                    ).first.inner_text().strip()
                    log(f"Page now displays client: {displayed_code} - {displayed_name}")

                    # ── Step A: Wait for initial client load (networkidle) ─────
                    log("  Đang chờ bảng dữ liệu tải sau khi chọn khách hàng...")
                    try:
                        page.wait_for_load_state("networkidle", timeout=15000)
                    except PlaywrightTimeoutError:
                        pass
                    page.wait_for_timeout(500)

                    # ── Step B: Capture table HTML before applying filter ──────
                    try:
                        old_html = page.locator("table.table-custom").inner_html(timeout=5000)
                    except Exception:
                        old_html = ""

                    # ── Step C: Apply month/year filter ───────────────────────
                    _apply_period_filter(page, target_month, target_year)

                    # ── Step D: Wait for table to refresh ─────────────────────
                    _wait_for_table_refresh(page, old_html)

                    # ── Step E: Validate — confirm table shows correct period ──
                    log(f"  [VALIDATE] Checking table rows for period {target_pattern}…")
                    rows = page.locator("table.table-custom tbody tr").all()
                    matching_rows = []

                    for row in rows:
                        cells = row.locator("td").all()
                        if len(cells) < 5:
                            continue
                        month_year_text = cells[3].inner_text().strip()
                        if target_pattern in month_year_text:
                            matching_rows.append((row, month_year_text))

                    if not matching_rows:
                        log(
                            f"  [INFO] Không tìm thấy hóa đơn {target_pattern} cho "
                            f"{client_code}. "
                            f"(Bảng có {len(rows)} dòng — không có dòng nào khớp với kỳ mục tiêu.)"
                        )
                        continue

                    log(f"Target month/year found: {target_pattern}")
                    log(f"  Tìm thấy {len(matching_rows)} hóa đơn phù hợp:")
                    for _, text in matching_rows:
                        log(f"    - {text}")

                    # ── Download each matching bill ────────────────────────────
                    for row, month_year_text in matching_rows:
                        period_match = re.search(r"Đợt\s*(\d+)", month_year_text)
                        period = period_match.group(1) if period_match else "unknown"

                        log(f"Downloading PDF...")
                        log(f"  Đang tải hóa đơn {month_year_text} (Đợt {period})...")

                        download_btn = row.locator(
                            ".btn-download-hd, [class*='download-hd'], img[title='Tải về']"
                        ).first
                        if download_btn.count() == 0:
                            log("    [ERROR] Không tìm thấy nút tải về trong dòng này!")
                            continue

                        download_btn.click()
                        page.wait_for_selector(
                            "#modalDownLoadHoaDon", state="visible", timeout=10000
                        )

                        pdf_detail_btn = page.locator(
                            "#modalDownLoadHoaDon div.btn-download-HDDT[loaihd='PDF-ChiTiet']"
                        ).first
                        if pdf_detail_btn.count() == 0:
                            log("    [ERROR] Không tìm thấy nút tải BẢN CHI TIẾT (.PDF)!")
                            close_btn = page.locator("#modalDownLoadHoaDon .close").first
                            if close_btn.is_visible():
                                close_btn.click()
                            continue

                        try:
                            with page.expect_download(timeout=30000) as download_info:
                                pdf_detail_btn.click()
                            download = download_info.value

                            # Save to: ah_raw_bill/YYYY_MM/client_code/period_X.pdf
                            client_dir = save_root / client_code
                            client_dir.mkdir(parents=True, exist_ok=True)
                            save_path = client_dir / f"period_{period}.pdf"

                            download.save_as(save_path)
                            log(f"    [OK] Đã lưu: {save_path}")
                            downloaded.append(save_path)

                        except Exception as e:
                            log(f"    [ERROR] Lỗi khi tải file: {e}")

                        # Close download modal
                        close_btn = page.locator("#modalDownLoadHoaDon .close").first
                        if close_btn.is_visible():
                            close_btn.click()
                        page.locator("#modalDownLoadHoaDon").wait_for(state="hidden", timeout=5000)

                except Exception as e:
                    log(f"  [ERROR] Lỗi xử lý khách hàng {client_code}: {e}")
                    for modal_id in ["#modalChonMAKH", "#modalDownLoadHoaDon"]:
                        try:
                            close_btn = page.locator(f"{modal_id} .close").first
                            if close_btn.is_visible():
                                close_btn.click()
                        except Exception:
                            pass

        except Exception as exc:
            log(f"[ERROR] Unexpected error: {exc}")
            import traceback
            traceback.print_exc()
        finally:
            browser.close()

    log(f"\n✓ Đã hoàn thành. Tải được {len(downloaded)} PDF:")
    for p in downloaded:
        log(f"  • {p}")
    return downloaded


# ─── CLI entry point ──────────────────────────────────────────────────────────

def main() -> None:
    now = datetime.now()
    parser = argparse.ArgumentParser(
        description="EVNHCMC Bill Crawler – downloads chi tiết PDFs"
    )
    parser.add_argument(
        "--month",
        type=int,
        default=TARGET_MONTH,
        help=f"Target month (1–12). Default: current month ({now.month}).",
    )
    parser.add_argument(
        "--year",
        type=int,
        default=TARGET_YEAR,
        help=f"Target year (e.g. 2026). Default: current year ({now.year}).",
    )
    args = parser.parse_args()

    if not 1 <= args.month <= 12:
        parser.error("--month must be between 1 and 12")

    log("=" * 60)
    log("  EVNHCMC Bill Crawler")
    log(f"  Target: {args.month:02d}/{args.year}")
    log("=" * 60)

    paths = crawl(args.month, args.year)
    log(f"\nTotal PDFs downloaded: {len(paths)}")


if __name__ == "__main__":
    main()