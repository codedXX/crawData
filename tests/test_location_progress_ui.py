"""离线浏览器验证级联选择、实时城市状态和窄屏布局。"""
import os
import unittest

from desktop import desktop_html
from service import Task, validate
from sites.boss_locations import search_targets


@unittest.skipUnless(os.environ.get('BOSS_BROWSER_TESTS') == '1', '设置 BOSS_BROWSER_TESTS=1 运行 UI 测试')
class LocationProgressUiTests(unittest.TestCase):
    def test_select_two_cities_and_restore_after_reload(self):
        from patchright.sync_api import sync_playwright

        state = dict(status='idle', message='等待查询')
        submitted = []

        def request(path, data=None):
            nonlocal state
            if path == '/jobs/query':
                submitted.append(data)
                state = Task(validate(data)).snapshot()
            return {'data': state}

        with sync_playwright() as driver:
            browser = driver.chromium.launch(channel='chrome', headless=True)
            try:
                page = browser.new_page()
                page.expose_function('desktopRequest', request)
                page.add_init_script('window.pywebview = {api: {request: (path, data) => window.desktopRequest(path, data)}};')
                page.route('**/*', lambda route: route.fulfill(content_type='text/html', body=desktop_html()))
                page.goto('http://multi-city.test/')
                page.locator('input[name=keyword]').fill('前端')
                page.locator('#location-toggle').click()
                page.locator('#province-options').get_by_role('button', name='广东省', exact=True).click()
                # 按住城市文字再松开，覆盖 mousedown 到 click 之间的焦点变化。
                for city in ('韶关', '惠州'):
                    page.locator('#city-options span').get_by_text(city, exact=True).click(delay=100)
                    self.assertTrue(page.locator('#location-popup').is_visible())
                    self.assertTrue(page.get_by_role('checkbox', name=city, exact=True).is_checked())
                self.assertFalse(page.get_by_role('checkbox', name='全省', exact=True).is_checked())
                self.assertEqual(page.locator('#city-list li span').all_text_contents(), ['韶关', '惠州'])
                for city in ('韶关', '惠州'):
                    page.locator('#city-options span').get_by_text(city, exact=True).click(delay=100)
                    self.assertTrue(page.locator('#location-popup').is_visible())
                    self.assertFalse(page.get_by_role('checkbox', name=city, exact=True).is_checked())
                self.assertTrue(page.locator('#location-done').is_disabled())
                page.keyboard.press('Escape')
                page.locator('#submit').click()
                self.assertEqual(submitted, [])
                self.assertIn('至少选择一个城市', page.locator('#status').inner_text())
                page.locator('#location-toggle').click()
                page.get_by_role('checkbox', name='韶关', exact=True).check()
                page.get_by_role('checkbox', name='惠州', exact=True).check()
                page.locator('#location-done').click()
                page.locator('#submit').click()
                page.wait_for_function("current?.status === 'running'")
                self.assertEqual(submitted[0]['selected_cities'], ['韶关', '惠州'])
                self.assertEqual(submitted[0]['city'], '广东省')
                page.reload()
                page.wait_for_function("document.getElementById('location-value').textContent.includes('韶关、惠州')")
                self.assertEqual(page.locator('#city-list li span').all_text_contents(), ['韶关', '惠州'])
                page.locator('#location-toggle').click()
                self.assertTrue(page.get_by_role('checkbox', name='韶关', exact=True).is_checked())
                self.assertTrue(page.get_by_role('checkbox', name='惠州', exact=True).is_checked())
                page.locator('header h1').click()
                self.assertFalse(page.locator('#location-popup').is_visible())
                page.locator('#location-toggle').click()
                page.locator('#location-done').focus()
                page.keyboard.press('Tab')
                page.locator('#location-popup').wait_for(state='hidden')
            finally:
                browser.close()

    def test_cascader_submission_live_progress_and_reload(self):
        from patchright.sync_api import sync_playwright

        state = dict(status='idle', message='等待查询')
        submissions = []

        def request(path, data=None):
            nonlocal state
            if path == '/jobs/query':
                submissions.append(data)
                state = Task(validate(data)).snapshot()
            return {'data': state}

        with sync_playwright() as driver:
            browser = driver.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page(viewport=dict(width=1280, height=900))
            page.set_default_timeout(10000)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.expose_function('desktopRequest', request)
            page.add_init_script('window.pywebview = {api: {request: (path, data) => window.desktopRequest(path, data)}};')
            page.route('**/*', lambda route: route.fulfill(content_type='text/html', body=desktop_html()))
            page.goto('http://location-progress.test/')
            page.wait_for_function("current?.status === 'idle'")
            page.locator('input[name=keyword]').fill('前端')

            def province(name):
                page.locator('#location-toggle').click()
                page.locator('#province-options').get_by_role('button', name=name, exact=True).click()

            province('广东省')
            self.assertTrue(page.locator('#location-popup').is_visible())
            self.assertEqual(page.locator('#city-list li').count(), len(search_targets('广东省')))
            page.locator('#city-options').get_by_role('checkbox', name='深圳', exact=True).check()
            page.locator('#location-done').click()
            self.assertIn('深圳', page.locator('#location-value').inner_text())
            self.assertEqual(page.locator('#city-list li').count(), 1)
            province('吉林省')
            page.locator('#city-options').get_by_role('checkbox', name='吉林', exact=True).check()
            page.locator('#location-done').click()
            self.assertIn('吉林省 / 吉林', page.locator('#location-value').inner_text())
            province('北京市')
            self.assertEqual(page.locator('input[name=city]').input_value(), '北京')
            self.assertFalse(page.locator('#location-popup').is_visible())
            province('广东省')
            page.locator('#location-done').click()
            page.locator('#submit').click()
            page.wait_for_function("current?.status === 'running'")
            self.assertEqual(submissions[0]['city'], '广东省')
            progress = state['city_progress']
            progress[0]['status'] = 'done'
            progress[1]['status'] = 'partial'
            progress[1]['message'] = '达到每市上限'
            progress[2]['status'] = 'running'
            page.wait_for_function("document.querySelector('.city-card[data-status=running]') !== null")
            self.assertIn(progress[2]['city'], page.locator('#city-progress-current').inner_text())
            self.assertEqual(page.locator('.city-card[data-status=running]').count(), 1)
            self.assertIn('已查 2', page.locator('#city-progress-summary').inner_text())
            self.assertIn('达到每市上限', page.locator('#city-issues').inner_text())

            # 修改下一次的输入不能改变正在运行的查询进度。
            province('浙江省')
            page.keyboard.press('Escape')
            self.assertIn('广东省', page.locator('#city-progress-title').inner_text())
            page.reload()
            page.wait_for_function("document.querySelector('.city-card[data-status=running]') !== null")
            self.assertEqual(page.locator('#city-list li').count(), len(progress))
            state['status'] = 'error'
            state['message'] = '查询已停止'
            progress[2]['status'] = 'partial'
            page.wait_for_function("document.querySelector('.city-card[data-status=running]') === null")
            self.assertEqual(page.locator('.city-card[data-status=running]').count(), 0)
            self.assertIn('已结束', page.locator('#city-progress-current').inner_text())
            self.assertEqual(page.locator('.city-card[data-status=pending]').count(), len(progress) - 3)
            for width in (390, 820, 1280):
                page.set_viewport_size(dict(width=width, height=900))
                self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'))
            self.assertEqual(errors, [])
            browser.close()
