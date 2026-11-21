"""Offline UI regressions for the warm workbench layout and view controls."""
import copy
import os
import unittest

from desktop import desktop_html
from sites.boss_search import salary_summary


def workbench_result():
    rows = [
        ('高级前端工程师', '云杉科技', '深圳', 18000, 28000, 14),
        ('前端开发工程师', '海屿软件', '广州', 20000, 30000, 13),
        ('Web 前端工程师', '星图互动', '深圳', 15000, 25000, 13),
        ('前端开发 · 企业应用', '云杉科技', '广州', 16000, 24000, 14),
        ('资深前端工程师', '海屿软件', '深圳', 25000, 40000, 15),
        ('前端开发工程师', '青禾数字', '珠海', 12000, 18000, 12),
    ]
    jobs = [dict(title=title, company=company, city=city, area='南山区' if city == '深圳' else '',
                 salary_raw=f'{low // 1000}–{high // 1000}K · {months}薪', salary_min=low,
                 salary_max=high, months=months, salary_type='monthly',
                 experience='3–5 年', degree='本科', skills=['Vue 3', 'TypeScript'],
                 url=f'https://example.invalid/job/{i}', registered_capital='', established_date='')
            for i, (title, company, city, low, high, months) in enumerate(rows)]
    query = dict(company='', keyword='前端', city='广东省', selected_cities=['深圳', '广州', '珠海'],
                 batches=10, per_city_batches=3, pages=1, company_details=True)
    cities = [dict(city=city, status='done', message='已读完') for city in query['selected_cities']]
    companies = []
    for company in dict.fromkeys(job['company'] for job in jobs):
        group = [job for job in jobs if job['company'] == company]
        summary = salary_summary(group)
        companies.append(dict(company=company, count=len(group), monthly_count=summary['monthly_count'], median=summary['median']))
    return dict(id='workbench-fixture', status='done', message='已读完本次搜索提供的结果', prompt=None,
                query=query, city_progress=cities, result=dict(
                    jobs=jobs, summary=salary_summary(jobs), query=query, city_progress=cities,
                    read_at='2026-11-21T14:32:00+08:00', mode='search', complete=True,
                    batches=3, company_count=len(companies), companies=companies,
                    province_search=True, cities_visited=3, cities_done=3, cities_total=3,
                    stop_message='已读完本次搜索提供的结果，不代表平台全量。'))


@unittest.skipUnless(os.environ.get('BOSS_BROWSER_TESTS') == '1', '设置 BOSS_BROWSER_TESTS=1 运行离线 UI 测试')
class WorkbenchUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from patchright.sync_api import sync_playwright
        cls.driver = sync_playwright().start()
        cls.browser = cls.driver.chromium.launch(channel='chrome', headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.driver.stop()

    def setUp(self):
        self.state = workbench_result()
        self.submitted = []
        self.saved = []
        self.errors = []
        self.page = self.browser.new_page(viewport=dict(width=1440, height=900))
        self.page.set_default_timeout(10000)
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.expose_function('desktopRequest', self.request)
        self.page.expose_function('desktopSave', self.save)
        self.page.add_init_script('window.pywebview={api:{request:(p,d)=>window.desktopRequest(p,d),save_csv:id=>window.desktopSave(id)}};')
        self.page.route('**/*', lambda route: route.fulfill(content_type='text/html', body=desktop_html()))

    def tearDown(self):
        self.page.close()
        self.assertEqual(self.errors, [])

    def request(self, path, data=None):
        if path == '/access':
            return {'data': {'blocked': False}}
        if path == '/jobs/query':
            self.submitted.append(data)
        return {'data': self.state}

    def save(self, task_id):
        self.saved.append(task_id)
        return {'path': 'offline.csv'}

    def open(self):
        self.page.goto('http://workbench.test/')
        self.page.wait_for_function('current !== undefined')

    def test_mode_validation_and_reset_preserve_running_city_plan(self):
        self.state = dict(status='idle', message='等待查询')
        self.open()
        self.page.locator('#mode-company').click()
        self.assertTrue(self.page.locator('input[name=company]').evaluate('(e)=>e.required'))
        self.assertTrue(self.page.locator('#company-pages').is_visible())
        self.page.locator('#submit').click()
        self.assertEqual(self.submitted, [])
        self.page.locator('input[name=company]').fill('云杉科技')
        self.page.locator('#mode-search').click()
        self.assertEqual(self.page.locator('input[name=company]').input_value(), '')
        self.assertTrue(self.page.locator('#city-batches').is_visible())
        self.state = copy.deepcopy(workbench_result())
        self.state.update(status='running', id='running', message='准备查询')
        self.state.pop('result')
        self.page.wait_for_function("document.getElementById('status').textContent.includes('准备查询')")
        self.page.locator('input[name=keyword]').fill('测试关键词')
        self.page.locator('#reset-search').click()
        self.assertEqual(self.page.locator('input[name=keyword]').input_value(), '')
        self.assertEqual(self.page.locator('#location-value').inner_text(), '广东省 / 全省')
        self.assertEqual(self.page.locator('#city-list li span').all_text_contents(), ['深圳', '广州', '珠海'])
        self.assertTrue(self.page.locator('#submit').is_disabled())

    def test_result_tabs_fields_and_query_restore(self):
        self.open()
        self.page.wait_for_selector('#table')
        self.assertEqual(self.page.locator('select[name=batches]').input_value(), '10')
        self.assertEqual(self.page.locator('select[name=per_city_batches]').input_value(), '3')
        self.assertIn('深圳、广州、珠海', self.page.locator('#location-value').inner_text())
        values = self.page.locator('#metrics dd').all_text_contents()
        self.assertEqual(values, ['6个', '4家', '21,500元', '12–40K'])
        self.assertEqual(self.page.locator('#table thead th:visible').count(), 5)
        self.page.locator('#toggle-fields').click()
        self.assertEqual(self.page.locator('#table thead th:visible').count(), 8)
        self.assertIn('18,000', self.page.locator('#table tbody tr').first.inner_text())
        self.page.locator('#jobs-tab').focus()
        self.page.keyboard.press('ArrowRight')
        self.assertTrue(self.page.locator('#companies').is_visible())
        self.assertEqual(self.page.locator('#company-rows tr').count(), 4)
        self.page.keyboard.press('Home')
        self.assertTrue(self.page.locator('#table').is_visible())
        self.page.locator('input[name=keyword]').fill('运营')
        self.state['message'] = '已更新状态'
        self.page.wait_for_function("document.getElementById('status').textContent === '已更新状态'")
        self.assertEqual(self.page.locator('input[name=keyword]').input_value(), '运营')
        self.page.reload()
        self.page.wait_for_selector('#table')
        self.assertEqual(self.page.locator('input[name=keyword]').input_value(), '前端')

    def test_retained_result_export_uses_displayed_task_and_responsive_layout(self):
        self.open()
        self.page.wait_for_selector('#csv')
        self.state = dict(id='new-running-task', status='running', message='查询中', query={'keyword':'运营'})
        self.page.wait_for_function("document.getElementById('status').textContent === '查询中'")
        self.page.locator('#csv').click()
        self.page.wait_for_function("document.getElementById('export-status').textContent.includes('已保存')")
        self.assertEqual(self.saved, ['workbench-fixture'])
        self.assertEqual(self.page.locator('#csv').get_attribute('aria-disabled'), 'false')
        for width in (1440, 1180, 820, 390):
            self.page.set_viewport_size(dict(width=width, height=900))
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width)
        self.page.locator('#toggle-fields').click()
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))

    def test_details_progress_survives_reload_and_stops_at_actual_count(self):
        self.state.update(status='running', phase='company_details', message='读取工商信息 · 等待 20 秒',
                          company_details_progress=dict(checked=1, total=4))
        self.state.pop('result')
        self.open()
        self.assertEqual(self.page.locator('#city-list small').all_text_contents(), ['岗位已读完'] * 3)
        self.assertEqual(self.page.locator('#company-details-count').inner_text(), '工商信息已检查 1/4 家')
        self.assertIn('尚未结束', self.page.locator('#company-details-note').inner_text())
        self.assertEqual(self.page.locator('#submit').inner_text(), '读取工商信息中…')
        self.assertFalse(self.page.locator('#csv').is_visible())
        self.state['company_details_progress'] = dict(checked=2, total=4)
        self.page.wait_for_function("document.getElementById('company-details-count').textContent === '工商信息已检查 2/4 家'")
        self.page.reload()
        self.page.wait_for_selector('#company-details-progress')
        self.assertEqual(self.page.locator('#company-details-count').inner_text(), '工商信息已检查 2/4 家')
        # 单城市/指定公司没有城市面板，工商信息进度仍独立显示。
        self.state.pop('city_progress')
        self.page.wait_for_function("document.getElementById('city-progress').hidden")
        self.assertTrue(self.page.locator('#company-details-progress').is_visible())
        self.state.update(status='partial', message='已停止补充工商信息', result=workbench_result()['result'])
        self.page.wait_for_function("document.getElementById('company-details-note').textContent.includes('检查已结束')")
        self.assertEqual(self.page.locator('#company-details-count').inner_text(), '工商信息已检查 2/4 家')
        self.assertTrue(self.page.locator('#csv').is_visible())
        for width in (1440, 390):
            self.page.set_viewport_size(dict(width=width, height=900))
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        self.state = dict(id='next-query', status='running', message='准备查询', query={'company_details': False})
        self.page.wait_for_function("document.getElementById('company-details-progress').hidden")
