"""网页导出：刷新后停止补充并下载，再次保存不能重启补充。"""
import csv
import io
import json
import os
import unittest
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sites.boss import map_job
from sites.boss_search import salary_summary
from store import csv_bytes
from ui import page_html


@unittest.skipUnless(os.environ.get('BOSS_BROWSER_TESTS') == '1', '设置 BOSS_BROWSER_TESTS=1 运行网页导出测试')
class ExportFeedbackTests(unittest.TestCase):
    def test_direct_download_and_repeat_save_do_not_request_enrichment(self):
        from patchright.sync_api import sync_playwright

        jobs = [map_job(dict(encryptJobId='offline', jobName='前端开发', salaryDesc='10-20K',
                             brandName='测试公司', cityName='深圳', skills=['Vue']), {'name': '测试公司'})]
        state = dict(id='offline', status='partial', message='查询未读完', query={'city': '广东省'},
                     city_progress=[dict(city='深圳', status='partial', message='搜索页面操作超时，未确认读完')],
                     result=dict(jobs=jobs, summary=salary_summary(jobs), query={'city': '广东省'},
                                 read_at='2026-11-13T12:00:00+08:00', mode='search', complete=False,
                                 batches=1, company_count=1, companies=[], stop_message='查询未读完'),
                     export=dict(status='partial', message='旧版补充未完成'))
        export_calls = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == '/':
                    content, kind = page_html().encode(), 'text/html; charset=utf-8'
                elif self.path == '/jobs/offline/csv':
                    content, kind = csv_bytes(jobs), 'text/csv; charset=utf-8'
                else:
                    content, kind = json.dumps(state).encode(), 'application/json'
                self.send_response(200)
                self.send_header('Content-Type', kind)
                self.send_header('Content-Length', str(len(content)))
                if self.path.endswith('/csv'):
                    self.send_header('Content-Disposition', 'attachment; filename="boss-jobs.csv"')
                self.end_headers()
                self.wfile.write(content)

            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                if self.path == '/jobs/offline/cancel':
                    state['export'] = dict(status='partial', message='已停止补充，保留岗位', missing_count=1)
                if self.path == '/jobs/offline/export':
                    export_calls.append(self.path)
                self.do_GET()

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        with sync_playwright() as driver:
            browser = driver.chromium.launch(channel='chrome', headless=True)
            try:
                page = browser.new_page(accept_downloads=True)
                page.goto(f'http://127.0.0.1:{server.server_port}/')
                page.wait_for_selector('#csv')
                self.assertIn('不访问 BOSS', page.locator('#export-status').inner_text())
                self.assertIn('操作超时', page.locator('#city-issues').inner_text())
                with page.expect_download() as download_info:
                    page.locator('#csv').click()
                content = download_info.value.path().read_bytes().decode('utf-8-sig')
                self.assertEqual(len(list(csv.DictReader(io.StringIO(content)))), 1)
                page.wait_for_function("document.getElementById('csv').getAttribute('aria-disabled') === 'false'")
                with page.expect_download():
                    page.locator('#csv').click()
                self.assertEqual(export_calls, [])
                self.assertEqual(page.locator('#export-retry').count(), 0)
            finally:
                browser.close()
