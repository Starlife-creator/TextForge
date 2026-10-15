@echo off
REM ============================================================
REM TextForge 后端 Sidecar 构建脚本（Windows x86_64）
REM
REM 前置：backend\venv 中已安装 pyinstaller（pip install pyinstaller）
REM 产物：src-tauri\binaries\backend-x86_64-pc-windows-msvc.exe
REM       src-tauri\binaries\backend-onedir\  （exe 同级的 _internal，供便携包组装）
REM ============================================================
setlocal
cd /d "%~dp0"

echo [1/4] 检查 PyInstaller...
venv\Scripts\python.exe -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo PyInstaller 未安装，正在安装...
    venv\Scripts\pip.exe install pyinstaller || goto :fail
)

REM ---- 为什么用 --onedir 而不是 --onefile ----
REM onefile 每次运行都要把压缩载荷解压到 %TEMP%\_MEIxxxxxx（实测约 40MB），
REM 实测启动 2.5~2.8s；而 onedir 直接铺开，启动只要 0.65~0.69s，且完全不碰 %TEMP%。
REM 另外 onefile 是父子双进程（引导进程 + Python 子进程），child.kill() 只杀得掉
REM 引导进程，子进程会变孤儿并残留 _MEI 目录；onedir 单进程，kill 直接生效。
REM 自解压行为也正是 Defender 把它误判为恶意软件的主要特征之一。
REM
REM Tauri 的 externalBin 要求单文件，因此这里仍然产出那个单文件占位给 tauri build，
REM 真正的便携包由 .github\workflows\release.yml（或 assemble_portable）把
REM backend.exe 与 _internal 一起铺到同一个目录里 —— onedir 的 exe 正是从
REM 「自身所在目录/_internal」找依赖的，两者同级即可。

echo [2/4] PyInstaller 打包（onedir，控制台关闭）...
REM --collect-all 列表是本项目 sidecar 的**唯一**配置源：release.yml 也调用本脚本，
REM 不要在两处各写一份。--noconfirm --clean 保证重建时不会卡在覆盖确认上。
venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --noconsole --onedir ^
  --collect-all fastapi --collect-all starlette --collect-all pydantic ^
  --collect-all pydantic_core --collect-all uvicorn --collect-all anyio ^
  --collect-all httpx --collect-all h11 --collect-all sniffio ^
  --collect-all chardet --collect-all charset_normalizer ^
  --collect-all socksio --collect-all lxml --collect-all docx ^
  --collect-all cn2an --collect-all certifi --collect-all httpcore --collect-all idna ^
  --distpath build\dist --workpath build\work --specpath build ^
  --name backend main.py || goto :fail

echo [3/4] 拷贝 sidecar 到 src-tauri\binaries...
if not exist "..\src-tauri\binaries" mkdir "..\src-tauri\binaries"
copy /Y "build\dist\backend\backend.exe" "..\src-tauri\binaries\backend-x86_64-pc-windows-msvc.exe" || goto :fail

echo [4/4] 拷贝 onedir 依赖（_internal）到 src-tauri\binaries\backend-ondir...
if exist "..\src-tauri\binaries\backend-ondir" rmdir /S /Q "..\src-tauri\binaries\backend-ondir"
mkdir "..\src-tauri\binaries\backend-ondir"
xcopy /E /I /Q /Y "build\dist\backend" "..\src-tauri\binaries\backend-ondir" || goto :fail

echo.
echo 构建完成：
echo   src-tauri\binaries\backend-x86_64-pc-windows-msvc.exe
echo   src-tauri\binaries\backend-ondir\   ^(backend.exe + _internal^)
exit /b 0

:fail
echo 构建失败，请检查上方日志
exit /b 1