@echo off
cd /d "%~dp0"
chcp 65001 >nul 2>&1
title PaperPilot

echo.
echo   ==========================================
echo        PaperPilot - Smart Literature Tool
echo   ==========================================
echo.

REM -- Check Python --
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo   [ERROR] Python not found.
    echo   Please install Python 3.10+ from https://python.org
    echo   Make sure to check "Add Python to PATH"
    echo.
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo   [OK] Python %PYVER%

REM -- Install dependencies --
echo   [..] Installing dependencies...
echo.
python -m pip install -r requirements.txt --disable-pip-version-check -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn
if %errorlevel% neq 0 (
    echo.
    echo   [ERROR] Install failed. Check your network and retry.
    pause
    exit /b 1
)
echo.
echo   [OK] Dependencies ready

REM -- Generate config --
if not exist config.yaml (
    copy config.example.yaml config.yaml >nul
    echo   [OK] config.yaml created
)

REM -- Desktop shortcut --
if exist .shortcut_created goto :skip_shortcut
echo.
set /p CREATESC="  [?] Create desktop shortcut? (Y/N): "
if /i not "%CREATESC%"=="Y" goto :mark_shortcut
powershell -Command "$ws=New-Object -ComObject WScript.Shell;$s=$ws.CreateShortcut([Environment]::GetFolderPath('Desktop')+'\PaperPilot.lnk');$s.TargetPath=(Get-Command python).Source;$s.Arguments='app.py';$s.WorkingDirectory='%~dp0';$s.Description='PaperPilot Smart Literature Tool';$s.Save()" >nul 2>&1
echo   [OK] Desktop shortcut created
:mark_shortcut
echo.>.shortcut_created
:skip_shortcut

REM -- Launch --
echo   [OK] Starting PaperPilot...
echo.
python app.py
if %errorlevel% neq 0 (
    echo.
    echo   [ERROR] Program exited abnormally
    pause
)
