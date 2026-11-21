"""省份与城市筛选：地区代码、同名歧义及公司岗位的本地归属。"""
import unittest

from service import validate
from sites.boss import matches
from sites.boss_locations import search_targets
from sites.boss_search import city_code


class LocationTests(unittest.TestCase):
    def test_province_aliases_and_municipalities(self):
        for name, expected in (
            ("广东", "101280000"), ("广东省", "101280000"),
            ("广西", "101300000"), ("广西壮族自治区", "101300000"),
            ("北京市", "101010100"), ("北京", "101010100"),
            ("重庆市", "101040100"), ("深圳市", "101280600"),
        ):
            with self.subTest(name=name):
                self.assertEqual(city_code(name), expected)

    def test_jilin_province_does_not_override_existing_city(self):
        self.assertEqual(city_code("吉林省"), "101060000")
        self.assertEqual(city_code("吉林"), "101060200")
        self.assertEqual(city_code("吉林市"), "101060200")

    def test_unrestricted_location_in_search_and_company_filter(self):
        job = dict(title="前端开发", skills=[], city="")
        for name in ("", "全国", "不限", "不限城市", "不限省份", "不限地区"):
            with self.subTest(name=name):
                self.assertEqual(city_code(name), city_code("全国"))
                self.assertTrue(matches(job, "前端", name))

    def test_province_filter_uses_city_membership_and_keeps_role_filter(self):
        for city, expected in (("深圳", True), ("广州市", True), ("珠海", True),
                               ("北京", False), ("", False), ("未知城市", False)):
            with self.subTest(city=city):
                job = dict(title="前端开发", skills=["Vue"], city=city)
                self.assertEqual(matches(job, "前端", "广东省"), expected)
                self.assertEqual(matches(job, "Vue", "广东"), expected)
                self.assertFalse(matches(job, "Java", "广东省"))
        self.assertFalse(matches(dict(title="前端", skills=[], city="广州"), "前端", "深圳"))

    def test_jilin_and_municipality_membership(self):
        for city in ("长春", "吉林", "吉林市", "延边朝鲜族自治州"):
            with self.subTest(city=city):
                self.assertTrue(matches(dict(title="前端", skills=[], city=city), "前端", "吉林省"))
        self.assertFalse(matches(dict(title="前端", skills=[], city="长春"), "前端", "吉林市"))
        self.assertTrue(matches(dict(title="前端", skills=[], city="北京"), "前端", "北京市"))

    def test_validation_recognizes_provinces_in_both_query_modes(self):
        for company in ("", "测试公司"):
            with self.subTest(company=company):
                self.assertEqual(validate(dict(company=company, city=" 广东省 "))["city"], "广东省")
                with self.assertRaises(ValueError):
                    validate(dict(company=company, city="不存在的省份"))

    def test_province_search_targets_are_actual_cities_not_province_code(self):
        targets = search_targets("广东省")
        self.assertEqual(targets, search_targets("广东"))
        self.assertIn(("广州", "101280100"), targets)
        self.assertIn(("深圳", "101280600"), targets)
        self.assertIn(("珠海", "101280700"), targets)
        self.assertNotIn(("北京", "101010100"), targets)
        codes = [code for _, code in targets]
        self.assertNotIn("101280000", codes)
        self.assertEqual(len(codes), len(set(codes)))
        self.assertTrue(all(code.startswith("10128") and not code.endswith("0000") for code in codes))

    def test_city_and_national_search_targets_do_not_expand(self):
        self.assertEqual(search_targets("深圳市"), [("深圳", "101280600")])
        self.assertEqual(search_targets("北京"), [("北京", "101010100")])
        for name in ("", "全国", "不限省份", "不限地区"):
            with self.subTest(name=name):
                self.assertEqual(search_targets(name), [("全国", "100010000")])

    def test_jilin_province_expands_but_same_named_city_remains_one_target(self):
        province = search_targets("吉林省")
        self.assertIn(("长春", "101060100"), province)
        self.assertIn(("吉林", "101060200"), province)
        self.assertGreater(len(province), 1)
        self.assertNotIn("101060000", [code for _, code in province])
        self.assertEqual(search_targets("吉林"), [("吉林", "101060200")])
        self.assertEqual(search_targets("吉林市"), [("吉林", "101060200")])


if __name__ == "__main__":
    unittest.main()
