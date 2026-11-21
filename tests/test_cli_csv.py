"""CLI 在工商信息补充失败后，仍能打印并导出原岗位结果。"""
import csv
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from crawl import print_result, query
from sites.boss import map_job
from store import ROOT


class CliCsvTests(unittest.TestCase):
    def test_query_exports_result_without_any_followup_request(self):
        args = SimpleNamespace(company='', keyword='前端', city='深圳', pages=1, batches=1, reselect=False, dev=True)
        state = dict(id='offline', status='partial', message='已有部分结果', result={'jobs': [{'title': '前端'}]})
        with patch('crawl.request', return_value=state) as request, patch('crawl.print_result') as save, redirect_stdout(io.StringIO()):
            query('http://127.0.0.1:8766', args)
        request.assert_called_once()
        self.assertTrue(request.call_args.args[0].endswith('/jobs/query'))
        save.assert_called_once()

    def test_original_jobs_without_company_fields_still_print_and_export(self):
        job = map_job(dict(encryptJobId="job", jobName="前端开发", salaryDesc="15-25K·14薪",
                           brandName="测试公司", cityName="深圳", skills=["Vue"]),
                      {"name": "测试公司", "id": "brand"})
        for field in ("registered_capital", "industry", "established_date"):
            self.assertNotIn(field, job)
        result = dict(jobs=[job], summary=dict(monthly_count=1, min=15000, max=25000,
                                             median=20000, avg_months=14))
        output = io.StringIO()
        with tempfile.TemporaryDirectory(dir=ROOT, prefix=".test-cli-csv-") as directory:
            with redirect_stdout(output):
                print_result(result, "original", Path(directory))
            saved = Path(directory) / "boss-original.csv"
            with saved.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.reader(stream))
        self.assertEqual(rows[0][:4], ["注册资金", "所属行业", "成立日期", "公司"])
        self.assertEqual(rows[1][:5], ["", "软件和信息技术服务业（按岗位估算）", "", "测试公司", "前端开发"])
        self.assertIn("CSV 已写入", output.getvalue())
        self.assertIn("软件和信息技术服务业（按岗位估算）", output.getvalue())
        self.assertNotIn("registered_capital", job)


if __name__ == "__main__":
    unittest.main()
