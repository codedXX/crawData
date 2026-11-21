"""查询阶段读取详情页，按公司去重；导出不得再次访问网站。"""
import tempfile
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from guard import Guard, QueryError
from service import Service, Task, validate
from sites.boss_details import CompanyDetailsCollector
from sites.boss_company import parse_company_info
from store import Store, csv_bytes
from test_csv import posting


def row(ident='one', company_id='brand', company='测试科技', **extra):
    result = posting(company_id=company_id, **extra)
    result.update(company=company, url=f'https://www.zhipin.com/job_detail/{ident}.html')
    return result


def document(company='深圳测试科技有限公司', capital='4114113.182万元', date='1987-09-15'):
    return f"""<!doctype html><meta charset="utf-8"><section><h3>工商信息</h3><div><ul>
    <li><span>公司名称</span><a>{company}</a></li>
    <li><span>法定代表人</span><span>不采集</span></li>
    <li><span>成立日期</span><span>{date}</span></li>
    <li><span>企业类型</span><span>有限责任公司</span></li>
    <li><span>经营状态</span><span>存续</span></li>
    <li><span>注册资金</span><span>{capital}</span></li>
    </ul></div></section>"""


def brand_document(brand_id='brand'):
    # 按用户截图构造的离线结构：招聘品牌与工商主体名称不同，币种须原样保留。
    return (f'<div class="job-banner"><div class="company-info">'
            f'<a href="/gongsi/{brand_id}.html">星财富</a></div></div>'
            + document('星财智科技（深圳）有限公司', '1900万美元', '2020-12-21'))


class DetailPage:
    def __init__(self, documents):
        self.documents, self.visits = documents, []
        self.url, self.html = 'about:blank', ''
        self.handlers = {}
        self.closed = False
    def on(self, name, handler):
        self.handlers[name] = handler
    def remove_listener(self, name, handler):
        self.handlers.pop(name, None)
    def locator(self, selector):
        return Mock(count=lambda: 0, inner_text=lambda **kwargs: '')
    def get_by_text(self, text, **kwargs):
        control = Mock()
        control.first = control
        return control
    def goto(self, url, **kwargs):
        self.url = url
        self.visits.append(url)
        value = self.documents[url]
        if isinstance(value, Exception):
            raise value
        if not isinstance(value, str):
            return value
        self.html = value
        return SimpleNamespace(status=200, ok=True, url=url)
    def content(self):
        return self.html
    def close(self):
        self.closed = True


class CompanyDetailsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.browser = Mock()
        self.task = Task({})
        self.wait = patch('sites.boss_details.Guard.wait').start()
        self.addCleanup(patch.stopall)
    def collect(self, jobs, documents):
        self.page = DetailPage(documents)
        self.browser.page.return_value.context.new_page.return_value = self.page
        return CompanyDetailsCollector(self.browser, self.store).collect(jobs, self.task)

    def test_screenshot_labels_and_company_identity(self):
        info = parse_company_info(document(), include_identity=True)
        self.assertEqual(info, dict(company_name='深圳测试科技有限公司',
                                   registered_capital='4114113.182万元', established_date='1987-09-15'))
        self.assertNotIn('company_name', parse_company_info(document()))

    def test_one_detail_per_company_and_exports_reuse_collected_values(self):
        jobs = [row('one'), row('two'), row('three', 'other', '另一公司')]
        waiting = []
        self.wait.side_effect = lambda seconds: waiting.append(self.task.snapshot()['company_details_progress'])
        result = self.collect(jobs, {jobs[0]['url']: document(),
                                    jobs[2]['url']: document('另一公司', '100万元', '2020-01-01')})
        self.assertEqual(waiting, [dict(checked=0, total=2), dict(checked=1, total=2)])
        self.assertEqual(self.task.snapshot()['company_details_progress'], dict(checked=2, total=2))
        self.assertTrue(result['complete'])
        self.assertEqual(result['visited'], 2)
        self.assertEqual(self.page.visits, [jobs[0]['url'], jobs[2]['url']])
        self.assertEqual([r['registered_capital'] for r in jobs], ['4114113.182万元'] * 2 + ['100万元'])
        self.assertEqual(jobs[1]['established_date'], '1987-09-15')
        with patch('socket.socket', side_effect=AssertionError('导出不得联网')):
            self.assertEqual(csv_bytes(jobs), csv_bytes(jobs))
        self.assertTrue(self.page.closed)
        self.assertFalse(self.page.handlers)

    def test_mismatched_employer_and_missing_fields_stay_blank(self):
        jobs = [row(), row('two', 'other', '另一公司')]
        result = self.collect(jobs, {jobs[0]['url']: document('不相关企业'),
                                    jobs[1]['url']: document('另一公司', '', '2020-01-01')})
        self.assertFalse(result['complete'])
        self.assertEqual(result['missing_count'], 2)
        self.assertNotIn('registered_capital', jobs[0])
        self.assertNotIn('established_date', jobs[0])
        self.assertEqual(jobs[1]['established_date'], '2020-01-01')
        self.assertEqual(len(result['issues']), 2)

    def test_brand_alias_uses_same_company_id_and_exports_without_revisiting(self):
        import csv
        import io
        jobs = [row(company='星财富'), row('two', company='星财富')]
        result = self.collect(jobs, {jobs[0]['url']: brand_document()})
        self.assertTrue(result['complete'], result)
        self.assertEqual(self.page.visits, [jobs[0]['url']])
        with patch.object(self.browser, 'page', side_effect=AssertionError('Export must stay offline')), \
                patch('socket.socket', side_effect=AssertionError('Export must stay offline')):
            first = csv_bytes(jobs)
            self.assertEqual(first, csv_bytes(jobs))
        exported = list(csv.DictReader(io.StringIO(first.decode('utf-8-sig'))))
        self.assertEqual([item['注册资金'] for item in exported], ['1900万美元'] * 2)
        self.assertEqual([item['成立日期'] for item in exported], ['2020-12-21'] * 2)

    def test_other_company_id_does_not_supply_alias_metadata(self):
        jobs = [row(company='星财富')]
        result = self.collect(jobs, {jobs[0]['url']: brand_document('another-brand')})
        self.assertFalse(result['complete'])
        self.assertNotIn('registered_capital', jobs[0])
        self.assertNotIn('established_date', jobs[0])

    def test_429_stops_other_companies_and_keeps_already_collected_metadata(self):
        jobs = [row(), row('two', 'other', '另一公司'), row('three', 'third', '第三公司')]
        before = time.time()
        result = self.collect(jobs, {jobs[0]['url']: document(), jobs[1]['url']:
                        SimpleNamespace(status=429, headers={'retry-after': '7200'})})
        self.assertFalse(result['complete'])
        self.assertEqual(result['visited'], 2)
        self.assertEqual(jobs[0]['registered_capital'], '4114113.182万元')
        self.assertNotIn('registered_capital', jobs[1])
        self.assertEqual(len(jobs), 3)
        self.assertGreaterEqual(self.store.read('quota.json')['blocked_until'], before + 7200)
        self.assertEqual(self.task.snapshot()['company_details_progress'], dict(checked=1, total=3))
        with self.assertRaises(QueryError):
            Guard(self.store, Mock()).check()
        self.assertTrue(self.page.closed)

    def test_cancel_before_second_company_keeps_first_company(self):
        jobs = [row(), row('two', 'other', '另一公司')]
        original_report = self.task.report
        def report(message):
            original_report(message)
            if message.startswith('已检查工商信息 1/'):
                self.task.cancel()
        self.task.report = report
        result = self.collect(jobs, {jobs[0]['url']: document()})
        self.assertFalse(result['complete'])
        self.assertEqual(result['visited'], 1)
        self.assertEqual(jobs[0]['established_date'], '1987-09-15')
        self.assertEqual(len(jobs), 2)
        self.assertEqual(self.task.snapshot()['company_details_progress'], dict(checked=1, total=2))

    def test_existing_metadata_reused_without_browser(self):
        jobs = [row(registered_capital='10万元', established_date='2020-01-01'), row('two')]
        result = self.collect(jobs, {})
        self.browser.page.assert_not_called()
        self.assertTrue(result['complete'])
        self.assertEqual(jobs[1]['registered_capital'], '10万元')
        self.assertEqual(self.task.snapshot()['company_details_progress'], dict(checked=1, total=1))

    def test_blocked_or_external_details_never_launch_browser(self):
        job = row()
        job['url'] = 'https://example.invalid/job_detail/one.html'
        self.collect([job], {})
        self.browser.page.assert_not_called()
        self.assertEqual(self.task.snapshot()['company_details_progress'], dict(checked=1, total=1))
        Guard(self.store, Mock()).record_risk('account_restricted')
        result = self.collect([row()], {})
        self.browser.page.assert_not_called()
        self.assertFalse(result['complete'])
        self.assertEqual(self.task.snapshot()['company_details_progress'], dict(checked=0, total=1))


class CompanyDetailServiceTests(unittest.TestCase):
    def test_option_validation_and_old_clients_remain_offline(self):
        self.assertFalse(validate({})['company_details'])
        self.assertTrue(validate({'company_details': True})['company_details'])
        for value in (1, 'true', None):
            with self.assertRaises(ValueError):
                validate({'company_details': value})

    def test_details_run_during_query_but_export_never_restarts_them(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(directory=directory)
            try:
                params = validate({'company_details': True})
                task = Task(params)
                service.tasks[task.id] = task
                jobs = [row()]
                with patch.object(service.boss, 'run', return_value=dict(jobs=jobs, complete=True)), \
                        patch('sites.boss_details.CompanyDetailsCollector.collect', return_value=dict(complete=True, message='工商信息完成')) as collect:
                    service.run(params, task)
                    self.assertEqual(task.snapshot()['status'], 'done')
                    self.assertEqual(task.snapshot()['phase'], 'company_details')
                    collect.assert_called_once_with(jobs, task)
                    service.prepare_export(task.id)
                    service.prepare_export(task.id)
                    collect.assert_called_once()
            finally:
                service.close()

    def test_detail_failure_retains_jobs_and_interrupted_search_skips_details(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(directory=directory)
            try:
                params = validate({'company_details': True})
                for interrupted in (False, True):
                    task = Task(params)
                    jobs = [row()]
                    result = dict(jobs=jobs, complete=not interrupted)
                    if interrupted:
                        result['stop_reason'] = 'interrupted'
                    with patch.object(service.boss, 'run', return_value=result), \
                            patch('sites.boss_details.CompanyDetailsCollector.collect', side_effect=RuntimeError('private log')) as collect:
                        service.run(params, task)
                    self.assertEqual(task.snapshot()['status'], 'partial')
                    self.assertIs(task.snapshot()['result']['jobs'], jobs)
                    self.assertNotIn('private log', task.snapshot()['message'])
                    self.assertEqual(collect.call_count, int(not interrupted))
            finally:
                service.close()


@unittest.skipUnless(os.environ.get('BOSS_BROWSER_TESTS') == '1', '设置 BOSS_BROWSER_TESTS=1 运行离线详情页验证')
class BrowserCompanyDetailTests(unittest.TestCase):
    def test_real_chrome_reads_business_panel_once_and_exports_without_network(self):
        from patchright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory() as directory, sync_playwright() as driver:
            browser = driver.chromium.launch(channel='chrome', headless=True)
            try:
                context = browser.new_context()
                visits = []
                def respond(route):
                    if route.request.resource_type == 'document':
                        visits.append(route.request.url)
                        route.fulfill(content_type='text/html', body=brand_document())
                    else:
                        route.fulfill(status=404, body='')
                context.route('**/*', respond)
                main = context.new_page()
                client = Mock()
                client.page.return_value = main
                jobs = [row(company='星财富'), row('two', company='星财富')]
                with patch('sites.boss_details.Guard.wait'):
                    result = CompanyDetailsCollector(client, Store(directory)).collect(jobs, Task({}))
                self.assertTrue(result['complete'], result)
                self.assertEqual(visits, [jobs[0]['url']])
                self.assertEqual(context.pages, [main])
                for job in jobs:
                    self.assertEqual(job['registered_capital'], '1900万美元')
                    self.assertEqual(job['established_date'], '2020-12-21')
                with patch('socket.socket', side_effect=AssertionError('导出不联网')):
                    self.assertEqual(csv_bytes(jobs), csv_bytes(jobs))
                self.assertEqual(len(visits), 1)
            finally:
                browser.close()

