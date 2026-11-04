"""跨公司搜索回归：虚拟卡片回收、连续/乱序响应、停止与部分结果。"""
import json
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from guard import Guard, QueryError
from service import Service, Task, validate
from sites.boss import BossQuery
from sites.boss_search import SEARCH_CARDS, city_code, search_request_matches
from store import Store
from test_boss_flow import Control, HOME, Page, Response, job


def batch(*jobs, more=True):
    return dict(code=0, zpData=dict(jobList=list(jobs), hasMore=more))


class SearchControl(Control):
    def is_visible(self):
        return True


class SearchPage(Page):
    def __init__(self, pages):
        super().__init__(pages)
        self.clock = 0
        self.mouse.wheel.side_effect = self.scroll

    def goto(self, url, **kwargs):
        self.url = url
        self.list_url = HOME + 'wapi/zpgeek/search/joblist.json?' + urlsplit(url).query
        self.emit_next()

    def scroll(self, x, y):
        assert self.hovered, "必须在岗位列表中滚动"
        self.emit_next()

    def locator(self, selector):
        if selector == SEARCH_CARDS:
            return SearchControl(self, "card")
        return super().locator(selector)

    def get_by_text(self, *args, **kwargs):
        raise AssertionError("虚拟列表会回收卡片，不能依赖上一批的岗位标题")

    def wait_for_timeout(self, milliseconds):
        self.clock += milliseconds / 1000


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.task = Mock()
        self.browser = Mock()
        self.query = BossQuery(self.browser, self.store)

    def run_search(self, pages=None, page=None, **params):
        self.page = page or SearchPage(pages)
        self.browser.page.return_value = self.page
        with patch('sites.boss_search.time.monotonic', side_effect=lambda: self.page.clock):
            return self.query.run(validate(dict(keyword="前端", city="深圳", **params)), self.task)

    def test_auto_scroll_beyond_five_batches_with_recycled_cards(self):
        pages = [batch(dict(job("前端开发", str(i)), encryptBrandId=f"b{i % 2}",
                            brandName=f"公司{i % 2}"), more=i < 6) for i in range(7)]
        result = self.run_search(pages)
        self.assertTrue(result['complete'])
        self.assertEqual(result['batches'], 7)
        self.assertEqual(result['company_count'], 2)
        self.assertEqual([c['count'] for c in result['companies']], [4, 3])
        self.assertEqual(len(result['jobs']), 7)
        self.assertEqual(self.page.mouse.wheel.call_count, 6)
        self.task.ask.assert_not_called()
        self.assertEqual(parse_qs(urlsplit(self.page.url).query), {'query': ['前端'], 'city': ['101280600']})
        state = self.store.read('quota.json')
        self.assertEqual((state['requests'], state['companies']), (7, 1))
        self.assertFalse(any(self.page.handlers.values()))
        self.assertFalse((self.store.directory / 'cache.json').exists())

    def test_batch_cap_keeps_partial_result(self):
        result = self.run_search([batch(job('前端', '1')), batch(job('前端', '2'))], batches=1)
        self.assertFalse(result['complete'])
        self.assertEqual(result['stop_reason'], 'batch_limit')
        self.assertEqual(len(result['jobs']), 1)
        self.page.mouse.wheel.assert_not_called()
        self.assertEqual(self.store.read('quota.json')['companies'], 0)

    def test_legacy_exhausted_limits_do_not_block_search(self):
        state = Guard(self.store, Mock()).state()
        self.store.write('quota.json', dict(state, requests=30, request_limit=30,
                                            companies=20, company_limit=20))
        result = self.run_search([batch(job('前端', '1'), more=False)])
        self.assertTrue(result['complete'])
        self.assertEqual(self.store.read('quota.json')['requests'], 31)
        self.assertEqual(self.store.read('quota.json')['companies'], 21)

    def test_auto_scroll_continues_beyond_old_daily_request_limit(self):
        state = Guard(self.store, Mock()).state()
        self.store.write('quota.json', dict(state, request_limit=60))
        result = self.run_search([batch(job('前端', str(i)), more=i < 60) for i in range(61)])
        self.assertTrue(result['complete'])
        self.assertEqual(len(result['jobs']), 61)
        self.assertEqual(self.store.read('quota.json')['requests'], 61)
        self.assertEqual(self.page.mouse.wheel.call_count, 60)

    def test_silence_and_missing_has_more_are_not_end(self):
        data = batch(job('前端', '1'))
        del data['zpData']['hasMore']
        page = SearchPage([data])
        page.mouse.wheel.side_effect = None
        result = self.run_search(page=page)
        self.assertFalse(result['complete'])
        self.assertIn('超时', result['stop_message'])
        self.assertEqual(result['batches'], 1)
        diagnostic = json.dumps(self.store.read('last_failure.json'))
        self.assertNotIn('query=', diagnostic)
        self.assertFalse(any(page.handlers.values()))

    def test_empty_terminal_batch_is_complete(self):
        result = self.run_search([batch(more=False)])
        self.assertTrue(result['complete'])
        self.assertEqual(result['jobs'], [])
        self.assertEqual(result['company_count'], 0)

    def test_empty_nonterminal_batch_is_partial(self):
        result = self.run_search([batch(job('前端', '1')), batch()])
        self.assertFalse(result['complete'])
        self.assertIn('空批次', result['stop_message'])

    def test_repeated_batch_stops_without_claiming_completion(self):
        result = self.run_search([batch(job('前端', '1')), batch(job('前端', '1'))])
        self.assertFalse(result['complete'])
        self.assertEqual(len(result['jobs']), 1)
        self.assertIn('相同岗位', result['stop_message'])

    def test_filter_empty_batch_does_not_stop_loading(self):
        result = self.run_search([batch(job('后端开发', '1')), batch(job('前端开发', '2'), more=False)])
        self.assertTrue(result['complete'])
        self.assertEqual(result['scanned'], 2)
        self.assertEqual(len(result['jobs']), 1)

    def test_cancel_keeps_partial_results_and_removes_listeners(self):
        self.task = Task({})
        self.task.sleep = lambda seconds: None if not self.task.cancelled.is_set() else self.task.report('等待')
        report = self.task.report
        def cancel_after_batch(message):
            report(message)
            if message.startswith('已读'):
                self.task.cancel()
        self.task.report = cancel_after_batch
        result = self.run_search([batch(job('前端', '1'))])
        self.assertFalse(result['complete'])
        self.assertIn('取消', result['stop_message'])
        self.assertEqual(len(result['jobs']), 1)
        self.assertFalse(any(self.page.handlers.values()))

    def test_verification_preserves_partial_and_records_risk(self):
        state = Guard(self.store, Mock()).state()
        self.store.write('quota.json', dict(state, risks=1))
        result = self.run_search([batch(job('前端', '1')), dict(code=37, message='访问异常')])
        self.assertFalse(result['complete'])
        self.assertEqual(len(result['jobs']), 1)
        self.assertEqual(self.store.read('quota.json')['risks'], 2)
        self.assertTrue(self.store.read('quota.json')['needs_verify'])
        self.assertNotIn('明天', result['stop_message'])
        self.task.ask.assert_not_called()

    def test_login_expiry_preserves_partial(self):
        link = Mock()
        link.first = link
        link.count.return_value = 1
        with patch('sites.boss.login_link', return_value=link):
            # 跳过查询前登录检查，专门验证采集中登录失效的恢复流程。
            with patch('sites.boss.BossSession.login'):
                result = self.run_search([batch(job('前端', '1')), dict(code=1, message='请先登录')])
        self.assertFalse(result['complete'])
        self.assertIn('登录', result['stop_message'])
        self.assertEqual(self.store.read('quota.json')['risks'], 0)
        link.click.assert_called_once()
        self.assertEqual(self.task.ask.call_args.args[0], 'login')

    def test_browser_closed_preserves_previous_batches(self):
        from patchright.sync_api import Error
        page = SearchPage([batch(job('前端', '1'))])
        error = Error('closed')
        error._name = 'TargetClosedError'
        page.mouse.wheel.side_effect = error
        result = self.run_search(page=page)
        self.assertFalse(result['complete'])
        self.assertIn('Chrome', result['stop_message'])
        self.assertEqual(len(result['jobs']), 1)
        self.browser.close.assert_called_once()

    def test_browser_transport_error_preserves_previous_batches(self):
        from patchright.sync_api import Error
        page = SearchPage([batch(job('前端', '1'))])
        page.mouse.wheel.side_effect = Error('连接日志可能包含私密参数')
        result = self.run_search(page=page)
        self.assertFalse(result['complete'])
        self.assertEqual(len(result['jobs']), 1)
        self.assertNotIn('私密参数', result['stop_message'])

    def test_transient_guest_header_does_not_override_valid_response(self):
        page = SearchPage([batch(job('前端', '1'), more=False)])
        locator = page.locator
        page.locator = lambda selector: Mock(count=lambda: 1) if selector == '.header-login-btn:visible' else locator(selector)
        result = self.run_search(page=page)
        self.assertTrue(result['complete'])
        self.task.ask.assert_not_called()

    def test_invalid_second_batch_does_not_pollute_previous_result(self):
        result = self.run_search([batch(job('前端', '1')), batch(job('前端', '2'), {'jobName': '坏数据'})])
        self.assertFalse(result['complete'])
        self.assertEqual(len(result['jobs']), 1)
        self.assertEqual(result['batches'], 1)

    def test_no_initial_response_is_an_error_not_empty_success(self):
        page = SearchPage([])
        page.goto = lambda *args, **kwargs: None
        with self.assertRaisesRegex(QueryError, '超时'):
            self.run_search(page=page)
        self.assertFalse(any(page.handlers.values()))

    def test_buffered_out_of_order_responses_are_read_in_request_order(self):
        page = SearchPage([])
        def navigate(url, **kwargs):
            page.url = url
            response_url = HOME + 'wapi/zpgeek/search/joblist.json?' + urlsplit(url).query
            first = Response(batch(job('前端', '1')), url=response_url + '&page=1')
            last = Response(batch(job('前端', '2'), more=False), url=response_url + '&page=2')
            stale = Response(batch(job('前端', 'stale'), more=False), url=response_url)
            for handler in list(page.handlers['response']):
                handler(stale)  # 上次查询的在途响应，没有本次 request 事件。
            for response in (first, last):
                for handler in list(page.handlers['request']):
                    handler(response.request)
            for response in (last, first):
                for handler in list(page.handlers['response']):
                    handler(response)
        page.goto = navigate
        result = self.run_search(page=page)
        self.assertTrue(result['complete'])
        self.assertEqual([j['url'].split('/')[-1] for j in result['jobs']], ['1.html', '2.html'])
        page.mouse.wheel.assert_not_called()

    def test_different_query_city_and_company_responses_are_ignored(self):
        page = SearchPage([batch(job('前端', 'real'), more=False)])
        navigate = page.goto
        def with_unrelated(url, **kwargs):
            for suffix in ('query=其他&city=101280600', 'query=前端&city=101010100'):
                page.list_url = HOME + 'wapi/zpgeek/search/joblist.json?' + suffix
                page.emit(batch(job('前端', 'wrong'), more=False))
            page.list_url = HOME + 'wapi/zpgeek/brand/job/querylist.json?query=前端&city=101280600'
            page.emit(batch(job('前端', 'wrong'), more=False))
            navigate(url, **kwargs)
        page.goto = with_unrelated
        result = self.run_search(page=page)
        self.assertTrue(result['complete'])
        self.assertEqual(len(result['jobs']), 1)
        self.assertTrue(result['jobs'][0]['url'].endswith('/real.html'))

    def test_service_marks_partial_and_accepts_next_task(self):
        with patch('service.DATA', Path(self.temp.name)):
            service = Service(dev=True)
        self.addCleanup(service.close)
        service.boss = Mock()
        service.boss.run.return_value = {'complete': False, 'stop_message': '未读完', 'jobs': []}
        task = service.submit({'keyword': '前端'})
        service.worker.submit(lambda: None).result(timeout=5)
        self.assertEqual(task.snapshot()['status'], 'partial')
        following = service.submit({'keyword': '销售'})
        self.assertNotEqual(task.id, following.id)

    def test_partial_csv_is_downloadable_through_http_handler(self):
        from crawl import Handler
        result = self.run_search([batch(job('前端', '1'))], batches=1)
        task = Task({})
        task.finish('partial', '未读完', result=result)
        service = Mock()
        service.get.return_value = task
        class Connection:
            response = b''
            def makefile(self, *args):
                return io.BytesIO(b'GET /jobs/test/csv HTTP/1.0\r\nHost: 127.0.0.1:8765\r\n\r\n')
            def sendall(self, data):
                self.response += data
        connection = Connection()
        Handler(connection, ('127.0.0.1', 1234), SimpleNamespace(server_port=8765, service=service))
        self.assertIn(b'200 OK', connection.response)
        self.assertIn('前端'.encode(), connection.response)
        self.assertIn(b'Content-Disposition: attachment;', connection.response)

    def test_cli_ctrl_c_prints_and_exports_partial_results(self):
        from crawl import query
        state = dict(id='test', status='running', message='加载中')
        partial = dict(id='test', status='partial', message='查询已取消', result={'jobs': []})
        args = SimpleNamespace(company='', keyword='前端', city='深圳', pages=5,
                               batches=0, reselect=False, dev=True)
        with patch('crawl.request', side_effect=[state, state, partial]) as request, \
                patch('crawl.time.sleep', side_effect=KeyboardInterrupt()), \
                patch('crawl.print_result') as output, patch('builtins.print'):
            query('http://127.0.0.1:8766', args)
        self.assertTrue(request.call_args_list[1].args[0].endswith('/cancel'))
        output.assert_called_once()
        self.assertEqual(output.call_args.args[0], partial['result'])

    def test_city_and_search_parameter_validation(self):
        self.assertEqual(city_code('深圳市'), '101280600')
        self.assertEqual(city_code('北京'), '101010100')
        self.assertEqual(city_code(''), city_code('全国'))
        self.assertEqual(validate({})['batches'], 0)
        for params in ({'keyword': ''}, {'city': '不存在的城市'}, {'reselect': True},
                       {'batches': True}, {'batches': -1}, {'batches': 61}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                validate(params)

    def test_search_conditions_in_post_body_or_active_page(self):
        source = HOME + 'web/geek/jobs?query=前端&city=101280600'
        endpoint = HOME + 'wapi/zpgeek/search/joblist.json'
        for body in ('query=前端&city=101280600', '{"query":"前端","city":101280600}', None):
            request = Mock(url=endpoint, method='POST', post_data=body)
            self.assertTrue(search_request_matches(request, source, '前端', '101280600'))
        for body in ('query=销售&city=101280600', '{"query":"前端","city":101010100}'):
            request = Mock(url=endpoint, method='POST', post_data=body)
            self.assertFalse(search_request_matches(request, source, '前端', '101280600'))
        request = Mock(url=endpoint, method='GET')
        self.assertFalse(search_request_matches(request, HOME + 'web/geek/jobs?query=销售&city=101280600', '前端', '101280600'))
        self.assertFalse(search_request_matches(request, HOME, '前端', '101280600'))


if __name__ == '__main__':
    unittest.main()
