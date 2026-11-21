"""账号暂停期间页面禁止新查询，但保留已有数据的导出。"""
import os
import tempfile
import time
import unittest

from desktop import DesktopAPI, desktop_html
from guard import Guard
from service import Service, Task
from sites.boss_search import salary_summary


@unittest.skipUnless(os.environ.get('BOSS_BROWSER_TESTS') == '1', '设置 BOSS_BROWSER_TESTS=1 运行离线 UI 测试')
class AccessUiTests(unittest.TestCase):
    def test_block_notice_disables_query_but_not_export(self):
        from patchright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory() as directory, sync_playwright() as driver:
            service = Service(directory=directory)
            Guard(service.store, lambda text: None).record_risk('account_restricted', time.time() + 86400)
            task = Task({})
            task.finish('partial', '已有结果', result=dict(jobs=[], summary=salary_summary([]), query={},
                        read_at='2026-11-13', mode='search', batches=1, company_count=0, companies=[]))
            service.tasks[task.id] = task
            browser = driver.chromium.launch(channel='chrome', headless=True)
            try:
                page = browser.new_page()
                page.expose_function('desktopRequest', DesktopAPI(service).request)
                page.add_init_script('window.pywebview={api:{request:(p,d)=>window.desktopRequest(p,d)}};')
                page.route('**/*', lambda route: route.fulfill(content_type='text/html', body=desktop_html()))
                page.goto('http://access-ui.test/')
                page.wait_for_selector('#access-notice')
                self.assertIn('已暂停新查询', page.locator('#access-notice').inner_text())
                self.assertIn('不会自动重试', page.locator('#access-notice').inner_text())
                self.assertTrue(page.locator('#submit').is_disabled())
                self.assertIn('访问受限', page.locator('#submit').inner_text())
                self.assertEqual(page.locator('#submit').get_attribute('aria-describedby'), 'access-notice')
                self.assertTrue(page.locator('#csv').is_visible())
                self.assertEqual(page.locator('#csv').get_attribute('aria-disabled'), 'false')
                page.reload()
                page.wait_for_selector('#access-notice')
                self.assertTrue(page.locator('#submit').is_disabled())
            finally:
                browser.close()
                service.close()
