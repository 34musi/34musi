# 双击若被策略拦截：右键「使用 PowerShell 运行」，或先执行 Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "[错误] 未找到 .venv，请先安装依赖：" -ForegroundColor Red
    Write-Host "  python -m venv .venv"
    Write-Host "  .\.venv\Scripts\pip install -r requirements.txt"
    Read-Host "按 Enter 退出"
    exit 1
}

Write-Host ""
Write-Host "========================================"
Write-Host "  quant-monitor 正在启动"
Write-Host "  控制台: http://127.0.0.1:8000/ui"
Write-Host "  文档:   http://127.0.0.1:8000/docs"
Write-Host "  关闭本窗口即停止服务"
Write-Host "========================================"
Write-Host ""

& $venvPy -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

Read-Host "按 Enter 退出"
