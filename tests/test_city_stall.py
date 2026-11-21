"""省内停滞隔离：只跳过明确的城市加载错误，不吞掉安全或取消信号。"""
import unittest
from unittest.mock import patch

from sites.boss_search import request_page
from test_boss_flow import Response
import test_province_search as province_tests
from test_province_search import MultiCityPage, TARGETS, operation
from test_search import batch


class CityStallTests(unittest.TestCase):
    setUp = province_tests.ProvinceSearchTests.setUp
    run_search = province_tests.ProvinceSearchTests.run_search

    def assert_failed_city(self, result, reason):
        self.assertFalse(result['complete'])
        self.assertEqual(result['stop_reason'], 'city_error')
        self.assertEqual((result['cities_visited'], result['cities_done']), (2, 1))
        self.assertEqual([(item['city'], item['reason']) for item in result['failed_cities']],
                         [('广州', reason)])
        self.assertIn('广州', result['stop_message'])
        self.assertIn('sz.html', [job['url'].rsplit('/', 1)[-1] for job in result['jobs']])
        self.assertFalse(any(self.page.handlers.values()))

    def test_repeated_jobs_skip_city_and_keep_collected_rows(self):
        result = self.run_search({
            '101280100': [batch(operation('gz', '广州'))] * 4,
            '101280600': [batch(operation('sz', '深圳'), more=False)],
        }, targets=TARGETS[:2])
        self.assert_failed_city(result, 'no_new_jobs')
        self.assertEqual(len(result['jobs']), 2)
        self.assertEqual(result['batches'], 5)
        self.assertIn('本批新增 0', str(self.task.report.call_args_list))

    def test_single_duplicate_then_new_data_still_finishes(self):
        result = self.run_search({
            '101280100': [batch(operation('gz', '广州'))] * 2 +
                         [batch(operation('gz2', '广州'), more=False)],
            '101280600': [batch(operation('sz', '深圳'), more=False)],
        }, targets=TARGETS[:2])
        self.assertTrue(result['complete'])
        self.assertEqual(result['failed_cities'], [])
        self.assertEqual(len(result['jobs']), 3)

    def test_changing_jobs_do_not_hide_stuck_page_number(self):
        page = MultiCityPage({
            '101280100': [batch(operation(str(i), '广州')) for i in range(4)],
            '101280600': [batch(operation('sz', '深圳'), more=False)],
        })
        emit = page.emit
        def with_page(data, document=False):
            if 'page=' not in page.list_url:
                page.list_url += '&page=1'
            emit(data, document)
        page.emit = with_page
        result = self.run_search({}, page=page, targets=TARGETS[:2])
        self.assert_failed_city(result, 'pagination_stalled')
        self.assertEqual(len(result['jobs']), 5)

    def test_no_initial_response_skips_city_without_claiming_zero_jobs(self):
        result = self.run_search({
            '101280100': None,
            '101280600': [batch(operation('sz', '深圳'), more=False)],
        }, targets=TARGETS[:2])
        self.assert_failed_city(result, 'timeout')

    def test_scroll_without_a_request_times_out_then_switches(self):
        page = MultiCityPage({
            '101280100': [batch(operation('gz', '广州'))],
            '101280600': [batch(operation('sz', '深圳'), more=False)],
        })
        page.mouse.wheel.side_effect = None
        result = self.run_search({}, page=page, targets=TARGETS[:2])
        self.assert_failed_city(result, 'timeout')
        self.assertEqual(len(result['jobs']), 2)

    def test_failed_request_does_not_block_response_queue_forever(self):
        page = MultiCityPage({
            '101280100': None,
            '101280600': [batch(operation('sz', '深圳'), more=False)],
        })
        goto = page.goto
        def fail_first(url, **kwargs):
            goto(url, **kwargs)
            if '101280100' in url:
                request = Response({}, url=page.list_url).request
                for handler in list(page.handlers['request']):
                    handler(request)
                for handler in list(page.handlers['requestfailed']):
                    handler(request)
        page.goto = fail_first
        result = self.run_search({}, page=page, targets=TARGETS[:2])
        self.assert_failed_city(result, 'request_failed')

    def test_every_city_timeout_is_partial_not_empty_success(self):
        result = self.run_search(dict.fromkeys(code for _, code in TARGETS))
        self.assertFalse(result['complete'])
        self.assertEqual(result['cities_done'], 0)
        self.assertEqual(len(result['failed_cities']), 3)
        self.assertEqual(result['jobs'], [])

    def test_empty_nonterminal_city_is_not_complete(self):
        result = self.run_search({
            '101280100': [batch()],
            '101280600': [batch(operation('sz', '深圳'), more=False)],
        }, targets=TARGETS[:2])
        self.assert_failed_city(result, 'empty_batch')

    def test_global_cap_still_stops_after_stall_and_retains_reason(self):
        result = self.run_search({
            '101280100': [batch(operation('gz', '广州'))] * 4,
            '101280600': [batch(operation('sz', '深圳'), more=False)],
        }, targets=TARGETS[:2], batches=4)
        self.assertEqual(result['stop_reason'], 'batch_limit')
        self.assertEqual(result['cities_visited'], 1)
        self.assertEqual(result['failed_cities'][0]['reason'], 'no_new_jobs')

    def test_risk_login_and_unknown_schema_errors_do_not_skip_city(self):
        for error in (dict(code=37, message='访问异常'), dict(code=1, message='请先登录'),
                      dict(code=0, zpData=dict(jobList=[{}], hasMore=True))):
            with self.subTest(error=error), patch('sites.boss.BossSession.open_login', return_value=False):
                self.store.write('quota.json', {})
                result = self.run_search({
                    '101280100': [batch(operation('gz', '广州')), error],
                    '101280600': [batch(operation('sz', '深圳'), more=False)],
                }, targets=TARGETS[:2])
                self.assertEqual(result['stop_reason'], 'interrupted')
                self.assertEqual(result['cities_visited'], 1)
                self.assertEqual(result['failed_cities'], [])

    def test_request_page_supports_json_form_and_url(self):
        from types import SimpleNamespace
        for url, body, expected in [
            ('https://x/?page=2', None, 2),
            ('https://x/', '{"page":3}', 3),
            ('https://x/', 'page=4', 4),
            ('https://x/', '{"page":true}', None),
            ('https://x/', '{}', None),
        ]:
            with self.subTest(body=body):
                self.assertEqual(request_page(SimpleNamespace(url=url, method='POST', post_data=body)), expected)


if __name__ == '__main__':
    unittest.main()
