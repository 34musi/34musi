@echo off
chcp 65001 >nul
cd /d "%~dp0"
start "" cmd /c "timeout /t 3 /nobreak >nul && start http://127.0.0.1:8000/ui"
call "%~dp0start.bat"
