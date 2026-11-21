"""省级搜索通过真实城市逐个收集；省码返回空不能代表全省无岗位。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from service import Task, validate
from sites.boss import BossQuery, HOME
from store import ROOT, Store
from test_boss_flow import Response, job
from test_search import SearchPage, batch


TARGETS = [('广州', '101280100'), ('深圳', '101280600'), ('珠海', '101280700')]


def operation(ident, city, salary='10-20K'):
    return dict(job('电商运营', ident, salary), cityName=city)


class MultiCityPage(SearchPage):
    def __init__(self, cities):
        super().__init__([])
        self.city_pages = cities
        self.visits = []

    def goto(self, url, **kwargs):
        self.url = url
        query = parse_qs(urlsplit(url).query)
        code = query['city'][0]
        self.visits.append(query)
        self.list_url = HOME + 'wapi/zpgeek/search/joblist.json?' + urlsplit(url).query
        documents = self.city_pages[code]
        self.pages = iter(documents or [])
        if documents is not None:
            self.emit_next()


class ProvinceSearchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=ROOT, prefix='.test-province-')
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name))
        self.task = Mock()
        self.browser = Mock()
        self.query = BossQuery(self.browser, self.store)

    def run_search(self, pages, targets=TARGETS, page=None, **params):
        self.page = page or MultiCityPage(pages)
        self.browser.page.return_value = self.page
        with patch('sites.boss_search.search_targets', return_value=targets), \
                patch('sites.boss_search.time.monotonic', side_effect=lambda: self.page.clock):
            return self.query.run(validate(dict(dict(keyword='运营', city='广东省', batches=0, per_city_batches=0), **params)), self.task)

    def test_first_empty_city_does_not_end_whole_province(self):
        result = self.run_search({
            '101280100': [batch(more=False)],
            '101280600': [batch(operation('sz', '深圳'), more=False)],
            '101280700': [batch(more=False)],
        })
        self.assertTrue(result['complete'])
        self.assertEqual((result['cities_total'], result['cities_done']), (3, 3))
        self.assertEqual(result['current_city'], '珠海')
        self.assertTrue(result['province_search'])
        self.assertEqual((result['batches'], result['scanned']), (3, 1))
        self.assertEqual([j['city'] for j in result['jobs']], ['深圳'])
        self.assertEqual([query['city'] for query in self.page.visits],
                         [['101280100'], ['101280600'], ['101280700']])
        self.assertEqual({query['query'][0] for query in self.page.visits}, {'运营'})
        self.assertEqual([parse_qs(urlsplit(source).query)['city'][0] for source in result['sources']],
                         ['101280100', '101280600', '101280700'])
        self.assertFalse(any(self.page.handlers.values()))

    def test_global_batch_limit_after_empty_city_is_partial_not_empty_province(self):
        result = self.run_search({
            '101280100': [batch(more=False)],
            '101280600': [batch(operation('sz', '深圳'), more=False)],
            '101280700': [batch(more=False)],
        }, batches=1)
        self.assertFalse(result['complete'])
        self.assertEqual(result['stop_reason'], 'batch_limit')
        self.assertEqual((result['cities_total'], result['cities_done']), (3, 1))
        self.assertEqual(result['jobs'], [])
        self.assertEqual(len(self.page.visits), 1)
        self.assertFalse(any(self.page.handlers.values()))

    def test_last_city_finished_at_global_limit_is_complete(self):
        result = self.run_search({
            '101280100': [batch(operation('gz', '广州'), more=False)],
            '101280600': [batch(operation('sz', '深圳'), more=False)],
        }, targets=TARGETS[:2], batches=2)
        self.assertTrue(result['complete'])
        self.assertEqual((result['cities_total'], result['cities_done'], result['batches']), (2, 2, 2))

    def test_duplicate_only_first_batch_in_next_city_is_not_repeated_page(self):
        result = self.run_search({
            '101280100': [batch(operation('shared', '广州'), more=False)],
            '101280600': [batch(operation('shared', '深圳')),
                          batch(operation('sz', '深圳', '20-40K'), more=False)],
        }, targets=TARGETS[:2])
        self.assertTrue(result['complete'])
        self.assertEqual(result['batches'], 3)
        self.assertEqual(result['scanned'], 3)
        self.assertEqual(len(result['jobs']), 2)
        self.assertEqual(result['summary']['monthly_count'], 2)
        self.assertEqual(result['summary']['median'], 22500)
        self.assertEqual(self.page.mouse.wheel.call_count, 1)

    def test_previous_city_response_is_not_accepted_by_next_city_listener(self):
        page = MultiCityPage({
            '101280100': [batch(operation('gz', '广州'), more=False)],
            '101280600': [batch(operation('sz', '深圳'), more=False)],
        })
        navigate = page.goto
        stale = Response(batch(operation('stale', '广州'), more=False), url=
                         HOME + 'wapi/zpgeek/search/joblist.json?query=运营&city=101280100&page=2')
        def with_delayed_old_city(url, **kwargs):
            code = parse_qs(urlsplit(url).query)['city'][0]
            if code == '101280600':
                # 上一个城市已发起但未完成的请求，在新城市监听器建立后才返回。
                for handler in list(page.handlers['response']):
                    handler(stale)
                # 新产生的明确旧城市请求也必须被地区条件排除。
                for handler in list(page.handlers['request']):
                    handler(stale.request)
                for handler in list(page.handlers['response']):
                    handler(stale)
            navigate(url, **kwargs)
            if code == '101280100':
                for handler in list(page.handlers['request']):
                    handler(stale.request)
        page.goto = with_delayed_old_city
        result = self.run_search({}, targets=TARGETS[:2], page=page)
        self.assertTrue(result['complete'])
        self.assertEqual({j['url'].rsplit('/', 1)[-1] for j in result['jobs']}, {'gz.html', 'sz.html'})
        self.assertEqual(result['batches'], 2)
        self.assertFalse(any(page.handlers.values()))

    def test_cancel_between_cities_keeps_previous_city_jobs(self):
        self.task = Task({})
        self.task.sleep = lambda seconds: None if not self.task.cancelled.is_set() else self.task.report('等待')
        report = self.task.report
        def stop_after_first_batch(message):
            report(message)
            if '已读' in message:
                self.task.cancel()
        self.task.report = stop_after_first_batch
        result = self.run_search({
            '101280100': [batch(operation('gz', '广州'), more=False)],
            '101280600': [batch(operation('sz', '深圳'), more=False)],
        }, targets=TARGETS[:2])
        self.assertFalse(result['complete'])
        self.assertIn('取消', result['stop_message'])
        self.assertEqual(len(result['jobs']), 1)
        self.assertEqual(len(self.page.visits), 1)
        self.assertEqual(result['cities_done'], 1)
        self.assertFalse(any(self.page.handlers.values()))

    def test_next_city_timeout_preserves_previous_city_and_progress(self):
        result = self.run_search({
            '101280100': [batch(operation('gz', '广州'), more=False)],
            '101280600': None,
        }, targets=TARGETS[:2])
        self.assertFalse(result['complete'])
        self.assertEqual((result['cities_total'], result['cities_done']), (2, 1))
        self.assertEqual(result['current_city'], '深圳')
        self.assertIn('超时', result['stop_message'])
        self.assertEqual(result['batches'], 1)
        self.assertEqual([j['city'] for j in result['jobs']], ['广州'])
        self.assertFalse(any(self.page.handlers.values()))

    def test_optional_city_budget_switches_away_from_continuing_first_city(self):
        result = self.run_search({
            # 每批都包含新岗位且一直声明还有下一批，重现大城市长期占用搜索。
            '101280100': [batch(operation(f'gz-{i}', '广州')) for i in range(12)],
            '101280600': [batch(operation('sz', '深圳'), more=False)],
        }, targets=TARGETS[:2], per_city_batches=5)
        self.assertFalse(result['complete'])
        self.assertEqual(result['stop_reason'], 'city_limit')
        self.assertEqual(result['limited_cities'], ['广州'])
        self.assertEqual((result['cities_visited'], result['cities_done'], result['cities_total']), (2, 1, 2))
        self.assertEqual(result['batches'], 6)
        self.assertEqual({j['url'].rsplit('/', 1)[-1] for j in result['jobs']},
                         {f'gz-{i}.html' for i in range(5)} | {'sz.html'})
        self.assertEqual([query['city'][0] for query in self.page.visits], ['101280100', '101280600'])
        self.assertEqual({query['query'][0] for query in self.page.visits}, {'运营'})
        self.assertIn('未读完', result['stop_message'])
        self.assertFalse(any(self.page.handlers.values()))

    def test_unlimited_city_budget_keeps_loading_until_city_explicitly_ends(self):
        result = self.run_search({
            '101280100': [batch(operation(f'gz-{i}', '广州'), more=i < 6) for i in range(7)],
            '101280600': [batch(operation('sz', '深圳'), more=False)],
        }, targets=TARGETS[:2], per_city_batches=0)
        self.assertTrue(result['complete'])
        self.assertEqual(result['limited_cities'], [])
        self.assertEqual((result['cities_visited'], result['cities_done']), (2, 2))
        self.assertEqual(result['batches'], 8)
        self.assertEqual(len(result['jobs']), 8)
        self.assertEqual(self.page.mouse.wheel.call_count, 6)
        self.assertFalse(any(self.page.handlers.values()))

    def test_one_batch_budget_visits_each_city_and_deduplicates_shared_jobs(self):
        result = self.run_search({
            '101280100': [batch(operation('shared', '广州'), operation('gz', '广州')),
                          batch(operation('gz-unread', '广州'), more=False)],
            '101280600': [batch(operation('shared', '深圳'), operation('sz', '深圳')),
                          batch(operation('sz-unread', '深圳'), more=False)],
        }, targets=TARGETS[:2], per_city_batches=1)
        self.assertFalse(result['complete'])
        self.assertEqual(result['stop_reason'], 'city_limit')
        self.assertEqual(result['limited_cities'], ['广州', '深圳'])
        self.assertEqual((result['cities_visited'], result['cities_done'], result['batches']), (2, 0, 2))
        self.assertEqual({j['url'].rsplit('/', 1)[-1] for j in result['jobs']}, {'shared.html', 'gz.html', 'sz.html'})
        self.assertEqual({query['query'][0] for query in self.page.visits}, {'运营'})
        self.page.mouse.wheel.assert_not_called()
        self.assertFalse(any(self.page.handlers.values()))

    def test_explicit_city_end_at_city_budget_is_complete(self):
        result = self.run_search({
            '101280100': [batch(operation('gz', '广州'), more=False)],
            '101280600': [batch(operation('sz', '深圳'), more=False)],
        }, targets=TARGETS[:2], per_city_batches=1)
        self.assertTrue(result['complete'])
        self.assertEqual(result['limited_cities'], [])
        self.assertEqual((result['cities_visited'], result['cities_done']), (2, 2))

    def test_total_batch_limit_stops_before_city_budget_can_schedule_next_city(self):
        result = self.run_search({
            '101280100': [batch(operation('gz', '广州'))],
            '101280600': [batch(operation('sz', '深圳'), more=False)],
        }, targets=TARGETS[:2], per_city_batches=1, batches=1)
        self.assertFalse(result['complete'])
        self.assertEqual(result['stop_reason'], 'batch_limit')
        self.assertEqual((result['cities_visited'], result['cities_done'], result['batches']), (1, 0, 1))
        self.assertEqual(len(self.page.visits), 1)
        self.assertFalse(any(self.page.handlers.values()))

    def test_final_city_end_does_not_hide_previous_city_truncation(self):
        result = self.run_search({
            '101280100': [batch(operation('gz', '广州'))],
            '101280600': [batch(operation('sz', '深圳'), more=False)],
        }, targets=TARGETS[:2], per_city_batches=1, batches=2)
        self.assertFalse(result['complete'])
        # 总预算也已耗尽时以总上限为停止原因，仍须保留之前城市未读完的信息。
        self.assertEqual(result['stop_reason'], 'batch_limit')
        self.assertEqual(result['limited_cities'], ['广州'])
        self.assertEqual((result['cities_visited'], result['cities_done'], result['batches']), (2, 1, 2))


if __name__ == '__main__':
    unittest.main()
