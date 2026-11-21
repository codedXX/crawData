"""省份查询每市读取预算的验证；不会改变单市和全国查询参数。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from service import validate
from sites.boss_search import SearchSession
from store import ROOT, Store
from test_boss_flow import job
from test_search import SearchPage, batch


class CityBudgetValidationTests(unittest.TestCase):
    def test_default_city_budget_is_unlimited(self):
        self.assertEqual(validate({'city': '广东省'})['per_city_batches'], 0)

    def test_city_budget_accepts_unlimited_and_valid_integer_limits(self):
        for budget in (0, 1, 3, 5, 60):
            with self.subTest(budget=budget):
                params = validate({'city': '广东省', 'per_city_batches': budget})
                self.assertEqual(params['per_city_batches'], budget)
                self.assertEqual(params['batches'], 0)

    def test_city_budget_rejects_out_of_range_and_non_integer_values(self):
        for budget in (-1, 61, True, False, 1.0, '5', None):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                validate({'city': '广东省', 'per_city_batches': budget})

    def test_single_city_and_national_search_ignore_per_city_budget(self):
        for region in ('深圳', '全国'):
            with self.subTest(region=region), tempfile.TemporaryDirectory(dir=ROOT, prefix='.test-city-budget-') as directory:
                page = SearchPage([batch(job('前端开发', str(i)), more=i < 2) for i in range(3)])
                session = SearchSession(page, Store(Path(directory)), Mock())
                page.on('request', session.meter)
                with patch('sites.boss_search.time.monotonic', side_effect=lambda: page.clock):
                    result = session.read_search(validate(dict(keyword='前端', city=region, per_city_batches=1)))
                self.assertTrue(result['complete'])
                self.assertEqual(result['batches'], 3)
                self.assertEqual(len(result['jobs']), 3)


if __name__ == '__main__':
    unittest.main()
