# 📘 文炼工坊 TextForge

本地桌面应用，将长篇原始小说全自动重构为高质量成品。
Tauri + Vue 3 + FastAPI，后端以 sidecar 随应用分发。

所有处理都在本机完成，不上传任何文件（除非你主动连接本地大模型）。

---

## 功能流水线

| 阶段 | 作用 |
|---|---|
| 阶段0 拆书 | 按章节拆分，支持无分隔符格式；**人工确认**后继续；识别不准可填自定义章节标记重拆 |
| 阶段1 诊断 | 逐批次生成章节摘要、人物状态、断层诊断；**人工勾选**要修复的断层 |
| 阶段2 蓝图 | 分卷压缩全书摘要后汇总重构建议书（长书覆盖首尾）；**人工确认/编辑**后进入重构 |
| 阶段3 重构+缝合 | 按章重构，自动带入上一批结尾与前情摘要；批次落盘前做内容级质检，硬伤自动重写一次；跨批次、跨虚拟章边界执行文字缝合（单处失败降级跳过，不报废整轮）；**逐章验收** |
| 阶段4 收尾 | 合并输出 txt/docx（可选富文本：章标题样式 + 章间分页），备份旧稿，生成缝合报告与质量报告 |

所有闸门都是**无界等待**（`while not Event`）——程序会一直停在那里等你确认，
漏掉任何一个都会让任务假死。前端靠 `/api/status` 的 `resume` 字段在刷新后恢复弹窗。

### 其他能力

- **上下文预算**：按 token 估算自动分批；上下文超限时对半拆批自愈
- **跨批连续性**：重构下一批时带入上一批结尾（优先重构稿，未写完则回退原文），避免称谓/伏笔断层
- **前情摘要**：同时带入上一批的诊断摘要（剧情走向 + 人物状态），补蓝图之外的局部事实一致性
- **内容级质检**：空章、塌缩、占位、AI 元话术、残留标记、重复章等硬伤会在落盘前拦截；开启自动重写时每批最多重写一次（整轮有上限）
- **缝合降级**：缝合输出契约不合格会带错因重试一次；仍失败则跳过该交界、保留原文，并计入质量报告，不再让整本已重构好的稿子报废
- **有界并发**：默认串行；云端可将并发批次数调到 2-4，本地小模型建议保持 1
- **docx 富文本**：勾选后章标题用 Word 标题样式、章间分页、`**粗体**`/`*斜体*` 生效；不勾选保持原有纯段落排版
- **质量报告**：每次成功运行生成 `_diagnostics/quality_report.json`，界面可查看逐章指标与问题证据
- **缝合改动观测与最小改动闸门**：逐边界记录覆盖字数与「原文句子未被原样保留的比例」。
  缝合窗口封顶 1200 字——窗口原本随「上下文」线性放大，128k 时会算出 1.4 万字，比一整章还长，
  「修补交界」就悄悄变成「替换整章」。默认开启的闸门拒绝改动率 >35% 的补丁（带错因重试一次，
  仍不合格则保留原文交界），在「重构保护门」里可关掉；平均改动率超 25% 时质量报告提示抽读。
  35% 与 25% 是**设计取值**，不是真机分布测出来的阈值
- **开跑前预估**：点「预估本次」给出章数/批数/调用数/token 与缝合覆盖面积，算式与真跑同源
  （共用 `body_token_budget`、`pack_groups`、`diagnose_max_tokens`、缝合窗口上限，蓝图分卷
  压缩更是直接调用真跑的 `chunk_by_budget`，窗口派生系数只有一处定义）；填了单价
  （¥/百万 token）即折算预计花费与已花费，本地模型留 0 就是不计费。预估不含自动重写与拆批重跑，
  因此是下界
- **上下文默认 32768**（旧值 8000）：窗口决定单批装多少字，窗口小→批数多→蓝图/前情这些脚手架
  要随每批重发。按预估算式**推算**（非真机实测）：60 章的书在 8000 下切 30 批、脚手架占输入 68%，
  32768 下 8 批、占 42%；再往上批数不再下降（撞上单批 8 章上限），预留却继续随窗口放大，
  128k 反而多花约 40% 的 token——所以预设里**刻意没有 128k 档**。本地小模型选「快速低保真」（8000），
  拐点判据由 `backend/tests/test_estimate.py` 的用例守着
- **单章重生成同步质检**：逐章验收里驳回某章会重写它，该章的结构性质检结论同时刷新——
  已经改好的不再被报告指控，新引入的占位/塌缩会被记下并提示「可再次驳回」；同批其他章的结论
  不动，刷新结果并回流水线内存、不会被下一次写盘覆盖回旧账
- **断点续跑**：中断后重跑同一输出目录，只做未完成的批次，并检测配置漂移
- **成品版本**：重跑覆盖前自动快照上一版，可回滚
- **任务历史**：跨会话记录跑过哪些书、停在哪、为什么停

---

## 目录结构

```
backend/               FastAPI 后端（Python 3.13+）
  core/                流水线各阶段：拆章、诊断、蓝图、重构、缝合、收尾
  routes/              HTTP 接口
  utils/               appdir（数据目录）、auth（调用方鉴权）、日志、token 估算
  main.py              入口：绑随机端口 → 写 port.json → 起 uvicorn
src/                   Vue 3 前端
  api/                 后端调用（client.ts 是 token 注入的唯一中转站）
  components/          弹窗与面板组件
src-tauri/             Tauri 壳：sidecar 生命周期、端口发现、进程清理
docs/使用说明.txt       随便携包分发的用户文档
```

---

## 架构要点

### 数据目录（便携自包含）

历史、日志、端口文件全部由 `backend/utils/appdir.py` 解析，**唯一数据源**：

```
TEXTFORGE_DATA_DIR（外壳注入） > exe 旁的 data/ > %APPDATA%\TextForge
```

Rust 外壳先解析目录，再经环境变量注入 sidecar，避免两侧各算一套路径
（一旦不一致，前端就永远读不到 `port.json`）。便携包因此完全自包含。

### 调用方鉴权

后端绑 `127.0.0.1` 随机端口，但**任何本地进程**读一下 `port.json` 就能直接调接口，
而 `/api/start` 可指定任意读写路径。防护方式：

1. 外壳用 CSPRNG 生成 token，经环境变量注入 sidecar，**从不落盘**
2. 前端只能经 Tauri IPC（同进程）取到
3. 后端纯 ASGI 中间件校验 `X-TextForge-Token`

`/api/health` 豁免（不返回任何数据，供探活）。**未配置 token 时默认拒绝**（即使没注入 token 也不会放行）；
只有显式设置 `TEXTFORGE_DEV=1` 才关闭鉴权，`run_dev.ps1` 与 e2e 已设置。

> 注意：外壳自己发起的 HTTP 调用**也必须**带 token，漏带会让关窗时的
> `/api/stop`、`/api/system/shutdown` 被 401 静默拒绝，优雅退出退化成强杀。

### 进程生命周期

sidecar 是 PyInstaller **onedir**（不是 onefile）：

| | onefile | onedir（当前） |
|---|---|---|
| 启动到接口可用 | 2.5–2.8s | **0.65–0.69s** |
| `%TEMP%` 临时目录 | 1 个（约 40MB） | **0 个** |
| 进程数 | 2（引导 + Python） | **1** |

onefile 每次运行都要把载荷解压到 `%TEMP%\_MEIxxx`，既是启动慢的原因，
也正是 Defender 把它误判为恶意软件的主要特征。onedir 单进程，`child.kill()`
直接生效。

---

## 架构决策

关键取舍与其实测依据记在 [docs/ADR.md](docs/ADR.md)，例如：
为什么 sidecar 用 onedir 而不是 onefile、心跳为什么不落盘、
闸门标志的 clear 为什么必须早于阶段落盘。

---

## 本地开发

### 环境准备

| 步骤 | Windows | Linux / macOS |
|---|---|---|
| 后端虚拟环境 | `cd backend && python -m venv venv && venv\Scripts\activate` | `cd backend && python3 -m venv venv && source venv/bin/activate` |
| 后端依赖 | `pip install -r requirements-dev.txt` | 同左 |
| 前端依赖 | `npm ci`（仓库根目录） | 同左 |

```bash
# 构建 sidecar（产出 src-tauri\binaries\）
backend\build_sidecar.bat

# 开发模式：前端热更新 + 手动起后端
python backend\main.py          # 终端 A
npm run tauri dev               # 终端 B
```

### 测试与质量门命令表

| 检查项 | 命令（仓库根目录） | 说明 |
|---|---|---|
| 后端 pytest 单测 | `cd backend && python -m pytest tests -q` | 纯逻辑优先，异步用 `asyncio.run()` 包裹 |
| 后端回归脚本 | `cd backend && python regression_test.py` | 脚本形态，非 pytest 收集 |
| SSE 事件流 | `cd backend && python test_event_stream.py` | 事件契约回归 |
| 端到端 mock LLM | `cd backend && python test_e2e_pipeline.py` | phase0→phase4，自动确认闸门 |
| 后端 Lint | `cd backend && ruff check .` | CI 阻断 |
| 后端类型检查 | `cd backend && mypy --config-file mypy.ini .` | 新模块增量门槛（#10） |
| 前端单测 | `npm run test`（vitest） | CI 阻断 |
| 前端 Lint | `npm run lint` | ESLint，零警告阻断 |
| 前端排版 | `npm run format:check` | Prettier 阻断 |
| 前端构建 | `npm run build` | `vue-tsc --noEmit && vite build` |
| Rust 单测 | `cd src-tauri && cargo test` | 需 Rust 工具链 |

> 离线环境：`requirements-dev.txt` 已手动锁定版本；`requirements.in` /
> `requirements-dev.in` 是 `pip-compile --generate-hashes` 的输入，联网后生成带哈希的
> 锁文件替换当前运行时清单（#14）。架构边界、状态机与持久化清单见
> [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

代码风格由 `eslint.config.js`（ESLint 10 扁平配置）+ `.prettierrc.json` 统一，
两者的规则边界靠 `@vue/eslint-config-prettier` 划开，CI 里都会阻断。

### 版本号（单一来源）

版本号**只在 `package.json` 的 `version` 改这一处**。`npm run build` / `npm run dev`
会经 `prebuild` / `predev` 钩子调用 `scripts/sync-version.mjs`，把版本同步进
`src-tauri/Cargo.toml` 与 `src-tauri/tauri.conf.json`；前端由 Vite 注入 `__APP_VERSION__`，
后端由 Tauri 外壳注入 `TEXTFORGE_VERSION`。发布时打 `v<version>` 标签即可，
`release.yml` 会校验三处版本与标签一致。

端到端测试会用 `mock_llm.py` 冒充大模型，并**自动确认所有人工闸门**
（`gate_driver`），因此可以无人值守跑完整条流水线。

### 组装便携包

```powershell
cd src-tauri
cargo tauri build --config tauri.portable.json --target x86_64-pc-windows-msvc
# 然后把 release\textforge.exe 与 backend\dist\backend\{backend.exe,_internal}
# 一起平铺到同一目录后压 zip —— onedir 的 exe 从**自身同级**的 _internal 找依赖
```

`bundle.active=false` 让 Tauri 不生成 NSIS/MSI 安装壳。

---

## 常见问题

**双击没反应 / 一闪即退**
看 `data\logs\backend.log`。常见原因：杀毒软件拦截、文件夹被压缩包直接运行、
手工整理时挪走了 `_internal\`。

**Defender 误报**
本项目未购买代码签名证书，没有数字签名。若被误报，在「病毒和威胁防护 → 保护历史记录」
恢复，并把文件夹加入排除项。便携版已去掉自解压行为，误报概率大幅降低。

**接本地大模型**
LM Studio / Ollama / llama.cpp / 任意 OpenAI 兼容端点均可。
设置面板有「实测上下文窗口」，探到的真实值会自动用来缩小批次。