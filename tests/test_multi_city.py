"""多选城市只访问选中的城市，拒绝空选择和跨省混入。"""
import unittest

from service import Task, validate
import test_province_search as province_tests
from test_province_search import operation
from test_search import batch


class MultiCityTests(unittest.TestCase):
    setUp = province_tests.ProvinceSearchTests.setUp
    run_search = province_tests.ProvinceSearchTests.run_search

    def test_shaoguan_and_huizhou_only(self):
        selected = ['韶关', '惠州']
        params = validate(dict(city='广东省', selected_cities=selected))
        self.assertEqual([c['city'] for c in Task(params).snapshot()['city_progress']], selected)
        result = self.run_search({
            '101280200': [batch(operation('sg', '韶关'), operation('wrong', '广州'), more=False)],
            '101280300': [batch(operation('hz', '惠州'), more=False)],
        }, targets=[('广州', '101280100'), ('韶关', '101280200'), ('惠州', '101280300')],
            selected_cities=selected)
        self.assertTrue(result['complete'])
        self.assertEqual([visit['city'][0] for visit in self.page.visits], ['101280200', '101280300'])
        self.assertEqual([job['city'] for job in result['jobs']], selected)
        self.assertEqual([c['city'] for c in result['city_progress']], selected)
        self.assertEqual(result['cities_total'], 2)

    def test_validation_normalizes_and_deduplicates(self):
        params = validate(dict(city='广东省', selected_cities=['惠州市', '韶关', '惠州']))
        self.assertEqual(params['selected_cities'], ['韶关', '惠州'])

    def test_invalid_selection_cannot_fall_back_to_whole_province(self):
        for selection in ([], None, '韶关', ['杭州'], ['广东省'], ['全国'], [123]):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                validate(dict(city='广东省', selected_cities=selection))
        with self.assertRaises(ValueError):
            validate(dict(city='全国', selected_cities=['韶关']))
