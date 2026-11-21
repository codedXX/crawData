"""行业输出是岗位推测，不能把通用职能或公司名称误当作行业证据。"""
import unittest

from industry import ESTIMATE_SUFFIX, UNKNOWN_INDUSTRY, estimate_industry


class IndustryTests(unittest.TestCase):
    def test_software_role_and_skill_hints_are_explicitly_estimated(self):
        for title, skills in (("前端开发", []), ("Web 开发工程师", []),
                              ("开发工程师", ["Vue", "TypeScript"]),
                              ("软件测试工程师", ["Excel"])):
            with self.subTest(title=title):
                self.assertEqual(estimate_industry(dict(title=title, skills=skills)),
                                 "软件和信息技术服务业" + ESTIMATE_SUFFIX)

    def test_explicit_business_domains_take_priority_over_developer_skills(self):
        for title, industry in (("银行前端开发", "金融业"), ("护士", "医药医疗相关行业"),
                                ("数控加工工程师", "制造业"), ("数学教师", "教育培训"),
                                ("物流调度员", "交通运输、仓储和邮政业"),
                                ("房地产经纪人", "房地产业"), ("餐饮厨师", "住宿和餐饮业")):
            with self.subTest(title=title):
                self.assertEqual(estimate_industry(dict(title=title, skills=["Vue"])),
                                 industry + ESTIMATE_SUFFIX)

    def test_generic_functions_do_not_establish_company_industry(self):
        for title in ("销售经理", "财务经理", "人事专员", "运营助理", "司机", "质量工程师"):
            with self.subTest(title=title):
                self.assertEqual(estimate_industry(dict(title=title, skills=["Excel", "沟通能力"])),
                                 UNKNOWN_INDUSTRY)

    def test_does_not_classify_from_company_or_recruiter_data(self):
        self.assertEqual(estimate_industry(dict(title="经理", skills=[], company="某某银行",
                                               bossName="软件开发经理")), UNKNOWN_INDUSTRY)
        self.assertEqual(estimate_industry(dict(title="前端开发", skills=[], company="某某医院")),
                         "软件和信息技术服务业" + ESTIMATE_SUFFIX)

    def test_empty_information_and_word_boundaries_remain_unknown(self):
        self.assertEqual(estimate_industry({}), UNKNOWN_INDUSTRY)
        self.assertEqual(estimate_industry(dict(title="经理", skills=["Avenue", "Springfield"])),
                         UNKNOWN_INDUSTRY)


if __name__ == "__main__":
    unittest.main()
