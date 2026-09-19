# 📘 文炼工坊（TextForge）

本地桌面应用，将长篇原始小说全自动重构为高质量成品。Tauri + Vue 3 + FastAPI，后端以 sidecar 随应用分发。

## 功能流水线
- **阶段0 拆书**：按章节拆分（支持 `第一章：`/`第一章` 无分隔符等格式），超长章自动虚拟化
- **阶段1 诊断**：逐批次生成章节摘要、人物状态、断层诊断
- **阶段2 蓝图**：汇总生成全书重构建议书，**人工确认**后进入重构
- **阶段3 重构+缝合**：按章重构；跨批次、跨虚拟章边界执行文字缝合（可断点续跑）
- **阶段4 收尾**：合并输出 `txt`/`docx`，备份旧稿，生成缝合报告

## 目录结构
```
backend/           FastAPI 后端（Python 3.12+）
src/               Vue 3 前端
src-tauri/         Tauri 壳（sidecar 生命周期、端口管理）
src-tauri/binaries/ sidecar 后端 exe（构建产物，入库忽略）
```

## 本地开发

### 1. 后端
```powershell
cd backend
# 首次：创建 venv 并安装依赖
python -m venv venv
.\venv\Scripts\pip install -r .\requirements.txt
# 启动（开发模式固定 8000 端口，与 Tauri debug 对齐）
.\run_dev.ps1
```
健康检查：`GET http://127.0.0.1:8000/api/health`

### 2. 前端
```bash
npm install
npm run dev        # Vite 开发服务（端口 1420）
npm run build      # 类型检查 + 产物构建
```

### 3. Tauri
```bash
npm run tauri dev    # 开发调试
```

## 打包发布
```powershell
# 1) 构建 sidecar 后端（PyInstaller --onefile，产物为单 exe）
cd backend
.\build_sidecar.bat
# 2) 打包安装程序
cd ..
npm run tauri build
```
**注意**：sidecar 必须用 `--onefile`。Tauri `externalBin` 只复制单个 exe，`--onedir` 的依赖目录不会带进安装包，会导致应用启动时后端缺依赖。

## 数据与日志
- 运行日志：`%APPDATA%\TextForge\logs\backend.log`
- 端口文件：`%APPDATA%\TextForge\port.json`（应用关闭时自动清理）
- 输出目录：`{output_path}/{01_summaries, 02_workspace, 03_final, _backup, progress.json}`
- `progress.json` 为唯一进度真相源，支持崩溃/断电后**断点恢复**（重启应用再次点击开始即续跑）