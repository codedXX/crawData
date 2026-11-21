"""桌面保存只使用当前结果，关闭浏览器也不影响导出。"""
import csv
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from desktop import DesktopAPI, desktop_html
from service import Service, Task
from sites.boss import map_job
from sites.boss_search import salary_summary


@unittest.skipUnless(os.environ.get('BOSS_BROWSER_TESTS') == '1', '设置 BOSS_BROWSER_TESTS=1 运行离线 CSV 测试')
class CsvBrowserTests(unittest.TestCase):
    def test_existing_metadata_and_missing_fields_export_without_collection(self):
        from patchright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory() as directory, sync_playwright() as driver:
            root = Path(directory)
            service = Service(directory=root / 'data')
            api = DesktopAPI(service)
            api._window = Mock()
            target = root / '岗位.csv'
            api._window.create_file_dialog.return_value = (str(target),)
            jobs = [map_job(dict(encryptJobId=str(i), jobName='前端开发', salaryDesc='10-20K',
                                 brandName='公司', cityName='深圳', skills=[]), {'name': '公司'}) for i in range(2)]
            jobs[0].update(registered_capital='1000万人民币', established_date='2021-08-30')
            task = Task({})
            task.finish('partial', '查询已停止', result=dict(jobs=jobs, summary=salary_summary(jobs),
                        query={}, read_at='2026-11-13', mode='search', batches=1, company_count=1, companies=[]))
            service.tasks[task.id] = task
            browser = driver.chromium.launch(channel='chrome', headless=True)
            try:
                page = browser.new_page()
                page.expose_function('desktopRequest', api.request)
                page.expose_function('desktopSave', api.save_csv)
                page.add_init_script('window.pywebview={api:{request:(p,d)=>window.desktopRequest(p,d),save_csv:id=>window.desktopSave(id)}};')
                page.route('**/*', lambda route: route.fulfill(content_type='text/html', body=desktop_html()))
                page.goto('http://desktop-csv.test/')
                page.wait_for_selector('#csv')
                with patch('company_export.CompanyExporter.enrich') as enrich, \
                        patch('sites.boss_details.CompanyDetailsCollector.collect') as collect, \
                        patch.object(service, 'prepare_export') as prepare, \
                        patch.object(service.boss, 'run') as query, \
                        patch.object(service.browser, 'page', side_effect=AssertionError('Must not open Chrome')) as launch, \
                        patch.dict('sys.modules', webview=SimpleNamespace(FileDialog=SimpleNamespace(SAVE='save'))):
                    page.locator('#csv').click()
                    page.wait_for_function("document.getElementById('export-status').textContent.startsWith('CSV 已保存')")
                    page.locator('#csv').click()
                    page.wait_for_function("document.getElementById('csv').getAttribute('aria-disabled') === 'false'")
                    self.assertEqual(api._window.create_file_dialog.call_count, 2)
                    enrich.assert_not_called()
                    collect.assert_not_called()
                    query.assert_not_called()
                    prepare.assert_not_called()
                    launch.assert_not_called()
                with target.open(encoding='utf-8-sig', newline='') as stream:
                    rows = list(csv.DictReader(stream))
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0]['注册资金'], '1000万人民币')
                self.assertEqual(rows[0]['成立日期'], '2021-08-30')
                self.assertEqual(rows[1]['注册资金'], '')
                self.assertEqual(rows[1]['成立日期'], '')
                self.assertEqual(page.locator('#export-retry').count(), 0)
            finally:
                browser.close()
                service.close()
