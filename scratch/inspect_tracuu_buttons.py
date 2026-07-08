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

        # Print all btn-tracuu outer HTML and parents
        buttons = page.locator("button.btn-tracuu").all()
        print(f"\nFound {len(buttons)} btn-tracuu buttons:")
        for idx, btn in enumerate(buttons):
            html = btn.evaluate("el => el.outerHTML")
            parent_info = btn.evaluate("el => { let p = el.parentElement; return p ? `${p.tagName} id=${p.id} class='${p.className}'` : 'none'; }")
            visible = btn.is_visible()
            print(f"  Button {idx}: visible={visible}")
            print(f"    Parent: {parent_info}")
            print(f"    HTML  : {html}")

        # Also print any form or container containing the input-maKH to see where the visible search button is!
        ma_kh_input = page.locator("input#input-maKH")
        if ma_kh_input.count() > 0:
            parent_form = ma_kh_input.evaluate("el => { let p = el.closest('form'); return p ? `${p.tagName} id=${p.id} class='${p.className}' HTML=${p.outerHTML.substring(0, 1000)}` : 'none'; }")
            print(f"\nParent Form of input-maKH:\n  {parent_form}")

        browser.close()

if __name__ == "__main__":
    main()
