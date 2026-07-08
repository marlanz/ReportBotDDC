import os
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

load_dotenv()

PHONE = os.getenv("EVN_AH_USERNAME")
PASSWORD = os.getenv("EVN_AH_PASSWORD")
CRAWL_PAGE = "https://www.evnhcmc.vn/Tracuu/HDDT"

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(locale="vi-VN")
        page = context.new_page()

        print("Logging in...")
        page.goto("https://www.evnhcmc.vn/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector("form.form-dangnhap-trangchu")
        login_form = page.locator("form.form-dangnhap-trangchu")
        login_form.locator(".input-user").fill(PHONE)
        login_form.locator(".input-pass").fill(PASSWORD)
        login_form.locator("button[type='submit']").click()
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except:
            pass

        print("Navigating to HDDT...")
        page.goto(CRAWL_PAGE, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except:
            pass

        # Check default value of input-maKH
        input_el = page.locator("input#input-maKH")
        if input_el.count() > 0:
            print(f"Default input-maKH value: {input_el.input_value()}")
        else:
            print("input-maKH not found directly by id, looking for input under label...")
            # Try inputs
            for inp in page.locator("input").all():
                iid = inp.get_attribute("id") or ""
                iname = inp.get_attribute("name") or ""
                ival = inp.input_value()
                print(f"  Input: id={iid!r}, name={iname!r}, value={ival!r}")

        # Check default table content (first row)
        first_row = page.locator("table.table-custom tbody tr").first
        if first_row.count() > 0:
            print(f"Default first row text: {first_row.inner_text().strip()}")
        else:
            print("No default table rows found.")

        # Open client modal
        print("\nOpening client modal...")
        page.locator("label:has-text('Mã khách hàng')").first.click()
        page.wait_for_selector("#modalChonMAKH", state="visible")

        # Let's target the second client: PE15000352030
        target_client = "PE15000352030"
        print(f"Clicking client item: {target_client}")
        
        # We will try clicking the item div
        client_item = page.locator(f"#modalChonMAKH div.item[ma_pe='{target_client}']").first
        client_item.click()

        # Wait for modal to hide
        page.locator("#modalChonMAKH").wait_for(state="hidden", timeout=5000)

        # Wait for AJAX table load
        page.wait_for_timeout(2000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except:
            pass

        # Check value of input-maKH after click
        if input_el.count() > 0:
            print(f"New input-maKH value: {input_el.input_value()}")
        
        # Check first row of table after click
        first_row_new = page.locator("table.table-custom tbody tr").first
        if first_row_new.count() > 0:
            print(f"New first row text: {first_row_new.inner_text().strip()}")
        else:
            print("No table rows found after selection.")

        browser.close()

if __name__ == "__main__":
    os.makedirs("scratch", exist_ok=True)
    main()
