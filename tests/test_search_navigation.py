"""搜索导航中断的离线回归：有条件恢复一次，错误与诊断不泄露参数。"""
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from patchright.sync_api import Error
from browser import browser_error_details
from guard import QueryError, RiskBlocked
from service import validate
from sites.boss import BossQuery, HOME
from sites.boss_search import SearchSession
from store import Store
from test_search import SearchPage, batch
from test_boss_flow import Response


class SearchNavigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.page = SearchPage([])
        self.page.wait_for_load_state = Mock()
        self.task = Mock()
        self.session = SearchSession(self.page, self.store, self.task)
        self.params = validate(dict(keyword='前端', city='深圳'))
        self.session.source = HOME + 'web/geek/jobs?query=前端&city=101280600'

    def test_security_return_recovers_conditions_once_with_or_without_aborted_navigation(self):
        for aborted in (False, True):
            with self.subTest(aborted=aborted):
                visits = []
                self.page.url = HOME
                def navigate(url, **kwargs):
                    visits.append(url)
                    if len(visits) == 1:
                        self.page.url = HOME + 'web/geek/jobs?_security_check=private-token'
                        if aborted:
                            raise Error('Page.goto: net::ERR_ABORTED at https://private.example/?token=secret')
                    else:
                        self.page.url = url
                self.page.goto = navigate
                self.session.open_search(self.params, '101280600', lambda: False)
                self.assertEqual(visits, [self.session.source, self.session.source])
                self.assertEqual(self.page.url, self.session.source)

    def test_repeated_security_redirect_stops_after_one_correction(self):
        def redirect(*args, **kwargs):
            self.page.url = HOME + 'web/geek/jobs?_security_check=private-token'
        self.page.goto = Mock(side_effect=redirect)
        with self.assertRaisesRegex(QueryError, '不继续重复访问'):
            self.session.open_search(self.params, '101280600', lambda: False)
        self.assertEqual(self.page.goto.call_count, 2)

    def test_no_retry_after_search_started_or_login_lost_or_unknown_network_error(self):
        for sent, authenticated, code in [(True, True, 'ERR_ABORTED'), (False, False, 'ERR_ABORTED'),
                                           (False, True, 'ERR_CONNECTION_RESET')]:
            with self.subTest(sent=sent, authenticated=authenticated, code=code):
                self.page.authenticated = authenticated
                def fail(url, **kwargs):
                    self.page.url = HOME + 'web/geek/jobs?_security_check=private-token'
                    raise Error('net::' + code)
                self.page.goto = Mock(side_effect=fail)
                with self.assertRaises(Error):
                    self.session.open_search(self.params, '101280600', lambda: sent)
                self.page.goto.assert_called_once()

    def test_navigation_error_rechecks_restriction_and_persists_pause(self):
        def restricted(url, **kwargs):
            self.page.url = HOME + 'web/passport/zp/403.html'
            raise Error('net::ERR_CONNECTION_RESET https://private.example/?token=secret')
        self.page.goto = Mock(side_effect=restricted)
        browser = Mock()
        browser.page.return_value = self.page
        with self.assertRaisesRegex(QueryError, '暂停新查询'):
            BossQuery(browser, self.store).run(self.params, self.task)
        self.page.goto.assert_called_once()
        self.assertEqual(self.store.read('quota.json')['risks'], 1)
        self.assertFalse(any(self.page.handlers.values()))

    def test_network_error_diagnostic_contains_only_safe_classification(self):
        self.page.goto = Mock(side_effect=Error('Page.goto: net::ERR_CONNECTION_RESET at https://x/?token=secret'))
        browser = Mock()
        browser.page.return_value = self.page
        with self.assertRaisesRegex(QueryError, 'ERR_CONNECTION_RESET') as caught:
            BossQuery(browser, self.store).run(self.params, self.task)
        diagnostic = self.store.read('last_failure.json')
        self.assertEqual(diagnostic['operation'], 'navigate')
        self.assertEqual(diagnostic['browser_error']['network_code'], 'ERR_CONNECTION_RESET')
        self.assertNotIn('secret', json.dumps(diagnostic) + str(caught.exception))
        self.assertNotIn('token=', json.dumps(diagnostic))
        self.assertEqual(self.store.read('quota.json')['risks'], 0)

    def test_429_never_uses_navigation_recovery(self):
        self.page.goto = Mock(return_value=type('Response', (), {'status': 429, 'headers': {}})())
        with self.assertRaises(RiskBlocked):
            self.session.open_search(self.params, '101280600', lambda: False)
        self.page.goto.assert_called_once()


@unittest.skipUnless(os.environ.get('BOSS_BROWSER_TESTS') == '1', '设置 BOSS_BROWSER_TESTS=1 运行离线导航验证')
class BrowserNavigationTests(unittest.TestCase):
    def test_script_redirect_drops_conditions_then_search_runs_once(self):
        from patchright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory() as directory, sync_playwright() as driver:
            browser = driver.chromium.launch(channel='chrome', headless=True)
            try:
                page = browser.new_page()
                requested, lists = [], []
                avatar = '<div class="user-nav"><div class="user-avatar">离线账号</div></div>'
                def respond(route):
                    url = urlsplit(route.request.url)
                    if route.request.resource_type == 'document':
                        requested.append(url.query)
                        if len(requested) == 1:
                            html = "<script>location.replace('/web/geek/jobs?_security_check=offline')</script>"
                        elif '_security_check' in url.query:
                            html = avatar
                        else:
                            html = avatar + "<script>fetch('/wapi/zpgeek/search/joblist.json'+location.search)</script>"
                        route.fulfill(content_type='text/html', body=html)
                    elif '/search/joblist.json' in url.path:
                        lists.append(url.query)
                        route.fulfill(content_type='application/json', body=json.dumps(batch(more=False)))
                    else:
                        route.fulfill(status=404, body='')
                page.context.route('**/*', respond)
                client = Mock()
                client.page.return_value = page
                with patch('sites.boss.BossSession.login'):
                    result = BossQuery(client, Store(directory)).run(validate(dict(keyword='前端', city='深圳')), Mock())
                self.assertTrue(result['complete'])
                self.assertEqual(len(requested), 3)
                self.assertEqual(len(lists), 1)
                self.assertIn('city=101280600', lists[0])
            finally:
                browser.close()
