@echo off
chcp 65001 >nul
echo 正在停止占用 8000 端口的 quant-monitor / uvicorn 进程...

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
  echo 结束 PID %%a
  taskkill /PID %%a /F >nul 2>&1
)

taskkill /IM uvicorn.exe /F >nul 2>&1

echo.
netstat -ano | findstr ":8000" | findstr "LISTENING" >nul
if errorlevel 1 (
  echo 8000 端口已释放。
) else (
  echo 仍有进程占用 8000，请以管理员身份再运行本脚本，或在任务管理器中结束 python.exe。
)
echo.
pause
