@echo off
title 抖音续火花助手 - 暂停管理器
setlocal enabledelayedexpansion

cd /d "%~dp0"

echo ==================================================
echo       抖音续火花助手 - 一键暂停 / 停止
echo ==================================================

rem 读取 .env 中的端口配置
set PORT=18101
if exist .env (
    for /f "usebackq tokens=1,2 delims==" %%i in (".env") do (
        if "%%i"=="PORT" set PORT=%%j
    )
)

set KILLED=0

rem 1. 优先依据 data/app.pid 终止
if exist "data\app.pid" (
    set /p PID_VAL=<"data\app.pid"
    if defined PID_VAL (
        if "!PID_VAL!" neq "0" (
            taskkill /F /PID !PID_VAL! >nul 2>&1
            if not errorlevel 1 (
                echo [已终止] 依据 PID: !PID_VAL! 停止了服务进程。
                set KILLED=1
            )
        )
    )
    del /f /q "data\app.pid" >nul 2>&1
)

rem 2. 检测并释放监听该端口的进程 (防残留)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr LISTENING ^| findstr ":%PORT% "') do (
    set PORT_PID=%%a
    if defined PORT_PID (
        if "!PORT_PID!" neq "0" (
            taskkill /F /PID !PORT_PID! >nul 2>&1
            if not errorlevel 1 (
                echo [已终止] 成功释放端口 %PORT% 上的进程 PID: !PORT_PID!。
                set KILLED=1
            )
        )
    )
)

rem 3. 结果反馈
echo ==================================================
if "%KILLED%"=="1" (
    echo [成功] 抖音续火花助手服务已暂停 / 停止！
    echo 端口 %PORT% 资源已成功释放。
    echo 如需重新启动，请双击运行 start.bat
) else (
    echo [提示] 未检测到正在运行的抖音续火花助手服务 [端口: %PORT%]。
)
echo ==================================================
echo.
pause
