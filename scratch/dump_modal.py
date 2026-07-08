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

        print("Opening client modal...")
        page.locator("label:has-text('Mã khách hàng')").first.click()
        page.wait_for_selector("#modalChonMAKH", state="visible")

        # Dump modal HTML
        html = page.locator("#modalChonMAKH").inner_html()
        with open("scratch/modal_makh.html", "w", encoding="utf-8") as f:
            f.write(html)
        print("Dumped modal HTML to scratch/modal_makh.html")

        browser.close()

if __name__ == "__main__":
    os.makedirs("scratch", exist_ok=True)
    main()
