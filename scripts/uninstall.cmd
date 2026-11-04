@echo off
rem BossJobs uninstall launcher. Double-click this file.
rem The script body lives in the sibling .ps1 with the same base name.
rem This file stays ASCII-only on purpose: cmd.exe decodes batch files with the
rem OEM code page, so non-ASCII text here would break on other locales.
setlocal
set "SCRIPT=%~dp0%~n0.ps1"
if not exist "%SCRIPT%" (
    echo Missing "%SCRIPT%".
    echo Keep this file next to the other uninstall files and try again.
    pause
    exit /b 1
)
rem Leave the application directory: it cannot be deleted while it is the
rem working directory of this process.
cd /d "%TEMP%"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" %*
endlocal