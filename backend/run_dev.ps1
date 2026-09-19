# TextForge 后端开发模式启动脚本
# 作用：固定监听 127.0.0.1:8000，与 Tauri debug 端（get_backend_port 固定返回 8000）对齐。
# 用法：在 backend 目录下执行  ./run_dev.ps1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$env:TEXTFORGE_DEBUG = "1"
$env:TEXTFORGE_DEV_PORT = "8000"

Write-Host "启动 TextForge 后端（开发模式，端口 8000）..." -ForegroundColor Cyan
& .\venv\Scripts\python.exe main.py
