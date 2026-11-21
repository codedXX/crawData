"""省份每市上限的离线 UI 回归：提交数值及区分已查询与已读完。"""
import os
import unittest

from desktop import desktop_html
from sites.boss_search import salary_summary


@unittest.skipUnless(os.environ.get("BOSS_BROWSER_TESTS") == "1", "设置 BOSS_BROWSER_TESTS=1 运行省份每市上限页面测试")
class CityBudgetUiTests(unittest.TestCase):
    def open_page(self, browser, request):
        page = browser.new_page()
        page.set_default_timeout(10000)
        page.expose_function("desktopRequest", request)
        page.add_init_script("""window.pywebview = {api: {
            request: (path, data) => window.desktopRequest(path, data)
        }};""")
        page.route("**/*", lambda route: route.fulfill(content_type="text/html", body=desktop_html()))
        page.goto("http://city-budget-ui.test/")
        return page

    def test_per_city_limit_defaults_to_unlimited_and_submits_selected_number(self):
        from patchright.sync_api import sync_playwright

        submitted = []
        state = dict(status="idle", message="等待查询")

        def request(path, data=None):
            nonlocal state
            if path == "/jobs/query":
                submitted.append(data)
                state = dict(id="city-budget", status="running", message="准备查询", query=data)
            return {"data": state}

        with sync_playwright() as driver:
            browser = driver.chromium.launch(channel="chrome", headless=True)
            try:
                page = self.open_page(browser, request)
                page.wait_for_function("document.getElementById('status').textContent === '等待查询'")
                limit = page.locator('select[name="per_city_batches"]')
                self.assertIn("省内每市上限", page.locator("#city-batches").inner_text())
                self.assertTrue(limit.is_visible())
                self.assertEqual(limit.input_value(), "0")
                self.assertTrue(page.get_by_role('checkbox', name='读取注册资本和成立日期', exact=False).is_checked())
                page.set_viewport_size(dict(width=820, height=800))
                self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))

                company = page.locator('input[name="company"]')
                company.fill("测试公司")
                self.assertFalse(limit.is_visible())
                self.assertTrue(limit.is_disabled())
                company.fill("")
                self.assertTrue(limit.is_visible())
                self.assertTrue(limit.is_enabled())

                page.locator("#location-toggle").click()
                page.locator("#province-options").get_by_role("button", name="广东省", exact=True).click()
                page.locator("#location-done").click()
                page.get_by_label("岗位关键词", exact=True).fill("运营")
                limit.select_option("2")
                self.assertEqual(page.locator('select[name="batches"]').input_value(), "0")
                page.locator("#submit").click()
                page.wait_for_function("document.getElementById('status').textContent === '准备查询'")

                self.assertEqual(len(submitted), 1)
                self.assertEqual(submitted[0]["city"], "广东省")
                self.assertEqual(submitted[0]["keyword"], "运营")
                self.assertEqual(submitted[0]["per_city_batches"], 2)
                self.assertIs(type(submitted[0]["per_city_batches"]), int)
                self.assertEqual(submitted[0]["batches"], 0)
                self.assertIs(submitted[0]['company_details'], True)
                self.assertTrue(page.locator("#submit").is_disabled())
            finally:
                browser.close()

    def test_finished_city_lists_show_ongoing_company_details(self):
        from patchright.sync_api import sync_playwright

        state = dict(id='details-progress', status='running', message='等待 25 秒',
            query=dict(city='广东省', company_details=True),
            city_progress=[dict(city=city, status='done') for city in ('珠海', '佛山')])
        with sync_playwright() as driver:
            browser = driver.chromium.launch(channel='chrome', headless=True)
            try:
                page = self.open_page(browser, lambda path, data=None: {'data': state})
                page.wait_for_function("document.getElementById('submit').textContent === '读取工商信息中…'")
                self.assertIn('正在读取工商信息', page.locator('#city-progress-current').inner_text())
                self.assertIn('正在读取注册资本和成立日期', page.locator('#status').inner_text())
                self.assertIn('等待 25 秒', page.locator('#status').inner_text())
                self.assertTrue(page.locator('#cancel').is_visible())
                self.assertTrue(page.locator('#submit').is_disabled())
                state.update(phase='company_details', city_progress=[])
                page.reload()
                page.wait_for_function("document.getElementById('submit').textContent === '读取工商信息中…'")
                state.update(status='error', message='工商信息读取停止，已有岗位可导出')
                page.wait_for_function("document.getElementById('submit').textContent === '查询岗位'")
                self.assertFalse(page.locator('#cancel').is_visible())
            finally:
                browser.close()

    def test_all_cities_visited_but_limited_cities_are_not_called_complete(self):
        from patchright.sync_api import sync_playwright

        # 明确构造所有城市均尝试、仅一个城市明确结束的结果，不访问招聘网站。
        state = dict(
            id="city-budget-partial", status="partial", message="部分城市未读完", prompt=None,
            result=dict(
                jobs=[], summary=salary_summary([]),
                query=dict(company="", keyword="运营", city="广东省", per_city_batches=5),
                read_at="2026-11-08T12:00:00+08:00", mode="search", complete=False,
                filter_mode="site", batches=11, company_count=0, companies=[],
                province_search=True, cities_visited=3, cities_done=1, cities_total=3,
                current_city="珠海", limited_cities=["广州", "深圳"],
                stop_message="已查询全部目标城市，但以下城市达到每市批数上限，尚未读完：广州、深圳。",
            ),
        )
        with sync_playwright() as driver:
            browser = driver.chromium.launch(channel="chrome", headless=True)
            try:
                page = self.open_page(browser, lambda path, data=None: {"data": state})
                page.wait_for_function("document.getElementById('context').textContent.includes('已查询 3/3 个城市')")
                context = page.locator("#context").inner_text()
                self.assertIn("已读完 1/3 个城市", context)
                empty = page.locator("#empty").inner_text()
                self.assertNotIn("仅查询了部分城市", empty)
                self.assertTrue("未读完" in empty or "未全部读完" in empty)
                self.assertIn("不能据此判断全省没有岗位", empty)
                self.assertIn("广州、深圳", page.locator("#result-note").inner_text())
                self.assertFalse(page.locator("#table").is_visible())
                self.assertTrue(page.locator("#submit").is_enabled())
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
