import unittest

from salary import parse_salary, summarize


class SalaryTests(unittest.TestCase):
    def test_formats(self):
        cases = [
            ("15-25K", 15000, 25000, 12, "monthly"),
            ("15-25K·14薪", 15000, 25000, 14, "monthly"),
            ("1-1.5万", 10000, 15000, 12, "monthly"),
            ("8千-1.2万", 8000, 12000, 12, "monthly"),
            ("200-300元/天", None, None, None, "daily"),
            ("面议", None, None, None, "negotiable"),
            ("２０ｋ／月", 20000, 20000, 12, "monthly"),
            ("15元/小时", None, None, None, "hourly"),
            ("10K以上", 10000, None, 12, "monthly"),
            ("1-2万/年", 833.33, 1666.67, 12, "monthly"),
            ("20万-30万/年", 16666.67, 25000, 12, "monthly"),
            ("25-15K", None, None, None, "unknown"),
            (None, None, None, None, "unknown"),
        ]
        for raw, low, high, months, kind in cases:
            with self.subTest(raw=raw):
                self.assertEqual(parse_salary(raw), dict(min=low, max=high, months=months, type=kind, raw=raw))

    def test_monthly_statistics(self):
        values = ["15-25K·14薪", "8千-1.2万", "200-300元/天", "面议", "未知", "10K以上", "15元/小时"]
        self.assertEqual(summarize(map(parse_salary, values)), dict(
            monthly_count=2, min=8000, max=25000, median=15000, avg_months=13))

    def test_no_monthly_samples(self):
        self.assertEqual(summarize([parse_salary("面议")]), dict(
            monthly_count=0, min=None, max=None, median=None, avg_months=None))
