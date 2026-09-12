@echo off
title 抖音续火花助手 - 启动管理器
setlocal enabledelayedexpansion

cd /d "%~dp0"

echo ==================================================
echo         抖音续火花助手 - 一键启动
echo ==================================================

rem 读取 .env 中的端口配置
set PORT=18101
if exist .env (
    for /f "usebackq tokens=1,2 delims==" %%i in (".env") do (
        if "%%i"=="PORT" set PORT=%%j
    )
)

rem 检测 Python 解释器
set PYTHON_EXE=python
if exist ".venv\Scripts\python.exe" (
    set PYTHON_EXE=.venv\Scripts\python.exe
)

%PYTHON_EXE% --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到有效的 Python 环境，请确认已安装 Python 并添加到系统 PATH。
    echo.
    pause
    exit /b 1
)

rem 检测端口是否已被占用 (过滤 LISTENING 状态且排除 PID 0)
set OCCUPIED_PID=
for /f "tokens=5" %%a in ('netstat -ano ^| findstr LISTENING ^| findstr ":%PORT% "') do (
    if "%%a" neq "0" set OCCUPIED_PID=%%a
)

if defined OCCUPIED_PID (
    echo [提示] 服务已在运行中 [端口: %PORT%, PID: !OCCUPIED_PID!]
    echo 正在为您打开网页控制台...
    start http://127.0.0.1:%PORT%/
    ping 127.0.0.1 -n 3 >nul
    exit /b 0
)

echo 正在启动 Web 服务 [端口: %PORT%]...
start "抖音续火花助手服务" %PYTHON_EXE% app.py

rem 循环检测服务是否成功就绪（最多等待 10 秒）
set /a count=0
:WAIT_LOOP
ping 127.0.0.1 -n 2 >nul
set /a count+=1
set LAUNCHED_PID=
for /f "tokens=5" %%a in ('netstat -ano ^| findstr LISTENING ^| findstr ":%PORT% "') do (
    if "%%a" neq "0" set LAUNCHED_PID=%%a
)
if defined LAUNCHED_PID goto LAUNCH_SUCCESS
if !count! geq 10 goto LAUNCH_TIMEOUT
goto WAIT_LOOP

:LAUNCH_TIMEOUT
echo [警告] 服务启动耗时较长，请查看弹出的服务窗口日志排查。
echo 正在尝试打开网页控制台...
start http://127.0.0.1:%PORT%/
exit /b 0

:LAUNCH_SUCCESS
echo ==================================================
echo [成功] 抖音续火花助手启动成功！
echo 运行端口: %PORT% [PID: !LAUNCHED_PID!]
echo 网页地址: http://127.0.0.1:%PORT%/
echo 如需暂停/停止服务，请双击运行 stop.bat
echo ==================================================
start http://127.0.0.1:%PORT%/
ping 127.0.0.1 -n 3 >nul
exit /b 0
