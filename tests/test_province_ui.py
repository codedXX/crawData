"""离线页面不能把省份的部分空结果说成全省无岗位。"""
import os
import unittest

from desktop import desktop_html
from sites.boss_search import salary_summary


@unittest.skipUnless(os.environ.get("BOSS_BROWSER_TESTS") == "1", "设置 BOSS_BROWSER_TESTS=1 运行省份页面测试")
class ProvinceUiTests(unittest.TestCase):
    def test_partial_empty_province_explains_unread_cities(self):
        from patchright.sync_api import sync_playwright

        # 明确构造总批数已用完、仅完成首个城市的状态，不访问招聘网站。
        state = dict(
            id="province-partial", status="partial", message="查询未读完", prompt=None,
            result=dict(
                jobs=[], summary=salary_summary([]), query=dict(company="", keyword="运营", city="广东省"),
                read_at="2026-11-08T12:00:00+08:00", mode="search", complete=False,
                filter_mode="site", batches=1, company_count=0, companies=[],
                province_search=True, cities_done=1, cities_total=3, current_city="广州",
                stop_message="广东省：已读完 1/3 个城市。已达到设置的总批数，尚有城市未查询，未读完全省结果。",
            ),
        )
        with sync_playwright() as driver:
            browser = driver.chromium.launch(channel="chrome", headless=True)
            try:
                page = browser.new_page()
                page.set_default_timeout(10000)
                page.expose_function("desktopRequest", lambda path, data=None: {"data": state})
                page.add_init_script("""window.pywebview = {api: {
                    request: (path, data) => window.desktopRequest(path, data)
                }};""")
                page.route("**/*", lambda route: route.fulfill(content_type="text/html", body=desktop_html()))
                page.goto("http://province-ui.test/")
                page.wait_for_function("document.getElementById('context').textContent.includes('1/3 个城市')")

                context = page.locator("#context").inner_text()
                self.assertIn("广东省", context)
                self.assertIn("运营", context)
                self.assertIn("已读完 1/3 个城市", context)
                self.assertTrue(page.locator("#empty").is_visible())
                empty = page.locator("#empty").inner_text()
                self.assertIn("仅查询了部分城市", empty)
                self.assertIn("不能据此判断全省没有岗位", empty)
                self.assertTrue(page.locator("#result-note").is_visible())
                self.assertIn("未读完", page.locator("#result-note").inner_text())
                self.assertFalse(page.locator("#table").is_visible())
                self.assertTrue(page.locator("#submit").is_enabled())
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
