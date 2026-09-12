from playwright.sync_api import sync_playwright


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.add_init_script("localStorage.setItem('ds_token', 'C0cAvyUiAQywJ7')")
    page.goto("http://127.0.0.1:8111/")
    page.wait_for_timeout(1200)
    print("title=", page.title())
    print("video_tab=", page.get_by_text("视频转发", exact=True).count())
    page.get_by_text("视频转发", exact=True).click()
    page.wait_for_timeout(300)
    print("queue_heading=", page.get_by_text("发送队列", exact=True).count())
    print("manual_placeholder=", page.get_by_placeholder("每行一个抖音视频链接").count())
    browser.close()
