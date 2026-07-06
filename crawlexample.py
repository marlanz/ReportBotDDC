import base64
import json
import os
import re
import urllib.request
from datetime import datetime
import io

import pandas as pd
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

# === THÊM THƯ VIỆN OCR ===
import pytesseract
from PIL import Image

# =========================
# CONFIG
# =========================


LOGIN_URL = (
    "https://www.cskh.evnspc.vn/TaiKhoan/DangNhap"
    "?previousLink=%2FTraCuu%2FHoaDonTienDien"
)
BILL_URL = "https://www.cskh.evnspc.vn/TraCuu/HoaDonTienDien"
OUTPUT_XLSX = os.path.join(os.path.expanduser("~"), "Desktop", "hoa_don_tien_dien_evnspc.xlsx")

# Cấu hình đường dẫn Tesseract (Bỏ comment và sửa đường dẫn nếu bạn dùng Windows)



def log(message: str) -> None:
    print(message, flush=True)


def normalize_space(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_money_to_number(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    if digits.isdigit():
        try:
            return int(digits)
        except Exception:
            return value
    return value


def parse_int_in_range(value, min_value, max_value):
    text = str(value).strip()
    if not text.isdigit():
        return None
    n = int(text)
    if min_value <= n <= max_value:
        return n
    return None


def pick_existing_browser():
    for path in CHROME_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def try_fill(page, selectors, value, timeout=6000):
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            loc.wait_for(state="visible", timeout=timeout)
            loc.fill(value)
            return True
        except Exception:
            continue
    return False


def try_click(page, selectors, timeout=5000):
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            loc.wait_for(state="visible", timeout=timeout)
            loc.click()
            return True
        except Exception:
            continue
    return False


def try_get_visible_text(page, selectors):
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if loc.count() > 0 and loc.is_visible():
                txt = normalize_space(loc.inner_text())
                if txt:
                    return txt
        except Exception:
            continue
    return ""


def get_selected_value(page, selector):
    loc = page.locator(selector).first
    if loc.count() == 0:
        return ""
    try:
        val = loc.input_value().strip()
        if val:
            return val
    except Exception:
        pass
    try:
        return normalize_space(loc.locator("option:checked").first.inner_text())
    except Exception:
        return ""


def ask_input(label, default=""):
    prompt = f"{label}"
    if default != "":
        prompt += f" [Enter={default}]"
    prompt += ": "
    try:
        value = input(prompt).strip()
    except Exception:
        value = ""
    return value if value else default


def ask_yes_no(label, default=False):
    default_hint = "Y/n" if default else "y/N"
    try:
        value = input(f"{label} [{default_hint}]: ").strip().lower()
    except Exception:
        value = ""
    if not value:
        return default
    return value in {"y", "yes", "1", "co", "c"}


def ensure_credentials():
    username = EVN_USERNAME or ask_input("Nhap EVN_USERNAME")
    password = EVN_PASSWORD or ask_input("Nhap EVN_PASSWORD")
    if not username or not password:
        raise ValueError("Thieu thong tin dang nhap (username/password).")
    return username, password


def ensure_screen_dir():
    if ENABLE_SCREENSHOT:
        os.makedirs(SCREEN_DIR, exist_ok=True)


def snap(page, tag, locator_selector=None):
    if not ENABLE_SCREENSHOT:
        return None
    ensure_screen_dir()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(SCREEN_DIR, f"{ts}_{tag}.png")
    try:
        if locator_selector:
            loc = page.locator(locator_selector).first
            if loc.count() > 0 and loc.is_visible():
                loc.screenshot(path=path)
                log(f"[SHOT] {path}")
                return path
        page.screenshot(path=path, full_page=True)
        log(f"[SHOT] {path}")
        return path
    except Exception as exc:
        log(f"[WARN] Khong chup duoc anh ({tag}): {exc}")
        return None


def gemini_describe_image(image_path, prompt):
    if not GEMINI_API_KEY or not ENABLE_GEMINI_DIAG or not image_path or not os.path.exists(image_path):
        return ""
    try:
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("utf-8")

        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
        )
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {"inline_data": {"mime_type": "image/png", "data": img_b64}},
                    ]
                }
            ]
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=35) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))

        candidates = data.get("candidates", [])
        if not candidates:
            return ""
        parts = candidates[0].get("content", {}).get("parts", [])
        if not parts:
            return ""
        return normalize_space(parts[0].get("text", ""))
    except Exception as exc:
        log(f"[WARN] Gemini diagnose failed: {exc}")
        return ""


def parse_json_from_text(raw_text):
    if not raw_text:
        return {}

    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass

    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
        if isinstance(data, dict):
            return data
    except Exception:
        return {}
    return {}


def gemini_extract_invoice_data(image_path):
    if not ENABLE_GEMINI_EXTRACT:
        return {}

    prompt = (
        "Doc anh bang hoa don EVNSPC va tra ve JSON hop le, khong markdown, khong giai thich.\n"
        "Schema:\n"
        "{\n"
        '  "invoice_count": 0,\n'
        '  "rows": [\n'
        "    {\n"
        '      "stt": "",\n'
        '      "ma_khach_hang": "",\n'
        '      "id_hoa_don": "",\n'
        '      "ky_hieu_so_hoa_don": "",\n'
        '      "tong_tien_vnd": "",\n'
        '      "loai_hoa_don": ""\n'
        "    }\n"
        "  ],\n"
        '  "notes": ""\n'
        "}\n"
        "Neu khong doc duoc thi rows=[] va notes mo ta ly do. Khong doan noi dung khong thay ro."
    )

    raw = gemini_describe_image(image_path, prompt)
    parsed = parse_json_from_text(raw)
    if not parsed:
        return {"invoice_count": 0, "rows": [], "notes": "Gemini khong tra JSON hop le."}
    if "rows" not in parsed or not isinstance(parsed.get("rows"), list):
        parsed["rows"] = []
    if "invoice_count" not in parsed:
        parsed["invoice_count"] = len(parsed["rows"])
    return parsed

# === HÀM XỬ LÝ OCR CAPTCHA ===
# === HÀM XỬ LÝ OCR CAPTCHA ===
def solve_captcha_with_ocr(page, selector="#imgCaptcha"):
    """Chụp ảnh captcha và dùng OCR để đọc chữ"""
    try:
        # Báo cho Tesseract biết chính xác thư mục tessdata nằm ở đâu
        os.environ["TESSDATA_PREFIX"] = r"C:\Program Files\Tesseract-OCR\tessdata"
        
        captcha_element = page.locator(selector)
        img_bytes = captcha_element.screenshot()
        
        # Chuyển sang ảnh PIL và tiền xử lý (grayscale)
        img = Image.open(io.BytesIO(img_bytes)).convert('L')
        
        # Chỉ dùng --psm 7, không cần --tessdata-dir nữa vì đã set ở trên
        captcha_text = pytesseract.image_to_string(img, config='--psm 7').strip()
        
        # Chỉ giữ lại chữ và số, loại bỏ ký tự đặc biệt do nhiễu
        captcha_text = "".join(filter(str.isalnum, captcha_text))
        
        return captcha_text
    except Exception as e:
        log(f"[WARN] Lỗi khi giải captcha bằng OCR: {e}")
        return ""


def login_with_manual_captcha(page, username, password, max_attempts=10):
    log(">> Mo trang dang nhap EVNSPC")
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_selector("#Username, input[name='Username']", timeout=30000)

    if not try_fill(page, ["#Username", "input[name='Username']"], username):
        raise RuntimeError("Khong tim thay o Username.")
    if not try_fill(page, ["#Password", "input[name='Password']"], password):
        raise RuntimeError("Khong tim thay o Password.")

    log(">> Bat dau quy trinh giai Captcha (Uu tien OCR tu dong)...")
    for attempt in range(1, max_attempts + 1):
        snap(page, f"login_full_attempt_{attempt}")
        cap_img = snap(page, f"captcha_attempt_{attempt}", locator_selector="#imgCaptcha")

        if cap_img:
            log(f"   [INFO] Da luu captcha tai: {cap_img}")

        if ENABLE_GEMINI_DIAG and cap_img:
            diag = gemini_describe_image(
                cap_img,
                (
                    "Mo ta ngan gon chat luong captcha (mo/ro, co bi che khong). "
                    "Khong doan ky tu captcha."
                ),
            )
            if diag:
                log(f"   [Gemini-Diag] {diag}")

        # --- THỬ GIẢI BẰNG OCR ---
        log(f"   [Lần {attempt}] Đang dùng OCR để đọc Captcha...")
        captcha_value = solve_captcha_with_ocr(page, "#imgCaptcha")
        
        # Nếu OCR không ra kết quả hoặc kết quả có vẻ sai (quá ngắn), yêu cầu nhập tay
        if len(captcha_value) >= 4:
            log(f"   [OCR] Đọc thành công: {captcha_value}. Đang thử submit...")
        else:
            log(f"   [OCR] Đọc thất bại hoặc text quá ngắn ('{captcha_value}'). Chuyển sang nhập tay.")
            captcha_value = ask_input(
                f"[Lan {attempt}/{max_attempts}] Nhap captcha (r=refresh, q=thoat)"
            ).strip()

        if not captcha_value:
            log("   [WARN] Ban chua nhap captcha.")
            continue
        if captcha_value.lower() in {"q", "quit", "exit"}:
            raise RuntimeError("Nguoi dung dung quy trinh dang nhap.")
        if captcha_value.lower() in {"r", "refresh"}:
            try_click(
                page,
                [
                    "a:has-text('Lay lai captcha')",
                    "a:has-text('Lấy lại captcha')",
                    "a[href*='RefreshCaptcha']",
                ],
            )
            page.wait_for_timeout(500)
            continue

        if not try_fill(page, ["#clientCaptcha", "input[name='clientCaptcha']"], captcha_value):
            raise RuntimeError("Khong tim thay o captcha (#clientCaptcha).")

        if not try_click(
            page,
            [
                "#btnDangNhap",
                "input#btnDangNhap",
                "input[value*='Dang nhap']",
                "input[value*='Đăng nhập']",
            ],
        ):
            raise RuntimeError("Khong tim thay nut dang nhap.")

        page.wait_for_timeout(2500)
        try:
            page.wait_for_load_state("networkidle", timeout=9000)
        except PlaywrightTimeoutError:
            pass

        current_url = page.url.lower()
        if "/tracuu/hoadontiendien" in current_url and "/taikhoan/dangnhap" not in current_url:
            log(">> Dang nhap thanh cong.")
            snap(page, "login_success")
            return

        captcha_error = try_get_visible_text(page, ["#idThongBaoCaptcha"])
        login_error = try_get_visible_text(page, ["#idNoiDungThongBaoModalClose"])
        if captcha_error:
            log(f"   [WARN] Captcha khong hop le: {captcha_error}")
        if login_error:
            log(f"   [WARN] Loi dang nhap: {login_error}")
            try_click(
                page,
                [
                    "#FinishModalClose .btn-close",
                    "#FinishModalClose input[value*='Ket thuc']",
                    "#FinishModalClose input[value*='Kết thúc']",
                ],
                timeout=2000,
            )
            
        # NẾU LỖI LÀ DO CAPTCHA OCR SAI, LÀM MỚI CAPTCHA ĐỂ LƯỢT SAU OCR ẢNH MỚI
        if captcha_error:
            try_click(
                page,
                [
                    "a:has-text('Lay lai captcha')",
                    "a:has-text('Lấy lại captcha')",
                    "a[href*='RefreshCaptcha']",
                ],
            )
            page.wait_for_timeout(2500)

        if ENABLE_GEMINI_DIAG:
            err_shot = snap(page, f"login_error_{attempt}")
            diag = gemini_describe_image(
                err_shot,
                "Doc nhanh thong bao loi dang nhap tren trang va tom tat 1 cau.",
            )
            if diag:
                log(f"   [Gemini-Diag] {diag}")

        try_fill(page, ["#Username", "input[name='Username']"], username, timeout=1500)
        try_fill(page, ["#Password", "input[name='Password']"], password, timeout=1500)
        try_fill(page, ["#clientCaptcha", "input[name='clientCaptcha']"], "", timeout=1500)

    raise RuntimeError("Dang nhap that bai sau nhieu lan thu captcha.")


def configure_month_year(page):
    try:
        page.wait_for_selector("#month, #year", timeout=15000)
    except PlaywrightTimeoutError:
        log("[WARN] Khong tim thay bo loc thang/nam.")
        return {"month": "", "year": ""}

    default_month = EVN_MONTH or get_selected_value(page, "#month") or str(datetime.now().month)
    default_year = EVN_YEAR or get_selected_value(page, "#year") or str(datetime.now().year)

    month_in = ask_input("Thang can lay", default_month)
    year_in = ask_input("Nam can lay", default_year)

    month_val = parse_int_in_range(month_in, 1, 12)
    year_val = parse_int_in_range(year_in, 2000, 2100)
    selected = {
        "month": str(month_val) if month_val is not None else str(default_month),
        "year": str(year_val) if year_val is not None else str(default_year),
    }

    try:
        page.locator("#month").first.select_option(value=selected["month"])
    except Exception:
        pass
    try:
        page.locator("#year").first.select_option(value=selected["year"])
    except Exception:
        pass

    log(f">> Loc hoa don: thang={selected['month']} nam={selected['year']}")
    return selected


def get_three_parts(page):
    options = []
    try:
        option_values = page.locator("#part option").all_attribute_values("value")
        for v in option_values:
            n = parse_int_in_range(v, 1, 12)
            if n is not None:
                options.append(n)
    except Exception:
        pass
    options = sorted(set(options))
    if not options:
        return [1, 2, 3]
    return options[:3]


def apply_filters(page, part, month, year):
    try:
        page.locator("#part").first.select_option(value=str(part))
    except Exception:
        pass
    try:
        page.locator("#month").first.select_option(value=str(month))
    except Exception:
        pass
    try:
        page.locator("#year").first.select_option(value=str(year))
    except Exception:
        pass


def trigger_lookup(page):
    clicked = try_click(
        page,
        [
            "#idTraCuuHoaDon",
            "input#idTraCuuHoaDon",
            "button:has-text('Tra cuu')",
            "button:has-text('Tra cứu')",
            "input[type='button'][value*='Tra']",
            "input[type='submit'][value*='Tra']",
        ],
        timeout=4500,
    )
    if clicked:
        page.wait_for_timeout(1000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except PlaywrightTimeoutError:
            pass
    else:
        log("[WARN] Khong thay nut tra cuu, tiep tuc doc du lieu hien co.")


def parse_invoice_ids_from_onclick(text):
    if not text:
        return {}
    match = re.search(
        r"\(\s*'?(?P<id>\d+)'?\s*,\s*'?(?P<ky>\d+)'?\s*,\s*'?(?P<thang>\d+)'?\s*,\s*'?(?P<nam>\d{4})'?\s*\)",
        text,
    )
    if not match:
        return {}
    return {
        "ID_HDON": match.group("id"),
        "Ky": match.group("ky"),
        "Thang": match.group("thang"),
        "Nam": match.group("nam"),
    }


def extract_invoice_rows(page):
    table = page.locator("#idThongTinHoaDonTienDien table:visible").first
    if table.count() == 0:
        table = page.locator("table:visible").first
    if table.count() == 0:
        return []

    headers = [normalize_space(h) for h in table.locator("thead tr th").all_inner_texts()]
    headers = [h for h in headers if h]

    tr_list = table.locator("tbody tr")
    if tr_list.count() == 0:
        tr_list = table.locator("tr")

    rows = []
    for i in range(tr_list.count()):
        tr = tr_list.nth(i)
        if tr.locator("th").count() > 0 and tr.locator("td").count() == 0:
            continue
        cells = [normalize_space(x) for x in tr.locator("td").all_inner_texts()]
        if not cells:
            continue

        row = {}
        if headers and len(headers) >= len(cells):
            for idx, cell in enumerate(cells):
                row[headers[idx]] = cell
        else:
            for idx, cell in enumerate(cells, start=1):
                row[f"Cot_{idx}"] = cell
        row["_raw"] = " | ".join(cells)

        view_onclick = ""
        down_onclick = ""
        try:
            a = tr.locator("a.view-btn").first
            if a.count() > 0:
                view_onclick = a.get_attribute("onclick") or ""
        except Exception:
            pass
        try:
            a = tr.locator("a.download-btn").first
            if a.count() > 0:
                down_onclick = a.get_attribute("onclick") or ""
        except Exception:
            pass
        row["_onclick_view"] = view_onclick
        row["_onclick_download"] = down_onclick

        ids = parse_invoice_ids_from_onclick(view_onclick) or parse_invoice_ids_from_onclick(down_onclick)
        if ids:
            row.update(ids)
        rows.append(row)
    return rows


def convert_numeric_columns(df):
    if df.empty:
        return df
    for col in df.columns:
        low = col.lower()
        if any(k in low for k in ["tien", "tong", "kwh", "san luong", "so hoa don", "id hoa don"]):
            df[col] = df[col].apply(normalize_money_to_number)
    return df


def fetch_three_invoice_parts(page, month, year):
    parts = get_three_parts(page)
    log(f">> Se lay {len(parts)} ky hoa don: {parts}")

    all_rows = []
    part_summary = []
    gemini_rows = []

    for idx, part in enumerate(parts, start=1):
        log(f">> [{idx}/{len(parts)}] Dang tra cuu ky={part}, thang={month}, nam={year}")
        apply_filters(page, part=part, month=month, year=year)
        trigger_lookup(page)

        try:
            page.wait_for_selector("#idThongTinHoaDonTienDien table, table:visible", timeout=15000)
        except PlaywrightTimeoutError:
            log("[WARN] Khong thay bang du lieu sau khi tra cuu.")

        result_shot = snap(page, f"result_part_{part}", locator_selector="#idThongTinHoaDonTienDien")
        notice = try_get_visible_text(page, ["#idSoThongTinHoaDon"])
        rows = extract_invoice_rows(page)

        for row in rows:
            row["Ky_loc"] = str(part)
            row["Thang_loc"] = str(month)
            row["Nam_loc"] = str(year)
        all_rows.extend(rows)

        gemini_count = 0
        gemini_note = ""
        if result_shot and GEMINI_API_KEY:
            gemini_data = gemini_extract_invoice_data(result_shot)
            gemini_note = normalize_space(gemini_data.get("notes", ""))
            g_rows = gemini_data.get("rows", [])
            if isinstance(g_rows, list):
                for gr in g_rows:
                    if not isinstance(gr, dict):
                        continue
                    gr["Ky_loc"] = str(part)
                    gr["Thang_loc"] = str(month)
                    gr["Nam_loc"] = str(year)
                    gr["source"] = "gemini_image"
                    gemini_rows.append(gr)
                gemini_count = len(g_rows)

        part_summary.append(
            {
                "part": str(part),
                "month": str(month),
                "year": str(year),
                "row_count": len(rows),
                "gemini_row_count": gemini_count,
                "gemini_note": gemini_note,
                "notice": notice,
            }
        )
        log(f"   -> Ky {part}: DOM={len(rows)} dong, Gemini={gemini_count} dong")

    return all_rows, part_summary, gemini_rows


def main():
    log("=== EVNSPC Invoice Collector (Integrated OCR) ===")
    log("Workflow: dang nhap -> OCR auto -> fallback thu cong -> lay 3 ky -> xuat Excel")
    if ENABLE_SCREENSHOT:
        log(f"[INFO] Screenshot ON, folder: {SCREEN_DIR}")
    else:
        log("[INFO] Screenshot OFF")
    if ENABLE_GEMINI_DIAG:
        log("[INFO] Gemini diagnostics ON (khong giai captcha).")
    else:
        log("[INFO] Gemini diagnostics OFF")
    if ENABLE_GEMINI_EXTRACT:
        log("[INFO] Gemini extraction ON (phan tich anh bang hoa don).")
    else:
        log("[INFO] Gemini extraction OFF")

    username, password = ensure_credentials()

    with sync_playwright() as playwright:
        launch_kwargs = {"headless": False}
        browser_path = pick_existing_browser()
        if browser_path:
            launch_kwargs["executable_path"] = browser_path

        browser = playwright.chromium.launch(**launch_kwargs)
        context = browser.new_context(locale="vi-VN")
        page = context.new_page()

        try:
            login_with_manual_captcha(page, username, password)

            if "/tracuu/hoadontiendien" not in page.url.lower():
                page.goto(BILL_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(1200)

            selected = configure_month_year(page)
            rows, part_summary, gemini_rows = fetch_three_invoice_parts(
                page,
                month=selected.get("month", ""),
                year=selected.get("year", ""),
            )

            df = pd.DataFrame(rows)
            df = convert_numeric_columns(df)
            summary_df = pd.DataFrame(part_summary)
            gemini_df = pd.DataFrame(gemini_rows)

            invoice_notice = try_get_visible_text(page, ["#idSoThongTinHoaDon"])
            meta_df = pd.DataFrame(
                [
                    {"key": "crawl_time", "value": datetime.now().isoformat(timespec="seconds")},
                    {"key": "source_url", "value": page.url},
                    {"key": "invoice_notice", "value": invoice_notice},
                    {"key": "total_rows", "value": len(df)},
                ]
            )

            with pd.ExcelWriter(OUTPUT_XLSX) as writer:
                df.to_excel(writer, sheet_name="Hoa_don_3_ky", index=False)
                gemini_df.to_excel(writer, sheet_name="Gemini_Extracted", index=False)
                summary_df.to_excel(writer, sheet_name="Tong_hop_3_ky", index=False)
                meta_df.to_excel(writer, sheet_name="Meta", index=False)

            log(f"OK - Da xuat file: {OUTPUT_XLSX}")
            if not gemini_df.empty:
                log(f"[Gemini] Da tra du lieu {len(gemini_df)} dong tu anh.")
                preview = gemini_df.head(3).to_dict(orient="records")
                log(f"[Gemini] Preview: {preview}")
            else:
                log("[Gemini] Chua co dong du lieu trich tu anh.")

            keep_open = ask_yes_no("Ban muon giu trinh duyet mo de kiem tra", default=False)
            if keep_open:
                input("Nhan Enter de dong trinh duyet...")
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()