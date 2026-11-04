"""在 Windows x64 上测试、打包并离线验收，成功后生成可分发 ZIP。"""
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    if sys.platform != "win32" or platform.machine().lower() not in ("amd64", "x86_64") or sys.maxsize < 2**32:
        raise SystemExit("请在 Windows x64 上使用 64 位 Python 3.12 构建。")
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONUTF8="1", BOSS_BROWSER_TESTS="1")
    subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                   cwd=root, env=env, check=True, timeout=300)
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "BossJobs.spec"],
                   cwd=root, check=True)
    report = root / "dist" / "windows-smoke.json"
    report.unlink(missing_ok=True)
    # 使用新的目录；不读取或打包构建机器的 BOSS 登录状态。
    with tempfile.TemporaryDirectory(prefix="boss-desktop-check-") as directory:
        subprocess.run([str(root / "dist" / "BossJobs" / "BossJobs.exe"),
                        "--data-dir", directory, "--smoke-test", str(report)],
                       cwd=directory, check=True, timeout=120)
    result = json.loads(report.read_text(encoding="utf-8"))
    if not result.get("ok") or result.get("chrome") != "desktop-offline-check":
        raise SystemExit(f"桌面验收失败，请检查 {report}")
    shutil.copy2(root / "WINDOWS.md", root / "dist" / "BossJobs" / "使用说明.md")
    archive = shutil.make_archive(str(root / "dist" / "BossJobs-windows-x64"), "zip",
                                  root / "dist", "BossJobs")
    print(f"已生成并通过离线验收：{archive}")


if __name__ == "__main__":
    main()
