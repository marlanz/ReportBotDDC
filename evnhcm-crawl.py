from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from dotenv import load_dotenv
import os
import json
import re
from datetime import datetime

load_dotenv()

PHONE = os.getenv("EVN_AH_USERNAME")
PASSWORD = os.getenv("EVN_AH_PASSWORD")

CRAWL_PAGE = "https://www.evnhcmc.vn/Tracuu/HDDT"

# Global Configuration
MONTH_OF_BILL = 6
YEAR_OF_BILL = 2026
SAVED_DIR = "ah_raw_bill"  # Directory to save bills

if not PHONE or not PASSWORD:
    raise ValueError("Thiếu EVN_AH_USERNAME hoặc EVN_AH_PASSWORD trong file .env")


# ─── Helpers ──────────────────────────────────────────────────────────────────

def try_click(page, selectors, timeout=1000):
    """Try clicking the first visible element from a list of CSS/text selectors."""
    # Fast-path: Check immediately visible elements
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if loc.is_visible():
                loc.click()
                print(f"  [OK] Clicked (immediate): {sel}")
                return True
        except Exception:
            continue

    # Slow-path: Wait with a short timeout
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            loc.wait_for(state="visible", timeout=timeout)
            loc.click()
            print(f"  [OK] Clicked (waited): {sel}")
            return True
        except Exception:
            continue
    return False


def open_makh_modal(page):
    """
    Try to open the 'Chọn Mã KH' modal on the HDDT page.
    We click the openChonMaKH elements to ensure the select callback gets bound in Javascript.
    Returns True when modal is visible, False otherwise.
    """
    trigger_selectors = [
        "input.openChonMaKH",   # input box that opens modal and binds callback
        "i.openChonMaKH",       # dropdown icon
        "input.input-maKH",
        ".openChonMaKH",
        "label:has-text('Mã khách hàng')", # fallback label
    ]

    print("\n[INFO] Opening 'Mã khách hàng' modal...")
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
    """
    Extract all customer codes from the modal.
    """
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


# ─── Main ─────────────────────────────────────────────────────────────────────

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=False,
        slow_mo=100,  # Faster interaction speed
    )

    context = browser.new_context(
        locale="vi-VN",
        viewport={"width": 1280, "height": 800},
    )

    page = context.new_page()

    # Step 1: Login
    print("Đang mở website EVNHCMC...")
    page.goto(
        "https://www.evnhcmc.vn/",
        wait_until="domcontentloaded",
        timeout=60000,
    )

    print("Đang chờ form đăng nhập...")
    page.wait_for_selector("form.form-dangnhap-trangchu", timeout=30000)

    login_form = page.locator("form.form-dangnhap-trangchu")
    login_form.locator(".input-user").fill(PHONE)
    login_form.locator(".input-pass").fill(PASSWORD)
    login_form.locator("button[type='submit']").click()

    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except PlaywrightTimeoutError:
        pass

    # Step 2: Navigate to CRAWL_PAGE
    print(f"\nĐang mở trang tra cứu: {CRAWL_PAGE}")
    page.goto(
        CRAWL_PAGE,
        wait_until="domcontentloaded",
        timeout=60000,
    )

    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except PlaywrightTimeoutError:
        pass

    # Verify session
    if "dangnhap" in page.url.lower() or "login" in page.url.lower():
        print("[ERROR] Bị chuyển hướng sang trang đăng nhập!")
        browser.close()
        exit(1)

    # Get customer codes from modal
    if open_makh_modal(page):
        codes = extract_customer_codes(page)
        # Close the modal
        close_btn = page.locator("#modalChonMAKH .close, #modalChonMAKH [data-dismiss='modal']").first
        if close_btn.count() > 0 and close_btn.is_visible():
            close_btn.click()
            page.locator("#modalChonMAKH").wait_for(state="hidden", timeout=3000)
    else:
        print("[ERROR] Không thể mở modal chọn khách hàng!")
        browser.close()
        exit(1)

    if not codes:
        print("[WARN] Không tìm thấy mã khách hàng nào!")
        browser.close()
        exit(0)

    print(f"\n✓ Lấy được {len(codes)} mã khách hàng: {codes}")
    
    current_year = datetime.now().year
    target_pattern = f"{TARGET_MONTH:02d}/{current_year}"
    print(f"Mục tiêu tìm kiếm hóa đơn tháng/năm: {target_pattern}")

    # Step 3: Loop through all clients
    for idx, client_code in enumerate(codes, 1):
        print(f"\n[{idx}/{len(codes)}] Bắt đầu xử lý khách hàng: {client_code}")

        try:
            # Open selection modal (click the input field to trigger the JS callback registration)
            if not open_makh_modal(page):
                print(f"  [ERROR] Không thể mở modal chọn mã khách hàng cho {client_code}. Bỏ qua.")
                continue

            print(f"Selecting client: {client_code}")
            # Select client item in the modal
            client_item = page.locator(f"#modalChonMAKH div.item[ma_pe='{client_code}']").first
            client_item.click()
            
            # Wait for modal to close
            page.locator("#modalChonMAKH").wait_for(state="hidden", timeout=5000)
            
            # Explicitly verify that the selected client has changed
            confirmed = False
            for attempt in range(10):  # 5 seconds max
                displayed_code = page.locator("input.input-maKH").first.input_value().strip()
                if displayed_code == client_code:
                    confirmed = True
                    break
                page.wait_for_timeout(500)
                
            if not confirmed:
                print(f"  [WARN] Mã khách hàng trên trang ({displayed_code}) không khớp với mã đã chọn ({client_code})! Đang thử click lại...")
                # Try opening and clicking again
                if open_makh_modal(page):
                    page.locator(f"#modalChonMAKH div.item[ma_pe='{client_code}']").first.click()
                    page.locator("#modalChonMAKH").wait_for(state="hidden", timeout=5000)
                    displayed_code = page.locator("input.input-maKH").first.input_value().strip()
                    if displayed_code == client_code:
                        confirmed = True
            
            if not confirmed:
                print(f"  [ERROR] Xác nhận khách hàng thất bại! Trang vẫn hiển thị: {displayed_code}. Bỏ qua khách hàng này.")
                continue
                
            # Log displayed client details
            displayed_name = page.locator(".thongTin_maKH_dangchon .ten").first.inner_text().strip()
            print(f"Page now displays client: {displayed_code} - {displayed_name}")
            
            # Wait for billing table to reload
            print("  Đang chờ bảng dữ liệu hóa đơn tải xong...")
            page.wait_for_timeout(1000)  # Let AJAX start
            page.wait_for_load_state("networkidle", timeout=15000)
            
            # Extract rows
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
                print(f"  [INFO] Không tìm thấy hóa đơn {target_pattern} cho khách hàng {client_code}.")
                continue
            
            print(f"Target month found: {target_pattern}")
            print(f"  Tìm thấy {len(matching_rows)} hóa đơn phù hợp:")
            for _, text in matching_rows:
                print(f"    - {text}")

            # Download bill for each matching row
            for row, month_year_text in matching_rows:
                # Parse period number (Đợt)
                period_match = re.search(r"Đợt\s*(\d+)", month_year_text)
                period = period_match.group(1) if period_match else "unknown"
                
                print(f"Downloading PDF...")
                print(f"  Đang tải hóa đơn {month_year_text} (Đợt {period})...")
                
                # Find download button in the row
                download_btn = row.locator(".btn-download-hd, [class*='download-hd'], img[title='Tải về']").first
                if download_btn.count() == 0:
                    print("    [ERROR] Không tìm thấy nút tải về trong dòng này!")
                    continue
                
                # Open download option modal
                download_btn.click()
                
                # Wait for download modal
                page.wait_for_selector("#modalDownLoadHoaDon", state="visible", timeout=10000)
                
                # Get the "BẢN CHI TIẾT (.PDF)" download element
                pdf_detail_btn = page.locator("#modalDownLoadHoaDon div.btn-download-HDDT[loaihd='PDF-ChiTiet']").first
                if pdf_detail_btn.count() == 0:
                    print("    [ERROR] Không tìm thấy nút tải BẢN CHI TIẾT (.PDF) trong modal!")
                    close_btn = page.locator("#modalDownLoadHoaDon .close").first
                    if close_btn.is_visible():
                        close_btn.click()
                    continue
                
                # Execute download
                try:
                    with page.expect_download(timeout=30000) as download_info:
                        pdf_detail_btn.click()
                    download = download_info.value
                    
                    # Ensure destination folder exists
                    client_dir = os.path.join(SAVED_DIR, client_code)
                    os.makedirs(client_dir, exist_ok=True)
                    save_path = os.path.join(client_dir, f"period_{period}.pdf")
                    
                    download.save_as(save_path)
                    print(f"    [OK] Đã lưu: {save_path}")
                except Exception as e:
                    print(f"    [ERROR] Lỗi khi tải file: {e}")
                
                # Close download modal
                close_btn = page.locator("#modalDownLoadHoaDon .close").first
                if close_btn.is_visible():
                    close_btn.click()
                page.locator("#modalDownLoadHoaDon").wait_for(state="hidden", timeout=5000)

        except Exception as e:
            print(f"  [ERROR] Lỗi xử lý khách hàng {client_code}: {e}")
            # Cleanup: make sure modals are closed
            try:
                for modal_id in ["#modalChonMAKH", "#modalDownLoadHoaDon"]:
                    close_btn = page.locator(f"{modal_id} .close").first
                    if close_btn.is_visible():
                        close_btn.click()
            except:
                pass

    print("\n✓ Đã hoàn thành tải tất cả hóa đơn!")
    browser.close()