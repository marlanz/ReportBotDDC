"""
export_to_excel.py
==================
Đọc các file JSON hóa đơn tiền điện từ thư mục output_bill,
rồi xuất ra file Excel với mỗi sheet tương ứng một kỳ hóa đơn.

Tên sheet: ngày kết thúc của kỳ (vd: "10/06/2026")
Nội dung mỗi sheet:
    - Mã KH       (ma_khach_hang)
    - Tổng kWh    (tong_kwh)
    - Tổng tiền   (tong_tien_thanh_toan)

Yêu cầu:
    pip install openpyxl

Sử dụng:
    python export_to_excel.py
    python export_to_excel.py --input-dir output_bill --output hoa_don.xlsx
"""

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime

try:
    import openpyxl
    from openpyxl.styles import (
        Alignment,
        Border,
        Font,
        PatternFill,
        Side,
    )
    from openpyxl.utils import get_column_letter
except ImportError:
    print("[ERROR] Thư viện 'openpyxl' chưa được cài đặt.")
    print("        Chạy:  pip install openpyxl")
    sys.exit(1)


# =========================
# LOGGING
# =========================
def log(message: str) -> None:
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((message + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()


# =========================
# CONFIG
# =========================
DEFAULT_INPUT_DIR = os.path.join(os.path.dirname(__file__), "output_bill")
DEFAULT_OUTPUT = os.path.join(os.path.dirname(__file__), "hoa_don_export.xlsx")

# Màu header (xanh dương đậm)
HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)

# Màu tổng cộng
TOTAL_FILL = PatternFill("solid", fgColor="BDD7EE")
TOTAL_FONT = Font(bold=True, size=11)

# Viền ô
THIN_SIDE = Side(style="thin", color="AAAAAA")
THIN_BORDER = Border(
    left=THIN_SIDE, right=THIN_SIDE, top=THIN_SIDE, bottom=THIN_SIDE
)

CENTER = Alignment(horizontal="center", vertical="center")
RIGHT = Alignment(horizontal="right", vertical="center")
LEFT = Alignment(horizontal="left", vertical="center")


# =========================
# HELPERS
# =========================
def parse_end_date(ky_hoa_don: str) -> str:
    """
    Trích ngày kết thúc kỳ từ chuỗi ky_hoa_don.

    Ví dụ đầu vào:
        "Kỳ 1 - 6/2026 (10 ngày từ 01/06/2026 đến 10/06/2026)"
        "Ky 2 - 6/2026 (10 ngay tu 11/06/2026 den 20/06/2026)"

    Trả về: "10/06/2026"  (chuỗi ngày kết thúc)
    Trả về "" nếu không tìm thấy.
    """
    if not ky_hoa_don:
        return ""
    # Tìm tất cả chuỗi dd/mm/yyyy, lấy cái cuối cùng (= ngày kết thúc)
    dates = re.findall(r"\d{2}/\d{2}/\d{4}", ky_hoa_don)
    return dates[-1] if dates else ""


def sheet_name_from_end_date(end_date: str) -> str:
    """
    Chuyển "10/06/2026" → "10_06_2026" (tên sheet hợp lệ cho Excel,
    vì dấu / bị cấm trong tên sheet).
    """
    return end_date.replace("/", "_") if end_date else "Unknown"


def sort_key_for_date(date_str: str) -> datetime:
    """Chuyển dd_mm_yyyy hoặc dd/mm/yyyy thành datetime để sắp xếp."""
    normalized = date_str.replace("_", "/")
    try:
        return datetime.strptime(normalized, "%d/%m/%Y")
    except ValueError:
        return datetime.min


def format_vnd(amount) -> str:
    """Định dạng số thành chuỗi tiền VNĐ (có dấu chấm phân cách nghìn)."""
    if amount is None:
        return ""
    try:
        return f"{int(amount):,}".replace(",", ".")
    except (ValueError, TypeError):
        return str(amount)


# =========================
# EXCEL STYLING
# =========================
def apply_header(ws, headers: list[str], row: int = 1):
    """Ghi và tô màu hàng header."""
    for col_idx, title in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=col_idx, value=title)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = CENTER
        cell.border = THIN_BORDER


def apply_data_row(ws, row: int, values: list, alignments: list):
    """Ghi một hàng dữ liệu với căn lề và viền."""
    for col_idx, (val, align) in enumerate(zip(values, alignments), start=1):
        cell = ws.cell(row=row, column=col_idx, value=val)
        cell.alignment = align
        cell.border = THIN_BORDER


def apply_total_row(ws, row: int, values: list, alignments: list):
    """Ghi hàng tổng cộng với màu nền khác."""
    for col_idx, (val, align) in enumerate(zip(values, alignments), start=1):
        cell = ws.cell(row=row, column=col_idx, value=val)
        cell.fill = TOTAL_FILL
        cell.font = TOTAL_FONT
        cell.alignment = align
        cell.border = THIN_BORDER


def auto_fit_columns(ws):
    """Tự động điều chỉnh độ rộng cột theo nội dung."""
    for col_cells in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col_cells[0].column)
        for cell in col_cells:
            try:
                cell_len = len(str(cell.value)) if cell.value is not None else 0
                if cell_len > max_len:
                    max_len = cell_len
            except Exception:
                pass
        # Thêm padding; giới hạn tối đa 50
        adjusted = min(max_len + 4, 50)
        ws.column_dimensions[col_letter].width = adjusted


# =========================
# CORE LOGIC
# =========================
def load_all_json(input_dir: str) -> list[dict]:
    """Đọc tất cả file JSON trong input_dir và gộp danh sách clients."""
    pattern = os.path.join(input_dir, "*.json")
    json_files = sorted(glob.glob(pattern))
    if not json_files:
        log(f"[ERROR] Khong tim thay file JSON nao trong: {input_dir}")
        sys.exit(1)

    log(f"[INFO] Tim thay {len(json_files)} file JSON:")
    all_clients: list[dict] = []
    for path in json_files:
        log(f"       - {os.path.basename(path)}")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        clients = data.get("clients", [])
        all_clients.extend(clients)

    log(f"[INFO] Tong so ban ghi: {len(all_clients)}")
    return all_clients


def group_by_period(clients: list[dict]) -> dict[str, list[dict]]:
    """
    Nhóm clients theo kỳ hóa đơn (dùng ngày kết thúc kỳ làm key).
    Key: tên sheet dạng "10_06_2026"
    """
    groups: dict[str, list[dict]] = {}
    for client in clients:
        if "error" in client:
            continue  # bỏ qua bản ghi lỗi
        ky = client.get("ky_hoa_don", "")
        end_date = parse_end_date(ky)
        sheet_name = sheet_name_from_end_date(end_date) if end_date else "Unknown"
        groups.setdefault(sheet_name, []).append(client)
    return groups


def build_excel(groups: dict[str, list[dict]], output_path: str):
    """Tạo file Excel từ dữ liệu đã nhóm."""
    wb = openpyxl.Workbook()
    # Xóa sheet mặc định
    wb.remove(wb.active)

    # Sắp xếp sheet theo ngày tăng dần
    sorted_sheet_names = sorted(groups.keys(), key=sort_key_for_date)

    HEADERS = ["Mã khách hàng", "Tổng kWh", "Tổng tiền thanh toán (VNĐ)"]
    ALIGNMENTS = [CENTER, RIGHT, RIGHT]
    TOTAL_ALIGNMENTS = [CENTER, RIGHT, RIGHT]

    for sheet_name in sorted_sheet_names:
        records = groups[sheet_name]
        ws = wb.create_sheet(title=sheet_name)

        # Freeze panes (cố định hàng header)
        ws.freeze_panes = "A2"

        # Header
        apply_header(ws, HEADERS, row=1)

        # Dữ liệu
        total_kwh = 0
        total_tien = 0

        for row_idx, client in enumerate(records, start=2):
            ma_kh = client.get("ma_khach_hang", "")
            kwh = client.get("tong_kwh") or 0
            tien = client.get("tong_tien_thanh_toan")

            # Hiển thị số nguyên; None → chuỗi rỗng để dễ nhận biết
            kwh_display = int(kwh) if kwh else 0
            tien_display = int(tien) if tien is not None else None

            apply_data_row(
                ws,
                row=row_idx,
                values=[ma_kh, kwh_display, tien_display],
                alignments=ALIGNMENTS,
            )

            total_kwh += kwh_display
            if tien is not None:
                total_tien += int(tien)

        # Hàng tổng cộng
        total_row = len(records) + 2
        apply_total_row(
            ws,
            row=total_row,
            values=["TỔNG CỘNG", total_kwh, total_tien],
            alignments=TOTAL_ALIGNMENTS,
        )

        # Định dạng số cho cột kWh (B) và tiền (C) — dùng number format Excel
        for row_idx in range(2, total_row + 1):
            ws.cell(row=row_idx, column=2).number_format = "#,##0"
            ws.cell(row=row_idx, column=3).number_format = "#,##0"

        # Auto-fit
        auto_fit_columns(ws)

        # Tiêu đề kỳ (hiển thị ngày dạng đọc được) ở trên cùng — tuỳ chọn
        # Ví dụ sheet "10_06_2026" → hiển thị "Kỳ kết thúc: 10/06/2026"
        readable_date = sheet_name.replace("_", "/")
        ws.sheet_properties.tabColor = "1F4E79"

        log(f"   [OK] Sheet '{sheet_name}': {len(records)} khach hang")

    wb.save(output_path)
    log(f"\n[OK] Da xuat file Excel: {output_path}")


# =========================
# MAIN
# =========================
def main():
    parser = argparse.ArgumentParser(
        description="Xuất dữ liệu hóa đơn điện từ JSON ra Excel (mỗi kỳ một sheet)"
    )
    parser.add_argument(
        "--input-dir",
        default=DEFAULT_INPUT_DIR,
        help=f"Thư mục chứa các file JSON đầu ra (default: {DEFAULT_INPUT_DIR})",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Đường dẫn file Excel đầu ra (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()

    log("=== Xuat hoa don dien -> Excel ===")
    log(f"[INFO] Thu muc JSON : {args.input_dir}")
    log(f"[INFO] File Excel   : {args.output}")
    log("")

    clients = load_all_json(args.input_dir)
    groups = group_by_period(clients)

    log(f"\n[INFO] So ky hoa don: {len(groups)}")
    build_excel(groups, args.output)
    log("=== Hoan thanh ===")


if __name__ == "__main__":
    main()
