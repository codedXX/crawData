"""桌面桥、独立数据、退出和 Windows 文件写入的离线回归。"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app_lock import instance_lock
from desktop import DesktopAPI, app_directory, desktop_html, run_app
from guard import QueryError
from service import Busy, Service, Task
from sites.boss import map_job
from store import ROOT, Store, csv_bytes, write_json


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = Service(directory=self.root / "data", profile=self.root / "profile")
        self.addCleanup(self.service.close)
        self.api = DesktopAPI(self.service)
        self.api._window = Mock()

    def completed(self, status="done"):
        task = Task({})
        job = map_job(dict(encryptJobId="offline", jobName="前端开发", salaryDesc="10-20K",
                           brandName="测试公司", cityName="广州", skills=[]), {"name": "测试公司"})
        task.finish(status, "离线结果", result={"jobs": [job]})
        self.service.tasks[task.id] = task
        return task

    def test_bridge_validation_and_background_query(self):
        finished = threading.Event()
        self.service.boss.run = Mock(side_effect=lambda params, task: (finished.set() or {"jobs": []}))
        self.assertEqual(self.api.request("/jobs")["data"]["status"], "idle")
        for path, data in [("/jobs/query", []), ("/jobs/query", {"pages": 99}),
                           ("/jobs/missing/cancel", {}), ("/invalid", None)]:
            self.assertIn("error", self.api.request(path, data))
        submitted = self.api.request("/jobs/query", {"keyword": "前端", "city": "深圳"})["data"]
        self.assertTrue(finished.wait(2))
        self.service.worker.submit(lambda: None).result(timeout=2)
        state = self.api.request("/jobs/" + submitted["id"])["data"]
        self.assertEqual(state["status"], "done")
        self.service.boss.run.assert_called_once()

    def test_reply_and_cancel_reuse_task_rules(self):
        task = Task({})
        task.state.update(status="choose", prompt="current", choices=[{"id": "brand"}])
        self.service.tasks[task.id] = task
        path = f"/jobs/{task.id}"
        self.assertIn("error", self.api.request(path + "/reply", {"prompt": "old", "answer": "brand"}))
        self.assertNotIn("error", self.api.request(path + "/reply", {"prompt": "current", "answer": "brand"}))
        self.assertEqual(task.answer, "brand")
        self.api.request(path + "/cancel", {})
        self.assertTrue(task.cancelled.is_set())

    def test_save_completed_and_partial_results_with_native_dialog(self):
        # 原生保存框由操作系统负责交互；测试只替换文件选择。
        fake_webview = SimpleNamespace(FileDialog=SimpleNamespace(SAVE="save"))
        with patch.dict(sys.modules, webview=fake_webview):
            for status in ("done", "partial"):
                task = self.completed(status)
                target = self.root / f"结果-{status}.csv"
                self.api._window.create_file_dialog.return_value = (str(target),)
                self.assertEqual(self.api.save_csv(task.id), {"path": str(target)})
                self.assertEqual(target.read_bytes(), csv_bytes(task.snapshot()["result"]["jobs"]))
                self.assertTrue(target.read_bytes().startswith(b"\xef\xbb\xbf"))
            self.api._window.create_file_dialog.return_value = None
            self.assertEqual(self.api.save_csv(task.id), {"cancelled": True})
        self.assertIn("error", self.api.save_csv("not-found"))
        self.assertIn("error", self.api.save_csv([]))

    def test_save_failure_can_be_retried(self):
        task = self.completed()
        fake_webview = SimpleNamespace(FileDialog=SimpleNamespace(SAVE="save"))
        self.api._window.create_file_dialog.return_value = (str(self.root / "不存在" / "结果.csv"),)
        with patch.dict(sys.modules, webview=fake_webview):
            self.assertIn("error", self.api.save_csv(task.id))
            self.api._window.create_file_dialog.return_value = None
            self.assertEqual(self.api.save_csv(task.id), {"cancelled": True})

    def test_writable_paths_and_samples_are_outside_resources(self):
        self.assertEqual(self.service.browser.profile, self.root / "profile")
        self.assertEqual(self.service.boss.store.samples, self.root / "data" / "examples")
        self.service.boss.store.remember("公司", {"id": "id"})
        self.assertEqual(Store(self.root / "data").company("公司"), {"id": "id"})
        with patch("desktop.sys.platform", "win32"), patch.dict(os.environ, LOCALAPPDATA=str(self.root)):
            self.assertEqual(app_directory(), self.root / "BossJobs")
            self.assertEqual(app_directory(True), self.root / "BossJobs" / "dev")

    def test_close_cancels_login_wait_and_closes_browser_on_worker(self):
        waiting = threading.Event()
        def wait_for_login(params, task):
            waiting.set()
            task.ask("login", "离线扫码等待", timeout=30)
        self.service.boss.run = wait_for_login
        threads = []
        self.service.browser.close = lambda: threads.append(threading.current_thread().name)
        task = self.service.submit({"keyword": "前端"})
        self.assertTrue(waiting.wait(2))
        self.service.close()
        self.assertTrue(task.cancelled.is_set())
        self.assertEqual(task.snapshot()["status"], "error")
        self.assertEqual(len(threads), 1)
        self.assertTrue(threads[0].startswith("boss"))
        self.service.close()
        self.assertEqual(len(threads), 1)
        with self.assertRaises(Busy):
            self.service.submit({"keyword": "前端"})

    def test_window_start_failure_cleans_service(self):
        fake_webview = SimpleNamespace(settings={}, create_window=Mock(side_effect=RuntimeError("no renderer")))
        with patch.dict(sys.modules, webview=fake_webview), patch("desktop.Service", return_value=self.service):
            with self.assertRaisesRegex(RuntimeError, "no renderer"):
                run_app(self.root)
        self.assertTrue(self.service.closed)
        with instance_lock(self.root / "desktop.lock"):
            pass

    def test_desktop_resources_load_independent_of_working_directory(self):
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            self.assertIn("window.BOSS_DESKTOP = true", desktop_html())
            from sites.boss_search import city_code
            self.assertEqual(city_code("深圳"), "101280600")
        finally:
            os.chdir(previous)


class StorageAndLockTests(unittest.TestCase):
    def test_cli_service_reuses_port_and_cleans_up_cross_platform_lock(self):
        from crawl import backend, request
        with tempfile.TemporaryDirectory() as directory, patch("crawl.DATA", Path(directory)), patch("service.DATA", Path(directory)):
            with backend(0, attach=False, dev=True) as url:
                self.assertEqual(request(url + "/jobs")["status"], "idle")
                with backend(0, attach=True, dev=True) as attached:
                    self.assertEqual(attached, url)
                with self.assertRaisesRegex(QueryError, "已启动"):
                    with backend(0, attach=False, dev=True):
                        self.fail("第二个服务不应启动")
            self.assertEqual((Path(directory) / "dev" / "service.lock").read_text(), "")
            with backend(0, attach=False, dev=True) as url:
                self.assertEqual(request(url + "/jobs")["status"], "idle")

    def test_json_replace_after_file_close_and_failed_write_keeps_previous_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "companies.json"
            write_json(path, {"公司": "旧数据"})
            write_json(path, {"公司": "新数据"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"公司": "新数据"})
            with self.assertRaises(ValueError):
                write_json(path, {"bad": float("nan")})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"公司": "新数据"})
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_process_lock_excludes_second_instance_and_releases(self):
        script = """import sys
from pathlib import Path
from app_lock import AlreadyRunning, instance_lock
try:
    with instance_lock(Path(sys.argv[1])):
        pass
except AlreadyRunning:
    sys.exit(23)
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "desktop.lock"
            def child():
                return subprocess.run([sys.executable, "-c", script, str(path)], cwd=ROOT,
                                      capture_output=True, timeout=10)
            with instance_lock(path):
                result = child()
                self.assertEqual(result.returncode, 23, result.stderr)
            result = child()
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
