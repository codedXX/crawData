"""旧导出兼容接口也必须纯本地处理，不允许重新访问 BOSS。"""
import unittest
from unittest.mock import Mock, patch

from company_export import CompanyExporter
from test_csv import posting


class CompanyExportTests(unittest.TestCase):
    def test_missing_fields_and_repeat_exports_never_use_browser_store_or_task(self):
        browser, store, task = Mock(), Mock(), Mock()
        jobs = [posting(company_id="brand")]
        exporter = CompanyExporter(browser, store)
        with patch("socket.socket", side_effect=AssertionError("导出不得联网")):
            for _ in range(2):
                result = exporter.enrich(jobs, task)
                self.assertTrue(result["complete"])
                self.assertEqual(result["companies"], 0)
                self.assertEqual(result["missing_count"], 1)
                self.assertEqual(result["jobs"][0]["registered_capital"], "")
                self.assertEqual(result["jobs"][0]["established_date"], "")
                self.assertIn("按岗位估算", result["jobs"][0]["industry"])
        self.assertEqual(browser.mock_calls, [])
        self.assertEqual(store.mock_calls, [])
        self.assertEqual(task.mock_calls, [])
        self.assertNotIn("registered_capital", jobs[0])

    def test_existing_fields_preserved_without_filling_missing_company_facts(self):
        jobs = [posting(registered_capital="100万元", established_date="2021-08-30", industry="已有行业"),
                posting(registered_capital="200万元", company_id="brand")]
        result = CompanyExporter(None, None).enrich(jobs, None)
        self.assertEqual(result["jobs"][0], jobs[0])
        self.assertIsNot(result["jobs"][0], jobs[0])
        self.assertEqual(result["jobs"][1]["registered_capital"], "200万元")
        self.assertEqual(result["jobs"][1]["established_date"], "")
        self.assertEqual(result["missing_count"], 1)

    def test_empty_export_needs_no_browser(self):
        result = CompanyExporter(None, None).enrich([], None)
        self.assertEqual(result["jobs"], [])
        self.assertEqual(result["missing_count"], 0)
