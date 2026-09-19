@echo off
REM ============================================================
REM TextForge 后端 Sidecar 构建脚本（Windows x86_64）
REM 前置：backend\venv 中已安装 pyinstaller（pip install pyinstaller）
REM 产物：src-tauri\binaries\backend-x86_64-pc-windows-msvc.exe
REM ============================================================
setlocal
cd /d "%~dp0"

echo [1/3] 检查 PyInstaller...
venv\Scripts\python.exe -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo PyInstaller 未安装，正在安装...
    venv\Scripts\pip.exe install pyinstaller || goto :fail
)

REM 注意：必须用 --onefile 单文件。Tauri externalBin 只复制这一个 exe，
REM onedir 产生的同目录依赖不会被带进安装包，会导致应用启动时后端因缺依赖而失败。
echo [2/3] PyInstaller 打包（onefile 单文件、控制台关闭）...
venv\Scripts\python.exe -m PyInstaller --noconsole --onefile ^
  --collect-all fastapi --collect-all starlette --collect-all pydantic ^
  --collect-all pydantic_core --collect-all uvicorn --collect-all anyio ^
  --collect-all httpx --collect-all h11 --collect-all sniffio ^
  --collect-all natsort --collect-all chardet --collect-all charset_normalizer ^
  --collect-all socksio --collect-all lxml --collect-all docx ^
  --collect-all cn2an --collect-all certifi --collect-all httpcore --collect-all idna ^
  --distpath build\dist --workpath build\work --specpath build ^
  --name backend main.py || goto :fail

echo [3/3] 拷贝 sidecar 到 src-tauri\binaries...
if not exist "..\src-tauri\binaries" mkdir "..\src-tauri\binaries"
copy /Y "build\dist\backend.exe" "..\src-tauri\binaries\backend-x86_64-pc-windows-msvc.exe" || goto :fail

echo.
echo 构建完成：src-tauri\binaries\backend-x86_64-pc-windows-msvc.exe
echo 现在可以执行 npm run tauri build 打包安装程序。
exit /b 0

:fail
echo 构建失败，请检查上方日志。
exit /b 1
