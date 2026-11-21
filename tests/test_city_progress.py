"""逐城进度必须与实际读取、上限和中断一致。"""
import unittest

from service import Task, validate
from sites.boss_locations import city_code, location_options, search_targets
import test_province_search as province_tests
from test_province_search import TARGETS, operation
from test_search import batch


class CityProgressTests(unittest.TestCase):
    setUp = province_tests.ProvinceSearchTests.setUp
    run_search = province_tests.ProvinceSearchTests.run_search

    def test_progress_preserves_done_partial_and_unvisited_cities(self):
        self.task = Task({})
        snapshots = []
        update = self.task.set_city_progress

        def capture(cities):
            update(cities)
            snapshots.append(self.task.snapshot()['city_progress'])

        self.task.set_city_progress = capture
        self.task.sleep = lambda seconds: None
        result = self.run_search({
            '101280100': [batch(operation('gz', '广州'), more=False)],
            '101280600': [batch(operation('sz', '深圳'))],
        }, batches=2)
        self.assertEqual([c['status'] for c in snapshots[0]], ['running', 'pending', 'pending'])
        self.assertTrue(any([c['status'] for c in s] == ['done', 'running', 'pending'] for s in snapshots))
        self.assertEqual([c['status'] for c in result['city_progress']], ['done', 'partial', 'pending'])
        self.assertEqual(self.task.snapshot()['city_progress'], result['city_progress'])

    def test_city_limit_then_empty_finished_city(self):
        result = self.run_search({
            '101280100': [batch(operation('gz', '广州'))],
            '101280600': [batch(more=False)],
        }, targets=TARGETS[:2], per_city_batches=1)
        self.assertEqual([c['status'] for c in result['city_progress']], ['partial', 'done'])
        self.assertIn('上限', result['city_progress'][0]['message'])

    def test_plan_exists_before_browser_or_login_and_errors_settle_current_city(self):
        task = Task(validate({'city': '广东省'}))
        plan = task.snapshot()['city_progress']
        self.assertEqual([c['city'] for c in plan], [name for name, code in search_targets('广东省')])
        self.assertTrue(all(c['status'] == 'pending' for c in plan))
        changed = [dict(c) for c in plan]
        changed[0]['status'] = 'running'
        task.set_city_progress(changed)
        task.finish('error', '查询已取消。')
        self.assertEqual(task.snapshot()['city_progress'][0]['status'], 'partial')
        self.assertTrue(all(c['status'] == 'pending' for c in plan))
        self.assertNotIn('city_progress', Task(validate({'company': '公司', 'city': '广东省'})).snapshot())
        self.assertNotIn('city_progress', Task(validate({'city': '深圳'})).snapshot())

    def test_cascader_catalog_uses_search_targets_and_disambiguates_jilin(self):
        catalog = location_options()
        self.assertEqual(len(catalog), 34)
        for province in catalog:
            self.assertEqual([(c['label'], c['code']) for c in province['cities']], search_targets(province['value']))
            for city in province['cities']:
                self.assertEqual(city_code(city['value']), city['code'])
        jilin = next(p for p in catalog if p['value'] == '吉林省')
        self.assertNotEqual(city_code(jilin['value']), city_code('吉林'))
