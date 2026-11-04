<#
    BossJobs 卸载脚本（Windows 目录式包）

    用法：双击同目录的「卸载.cmd」，或在 PowerShell 中执行
        powershell -NoProfile -ExecutionPolicy Bypass -File .\卸载.ps1

    参数：
        -Force        跳过所有确认，直接卸载（供自动化使用）
        -RemoveData   同时删除用户数据（%LOCALAPPDATA%\BossJobs，含 BOSS 登录状态）
        -DataDir      指定用户数据目录，默认 %LOCALAPPDATA%\BossJobs

    只删除本脚本所在目录和上方的用户数据目录。找不到 BossJobs.exe 时一律拒绝执行，
    以免脚本被放到其他位置时误删。程序目录交给一个延迟启动的隐藏进程完成删除，
    因为正在运行的脚本无法删除它自己所在的目录。

    本文件必须保存为带 BOM 的 UTF-8：Windows PowerShell 读取无 BOM 的 .ps1 时按
    ANSI 代码页解码，中文提示会变成乱码。
#>
[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$RemoveData,
    [string]$DataDir
)

$ErrorActionPreference = 'Stop'
$ExeName = 'BossJobs.exe'

function Test-Yes($Text) {
    return ($Text -match '^\s*(y|yes|是)\s*$')
}

function Get-ItemSize($Path) {
    try {
        $measured = Get-ChildItem -LiteralPath $Path -Recurse -Force -File -ErrorAction SilentlyContinue |
                    Measure-Object -Property Length -Sum
        if ($null -eq $measured.Sum) { return [int64]0 }
        return [int64]$measured.Sum
    } catch {
        return [int64]0
    }
}

function Format-Size([int64]$Bytes) {
    if ($Bytes -ge 1GB) { return ('{0:N1} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N1} MB' -f ($Bytes / 1MB)) }
    if ($Bytes -ge 1KB) { return ('{0:N0} KB' -f ($Bytes / 1KB)) }
    return "$Bytes 字节"
}

function Stop-WithNotice($Message) {
    Write-Host ''
    Write-Host $Message -ForegroundColor Red
    if (-not $Force) { Read-Host '按回车键退出' | Out-Null }
    exit 1
}

# --- 定位并校验程序目录 -------------------------------------------------------

$AppDir = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($AppDir)) { $AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
try {
    $AppDir = (Resolve-Path -LiteralPath $AppDir).ProviderPath.TrimEnd('\')
} catch {
    Stop-WithNotice '无法定位脚本所在目录。'
}
if (-not (Split-Path -Parent $AppDir)) {
    Stop-WithNotice "拒绝在驱动器根目录 $AppDir 执行卸载。请把「卸载.cmd」放在应用目录中运行。"
}
if (-not (Test-Path -LiteralPath (Join-Path $AppDir $ExeName) -PathType Leaf)) {
    Stop-WithNotice ("未在 $AppDir 找到 $ExeName，为避免误删已停止。" +
                     "请把「卸载.cmd」放在解压后的应用目录中再运行。")
}

# --- 定位用户数据目录 ---------------------------------------------------------

if ([string]::IsNullOrWhiteSpace($DataDir)) {
    $base = $env:LOCALAPPDATA
    if ([string]::IsNullOrWhiteSpace($base)) { $base = Join-Path $env:USERPROFILE 'AppData\Local' }
    $DataDir = Join-Path $base 'BossJobs'
}
if (-not [System.IO.Path]::IsPathRooted($DataDir)) { $DataDir = Join-Path (Get-Location).Path $DataDir }
$DataDir = [System.IO.Path]::GetFullPath($DataDir)
if ($DataDir.Length -gt 3) { $DataDir = $DataDir.TrimEnd('\') }
$dataExists = (Test-Path -LiteralPath $DataDir) -and -not ($DataDir -match '^[A-Za-z]:$')

# --- 确认 ---------------------------------------------------------------------

if (-not $Force) {
    $appSize = Format-Size (Get-ItemSize $AppDir)
    $dataSize = if ($dataExists) { Format-Size (Get-ItemSize $DataDir) } else { '无' }
    Write-Host ''
    Write-Host 'BOSS 岗位查询 · 卸载' -ForegroundColor Cyan
    Write-Host "  程序目录：$AppDir（$appSize）"
    Write-Host "  用户数据：$DataDir（$dataSize）"
    Write-Host ''
    Write-Host '用户数据包含专用 Chrome 的 BOSS 登录状态、公司映射和请求计数。' -ForegroundColor DarkGray
    Write-Host ''
    if (-not (Test-Yes (Read-Host '确认卸载？[y/N]'))) {
        Write-Host '已取消，未做任何改动。'
        Read-Host '按回车键退出' | Out-Null
        exit 0
    }
    if ($dataExists -and -not $RemoveData) {
        if (Test-Yes (Read-Host '是否同时删除用户数据（含登录状态）？[y/N]')) { $RemoveData = $true }
    }
}

# --- 结束正在运行的程序 -------------------------------------------------------

$running = @(Get-Process -Name 'BossJobs' -ErrorAction SilentlyContinue)
if ($running.Count -gt 0) {
    Write-Host '正在关闭运行中的 BossJobs ...'
    $running | Stop-Process -Force -ErrorAction SilentlyContinue
    for ($i = 0; $i -lt 50; $i++) {
        if (-not (Get-Process -Name 'BossJobs' -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 200
    }
    if (Get-Process -Name 'BossJobs' -ErrorAction SilentlyContinue) {
        Stop-WithNotice '无法关闭正在运行的 BossJobs，请手动退出后重试。'
    }
}

# --- 删除用户数据 -------------------------------------------------------------

if ($RemoveData -and $dataExists) {
    # 专用 Chrome 会用用户数据里的 profile 目录，先只结束命令行带该目录的 Chrome。
    $profileDir = Join-Path $DataDir 'profiles'
    if (Test-Path -LiteralPath $profileDir) {
        $dedicated = @(Get-CimInstance Win32_Process -Filter "Name = 'chrome.exe'" -ErrorAction SilentlyContinue |
            Where-Object {
                $_.CommandLine -and
                $_.CommandLine.IndexOf($profileDir, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
            })
        if ($dedicated.Count -gt 0) {
            Write-Host '正在关闭应用启动的专用 Chrome ...'
            $dedicated | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            Start-Sleep -Milliseconds 800
        }
    }
    Write-Host "正在删除用户数据：$DataDir"
    try {
        Remove-Item -LiteralPath $DataDir -Recurse -Force -ErrorAction Stop
    } catch {
        Write-Host ''
        Write-Host "未能完全删除用户数据：$($_.Exception.Message)" -ForegroundColor Yellow
        Write-Host "可在关闭所有浏览器后手动删除：$DataDir" -ForegroundColor Yellow
    }
} elseif ($dataExists) {
    Write-Host "已保留用户数据：$DataDir"
}

# --- 删除程序目录 -------------------------------------------------------------

if (-not $Force) {
    Read-Host '按回车键删除程序目录并关闭此窗口' | Out-Null
}

# 脚本无法删除自己所在的目录，交给一个延迟启动的隐藏进程重试，直到目录消失。
$ComSpec = $env:ComSpec
if ([string]::IsNullOrWhiteSpace($ComSpec)) { $ComSpec = Join-Path $env:SystemRoot 'System32\cmd.exe' }
$helper = Join-Path $env:TEMP ('bossjobs-uninstall-' + [guid]::NewGuid().ToString('N') + '.cmd')
if ($AppDir -match '["%^&|<>]') {
    Write-Host ''
    Write-Host "路径包含批处理特殊字符，未自动删除，请手动删除：$AppDir" -ForegroundColor Yellow
    exit 0
}
# 数组元素里的字符串用 -f 拼接：@() 里的逗号比 + 结合得更紧，直接相加会把路径拆成多个元素。
$lines = @(
    '@echo off',
    'setlocal',
    ('set "TARGET={0}"' -f $AppDir),
    'set /a tries=0',
    ':retry',
    'rmdir /s /q "%TARGET%" 2>nul',
    'if not exist "%TARGET%" goto done',
    'ping -n 2 127.0.0.1 >nul',
    'set /a tries+=1',
    'if %tries% lss 120 goto retry',
    ':done',
    'del "%~f0" >nul 2>&1',
    'endlocal'
)
Set-Content -LiteralPath $helper -Value $lines -Encoding ASCII
Start-Process -FilePath $ComSpec -ArgumentList '/c', ('"' + $helper + '"') -WindowStyle Hidden

Write-Host '程序目录正在删除。' -ForegroundColor Green
Write-Host "若几秒后目录仍然存在，请手动删除：$AppDir"
exit 0