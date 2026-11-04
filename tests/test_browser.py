"""浏览器生命周期回归：只模拟驱动，不访问招聘网站或实际登录配置。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from patchright._impl._errors import TargetClosedError
from patchright.sync_api import Error

from browser import Browser
from guard import QueryError
from service import Service, Task


def page(closed=False, error=None):
    tab = Mock()
    tab.is_closed.return_value = closed
    tab.title.return_value = "离线测试"
    tab.title.side_effect = error
    return tab


def session(tab, connected=True):
    context, driver = Mock(), Mock()
    context.browser.is_connected.return_value = connected
    context.pages = [tab]
    driver.chromium.launch_persistent_context.return_value = context
    return context, driver


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.client = Browser(dev=True)
        self.addCleanup(self.client.close)
        patcher = patch("patchright.sync_api.sync_playwright")
        self.playwright = patcher.start()
        self.addCleanup(patcher.stop)

    def launch(self, *drivers):
        self.playwright.return_value.start.side_effect = drivers

    def test_healthy_session_is_reused(self):
        tab = page()
        context, driver = session(tab)
        self.launch(driver)
        self.assertIs(self.client.page(), tab)
        self.assertIs(self.client.page(), tab)
        self.playwright.return_value.start.assert_called_once()
        self.assertEqual(tab.title.call_count, 2)
        context.close.assert_not_called()

    def test_stale_connected_flags_recover_in_first_page_call(self):
        # 窗口退出但事件尚未派发：两个状态属性仍报告可用。
        old_tab = page(error=TargetClosedError())
        old_context, old_driver = session(old_tab)
        self.client.context, self.client.driver, self.client.tab = old_context, old_driver, old_tab
        new_tab = page()
        context, driver = session(new_tab)
        self.launch(driver)
        self.assertIs(self.client.page(), new_tab)
        old_context.close.assert_called_once()
        old_driver.stop.assert_called_once()
        self.assertEqual(driver.chromium.launch_persistent_context.call_args.args, (str(self.client.profile),))
        new_tab.title.assert_called_once()

    def test_closed_persistent_context_without_browser_is_recreated(self):
        old_tab, new_tab = page(), page()
        old_context, old_driver = session(old_tab)
        old_context.browser = None
        context, driver = session(new_tab)
        self.launch(old_driver, driver)
        self.client.page()
        event, callback = old_context.on.call_args.args
        self.assertEqual(event, "close")
        callback(old_context)
        self.assertIs(self.client.page(), new_tab)
        old_context.new_page.assert_not_called()
        # 旧会话迟到的关闭通知不能让新会话失效。
        callback(old_context)
        self.assertIs(self.client.page(), new_tab)
        self.assertEqual(self.playwright.return_value.start.call_count, 2)

    def test_target_closed_while_creating_tab_recovers(self):
        old_context, old_driver = session(page())
        old_context.browser = None
        old_context.pages = []
        old_context.new_page.side_effect = TargetClosedError()
        self.client.context, self.client.driver = old_context, old_driver
        new_tab = page()
        context, driver = session(new_tab)
        self.launch(driver)
        self.assertIs(self.client.page(), new_tab)
        old_driver.stop.assert_called_once()

    def test_closed_tabs_are_skipped(self):
        closed_tab, live_tab = page(closed=True), page()
        context, driver = session(closed_tab)
        context.pages.append(live_tab)
        self.launch(driver)
        self.assertIs(self.client.page(), live_tab)
        context.new_page.assert_not_called()
        closed_tab.title.assert_not_called()

    def test_recovery_is_bounded_and_clears_failed_session(self):
        first, first_driver = session(page(error=TargetClosedError()))
        second, second_driver = session(page(error=TargetClosedError()))
        self.launch(first_driver, second_driver)
        with self.assertRaisesRegex(QueryError, "自动重新打开仍未成功"):
            self.client.page()
        self.assertEqual(self.playwright.return_value.start.call_count, 2)
        first_driver.stop.assert_called_once()
        second_driver.stop.assert_called_once()
        self.assertIsNone(self.client.context)
        self.assertIsNone(self.client.driver)
        self.assertIsNone(self.client.tab)

    def test_unrelated_launch_failure_is_not_retried(self):
        context, driver = session(page())
        driver.chromium.launch_persistent_context.side_effect = Error("Executable not found")
        self.launch(driver)
        with self.assertRaisesRegex(Error, "Executable not found"):
            self.client.page()
        self.playwright.return_value.start.assert_called_once()
        driver.stop.assert_called_once()

    def test_closing_during_query_stops_without_replaying_query(self):
        with tempfile.TemporaryDirectory() as tmp, patch("service.DATA", Path(tmp)):
            service = Service(dev=True)
            try:
                service.boss.run = Mock(side_effect=TargetClosedError())
                service.browser.close = Mock()
                task = Task({})
                service.run({}, task)
                state = task.snapshot()
                self.assertEqual(state["status"], "error")
                self.assertIn("Chrome 窗口已关闭", state["message"])
                self.assertNotIn("TargetClosedError", state["message"])
                service.boss.run.assert_called_once()
                service.browser.close.assert_called_once()
            finally:
                service.close()
