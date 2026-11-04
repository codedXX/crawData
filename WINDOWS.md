# Windows 桌面版

目标平台为 Windows 10/11 x64。桌面版将查询页面和 Python 采集服务打包在一起，直接在本机启动专用 Chrome。不需要服务器、域名或单独安装 Python。首次查询以及登录失效时仍需在 Chrome 中扫码登录 BOSS。

## 使用

1. 安装 [Google Chrome](https://www.google.com/chrome/) 和 [Microsoft Edge WebView2 Evergreen Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)。WebView2 用来显示应用窗口，Chrome 用来采集岗位，两者用途不同。
2. 将 `BossJobs-windows-x64.zip` **完整解压**到一个文件夹，双击其中的 `BossJobs.exe`。保留旁边的 `_internal` 目录，不要只复制 exe。
3. 填写关键词、城市或公司，点击“查询岗位”。需要登录时，在新开的 Chrome 窗口扫码；需要选择公司时，在应用中确认。
4. 查询完成或停止后，在应用内查看结果，点击“导出 CSV”选择保存位置。
5. 关闭应用会取消正在执行的查询并清理专用 Chrome；正在等待浏览器响应时，后台进程可能需要等待当前操作结束。未导出的查询结果在退出后清空。

本机需要联网访问 BOSS。应用沿用原有的验证码处理、查询节奏和部分结果提示。若关闭了专用 Chrome，可再次查询让应用重新打开。岗位链接在系统默认浏览器打开，不会替换应用界面。

同一数据目录只允许一个应用实例。重复双击会提示切换到已打开的窗口。

## 数据目录

普通模式使用 `%LOCALAPPDATA%\BossJobs\`，通常为 `C:\Users\你的用户名\AppData\Local\BossJobs\`：

- `data\`：公司映射、计数、事件、诊断及脱敏样本。
- `profiles\boss\`：专用 Chrome 登录状态。
- `webview\`：桌面窗口运行数据。
- `desktop.lock`：应用单实例锁；退出后可保留，文件存在不代表程序仍在运行。

登录状态只保存在本机，不随应用分发。不写入应用安装目录，因此可从只读安装位置运行，更新程序时也能保留登录状态。桌面版使用新的数据目录，不自动复制原项目的浏览器配置。

`BossJobs.exe --dev` 使用独立的 `%LOCALAPPDATA%\BossJobs\dev\`。开发和验证不要使用日常账号目录。

## 从源码运行

在 Windows 上安装 Python 3.12 x64、Chrome 和 WebView2，然后用 PowerShell 在项目目录执行：

```powershell
py -3.12 -m venv .build-env
.\.build-env\Scripts\python.exe -m pip install -r requirements-desktop.txt
.\.build-env\Scripts\python.exe desktop.py --dev
```

桌面界面复用 `index.html`，通过 pywebview 的 JS/Python 桥调用 `Service`，不启动网页 HTTP 服务。原有 `crawl.py serve` 和 CLI 入口仍可使用。

## 构建 Windows 包

必须在 Windows x64 上构建，不能在 macOS 上直接生成 Windows exe。构建环境也需要 Chrome 和 WebView2，以便验证最终产物。

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build_windows.ps1
```

脚本会建立独立构建环境、安装固定版本依赖、运行单元测试和离线 Chrome 回放，然后用 PyInstaller 打包。最后启动**打包后的应用**，验证原生窗口、JS 桥、城市字典、JS 解析器和随包分发的浏览器驱动。检查使用临时目录和本地 HTML，不访问 BOSS。

产物：

- `dist\BossJobs\BossJobs.exe`：应用启动入口，需与整个目录一起分发。
- `dist\BossJobs-windows-x64.zip`：仅在所有检查通过后生成的分发包。
- `dist\windows-smoke.json`：原生窗口及驱动验收结果。

仓库还提供 `.github/workflows/windows-desktop.yml`：推送此分支或手动运行工作流后，在 Windows runner 上执行相同构建并上传 ZIP。工作流不自动发布 Release。签名和安装程序尚未配置，初版使用解压运行的目录式包。

## 验证

```powershell
$env:PYTHONUTF8 = "1"
$env:BOSS_BROWSER_TESTS = "1"
.\.build-env\Scripts\python.exe -m unittest discover -s tests -v
.\.build-env\Scripts\python.exe desktop.py --dev --smoke-test smoke.json
```

离线检查不会证明 BOSS 登录或页面结构仍然兼容。Windows 实机还需人工验证：中文用户名路径、首次扫码、登录复用、查询完成、停止后导出、取消保存、覆盖保存、打开岗位链接、查询中退出，以及再次启动。记录完成结果后再分发给其他使用者。

技术依据：[pywebview API](https://pywebview.flowrl.com/api/)、[WebView2 分发要求](https://learn.microsoft.com/microsoft-edge/webview2/concepts/distribution)、[PyInstaller 平台要求](https://pyinstaller.org/en/stable/)。
