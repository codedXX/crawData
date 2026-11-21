"""移除本地配置伪账号面板后，查询由真实会话检测驱动。"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from desktop import DesktopAPI, desktop_html
from service import Service
from sites.boss_search import salary_summary
from test_boss_flow import Page


@unittest.skipUnless(os.environ.get('BOSS_BROWSER_TESTS') == '1', '设置 BOSS_BROWSER_TESTS=1 运行离线 UI 测试')
class AccountUiTests(unittest.TestCase):
    def test_query_waits_for_login_then_continues_after_reload(self):
        from patchright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory() as directory, sync_playwright() as driver:
            root = Path(directory)
            service = Service(directory=root / 'data', profile=root / 'profile')
            official = Page([])
            official.authenticated = False
            official.bring_to_front = Mock()
            link = Mock()
            link.first = link
            link.count.side_effect = lambda: int(not official.authenticated)
            result = dict(jobs=[], summary=salary_summary([]), query={}, read_at='2026-11-20',
                          mode='search', batches=1, company_count=0, companies=[], complete=True)
            browser = driver.chromium.launch(channel='chrome', headless=True)
            try:
                page = browser.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.expose_function('desktopRequest', DesktopAPI(service).request)
                page.add_init_script('window.pywebview={api:{request:(p,d)=>window.desktopRequest(p,d)}};')
                page.route('**/*', lambda route: route.fulfill(content_type='text/html', body=desktop_html()))
                with patch.object(service.browser, 'page', return_value=official), \
                        patch('guard.Guard.start_company'), \
                        patch('sites.boss.random.uniform', return_value=0), \
                        patch('guard.login_link', return_value=link), \
                        patch('sites.boss.login_link', return_value=link), \
                        patch('sites.boss_search.SearchSession.read_search', return_value=result) as query:
                    page.goto('http://account-ui.test/')
                    page.wait_for_function("document.getElementById('status').textContent === '等待查询'")
                    page.locator('input[name=keyword]').fill('前端')
                    self.assertEqual(page.locator('#open-login, #login-controls').count(), 0)
                    query.assert_not_called()
                    link.click.assert_not_called()
                    page.locator('#submit').click()
                    page.wait_for_function("document.getElementById('submit').textContent === '等待登录确认'")
                    waiting_id = service.get().id
                    link.click.assert_called_once()
                    official.bring_to_front.assert_called_once()
                    query.assert_not_called()
                    page.reload()
                    page.wait_for_function("document.getElementById('submit').textContent === '等待登录确认'")
                    official.authenticated = True
                    page.wait_for_selector('#csv')
                    self.assertEqual(service.get().id, waiting_id)
                    query.assert_called_once()
                    self.assertTrue(page.locator('#submit').is_enabled())
                    # 已登录的下一次查询直接继续，不再点击登录或等待确认。
                    page.locator('#submit').click()
                    page.wait_for_function("document.getElementById('submit').textContent === '查询岗位'")
                    self.assertNotEqual(service.get().id, waiting_id)
                    self.assertEqual(query.call_count, 2)
                    link.click.assert_called_once()
                    self.assertFalse(errors)
            finally:
                service.close()
                browser.close()

    def test_removed_panel_does_not_leave_query_disabled_or_launch_browser(self):
        from patchright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory() as directory, sync_playwright() as driver:
            root = Path(directory)
            service = Service(directory=root / 'data', profile=root / 'profile')
            service.accounts.get(service.accounts.active)['ready'] = False
            result = dict(jobs=[], summary=salary_summary([]), query={}, read_at='2026-11-13',
                          mode='search', batches=1, company_count=0, companies=[])
            browser = driver.chromium.launch(channel='chrome', headless=True)
            try:
                page = browser.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.expose_function('desktopRequest', DesktopAPI(service).request)
                page.add_init_script('window.pywebview={api:{request:(p,d)=>window.desktopRequest(p,d)}};')
                page.route('**/*', lambda route: route.fulfill(content_type='text/html', body=desktop_html()))
                with patch.object(service.browser, 'page') as launch, \
                        patch.object(service.boss, 'run', return_value=result) as query:
                    page.goto('http://account-ui.test/')
                    page.wait_for_function("document.getElementById('status').textContent === '等待查询'")
                    page.locator('input[name=keyword]').fill('前端')
                    self.assertEqual(page.locator('#account-panel, #account-name, #account-select').count(), 0)
                    self.assertTrue(page.locator('#submit').is_enabled())
                    query.assert_not_called()
                    launch.assert_not_called()
                    page.locator('#submit').click()
                    page.wait_for_selector('#csv')
                    query.assert_called_once()
                    launch.assert_not_called()
                    self.assertFalse(errors)
            finally:
                browser.close()
                service.close()
