"""打包产物中的卸载入口：交付文件、编码和真实删除行为。"""
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from store import ROOT

SCRIPTS = ROOT / "scripts"


class UninstallSourceTests(unittest.TestCase):
    def test_script_keeps_utf8_bom_and_wrapper_stays_ascii(self):
        script = SCRIPTS / "uninstall.ps1"
        self.assertTrue(script.is_file())
        # 无 BOM 时 Windows PowerShell 按 ANSI 解码，中文提示会乱码。
        self.assertTrue(script.read_bytes().startswith(b"\xef\xbb\xbf"))
        wrapper = (SCRIPTS / "uninstall.cmd").read_bytes()
        wrapper.decode("ascii")
        # cmd.exe 按 OEM 代码页读取批处理，用同名的 %~n0 推导 .ps1 可避免出现中文。
        self.assertIn(b"%~dp0%~n0.ps1", wrapper)
        self.assertIn(b'cd /d "%TEMP%"', wrapper)

    def test_build_ships_uninstall_files_beside_exe(self):
        build = (SCRIPTS / "build_windows.py").read_text(encoding="utf-8")
        for name in ("卸载.cmd", "卸载.ps1", "使用说明.md"):
            self.assertIn(f'package / "{name}"', build)


@unittest.skipUnless(sys.platform == "win32", "卸载脚本只在 Windows 上运行")
class UninstallScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def prepare(self, exe=True):
        app = self.base / "BossJobs"
        app.mkdir()
        if exe:
            (app / "BossJobs.exe").write_bytes(b"offline")
        (app / "_internal").mkdir()
        (app / "_internal" / "library.dll").write_bytes(b"offline")
        (app / "使用说明.md").write_text("说明", encoding="utf-8")
        shutil.copy2(SCRIPTS / "uninstall.ps1", app / "卸载.ps1")
        return app

    def run_uninstall(self, app, *args):
        return subprocess.run(
            ["powershell", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(app / "卸载.ps1"), *args],
            capture_output=True, timeout=180)

    def wait_gone(self, path, seconds=45):
        for _ in range(seconds * 4):
            if not path.exists():
                return True
            time.sleep(0.25)
        return False

    def message(self, result):
        return (result.stdout + result.stderr).decode("utf-8", "replace")

    def test_removes_program_directory_and_keeps_user_data(self):
        app = self.prepare()
        data = self.base / "BossJobsData"
        data.mkdir()
        (data / "quota.json").write_text("{}", encoding="utf-8")
        result = self.run_uninstall(app, "-Force", "-DataDir", str(data))
        self.assertEqual(result.returncode, 0, self.message(result))
        self.assertTrue(self.wait_gone(app), list(app.rglob("*")))
        # 未指定 -RemoveData 时必须保留登录状态等用户数据。
        self.assertTrue((data / "quota.json").is_file())

    def test_removes_user_data_when_requested(self):
        app = self.prepare()
        data = self.base / "BossJobsData"
        (data / "profiles" / "boss").mkdir(parents=True)
        (data / "profiles" / "boss" / "Cookies").write_bytes(b"offline")
        result = self.run_uninstall(app, "-Force", "-RemoveData", "-DataDir", str(data))
        self.assertEqual(result.returncode, 0, self.message(result))
        self.assertTrue(self.wait_gone(app), list(app.rglob("*")))
        self.assertFalse(data.exists())

    def test_refuses_outside_application_directory(self):
        app = self.prepare(exe=False)
        data = self.base / "BossJobsData"
        data.mkdir()
        result = self.run_uninstall(app, "-Force", "-RemoveData", "-DataDir", str(data))
        self.assertEqual(result.returncode, 1, self.message(result))
        self.assertTrue((app / "使用说明.md").is_file())
        self.assertTrue((app / "卸载.ps1").is_file())
        self.assertTrue(data.is_dir())


if __name__ == "__main__":
    unittest.main()