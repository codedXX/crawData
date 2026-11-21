"""账号配置隔离和独立登录：只打开登录页，不采集、不清除旧限制。"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from accounts import AccountProfiles
from guard import AccessRestricted, Guard, QueryError
from service import Busy, Service, Task
from store import Store


class LoginPage:
    def __init__(self, authenticated=True):
        self.url = 'about:blank'
        self.authenticated = authenticated
        self.visits = []

    def goto(self, url, **kwargs):
        self.visits.append(url)
        self.url = 'https://www.zhipin.com/' if self.authenticated else url

    def bring_to_front(self):
        pass

    def wait_for_timeout(self, milliseconds):
        time.sleep(.01)


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = Service(directory=self.root / 'data', profile=self.root / 'profiles' / 'boss')
        self.addCleanup(self.service.close)
        self.old_store = self.service.store
        Guard(self.old_store, lambda message: None).record_risk('account_restricted', time.time() + 86400)
        self.old_quota = self.old_store.read('quota.json')
        self.task = Task({})
        self.task.finish('partial', '旧结果', result={'jobs': []})
        self.service.tasks[self.task.id] = self.task

    def flush(self):
        self.service.worker.submit(lambda: None).result(timeout=3)

    def test_official_login_reuses_current_session_and_rejects_credentials(self):
        from desktop import DesktopAPI
        api = DesktopAPI(self.service)
        page = LoginPage()
        profile = self.service.browser.profile
        with patch.object(self.service.browser, 'page', return_value=page), \
                patch('service.check_page'), patch('service.signed_in', return_value=True), \
                patch.object(self.service.boss, 'run') as query:
            self.assertNotIn('error', api.request('/login', {}))
            self.flush()
            query.assert_not_called()
        self.assertEqual(self.service.browser.profile, profile)
        self.assertEqual(len(self.service.accounts.entries), 1)
        self.assertEqual(api.request('/access')['data']['accounts']['operation']['status'], 'done')
        self.assertTrue(self.service.access_status()['blocked'])
        self.assertIs(self.service.get(), self.task)
        for data in ({'name': 'test'}, {'phone': '123', 'code': '1234'}):
            self.assertIn('error', api.request('/login', data))
        self.assertIn('error', api.request('/accounts/login', {'name': 'old'}))

    def test_official_login_closed_window_can_be_reopened(self):
        error = type('TargetClosedError', (Exception,), {})('closed')
        with patch.object(self.service.browser, 'page', side_effect=error), \
                patch.object(self.service.browser, 'close') as close:
            self.service.open_login({})
            self.flush()
            close.assert_called_once()
        self.assertIn('登录窗口已关闭', self.service.account_task.snapshot()['message'])
        with patch.object(self.service.browser, 'page', return_value=LoginPage()), \
                patch('service.check_page'), patch('service.signed_in', return_value=True):
            self.service.open_login({})
            self.flush()
        self.assertEqual(self.service.account_task.snapshot()['status'], 'done')

    def test_blocked_account_can_open_login_without_query_or_clearing_block(self):
        page = LoginPage()
        with patch.object(self.service.browser, 'page', return_value=page), \
                patch('service.check_page'), patch('service.signed_in', return_value=True), \
                patch.object(self.service.boss, 'run') as query:
            self.service.login_account({'account_id': 'default'})
            self.flush()
            query.assert_not_called()
        self.assertEqual(page.visits, ['https://www.zhipin.com/web/user/?ka=header-login'])
        self.assertEqual(self.service.account_task.snapshot()['status'], 'done')
        self.assertTrue(self.service.access_status()['blocked'])
        self.assertEqual(self.old_quota, self.old_store.read('quota.json'))
        with self.assertRaises(QueryError):
            self.service.submit({'keyword': '前端'})
        self.assertIs(self.service.prepare_export(self.task.id), self.task)

    def test_new_profile_isolated_and_restored_without_copying_old_cookies(self):
        page = LoginPage()
        browser = Mock()
        browser.page.return_value = page
        with patch('service.Browser', return_value=browser), patch('service.check_page'), \
                patch('service.signed_in', return_value=True):
            self.service.login_account({'name': '工作账号'})
            self.flush()
        ident = self.service.accounts.active
        self.assertNotEqual(ident, 'default')
        self.assertFalse(self.service.access_status()['blocked'])
        self.assertTrue(self.service.accounts.get(ident)['ready'])
        self.assertEqual(self.service.store.directory, self.root / 'data' / 'accounts' / ident)
        self.assertEqual(self.old_quota, self.old_store.read('quota.json'))
        self.assertIs(self.service.get(self.task.id), self.task)
        registry = AccountProfiles(self.root / 'data', self.root / 'profiles' / 'boss', self.root / 'data' / 'examples')
        self.assertEqual(registry.active, ident)
        self.assertTrue(registry.get(ident)['ready'])
        self.assertNotEqual(registry.paths('default')[1], registry.paths(ident)[1])
        # 切回旧配置，旧限制仍然生效。
        with patch('service.Browser', return_value=browser), patch('service.check_page'), patch('service.signed_in', return_value=True):
            self.service.login_account({'account_id': 'default'})
            self.flush()
        self.assertTrue(self.service.access_status()['blocked'])

    def test_cancel_login_preserves_result_and_does_not_mark_new_profile_ready(self):
        page = LoginPage(authenticated=False)
        browser = Mock()
        browser.page.return_value = page
        with patch('service.Browser', return_value=browser), patch('service.check_page'), patch('service.signed_in', return_value=False):
            self.service.login_account({'name': '待登录账号'})
            deadline = time.monotonic() + 3
            while self.service.account_task.snapshot()['status'] != 'login' and time.monotonic() < deadline:
                time.sleep(.01)
            with self.assertRaises(Busy):
                self.service.submit({'keyword': '前端'})
            with self.assertRaises(Busy):
                self.service.account_reply('cancel', {'id': 'stale'})
            self.service.account_reply('cancel', {'id': self.service.account_task.id})
            self.flush()
        self.assertEqual(self.service.account_task.snapshot()['status'], 'error')
        self.assertFalse(self.service.accounts.get(self.service.accounts.active)['ready'])
        self.assertIs(self.service.get(self.task.id), self.task)

    def test_new_account_restriction_is_saved_only_in_its_own_store(self):
        page = LoginPage()
        browser = Mock()
        browser.page.return_value = page
        with patch('service.Browser', return_value=browser), patch('service.check_page', side_effect=AccessRestricted(time.time() + 7200)):
            self.service.login_account({'name': '受限账号'})
            self.flush()
        self.assertTrue(self.service.access_status()['blocked'])
        self.assertFalse(self.service.accounts.get(self.service.accounts.active)['ready'])
        self.assertEqual(self.old_quota, self.old_store.read('quota.json'))
        self.assertEqual(len(page.visits), 1)

    def test_confirming_while_still_on_login_page_does_not_unlock_query(self):
        page = LoginPage(authenticated=False)
        browser = Mock()
        browser.page.return_value = page
        with patch('service.Browser', return_value=browser), patch('service.check_page'), \
                patch('service.signed_in', return_value=True), patch.object(Task, 'ask', return_value=True):
            self.service.login_account({'name': '还未登录'})
            self.flush()
        self.assertFalse(self.service.accounts.get(self.service.accounts.active)['ready'])
        self.assertEqual(self.service.account_task.snapshot()['status'], 'error')

    def test_invalid_names_ids_and_concurrent_query_are_rejected(self):
        for data in ({}, {'name': None}, {'name': 'a' * 41}, {'account_id': '../outside'}, {'name': 'a', 'account_id': 'default'}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.service.login_account(data)
        running = Task({})
        self.service.tasks[running.id] = running
        with self.assertRaises(Busy):
            self.service.login_account({'name': '新账号'})

    def test_blank_restriction_page_invalidates_previously_ready_account(self):
        page = Mock(url='https://www.zhipin.com/web/passport/zp/403.html?code=32')
        page.locator.return_value.inner_text.return_value = ''
        self.assertTrue(self.service.accounts.get('default')['ready'])
        with patch.object(self.service.browser, 'page', return_value=page), \
                patch('service.signed_in') as login_check:
            self.service.login_account({'account_id': 'default'})
            self.flush()
        login_check.assert_not_called()
        self.assertFalse(self.service.accounts.get('default')['ready'])
        self.assertEqual(self.service.account_task.snapshot()['status'], 'error')
        self.assertTrue(self.service.access_status()['blocked'])
        page.goto.assert_called_once()

    def test_empty_name_opens_new_login_despite_old_account_restriction(self):
        page = LoginPage()
        browser = Mock()
        browser.page.return_value = page
        with patch('service.Browser', return_value=browser), patch('service.check_page'), \
                patch('service.signed_in', return_value=True):
            self.service.login_account({'name': '   '})
            self.flush()
        entry = self.service.accounts.get(self.service.accounts.active)
        self.assertEqual(entry['name'], '新账号 1')
        self.assertNotEqual(entry['id'], 'default')
        self.assertEqual(len(page.visits), 1)
        self.assertEqual(self.service.account_task.snapshot()['status'], 'done')
        self.assertFalse(self.service.access_status()['blocked'])
        self.assertEqual(self.old_store.read('quota.json'), self.old_quota)
        self.assertEqual(self.service.accounts.add('')['name'], '新账号 2')
