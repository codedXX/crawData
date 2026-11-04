"""岗位查询桌面入口：本地页面通过受限 JS API 调用 Python，不监听 HTTP 端口。"""
import argparse
import json
import os
import sys
import threading
from pathlib import Path

from app_lock import AlreadyRunning, instance_lock
from guard import QueryError
from service import Service
from store import ROOT, csv_bytes, write_json


def app_directory(dev=False):
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    directory = base / "BossJobs"
    return directory / "dev" if dev else directory


def desktop_html():
    return (ROOT / "index.html").read_text(encoding="utf-8").replace(
        "<head>", "<head><script>window.BOSS_DESKTOP = true;</script>", 1)


class DesktopAPI:
    # 私有属性不会被 pywebview 暴露为 JS API。
    def __init__(self, service):
        self._service = service
        self._window = None
        self._saving = threading.Lock()

    def request(self, path, data=None):
        try:
            if not isinstance(path, str) or (data is not None and not isinstance(data, dict)):
                raise ValueError("查询参数格式无效。")
            return {"data": self._dispatch(path, data)}
        except (QueryError, ValueError) as exc:
            return {"error": str(exc)}

    def _dispatch(self, path, data):
        if path == "/jobs/query" and data is not None:
            return self._service.submit(data).snapshot()
        if path == "/jobs" and data is None:
            task = self._service.get()
            return task.snapshot() if task else dict(status="idle", message="等待查询")
        parts = path.strip("/").split("/")
        if len(parts) in (2, 3) and parts[0] == "jobs":
            task = self._service.get(parts[1])
            if task:
                if len(parts) == 2 and data is None:
                    return task.snapshot()
                if len(parts) == 3 and data is not None:
                    if parts[2] == "reply":
                        task.reply(data.get("prompt"), data.get("answer"))
                        return task.snapshot()
                    if parts[2] == "cancel":
                        task.cancel()
                        return task.snapshot()
        raise ValueError("查询不存在或操作无效。")

    def save_csv(self, ident):
        if not self._saving.acquire(blocking=False):
            return {"error": "请先完成当前保存操作。"}
        try:
            if not isinstance(ident, str):
                raise ValueError("查询编号无效。")
            task = self._service.get(ident)
            state = task.snapshot() if task else {}
            if state.get("status") not in ("done", "partial"):
                raise ValueError("查询结果尚未就绪。")
            # 仅保存服务中已完成的结果，不接受网页传入的文件内容或磁盘路径。
            content = csv_bytes(state["result"]["jobs"])
            import webview
            selected = self._window.create_file_dialog(
                webview.FileDialog.SAVE, save_filename=f"boss-{ident}.csv",
                file_types=("CSV (*.csv)",))
            if not selected:
                return {"cancelled": True}
            path = Path(selected if isinstance(selected, str) else selected[0])
            path.write_bytes(content)
            return {"path": str(path)}
        except (OSError, ValueError) as exc:
            return {"error": f"保存失败：{exc}"}
        finally:
            self._saving.release()


def smoke_check(window, report):
    """打包后的离线验收：原生窗口、JS 桥、资源和 Chrome 驱动，均不访问 BOSS。"""
    try:
        result, ready = {}, threading.Event()
        def received(value):
            result.update(value)
            ready.set()
        window.evaluate_js("""(async () => {
            const state = await api('/jobs');
            return {title: document.title, status: state.status,
                    desktop: window.BOSS_DESKTOP, form: !!document.getElementById('search')};
        })()""", callback=received)
        if not ready.wait(20) or result.get("status") != "idle" or not result.get("form"):
            raise RuntimeError("桌面窗口或 JS 桥未就绪")
        from sites.boss_search import city_code
        from sites.boss_ssr import company_list
        from patchright.sync_api import sync_playwright
        result["city"] = city_code("深圳")
        # 确认冻结程序包含惰性加载的 esprima 和 Patchright 的 Node 驱动。
        company_list('<script>window.__BOSSCOMPANY__=(function(){return {data:[{brandJobInfo:{jobList:[],hasMore:false}}]};}());</script>')
        with sync_playwright() as driver:
            browser = driver.chromium.launch(channel="chrome", headless=True)
            try:
                page = browser.new_page()
                page.route("**/*", lambda route: route.abort())
                page.set_content("<title>desktop-offline-check</title><p>离线验证</p>")
                result["chrome"] = page.title()
            finally:
                browser.close()
        write_json(report, dict(ok=True, **result))
    except Exception as exc:
        write_json(report, dict(ok=False, error=f"{type(exc).__name__}: {exc}"))
    finally:
        window.destroy()


def run_app(directory, smoke_report=None):
    import webview
    directory.mkdir(parents=True, exist_ok=True)
    with instance_lock(directory / "desktop.lock"):
        service = Service(directory=directory / "data", profile=directory / "profiles" / "boss")
        try:
            api = DesktopAPI(service)
            webview.settings["ALLOW_FILE_URLS"] = False
            webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
            window = webview.create_window(
                "岗位查询 · BOSS 直聘", html=desktop_html(), js_api=api,
                width=1180, height=820, min_size=(760, 560), background_color="#fbfdfd")
            api._window = window
            if smoke_report:
                window.events.loaded += lambda: smoke_check(window, smoke_report)
            # Windows 明确要求 WebView2，不回退到无法运行当前页面的旧 IE 引擎。
            webview.start(gui="edgechromium" if sys.platform == "win32" else None,
                          storage_path=str(directory / "webview"))
        finally:
            service.close()


def show_error(message):
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "岗位查询", 0x10)
    elif sys.stderr is not None:
        print(message, file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev", action="store_true", help="使用独立的开发数据")
    parser.add_argument("--data-dir", type=Path, help="指定应用数据目录")
    parser.add_argument("--smoke-test", type=Path, metavar="REPORT", help="离线验收后退出，写入 JSON 报告")
    args = parser.parse_args(argv)
    directory = args.data_dir or app_directory(args.dev)
    if args.smoke_test and not (args.dev or args.data_dir):
        parser.error("--smoke-test 必须配合 --dev 或 --data-dir，避免使用日常登录配置")
    try:
        run_app(directory, args.smoke_test)
        if args.smoke_test:
            return 0 if json.loads(args.smoke_test.read_text(encoding="utf-8")).get("ok") else 1
    except Exception as exc:
        if args.smoke_test:
            write_json(args.smoke_test, dict(ok=False, error=f"{type(exc).__name__}: {exc}"))
        else:
            message = str(exc) if isinstance(exc, AlreadyRunning) else (
                f"无法启动岗位查询（{type(exc).__name__}）。\n"
                "请确认已完整解压应用，且安装了 Microsoft Edge WebView2 Runtime。\n"
                "开发环境请先安装 requirements-desktop.txt 中的依赖。")
            show_error(message)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
