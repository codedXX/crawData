"""乱序风控优先停止，Retry-After 只能延长暂停；全部使用离线响应。"""
import os
import tempfile
import unittest
from datetime import datetime, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from guard import Guard, QueryError, RiskBlocked, retry_after_until
from service import validate
from sites.boss import BossQuery, BossSession, HOME
from store import Store
from test_boss_flow import Page, Response
from test_province_search import MultiCityPage, TARGETS, operation
from test_search import batch

NOW = 1800000000


class RetryAfterTests(unittest.TestCase):
    def test_seconds_dates_and_invalid_headers(self):
        date = format_datetime(datetime.fromtimestamp(NOW + 7200, timezone.utc), usegmt=True)
        self.assertEqual(retry_after_until(' 7200 ', NOW), NOW + 7200)
        self.assertEqual(retry_after_until(date, NOW), NOW + 7200)
        for value in (None, '', '0', '-1', '1.5', 'NaN', 'Infinity', 'nonsense', '9' * 500,
                      'Wed, 01 Jan 2020 00:00:00 GMT', object()):
            with self.subTest(value=str(value)[:30]):
                self.assertIsNone(retry_after_until(value, NOW))

    def test_server_delay_local_minimum_and_existing_pause_take_longest(self):
        for header, prior, expected in [('7200', 0, 7200), ('60', 0, 1800),
                                         ('invalid', 0, 1800), ('7200', 14400, 14400)]:
            with self.subTest(header=header, prior=prior), tempfile.TemporaryDirectory() as directory, \
                    patch('guard.time.time', return_value=NOW):
                store = Store(directory)
                guard = Guard(store, Mock())
                if prior:
                    store.write('quota.json', dict(guard.state(), blocked_until=NOW + prior))
                response = SimpleNamespace(status=429, headers={'retry-after': header})
                with self.assertRaises(RiskBlocked) as caught:
                    BossSession.check_status(response)
                guard.record_risk(caught.exception.code, caught.exception.until)
                self.assertEqual(store.read('quota.json')['blocked_until'], NOW + expected)
                # 重新加载且跨日、人工确认后也不能提前解除。
                store.write('quota.json', dict(store.read('quota.json'), date='2000-01-01'))
                restored = Guard(Store(directory), Mock())
                restored.clear_verification()
                with self.assertRaises(QueryError):
                    restored.check()
                with patch('guard.time.time', return_value=NOW + expected + 1):
                    restored.check()

    def test_missing_headers_never_suppress_429(self):
        class ResponseWithoutHeaders:
            status = 429
            @property
            def headers(self):
                raise RuntimeError('headers unavailable')
        with self.assertRaises(RiskBlocked) as caught:
            BossSession.check_status(ResponseWithoutHeaders())
        self.assertIsNone(caught.exception.until)

    def test_home_navigation_persists_retry_after(self):
        with tempfile.TemporaryDirectory() as directory, patch('guard.time.time', return_value=NOW):
            page = Page([])
            page.url = 'about:blank'
            def navigate(url, **kwargs):
                page.url = url
                return SimpleNamespace(status=429, headers={'retry-after': '7200'})
            page.goto = Mock(side_effect=navigate)
            browser, task = Mock(), Mock()
            browser.page.return_value = page
            store = Store(directory)
            with self.assertRaisesRegex(QueryError, '暂停新查询'):
                BossQuery(browser, store).run(validate({}), task)
            page.goto.assert_called_once()
            self.assertEqual(store.read('quota.json')['blocked_until'], NOW + 7200)


class OutOfOrderRiskTests(unittest.TestCase):
    def test_later_risk_beats_missing_failed_and_timed_out_earlier_request(self):
        for status, payload in [(429, {}), (200, {'code': 37, 'message': '访问异常'}),
                                (200, {'code': 99, 'message': '账户已被限制访问'})]:
            for earlier in ('pending', 'failed', 'timeout'):
                with self.subTest(status=status, payload=payload, earlier=earlier), \
                        tempfile.TemporaryDirectory() as directory:
                    page = MultiCityPage({'101280100': [batch(operation('saved', '广州'))],
                                          '101280600': [batch(more=False)]})
                    def out_of_order(*args):
                        first = Response({}, url=page.list_url + '&page=2')
                        last = Response(payload, url=page.list_url + '&page=3')
                        last.status = status
                        last.headers = {'retry-after': '7200'}
                        for response in (first, last):
                            for handler in list(page.handlers['request']):
                                handler(response.request)
                        if earlier == 'failed':
                            for handler in list(page.handlers['requestfailed']):
                                handler(first.request)
                        elif earlier == 'timeout':
                            page.clock += 100
                        for handler in list(page.handlers['response']):
                            handler(last)
                        for handler in list(page.handlers['requestfinished']):
                            handler(last.request)
                    page.mouse.wheel.side_effect = out_of_order
                    browser, task = Mock(), Mock()
                    browser.page.return_value = page
                    store = Store(directory)
                    with patch('sites.boss_search.search_targets', return_value=TARGETS[:2]), \
                            patch('sites.boss_search.time.monotonic', side_effect=lambda: page.clock), \
                            patch('guard.time.time', return_value=NOW):
                        result = BossQuery(browser, store).run(validate(dict(keyword='运营', city='广东省',
                                                        batches=0, per_city_batches=0)), task)
                    self.assertFalse(result['complete'])
                    self.assertEqual(result['stop_reason'], 'interrupted')
                    self.assertEqual(len(result['jobs']), 1)
                    self.assertEqual(result['failed_cities'], [])
                    self.assertEqual(len(page.visits), 1)
                    page.mouse.wheel.assert_called_once()
                    self.assertEqual(store.read('quota.json')['risks'], 1)
                    if status == 429:
                        self.assertEqual(store.read('quota.json')['blocked_until'], NOW + 7200)
                    self.assertFalse(any(page.handlers.values()))

    def test_unrelated_and_stale_responses_do_not_block_current_search(self):
        from test_search import SearchPage
        with tempfile.TemporaryDirectory() as directory:
            page = SearchPage([batch(more=False)])
            navigate = page.goto
            def inject(url, **kwargs):
                page.url = url
                for stale, suffix in [(True, 'query=前端&city=101280600'),
                                      (False, 'query=其他&city=101280600')]:
                    response = Response({}, url=HOME + 'wapi/zpgeek/search/joblist.json?' + suffix)
                    response.status = 429
                    if not stale:
                        for handler in list(page.handlers['request']):
                            handler(response.request)
                    for handler in list(page.handlers['response']):
                        handler(response)
                return navigate(url, **kwargs)
            page.goto = inject
            browser = Mock()
            browser.page.return_value = page
            store = Store(directory)
            result = BossQuery(browser, store).run(validate(dict(keyword='前端', city='深圳')), Mock())
            self.assertTrue(result['complete'])
            self.assertEqual(store.read('quota.json')['risks'], 0)

    def test_company_collection_checks_risk_before_returning_buffered_success(self):
        with tempfile.TemporaryDirectory() as directory:
            page = Page([])
            session = BossSession(page, Store(directory), Mock())
            def respond():
                good = Response(batch(more=False))
                risk = Response({})
                risk.status = 429
                risk.headers = {'retry-after': '7200'}
                for response in (good, risk):
                    for handler in list(page.handlers['request']):
                        handler(response.request)
                    for handler in list(page.handlers['response']):
                        handler(response)
            with patch('guard.time.time', return_value=NOW), self.assertRaises(RiskBlocked) as caught:
                session.collect('公司列表', respond)
            self.assertEqual(caught.exception.until, NOW + 7200)
            self.assertFalse(any(page.handlers.values()))


@unittest.skipUnless(os.environ.get('BOSS_BROWSER_TESTS') == '1', '设置 BOSS_BROWSER_TESTS=1 运行离线 Chrome 验证')
class BrowserRiskResponseTests(unittest.TestCase):
    def test_later_429_stops_while_first_fetch_has_no_response(self):
        self.replay_later_risk(429, 'limited', 7200)

    def test_later_json_risk_stops_while_first_fetch_has_no_response(self):
        self.replay_later_risk(200, '{"code":37,"message":"访问异常"}', 1800)

    def replay_later_risk(self, status, body, pause):
        from patchright.sync_api import sync_playwright
        html = """<body><script>
        const q = location.search.slice(1);
        fetch('/wapi/zpgeek/search/joblist.json?' + q + '&page=1');
        fetch('/wapi/zpgeek/search/joblist.json?' + q + '&page=2');
        </script></body>"""
        with tempfile.TemporaryDirectory() as directory, sync_playwright() as driver:
            browser = driver.chromium.launch(channel='chrome', headless=True)
            try:
                page = browser.new_page()
                documents, requests, held = [], [], []
                def route_request(route):
                    if route.request.resource_type == 'document':
                        documents.append(route.request.url)
                        route.fulfill(content_type='text/html', body=html)
                    elif '/search/joblist.json' in route.request.url:
                        number = parse_qs(urlsplit(route.request.url).query)['page'][0]
                        requests.append(number)
                        if number == '1':
                            held.append(route)
                        else:
                            route.fulfill(status=status, headers={'Retry-After': '7200'},
                                          content_type='application/json', body=body)
                    else:
                        route.fulfill(status=404, body='')
                page.context.route('**/*', route_request)
                client, task = Mock(), Mock()
                client.page.return_value = page
                store = Store(directory)
                with patch('sites.boss.BossSession.login'), \
                        patch('sites.boss_search.search_targets', return_value=TARGETS[:2]), \
                        patch('sites.boss_search.PROGRESS_TIMEOUT', 2), \
                        patch('guard.time.time', return_value=NOW):
                    with self.assertRaisesRegex(QueryError, '暂停新查询'):
                        BossQuery(client, store).run(validate(dict(keyword='运营', city='广东省',
                                                       batches=0, per_city_batches=0)), task)
                self.assertEqual(len(documents), 1)
                self.assertEqual(requests, ['1', '2'])
                self.assertEqual(len(held), 1)
                self.assertEqual(store.read('quota.json')['blocked_until'], NOW + pause)
            finally:
                try:
                    for route in held:
                        route.abort()
                    page.wait_for_timeout(50)
                finally:
                    browser.close()
