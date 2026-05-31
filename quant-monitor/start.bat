@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到 .venv，请先在 quant-monitor 目录执行：
    echo   python -m venv .venv
    echo   .\.venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

call ".venv\Scripts\activate.bat"

echo.
echo ========================================
echo   quant-monitor 正在启动
echo   控制台: http://127.0.0.1:8000/ui
echo   文档:   http://127.0.0.1:8000/docs
echo   关闭本窗口即停止服务
echo ========================================
echo.

".venv\Scripts\python.exe" -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

echo.
echo 服务已退出，错误码 %ERRORLEVEL%
pause
