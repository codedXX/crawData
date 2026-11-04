"""可选离线 UI 验收：真实页面、Python 桥、人工确认、停止和 CSV 保存。"""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from desktop import DesktopAPI, desktop_html
from guard import QueryError
from service import Service
from sites.boss import map_job
from sites.boss_search import salary_summary


@unittest.skipUnless(os.environ.get("BOSS_BROWSER_TESTS") == "1", "设置 BOSS_BROWSER_TESTS=1 运行离线桌面 UI 测试")
class DesktopBrowserTests(unittest.TestCase):
    def test_login_stop_partial_result_and_native_csv(self):
        from patchright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as driver:
            root = Path(tmp)
            service = Service(directory=root / "data", profile=root / "profile")
            self.addCleanup(service.close)
            api = DesktopAPI(service)
            target = root / "岗位.csv"
            api._window = Mock()
            api._window.create_file_dialog.return_value = (str(target),)
            job = map_job(dict(encryptJobId="offline", jobName="前端开发", salaryDesc="10-20K",
                               brandName="离线公司", cityName="深圳", skills=["Vue"]), {"name": "离线公司"})
            def collect(params, task):
                task.ask("login", "离线登录确认", timeout=15)
                task.report("离线采集中")
                try:
                    task.sleep(30)
                except QueryError:
                    pass
                return dict(jobs=[job], summary=salary_summary([job]), query=params,
                            read_at="2026-11-04T12:00:00+08:00", mode="search", complete=False,
                            batches=1, company_count=1, companies=[], stop_message="已停止，保留部分结果")
            service.boss.run = collect
            browser = driver.chromium.launch(channel="chrome", headless=True)
            try:
                page = browser.new_page()
                page.set_default_timeout(10000)
                page.expose_function("desktopRequest", api.request)
                page.expose_function("desktopSave", api.save_csv)
                page.add_init_script("""window.pywebview = {api: {
                    request: (path, data) => window.desktopRequest(path, data),
                    save_csv: id => window.desktopSave(id)
                }};""")
                page.route("**/*", lambda route: route.fulfill(content_type="text/html", body=desktop_html()))
                page.goto("http://desktop.test/")
                page.locator('input[name="city"]').fill("深圳")
                page.locator("#submit").click()
                page.get_by_role("button", name="我已登录", exact=True).click()
                page.wait_for_function("document.getElementById('status').textContent === '离线采集中'")
                page.locator("#cancel").click()
                page.wait_for_function("document.getElementById('status').textContent.includes('已停止')")
                self.assertEqual(page.locator("#job-rows tr").count(), 1)
                self.assertIn("前端开发", page.locator("#job-rows").inner_text())
                self.assertIn("15,000", page.locator("#metrics").inner_text())
                self.assertTrue(page.locator("#status").evaluate("el => el.classList.contains('is-finished')"))
                fake_webview = SimpleNamespace(FileDialog=SimpleNamespace(SAVE="save"))
                with patch.dict("sys.modules", webview=fake_webview):
                    page.locator("#csv").click()
                    page.wait_for_function("document.getElementById('export-status').textContent.startsWith('CSV 已保存')")
                self.assertIn("前端开发", target.read_text(encoding="utf-8-sig"))
                self.assertEqual(page.url, "http://desktop.test/")
                self.assertTrue(page.locator("#submit").is_enabled())
            finally:
                browser.close()
