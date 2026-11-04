"""在查询工作线程里按需启动、复用并关闭 Chrome。"""
from pathlib import Path

from guard import QueryError

ROOT = Path(__file__).resolve().parent


def is_target_closed_error(error):
    # 驱动在本地断连和远端关闭时分别使用异常类名和公开的 name 属性。
    return type(error).__name__ == "TargetClosedError" or getattr(error, "name", None) == "TargetClosedError"


class Browser:
    def __init__(self, platform="boss", dev=False, profile=None):
        self.profile = Path(profile) if profile is not None else ROOT / "profiles" / ("dev" if dev else "") / platform
        self.driver = None
        self.context = None
        self.tab = None
        self._context_closed = False

    def page(self):
        from patchright.sync_api import Error

        for attempt in range(2):
            try:
                if self.context and (self._context_closed or (
                        self.context.browser and not self.context.browser.is_connected())):
                    self.close()
                if self.context is None:
                    self._launch()
                if self.tab is None or self.tab.is_closed():
                    self.tab = next((tab for tab in self.context.pages if not tab.is_closed()), None)
                    if self.tab is None:
                        self.tab = self.context.new_page()
                # 状态属性可能尚未收到关闭事件；发一次驱动调用确认连接可用。
                # 只读取当前标题，不刷新页面，也不向招聘网站发送请求。
                self.tab.title()
                return self.tab
            except Error as exc:
                if not is_target_closed_error(exc):
                    raise
                self.close()
                if attempt:
                    raise QueryError("Chrome 会话已关闭，自动重新打开仍未成功。请检查 Chrome 是否能正常启动后再查询。") from exc

    def _launch(self):
        from patchright.sync_api import sync_playwright

        try:
            self.driver = sync_playwright().start()
            self.context = self.driver.chromium.launch_persistent_context(
                str(self.profile), channel="chrome", headless=False, no_viewport=True, handle_sigint=False
            )
            self._context_closed = False
            self.context.on("close", self._on_context_closed)
        except Exception as exc:
            self.close()
            message = str(exc).lower()
            if "chrome" in message and ("not found" in message or "doesn't exist" in message):
                raise QueryError("未找到 Google Chrome。请安装 Chrome 后重新查询。") from exc
            raise

    def _on_context_closed(self, context):
        # 事件回调仅标记失效；创建和清理驱动仍由查询工作线程顺序完成。
        if self.context is context:
            self._context_closed = True

    def close(self):
        context, driver = self.context, self.driver
        self.tab = self.context = self.driver = None
        self._context_closed = False
        if driver is None:
            return
        from patchright.sync_api import Error
        try:
            if context:
                context.close()
        except Error:
            # 用户可能已在窗口中退出 Chrome。
            pass
        finally:
            try:
                driver.stop()
            except Error:
                pass
