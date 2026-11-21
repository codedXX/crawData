"""CSV 的前置工商字段、行业推测及原岗位字段不得发生错位。"""
import csv
import io
import unittest

from store import csv_bytes


def posting(**extra):
    return dict(company="测试公司", title="前端开发", salary_raw="15-25K·14薪",
                salary_min=15000, salary_max=25000, months=14, salary_type="monthly",
                city="深圳", area="南山区", experience="3-5年", degree="本科",
                skills=["Vue", "TypeScript"], url="https://www.zhipin.com/job_detail/example.html", **extra)


class CsvTests(unittest.TestCase):
    def rows(self, jobs):
        return list(csv.reader(io.StringIO(csv_bytes(jobs).decode("utf-8-sig"))))

    def test_header_order_and_existing_columns_are_preserved(self):
        header, row = self.rows([posting(registered_capital="1000万人民币",
                                        industry="软件行业（按岗位估算）", established_date="2021-08-30")])
        self.assertEqual(header, ["注册资金", "所属行业", "成立日期", "公司", "岗位", "薪资原文",
                                  "最低月薪（元）", "最高月薪（元）", "年薪月数", "薪资类型", "城市",
                                  "区域", "经验", "学历", "技能", "岗位链接"])
        self.assertEqual(row[:6], ["1000万人民币", "软件行业（按岗位估算）", "2021-08-30",
                                   "测试公司", "前端开发", "15-25K·14薪"])
        self.assertEqual(row[6:], ["15000", "25000", "14", "monthly", "深圳", "南山区", "3-5年",
                                  "本科", "Vue、TypeScript", "https://www.zhipin.com/job_detail/example.html"])

    def test_missing_company_facts_stay_blank_and_industry_is_estimated(self):
        header, row = self.rows([posting()])
        self.assertEqual(row[:3], ["", "软件和信息技术服务业（按岗位估算）", ""])
        self.assertEqual(len(header), len(row))
        generic = posting()
        generic.update(title="销售经理", skills=[])
        self.assertEqual(self.rows([generic])[1][1], "无法判断（岗位信息不足）")

    def test_chinese_currency_commas_and_line_breaks_round_trip_with_bom(self):
        job = posting(registered_capital="1,000万元（人民币）\n未折算", established_date="2021-08-30")
        raw = csv_bytes([job])
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(self.rows([job])[1][0], "1,000万元（人民币）\n未折算")

    def test_optional_company_fields_preserve_formula_injection_protection(self):
        row = self.rows([posting(registered_capital="=1+1", industry=" +1", established_date="@test")])[1]
        self.assertEqual(row[:3], ["'=1+1", "' +1", "'@test"])

    def test_old_required_fields_are_still_required(self):
        job = posting()
        del job["salary_raw"]
        with self.assertRaises(KeyError):
            csv_bytes([job])

    def test_empty_export_still_contains_all_headers(self):
        rows = self.rows([])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:3], ["注册资金", "所属行业", "成立日期"])


if __name__ == "__main__":
    unittest.main()
