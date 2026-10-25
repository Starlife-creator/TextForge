# TextForge 架构说明

面向新贡献者：组件边界、流水线状态机、持久化文件清单。
用户视角的功能说明见 `README.md`；关键决策见 `docs/ADR.md`。

## 1. 组件分层

```
┌──────────────────────────────────────────────────────────────────────┐
│                          Tauri 桌面壳（src-tauri/）                     │
│  sidecar 生命周期 · 端口发现 · 进程清理 · 数据目录解析 · token 注入      │
└───────────────┬──────────────────────────────────────┬─────────────────┘
                │ 本机 127.0.0.1 随机端口 + X-TextForge-Token 鉴权
                │                                      │
┌───────────────▼───────────────┐      ┌───────────▼─────────────────┐
│  Vue 3 前端（src/）            │      │  FastAPI 后端 sidecar（backend/）│
│  App.vue 状态编排             │      │  routes/   HTTP 接口层        │
│  components/ 弹窗与面板        │ ◀──▶ │  core/     流水线各阶段        │
│  api/      client/SSE/system  │      │  utils/    共享工具与持久化    │
└───────────────────────────────┘      └──────────────────────────────┘
```

- **外壳**：Rust 侧只负责拉起 Python sidecar、发现 `port.json`、关窗时优雅停止；
  不参与业务逻辑。后端路径解析由 `backend/utils/appdir.py` 单一负责，外壳先解析
  目录再经环境变量注入，避免两侧各算一套路径。
- **前端**：`src/api/client.ts` 是 token 注入的唯一中转站；`src/api/sse.ts`
  订阅后端事件流；状态编排目前集中在 `src/App.vue`（#11 计划抽 composable）。
- **后端**：`routes/` 只做参数校验、线程化与错误收口；业务规则全部在 `core/`；
  `utils/` 提供原子写、脱敏、日志、token 估算、进度迁移等纯工具。

## 2. 模块边界

### 2.1 `backend/core/` 流水线阶段

| 模块 | 职责 |
|---|---|
| `splitter.py` | 阶段0：规则/兜底拆章，无分隔符格式回退固定段长；人工确认闸门 |
| `pipeline.py` | 阶段2 蓝图等待门、阶段3 重构/缝合主循环编排 |
| `blueprint.py` | 分卷压缩全书摘要、重构建议书；LRU 缓存 `arc_*.json` |
| `refactor.py` | 阶段3 批次重构、质检拦截、自动重写、逐章验收、连续性（结尾/memo/canon） |
| `stitch.py` | 批次边界与虚拟章边界缝合；失败降级跳过并计入质量报告 |
| `quality.py` | 空章/塌缩/占位/重复/AI 元话术等硬伤检测；章节健康分；质量报告 |
| `chapter_ai.py` | 单章重生成候选行选择与重写 |
| `canon.py` | #5 人物/伏笔档案：持久化、增量更新、按预算注入命中条目 |
| `epub.py` | #20 EPUB 导出器（导航目录、粗斜体映射、空章跳过） |
| `estimate.py` | 开跑前规模预估（章数/批数/调用数/token），算式与真跑同源；内存 LRU 缓存 |
| `budget.py` | 上下文窗口派生预算的单一事实源（单批上限、重叠、memo/连续性预留等） |
| `batching.py` | 装批算法 `pack_groups`，预估与真跑共用 |
| `versions.py` | 成品版本快照、回滚、路径白名单校验 |
| `finalize.py` | 阶段4 合并 txt/docx（可选 EPUB）、备份清理、质量报告落盘 |
| `prompt_registry.py` | #7 六阶段提示词版本注册表；`prompt_engine.py` 为加载器入口 |
| `document.py` | docx 富文本排版（标题样式、分页、粗斜体） |
| `context_probe.py` | 模型上下文探测 |
| `api_client.py` | 上游模型 HTTP 调用与重试 |

### 2.2 `backend/routes/` 接口域

| 模块 | 职责 |
|---|---|
| `start.py` | `/api/start` 开跑；预估 `estimate_run` 经线程池执行 |
| `pipeline_runner.py` | `run_pipeline` 阶段编排、断点恢复、配置漂移、心跳、历史登记 |
| `state.py` | #2 闸门状态机原语：进入/退出/等待时长/超时注册 |
| `status.py` | `/api/status` 状态、闸门等待时长、比较稿列表（拒绝符号链接） |
| `control.py` | 停止/暂停/继续、单章重生成、日志读取 |
| `output.py` | 输出目录清空、版本列表/回滚、质量报告读取 |
| `history.py` | 任务历史注册表读写、历史导出 JSON |
| `system.py` | 系统信息、模型候选、日志切片、诊断包导出 |
| `events.py` | SSE 事件流 |
| `health.py` | 免鉴权探活 |

### 2.3 `backend/utils/` 共享工具

`atomic.py` 原子写 JSON/文本；`redact.py` 密钥脱敏；`errors.py` 异常归类与 500 收口；
`progress_io.py` 进度读写缓存、迁移、线程化同步原语；`diagnostics.py` 诊断包打包；
`appdir.py` 数据目录解析；`auth.py` token 中间件；`logging_setup.py` 日志轮转与过滤器；
`tokens.py` token 估算；`sort.py` 自然排序；`paths.py` 路径规范化；`version.py` 版本号。

## 3. 流水线状态机

```
                         ┌──────────── 空闲 IDLE ────────────┐
                         │                                   │
                         ▼                                   │
              ┌─────────────────────┐                        │
              │ 阶段0 拆书 split     │                        │
              └──────────┬──────────┘                        │
                         │ 闸门① 拆书确认（wait_split_confirm）│
                         ▼                                   │
              ┌─────────────────────┐                        │
              │ 阶段1 诊断 diagnose  │                        │
              └──────────┬──────────┘                        │
                         │ 闸门② 断层勾选（wait_fix_review）    │
                         ▼                                   │
              ┌─────────────────────┐                        │
              │ 阶段2 蓝图 blueprint │                        │
              └──────────┬──────────┘                        │
                         │ 闸门③ 蓝图确认                     │
                         ▼                                   │
              ┌─────────────────────┐                        │
              │ 阶段3 重构+缝合       │◀── 闸门⑤ 质检拦截      │
              │  批次循环：诊断→重构  │◀── 闸门④ 逐章验收       │
              └──────────┬──────────┘                        │
                         │                                    │
                         ▼                                    │
              ┌─────────────────────┐                        │
              │ 阶段4 收尾 finalize  │                        │
              └──────────┬──────────┘                        │
                         └──────────────▶ 完成 DONE ─────────┘
```

### 3.1 闸门与运行状态

五个人工闸门（#2 已接入超时注册，默认 24h，env `TEXTFORGE_GATE_TIMEOUT` 可配）：

1. 拆书确认（`wait_split_confirm`）
2. 断层勾选（`wait_fix_review`）
3. 蓝图确认（阶段2 之后）
4. 逐章验收（阶段3 内，驳回可触发单章重生成）
5. 质检拦截（批次落盘前，硬伤自动重写一次）

运行状态由 `/api/status` 上报：`active_gate`、`waiting_seconds`、
`gate_timed_out`、`gate_timeout_gate`、`gate_wait_seconds`；超时安全暂停并落盘，
刷新页面后前端依据 `resume` 恢复弹窗。

异常分支：`stop_requested` 外部停止；批次契约失败按 `classify_batch_result`
分类（overflow/contract/raise/done）；断点续跑检测 `config_drift` 并覆盖本次漂移；
`versions.restore` 拒绝路径穿越与缺失版本，回滚前先快照。

### 3.2 阶段恢复契约

`progress.json` 是续跑唯一事实源：已完成批次/章节快照、闸门进入时间、
配置漂移、提示词版本、计数（重生成、批次质检）都写回其中；中断后以同一
`output_path` 重跑，只补未完成批次，不重复已落盘内容（#8 已补恢复分支单测）。

## 4. 持久化文件清单

### 4.1 单本书输出目录（用户自选 `output_path`）

| 路径 | 内容 | 写入方 |
|---|---|---|
| `progress.json` | 进度、批次快照、闸门时间、漂移、提示词版本、计数 | `utils/progress_io.py`、`pipeline_runner.py` |
| `02_workspace/split/<chapter_id>.txt` | 阶段0 规则拆章原文 | `splitter.py` |
| `02_workspace/reconstructed/chapter_<id>.txt` | 阶段3 重构稿（逐章落盘） | `refactor.py`、`stitch.py` |
| `02_workspace/snapshots/drafts/` | 批次过程稿快照 | `refactor.py` |
| `02_workspace/snapshots/final/` | 定稿快照 | `refactor.py` |
| `02_workspace/canon/canon.json`、`canon.md` | #5 人物/伏笔档案 | `core/canon.py` |
| `00_comparison/` | 章节重构前后对照稿（`chapter_<id>.comparison.md`） | `refactor.py` |
| `_diagnostics/quality_report.json` | 逐章指标、健康分、问题全量计数、趋势 | `quality.py`、`finalize.py` |
| `_diagnostics/blueprint_map/arc_<24>.json` | 蓝图分卷压缩缓存（LRU 50 条 / 20MB 上限） | `blueprint.py` |
| `_diagnostics/stitch_anchors.jsonl` | 缝合锚点与改动率记录 | `stitch.py` |
| `_backup/` | 成品覆盖前备份（`BACKUP_KEEP=20` 滚动清理） | `finalize.py` |
| 版本快照根目录 | 跨成品版本快照与回滚 | `versions.py` |
| `*.txt` / `*.docx` / `*.epub` | 阶段4 成品（EPUB 需勾选 `export_epub`） | `finalize.py` |

### 4.2 应用数据目录（`utils/appdir.py` 单一解析）

| 路径 | 内容 |
|---|---|
| `runs.json` | 任务历史元数据（脱敏后留存，不含进度正文；支持 `/api/history/export` 导出） |
| `logs/backend.log` | 后端日志（轮转 10MB × 5；诊断包仅取尾 2000 行与最近 100 条错误） |

## 5. 测试与本地开发

见 `README.md`「本地开发与测试」一节。后端纯逻辑优先单测（无 pytest-asyncio，
异步逻辑用 `asyncio.run()` 包同步用例）；e2e 由 `test_e2e_pipeline.py` 用 mock LLM
跑 phase0→phase4 兜底。
