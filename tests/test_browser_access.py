"""使用临时配置与离线响应验证真实 Chrome 启动参数和登录误判。"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from browser import Browser
from guard import AccessRestricted, check_page, signed_in


@unittest.skipUnless(os.environ.get('BOSS_BROWSER_TESTS') == '1', '设置 BOSS_BROWSER_TESTS=1 运行离线浏览器测试')
class BrowserAccessTests(unittest.TestCase):
    def test_sandbox_and_rendered_login_checks(self):
        from patchright.sync_api import BrowserType
        launch = BrowserType.launch_persistent_context

        def headless_launch(browser_type, *args, **kwargs):
            # 保留生产启动参数，仅隐藏回归测试窗口。
            kwargs['headless'] = True
            return launch(browser_type, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(BrowserType, 'launch_persistent_context', headless_launch):
            client = Browser(profile=Path(directory) / 'profile')
            try:
                page = client.page()
                page.goto('chrome://version')
                command = page.locator('#command_line').inner_text()
                self.assertNotIn('--no-sandbox', command)
                self.assertIn(str(client.profile), command)
                html = ['<body></body>']
                page.context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=html[0]))
                page.context.add_cookies([dict(name='wt2', value='expired-test-session', url='https://www.zhipin.com/')])
                page.goto('https://www.zhipin.com/')
                self.assertFalse(signed_in(page))
                html[0] = '<body><div class="user-nav"><div class="user-avatar">测试账号</div></div></body>'
                page.goto('https://www.zhipin.com/')
                self.assertTrue(signed_in(page))
                # 即使沿用头像结构或旧 Cookie，风控地址也不能被当成登录成功。
                page.goto('https://www.zhipin.com/web/passport/zp/403.html?code=32')
                self.assertFalse(signed_in(page))
                with self.assertRaises(AccessRestricted):
                    check_page(page)
                html[0] = '<body></body>'
                page.reload()
                with self.assertRaises(AccessRestricted):
                    check_page(page)
            finally:
                client.close()
