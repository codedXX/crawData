"""访问受限立即停止、跨日持久化暂停、拒绝新查询且允许本地导出。"""
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from guard import AccessRestricted, Guard, LoginRequired, QueryError, check_page, restriction_from_text, signed_in
from service import Service, Task, validate
from sites.boss import unpack
from store import Store
import test_province_search as province_tests
from test_province_search import MultiCityPage, TARGETS, operation
from test_search import batch
from test_boss_flow import Page
from sites.boss import BossQuery, HOME

NOTICE = '访问受限\n抱歉，您暂时无法访问此页面\n您的账户存在异常行为，已暂时被限制访问！\n将于 2026-11-16 12:31 恢复正常'


class AccessRestrictionTests(unittest.TestCase):
    def test_restriction_url_stops_even_before_body_renders(self):
        for failure in (None, TimeoutError('body not ready')):
            with self.subTest(body_error=failure):
                page = Mock(url='https://www.zhipin.com/web/passport/zp/403.html?code=32')
                page.locator.return_value.inner_text.return_value = ''
                page.locator.return_value.inner_text.side_effect = failure
                with self.assertRaises(AccessRestricted):
                    check_page(page)
                self.assertFalse(signed_in(page))
                page.context.cookies.assert_not_called()

    def test_stale_session_cookie_is_not_login_confirmation(self):
        page = Mock(url='https://www.zhipin.com/')
        page.locator.return_value.count.return_value = 0
        page.context.cookies.return_value = [{'name': 'wt2', 'value': 'stale-session'}]
        self.assertFalse(signed_in(page))
        page.context.cookies.assert_not_called()

    def test_notice_recognized_without_special_url_or_captcha(self):
        page = Mock(url='https://www.zhipin.com/web/geek/jobs')
        page.locator.return_value.inner_text.return_value = NOTICE
        with self.assertRaises(AccessRestricted) as caught:
            check_page(page)
        expected = datetime(2026, 11, 16, 12, 31, tzinfo=timezone(timedelta(hours=8))).timestamp()
        self.assertEqual(caught.exception.until, expected)
        self.assertIsNone(restriction_from_text('岗位要求：熟悉访问受限问题排查'))
        with self.assertRaises(AccessRestricted):
            unpack({'code': 99, 'message': '账户已被限制访问'})

    def test_cooldown_survives_midnight_and_manual_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            guard = Guard(store, lambda text: None)
            until = time.time() + 86400 * 3
            guard.record_risk('account_restricted', until)
            state = guard.state()
            store.write('quota.json', dict(state, date='2000-01-01'))
            guard.clear_verification()
            with self.assertRaises(QueryError):
                guard.check()
            self.assertEqual(guard.state()['blocked_until'], until)
            with patch('guard.time.time', return_value=until + 1):
                self.assertFalse(guard.access_status()['blocked'])
                guard.check()

    def test_blocked_service_never_starts_browser_but_export_remains_local(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(directory=directory)
            try:
                Guard(service.store, lambda text: None).record_risk('account_restricted', time.time() + 86400)
                with patch.object(service.browser, 'page') as browser, patch.object(service.worker, 'submit') as worker:
                    with self.assertRaises(QueryError):
                        service.submit({'keyword': '前端'})
                    browser.assert_not_called()
                    worker.assert_not_called()
                    task = Task({})
                    task.finish('partial', '已停止', result={'jobs': []})
                    service.tasks[task.id] = task
                    service.prepare_export(task.id)
                    worker.assert_not_called()
                self.assertEqual(task.snapshot()['export']['status'], 'done')
                self.assertTrue(service.access_status()['blocked'])
            finally:
                service.close()

    def test_verification_has_minimum_cooldown_with_unlimited_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            guard = Guard(Store(directory), lambda text: None)
            before = time.time()
            guard.record_risk(429)
            self.assertGreaterEqual(guard.state()['blocked_until'], before + 1800)
        params = validate({})
        self.assertEqual((params['batches'], params['per_city_batches'], params['pages']), (0, 0, 1))


class LoginRestrictionTests(unittest.TestCase):
    def test_risk_during_initial_and_expired_login_is_persisted(self):
        for expired in (False, True):
            for restricted_url in (HOME + 'web/passport/zp/403.html', HOME + 'web/verify'):
                with self.subTest(expired=expired, url=restricted_url), tempfile.TemporaryDirectory() as directory:
                    page = Page([])
                    page.authenticated = expired
                    browser, task = Mock(), Mock()
                    browser.page.return_value = page
                    store = Store(directory)

                    def wait_for_login(*args, **kwargs):
                        page.url = restricted_url
                        return kwargs['done']()

                    task.ask.side_effect = wait_for_login
                    with patch('sites.boss_search.SearchSession.read_search', side_effect=LoginRequired('登录已失效')) as read:
                        with self.assertRaisesRegex(QueryError, '暂停新查询'):
                            BossQuery(browser, store).run(validate({}), task)
                    self.assertEqual(read.call_count, int(expired))
                    task.ask.assert_called_once()
                    self.assertEqual(store.read('quota.json')['risks'], 1)
                    with self.assertRaises(QueryError):
                        Guard(Store(directory), Mock()).check()
                    self.assertFalse(any(page.handlers.values()))

    def test_home_and_search_document_429_stop_without_retry(self):
        for initial_url in ('about:blank', HOME):
            with self.subTest(initial_url=initial_url), tempfile.TemporaryDirectory() as directory:
                page = Page([])
                page.url = initial_url

                def navigate(url, **kwargs):
                    page.url = url
                    return SimpleNamespace(status=429, ok=False)

                page.goto = Mock(side_effect=navigate)
                browser, task = Mock(), Mock()
                browser.page.return_value = page
                store = Store(directory)
                with self.assertRaisesRegex(QueryError, '暂停新查询'):
                    BossQuery(browser, store).run(validate({}), task)
                page.goto.assert_called_once()
                task.ask.assert_not_called()
                self.assertEqual(store.read('quota.json')['risks'], 1)
                self.assertFalse(any(page.handlers.values()))


class ProvinceRestrictionTests(unittest.TestCase):
    setUp = province_tests.ProvinceSearchTests.setUp
    run_search = province_tests.ProvinceSearchTests.run_search

    def test_restriction_preserves_first_batch_and_does_not_visit_next_city(self):
        page = MultiCityPage({'101280100': [batch(operation('gz', '广州'))]})
        restricted = False
        locator = page.locator
        def locate(selector):
            if selector == 'body':
                return SimpleNamespace(inner_text=lambda **kwargs: NOTICE if restricted else '')
            return locator(selector)
        page.locator = locate
        def block(*args):
            nonlocal restricted
            restricted = True
        page.mouse.wheel.side_effect = block
        result = self.run_search({}, page=page, targets=TARGETS[:2], batches=0, per_city_batches=0)
        self.assertFalse(result['complete'])
        self.assertEqual(len(result['jobs']), 1)
        self.assertEqual(len(page.visits), 1)
        self.assertIn('暂停新查询', result['stop_message'])
        self.assertTrue(self.store.read('quota.json')['blocked_until'])
