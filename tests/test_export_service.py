"""导出准备兼容接口只用已有数据，不访问网站、不占用采集线程。"""
import unittest
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from service import Busy, Service, Task
from crawl import Handler
from guard import Guard
from store import csv_bytes
from test_csv import posting


class ExportServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = Service(directory=Path(__file__).parent / 'unused-export-service')
        self.addCleanup(self.service.close)
        self.jobs = [dict(title='前端', company='公司', registered_capital='100万元', established_date=''),
                     dict(title='前端测试', company='另一公司')]
        self.task = Task({})
        self.task.finish('done', '完成', result=dict(jobs=self.jobs, summary={'median': 15000}))
        self.service.tasks[self.task.id] = self.task

    def test_export_and_repeat_export_never_start_browser_or_worker(self):
        with patch.object(self.service.browser, 'page') as browser, \
                patch.object(self.service.worker, 'submit') as worker, \
                patch('company_export.CompanyExporter.enrich') as enrich:
            for _ in range(2):
                self.assertIs(self.service.prepare_export(self.task.id), self.task)
                self.assertEqual(self.task.snapshot()['export']['status'], 'done')
            browser.assert_not_called()
            worker.assert_not_called()
            enrich.assert_not_called()
        self.assertIs(self.task.snapshot()['result']['jobs'], self.jobs)
        self.assertEqual(self.task.snapshot()['export']['missing_count'], 2)
        self.assertNotIn('registered_capital', self.jobs[1])

    def test_partial_cancelled_result_exportable_while_another_query_exists(self):
        self.task.state['status'] = 'partial'
        self.task.cancelled.set()
        another = Task({})
        self.service.tasks[another.id] = another
        self.service.prepare_export(self.task.id)
        self.assertEqual(self.task.snapshot()['status'], 'partial')
        self.assertEqual(self.task.snapshot()['export']['status'], 'done')
        self.assertEqual(another.snapshot()['status'], 'running')

    def test_unknown_running_and_closed_service_rejected(self):
        with self.assertRaises(ValueError):
            self.service.prepare_export('missing')
        self.task.state['status'] = 'running'
        with self.assertRaises(ValueError):
            self.service.prepare_export(self.task.id)
        self.service.close()
        with self.assertRaises(Busy):
            self.service.prepare_export(self.task.id)

    def test_http_repeat_download_while_blocked_never_queries_boss(self):
        from urllib.request import ProxyHandler, build_opener
        with tempfile.TemporaryDirectory() as directory:
            service = Service(directory=directory)
            self.addCleanup(service.close)
            Guard(service.store, lambda message: None).record_risk('account_restricted')
            task = Task({})
            jobs = [posting(company_id='brand')]
            task.finish('partial', '已停止', result={'jobs': jobs})
            service.tasks[task.id] = task
            with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
                server.service = service
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    with patch.object(service.browser, 'page') as browser, \
                            patch.object(service.boss, 'run') as query, \
                            patch.object(service.worker, 'submit') as submit:
                        for _ in range(2):
                            url = f'http://127.0.0.1:{server.server_port}/jobs/{task.id}/csv'
                            with build_opener(ProxyHandler({})).open(url, timeout=5) as response:
                                self.assertEqual(response.status, 200)
                                self.assertEqual(response.read(), csv_bytes(jobs))
                        browser.assert_not_called()
                        query.assert_not_called()
                        submit.assert_not_called()
                finally:
                    server.shutdown()
                    worker.join(timeout=5)
