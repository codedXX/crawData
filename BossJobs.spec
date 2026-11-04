# Windows x64 目录式打包；只收集明确列出的资源，不包含用户数据或登录态。
import sys
from pathlib import Path

import patchright
from PyInstaller.utils.hooks import collect_data_files

if sys.platform != "win32":
    raise SystemExit("BossJobs.spec 必须在 Windows 上构建。")

root = Path(SPECPATH)
datas = [
    (str(root / "index.html"), "."),
    (str(root / "sites" / "boss_cities.json"), "sites"),
    (str(root / "WINDOWS.md"), "."),
    # Patchright 包自带的 hook 名称沿用了 playwright，显式包含 Node 和全部驱动文件。
    (str(Path(patchright.__file__).parent / "driver"), "patchright/driver"),
]
datas += collect_data_files("webview", includes=["js/**", "lib/**"])
a = Analysis(
    [str(root / "desktop.py")], pathex=[str(root)], datas=datas,
    hiddenimports=["webview.platforms.winforms", "webview.platforms.edgechromium",
                   "clr", "pythonnet", "sites.boss_search", "sites.boss_ssr", "esprima"],
    excludes=["PyQt5", "PyQt6", "PySide2", "PySide6", "cefpython3"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="BossJobs", console=False,
    debug=False, strip=False, upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="BossJobs", strip=False, upx=False)
