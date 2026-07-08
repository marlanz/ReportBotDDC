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

        # Print all buttons
        print("\n=== BUTTONS ===")
        for btn in page.locator("button, input[type='button'], input[type='submit'], a.btn").all():
            bid = btn.get_attribute("id") or ""
            bclass = btn.get_attribute("class") or ""
            btext = btn.inner_text().strip()
            btype = btn.get_attribute("type") or ""
            bval = btn.get_attribute("value") or ""
            bonclick = btn.get_attribute("onclick") or ""
            print(f"Button: id={bid!r}, type={btype!r}, val={bval!r}, text={btext!r}, onclick={bonclick!r}, class={bclass!r}")

        browser.close()

if __name__ == "__main__":
    main()
