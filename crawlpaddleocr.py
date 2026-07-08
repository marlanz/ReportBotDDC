"""
crawlpaddleocr.py
=================
Đọc các file PDF hóa đơn tiền điện EVNSPC trong thư mục test_pdf,
sử dụng PaddleOCR API (thay thế Gemini) để nhận dạng văn bản,
trích xuất các trường dữ liệu cần thiết rồi xuất ra file JSON.

Yêu cầu:
    pip install requests

Sử dụng:
    python crawlpaddleocr.py
    python crawlpaddleocr.py --pdf-dir path/to/pdfs --output output.json
"""

import json
import os
import re
import sys
import time
import argparse
from datetime import datetime
from html.parser import HTMLParser

import requests
from dotenv import load_dotenv

# =========================
# CONFIG
# =========================
load_dotenv()
JOB_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
TOKEN = os.getenv("PADDLEOCR_TOKEN").strip()
MODEL = os.getenv("PADDLEOCR_MODEL").strip()

# DEFAULT_PDF_DIR = os.path.join(os.path.dirname(__file__), "./raw_bill/hoa_don_ki_3_t6")
# DEFAULT_OUTPUT  = os.path.join(os.path.dirname(__file__), "./output_bill/hoa_don__ki3_t6_output.json")

DEFAULT_PDF_DIR = os.path.join(os.path.dirname(__file__), "./ah_raw_bill")
DEFAULT_OUTPUT  = os.path.join(os.path.dirname(__file__), "./output_bill/hoa_don_t6_ah_output.json")


POLL_INTERVAL_SEC = 5      # giây giữa các lần poll
MAX_POLL_ATTEMPTS = 120    # tối đa ~10 phút mỗi file


# =========================
# LOGGING
# =========================
def log(message: str) -> None:
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        # Fallback: encode to UTF-8 bytes and write directly to stdout buffer
        sys.stdout.buffer.write((message + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()


# =========================
# NORMALIZE HELPERS
# =========================
def normalize_space(value) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_money(value) -> int | None:
    """Chuyển chuỗi tiền VNĐ (vd: '68.145.333') thành số nguyên."""
    if value is None:
        return None
    text = re.sub(r"[^\d]", "", str(value))
    return int(text) if text.isdigit() else None


def clean_number(value) -> int | None:
    """Chuyển chuỗi số có dấu chấm/phẩy thành số nguyên (kWh, …)."""
    if value is None:
        return None
    text = re.sub(r"[^\d]", "", str(value))
    return int(text) if text.isdigit() else None


# =========================
# HTML TABLE PARSER
# =========================
class TableParser(HTMLParser):
    """Trích xuất các ô <td> từ một đoạn HTML bảng thành danh sách hàng."""

    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._current_row: list[str] = []
        self._in_td = False
        self._buf = ""

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._current_row = []
        elif tag in ("td", "th"):
            self._in_td = True
            self._buf = ""

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self._current_row.append(normalize_space(self._buf))
            self._in_td = False
            self._buf = ""
        elif tag == "tr":
            if self._current_row:
                self.rows.append(self._current_row)

    def handle_data(self, data):
        if self._in_td:
            self._buf += data


def parse_html_table(html: str) -> list[list[str]]:
    parser = TableParser()
    parser.feed(html)
    return parser.rows


# =========================
# PADDLEOCR API
# =========================
def _build_headers(json_mode: bool = False) -> dict:
    h = {"Authorization": f"bearer {TOKEN}"}
    if json_mode:
        h["Content-Type"] = "application/json"
    return h


def submit_pdf_job(pdf_path: str) -> str:
    """Gửi file PDF lên PaddleOCR API và trả về jobId."""
    optional_payload = {
        "useDocOrientationClassify": False,
        "useDocUnwarping": False,
        "useChartRecognition": False,
    }
    data = {
        "model": MODEL,
        "optionalPayload": json.dumps(optional_payload),
    }
    log(f"   [OCR] Uploading: {os.path.basename(pdf_path)}")
    with open(pdf_path, "rb") as f:
        resp = requests.post(
            JOB_URL,
            headers=_build_headers(),
            data=data,
            files={"file": f},
            timeout=60,
        )
    if resp.status_code != 200:
        raise RuntimeError(
            f"PaddleOCR submit failed ({resp.status_code}): {resp.text[:300]}"
        )
    job_id = resp.json()["data"]["jobId"]
    log(f"   [OCR] Job submitted. ID: {job_id}")
    return job_id


def poll_job(job_id: str) -> str:
    """Poll cho đến khi job xong và trả về URL kết quả JSONL."""
    for attempt in range(1, MAX_POLL_ATTEMPTS + 1):
        resp = requests.get(f"{JOB_URL}/{job_id}", headers=_build_headers(), timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"Poll failed ({resp.status_code}): {resp.text[:200]}")
        data = resp.json()["data"]
        state = data["state"]

        if state == "pending":
            log(f"   [OCR] [{attempt}] Pending…")
        elif state == "running":
            try:
                total = data["extractProgress"]["totalPages"]
                done  = data["extractProgress"]["extractedPages"]
                log(f"   [OCR] [{attempt}] Running… {done}/{total} pages")
            except (KeyError, TypeError):
                log(f"   [OCR] [{attempt}] Running…")
        elif state == "done":
            result_url = data["resultUrl"]["jsonUrl"]
            log(f"   [OCR] Done. Result URL obtained.")
            return result_url
        elif state == "failed":
            error = data.get("errorMsg", "unknown error")
            raise RuntimeError(f"OCR job failed: {error}")
        else:
            log(f"   [OCR] Unknown state: {state}")

        time.sleep(POLL_INTERVAL_SEC)

    raise TimeoutError(f"OCR job {job_id} did not complete within timeout.")


def download_markdown(result_url: str) -> str:
    """Tải về kết quả JSONL và ghép nối toàn bộ markdown text."""
    resp = requests.get(result_url, timeout=60)
    resp.raise_for_status()
    pages_md: list[str] = []
    for line in resp.text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        result = json.loads(line).get("result", {})
        for layout in result.get("layoutParsingResults", []):
            md = layout.get("markdown", {}).get("text", "")
            if md:
                pages_md.append(md)
    return "\n\n".join(pages_md)


def ocr_pdf(pdf_path: str) -> str:
    """Toàn bộ quy trình OCR một file PDF → trả về markdown string."""
    job_id = submit_pdf_job(pdf_path)
    result_url = poll_job(job_id)
    return download_markdown(result_url)


# =========================
# DATA EXTRACTION FROM MARKDOWN
# =========================

def _find_line_after(lines: list[str], keyword: str, max_gap: int = 3) -> str:
    """
    Tìm dòng chứa keyword, rồi trả về nội dung của dòng không rỗng tiếp theo
    trong phạm vi max_gap dòng.
    """
    for i, line in enumerate(lines):
        if keyword.lower() in line.lower():
            for j in range(i + 1, min(i + 1 + max_gap, len(lines))):
                val = normalize_space(lines[j])
                # bỏ qua dòng trống và dòng chỉ chứa thẻ HTML
                if val and not val.startswith("<"):
                    return val
    return ""


def _find_inline(lines: list[str], keyword: str) -> str:
    """
    Tìm giá trị ngay sau keyword trên cùng một dòng.
    Ví dụ: 'Điện thoại 0979556464' → '0979556464'
    """
    for line in lines:
        if keyword.lower() in line.lower():
            rest = re.split(keyword, line, flags=re.IGNORECASE, maxsplit=1)
            if len(rest) > 1:
                val = normalize_space(rest[1])
                if val:
                    return val
    return ""


def extract_invoice_data(markdown_text: str, pdf_filename: str) -> dict:
    """
    Trích xuất các trường hóa đơn từ markdown trả về bởi PaddleOCR.

    Trả về dict với các key:
        ma_khach_hang, ten_khach_hang, dia_chi, dien_thoai, email,
        ma_so_thue, dia_chi_su_dung_dien, cap_dien_ap,
        ky_hoa_don, so_hoa_don, ngay_hoa_don,
        tong_kwh, tong_tien_chua_thue, thue_gtgt_pct, thue_gtgt,
        tong_tien_thanh_toan, han_thanh_toan,
        kwh_binh_thuong, kwh_cao_diem, kwh_thap_diem,
        thanh_tien_binh_thuong, thanh_tien_cao_diem, thanh_tien_thap_diem,
        don_gia_binh_thuong, don_gia_cao_diem, don_gia_thap_diem,
        duoc_ky_boi, ngay_ky,
        source_file, crawl_time
    """
    lines = markdown_text.splitlines()

    # --- Khách hàng / Customer ---
    ten_kh = ""
    for i, line in enumerate(lines):
        # Tên khách hàng thường là heading ngay sau "Khách hàng"
        if "khách hàng" in line.lower() and not any(
            x in line.lower() for x in ["mã khách hàng", "tình hình", "số tiền"]
        ):
            for j in range(i + 1, min(i + 4, len(lines))):
                val = normalize_space(lines[j])
                if val and not val.startswith("<"):
                    # Bỏ tiêu đề markdown ##
                    ten_kh = re.sub(r"^#+\s*", "", val)
                    break
            if ten_kh:
                break

    dia_chi   = _find_line_after(lines, "Địa chỉ")
    dien_thoai = _find_inline(lines, "Điện thoại")
    email      = _find_inline(lines, "Email")
    ma_so_thue = _find_inline(lines, "Mã số thuế")
    dia_chi_sd = _find_line_after(lines, "Địa chỉ sử dụng điện")
    cap_dien_ap = _find_inline(lines, "Cấp điện áp")
    if not cap_dien_ap:
        cap_dien_ap = _find_line_after(lines, "Cấp điện áp")

    # --- Mã khách hàng ---
    ma_kh = ""
    for i, line in enumerate(lines):
        if "mã khách hàng" in line.lower():
            for j in range(i + 1, min(i + 4, len(lines))):
                val = normalize_space(lines[j])
                if val and not val.startswith("<") and re.match(r"^[A-Z0-9]+$", val.replace(" ", "")):
                    ma_kh = val
                    break
            if ma_kh:
                break
    # Fallback: lấy từ tên file
    if not ma_kh:
        m = re.search(r"PB\d+", pdf_filename)
        if m:
            ma_kh = m.group(0)

    # --- Kỳ hóa đơn ---
    ky_hoa_don = ""
    for line in lines:
        m = re.search(r"Kỳ hóa đơn[:\s]*(.*)", line, re.IGNORECASE)
        if m:
            ky_hoa_don = normalize_space(m.group(1))
            break

    # --- Số hóa đơn & ngày ---
    so_hoa_don = ""
    ngay_hoa_don = ""
    for line in lines:
        m = re.search(r"hóa đơn số\s+([\d]+)\s+ngày\s+(\d+\s+tháng\s+\d+\s+năm\s+\d+)", line, re.IGNORECASE)
        if m:
            so_hoa_don = m.group(1)
            ngay_hoa_don = normalize_space(m.group(2))
            break

    # --- Bảng điện năng & tiền (HTML tables trong markdown) ---
    html_tables = re.findall(r"<table[\s\S]*?</table>", markdown_text, re.IGNORECASE)

    # Bảng 1: Công tơ - điện tiêu thụ (có cột CÔNG TỔ/CÔNG TỘ)
    kwh_binh = kwh_cao = kwh_thap = 0
    tong_kwh = 0

    # Bảng 2: Đơn giá - thành tiền thanh toán
    don_gia_binh = don_gia_cao = don_gia_thap = None
    tt_binh = tt_cao = tt_thap = None
    tong_tien_chua_thue = None
    thue_gtgt_pct = ""
    thue_gtgt = None
    tong_thanh_toan = None

    for html in html_tables:
        rows = parse_html_table(html)
        if not rows:
            continue
        header = [c.lower() for c in rows[0]]

        # Phát hiện bảng 1: có cột "điện tiêu thụ" hoặc "chi số"
        if any("tiêu th" in h or "chi s" in h or "chí s" in h for h in header):
            for row in rows[1:]:
                if len(row) < 2:
                    continue
                label = row[0].lower()
                kwh_val = clean_number(row[-1]) if len(row) >= 5 else None
                if "bình thường" in label or "binh thuong" in label:
                    kwh_binh = kwh_val or 0
                elif "cao điểm" in label or "cao diem" in label:
                    kwh_cao = kwh_val or 0
                elif "thấp điểm" in label or "thap diem" in label:
                    kwh_thap = kwh_val or 0

        # Phát hiện bảng 2: có cột "đơn giá" hoặc "thành tiền"
        elif any("đơn gi" in h or "don gi" in h or "thành ti" in h or "thanh ti" in h for h in header):
            for row in rows[1:]:
                if len(row) < 2:
                    continue
                label = row[0].lower()
                if "bình thường" in label or "binh thuong" in label:
                    don_gia_binh = clean_number(row[1]) if len(row) > 1 else None
                    tt_binh      = normalize_money(row[-1]) if len(row) > 3 else None
                elif "cao điểm" in label or "cao diem" in label:
                    don_gia_cao = clean_number(row[1]) if len(row) > 1 else None
                    tt_cao      = normalize_money(row[-1]) if len(row) > 3 else None
                elif "thấp điểm" in label or "thap diem" in label:
                    don_gia_thap = clean_number(row[1]) if len(row) > 1 else None
                    tt_thap      = normalize_money(row[-1]) if len(row) > 3 else None
                elif "tổng điện năng" in label or "tong dien nang" in label:
                    tong_kwh = clean_number(row[2]) if len(row) > 2 else (kwh_binh + kwh_cao + kwh_thap)
                elif "chưa thuế" in label or "chua thue" in label:
                    tong_tien_chua_thue = normalize_money(row[-1])
                elif "thuế suất" in label or "thue suat" in label:
                    thue_gtgt_pct = normalize_space(row[-1])
                elif "thuế gtgt" in label or "thue gtgt" in label:
                    if "suất" not in label and "suat" not in label:
                        thue_gtgt = normalize_money(row[-1])
                elif "tổng cộng" in label or "tong cong" in label:
                    tong_thanh_toan = normalize_money(row[-1])

    # Nếu tổng kWh chưa tìm được từ bảng → tính lại
    if not tong_kwh:
        tong_kwh = kwh_binh + kwh_cao + kwh_thap
        # thử parse từ dòng "Tổng: xxx"
        for line in lines:
            m = re.search(r"Tổng[:\s]*([\d.,]+)", line, re.IGNORECASE)
            if m:
                v = clean_number(m.group(1))
                if v:
                    tong_kwh = v
                    break

    # --- Số tiền thanh toán (hiển thị lớn trong PDF) ---
    so_tien_tt = ""
    for i, line in enumerate(lines):
        if "số tiền thanh toán" in line.lower():
            for j in range(i + 1, min(i + 4, len(lines))):
                val = normalize_space(lines[j])
                if val and not val.startswith("<"):
                    so_tien_tt = val
                    break
            if so_tien_tt:
                break
    # Fallback: nếu tong_thanh_toan đã parse được từ bảng
    if not so_tien_tt and tong_thanh_toan:
        so_tien_tt = f"{tong_thanh_toan:,} đồng".replace(",", ".")

    # Fallback: nếu tong_thanh_toan vẫn null nhưng so_tien_hien_thi đọc được
    # → parse so_tien_hien_thi thành số nguyên để dùng làm tong_tien_thanh_toan
    if tong_thanh_toan is None and so_tien_tt:
        tong_thanh_toan = normalize_money(so_tien_tt)

    # --- Hạn thanh toán ---
    han_thanh_toan = _find_line_after(lines, "Hạn thanh toán")
    if not han_thanh_toan:
        for line in lines:
            m = re.search(r"\d{2}/\d{2}/\d{4}", line)
            if m:
                han_thanh_toan = m.group(0)
                break

    # --- Người ký & ngày ký ---
    duoc_ky_boi = ""
    ngay_ky = ""
    for line in lines:
        if "được ký bởi" in line.lower():
            m = re.search(r"Được ký bởi[:\s]*(.*)", line, re.IGNORECASE)
            if m:
                duoc_ky_boi = normalize_space(m.group(1))
        if "ngày ký" in line.lower():
            m = re.search(r"Ngày ký[:\s]*(.*)", line, re.IGNORECASE)
            if m:
                ngay_ky = normalize_space(m.group(1))

    return {
        "ma_khach_hang":          ma_kh,
        "ten_khach_hang":         ten_kh,
        "dia_chi":                dia_chi,
        "dien_thoai":             dien_thoai,
        "email":                  email,
        "ma_so_thue":             ma_so_thue,
        "dia_chi_su_dung_dien":   dia_chi_sd,
        "cap_dien_ap":            cap_dien_ap,
        "ky_hoa_don":             ky_hoa_don,
        "so_hoa_don":             so_hoa_don,
        "ngay_hoa_don":           ngay_hoa_don,
        "tong_kwh":               tong_kwh,
        "kwh_binh_thuong":        kwh_binh,
        "kwh_cao_diem":           kwh_cao,
        "kwh_thap_diem":          kwh_thap,
        "don_gia_binh_thuong":    don_gia_binh,
        "don_gia_cao_diem":       don_gia_cao,
        "don_gia_thap_diem":      don_gia_thap,
        "thanh_tien_binh_thuong": tt_binh,
        "thanh_tien_cao_diem":    tt_cao,
        "thanh_tien_thap_diem":   tt_thap,
        "tong_tien_chua_thue":    tong_tien_chua_thue,
        "thue_gtgt_phan_tram":    thue_gtgt_pct,
        "thue_gtgt":              thue_gtgt,
        "tong_tien_thanh_toan":   tong_thanh_toan,
        "so_tien_hien_thi":       so_tien_tt,
        "han_thanh_toan":         han_thanh_toan,
        "duoc_ky_boi":            duoc_ky_boi,
        "ngay_ky":                ngay_ky,
        "source_file":            pdf_filename,
        "crawl_time":             datetime.now().isoformat(timespec="seconds"),
    }


# =========================
# DISCOVER PDF FILES
# =========================
def find_pdf_files(pdf_dir: str) -> list[tuple[str, str]]:
    """
    Tìm tất cả file .pdf trong pdf_dir (bao gồm các thư mục con một cấp).
    Returns list of (pdf_path, client_code_from_folder).
    client_code_from_folder is the parent folder name if in a subfolder, else empty string.
    """
    pdf_files = []
    for entry in os.scandir(pdf_dir):
        if entry.is_file() and entry.name.lower().endswith(".pdf"):
            pdf_files.append((entry.path, ""))
        elif entry.is_dir():
            folder_client_code = entry.name  # e.g. "PE15000352029"
            for sub in os.scandir(entry.path):
                if sub.is_file() and sub.name.lower().endswith(".pdf"):
                    pdf_files.append((sub.path, folder_client_code))
    return sorted(pdf_files, key=lambda x: x[0])


# =========================
# MAIN
# =========================
def main():
    parser = argparse.ArgumentParser(
        description="Crawl electricity bill PDFs using PaddleOCR API"
    )
    parser.add_argument(
        "--pdf-dir",
        default=DEFAULT_PDF_DIR,
        help=f"Thư mục chứa các PDF hóa đơn (default: {DEFAULT_PDF_DIR})",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Đường dẫn file JSON đầu ra (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()

    log("=== EVNSPC Invoice Collector – PaddleOCR Edition ===")
    log(f"[INFO] PDF directory : {args.pdf_dir}")
    log(f"[INFO] Output file   : {args.output}")
    log(f"[INFO] OCR model     : {MODEL}")
    log("")

    # 1. Tìm các file PDF
    pdf_files = find_pdf_files(args.pdf_dir)
    if not pdf_files:
        log(f"[ERROR] Không tìm thấy file PDF nào trong: {args.pdf_dir}")
        sys.exit(1)
    log(f"[INFO] Tìm thấy {len(pdf_files)} file PDF.")

    # 2. OCR từng file → trích xuất dữ liệu
    all_records = []
    for idx, (pdf_path, folder_client_code) in enumerate(pdf_files, start=1):
        pdf_name = os.path.basename(pdf_path)
        display_name = f"{folder_client_code}/{pdf_name}" if folder_client_code else pdf_name
        log(f"\n>> [{idx}/{len(pdf_files)}] Đang xử lý: {display_name}")
        try:
            markdown_text = ocr_pdf(pdf_path)
            record = extract_invoice_data(markdown_text, pdf_name)
            # Override ma_khach_hang with folder name if available
            # (folder structure is the definitive source from the Playwright crawler)
            if folder_client_code:
                record["ma_khach_hang"] = folder_client_code
            all_records.append(record)
            log(f"   [OK] Mã KH: {record['ma_khach_hang']} | "
                f"Tổng tiền: {record['tong_tien_thanh_toan']:,} đồng"
                if record['tong_tien_thanh_toan'] else
                f"   [OK] Mã KH: {record['ma_khach_hang']} | Tổng tiền: N/A")
        except Exception as exc:
            log(f"   [ERROR] {display_name}: {exc}")
            all_records.append({
                "source_file": display_name,
                "ma_khach_hang": folder_client_code,
                "error": str(exc),
                "crawl_time": datetime.now().isoformat(timespec="seconds"),
            })

    # 3. Ghi file JSON
    output_data = {
        "crawl_time": datetime.now().isoformat(timespec="seconds"),
        "total_files": len(pdf_files),
        "success_count": sum(1 for r in all_records if "error" not in r),
        "clients": all_records,
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    log(f"\n>> Đã ghi {len(all_records)} bản ghi vào: {args.output}")
    log("=== Hoàn thành ===")


if __name__ == "__main__":
    main()