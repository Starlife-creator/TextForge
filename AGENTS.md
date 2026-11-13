# AGENTS.md

> 状态：**已获用户批准，正式生效**，对本仓库后续所有代理会话构成约束。
>
> 依据：`README.md`「本地开发」章、`.github/workflows/build-check.yml`、
> `docs/ADR.md`、`docs/REVIEW-LOG.md`，以及本机实跑记录（2026-11-13）。

---

## 1. 三段命令表（前端 / 后端 / CI）

「构建与测试入口是哪两条命令」这个前提在 TextForge 不成立：这里没有一条
`build` 一条 `test` 就能覆盖的入口，前端（Vue + Vite + Tauri）、后端
（Python + FastAPI + PyInstaller sidecar）、CI 是三条独立的链路。

### 1.1 前端（仓库根目录，Node 20+）

| # | 目的 | 命令 | 本机受限沙箱下的状态 |
|---|---|---|---|
| F1 | 单元测试 | `npm run test`（= `vitest run`） | **阻断**：vite 启动时 `optimizeSafeRealPathSync()` 调 `exec("net use")`，沙箱禁 spawn → `spawn EPERM`。绕过：`NODE_OPTIONS='--require ./.tmp/nospawn.cjs' npx vitest run --pool=threads` |
| F2 | Lint | `npm run lint`（= `eslint . --max-warnings 0`） | **可用**；注意 §5 的遍历边界 |
| F3 | 排版 | `npm run format:check`（= `prettier --check .`） | **可用**；同上 |
| F4 | 类型检查 + 构建 | `npm run build`（= `vue-tsc --noEmit && vite build`） | 仅记录，未在沙箱内跑完 vite build；`vue-tsc --noEmit` 已实测 exit 0 |

### 1.2 后端（`backend/`，Python 3.13，用 `backend\venv`）

| # | 目的 | 命令 | 本机受限沙箱下的状态 |
|---|---|---|---|
| B1 | 单测 | `cd backend && python -m pytest tests -q` | **可用但需垫片**：CPython 在 Windows 上 `mode != 0o777` 会写显式 DACL，冲掉继承来的沙箱授权 → 成片 `PermissionError [WinError 5]`。垫片 `.tmp/sandbox_nodaclmode.py`（把 `os.mkdir/os.makedirs` 的 mode 折成 0o777）+ `-p no:cacheprovider --basetemp` 预建目录 + `APPDATA` 指到工作区 |
| B2 | 回归脚本 | `cd backend && python regression_test.py` | **可用**（需 B1 同款垫片；脚本形态，不进 pytest 收集） |
| B3 | SSE 事件流 | `cd backend && python test_event_stream.py` | **可用**（同上） |
| B4 | 端到端 | `cd backend && python test_e2e_pipeline.py` | **可用**（同上，额外需要 `APPDATA` 隔离，否则 backend.log 写 `%APPDATA%\TextForge\logs` 被拒） |
| B5 | Lint | `cd backend && ruff check .` | **可用**（venv 内 ruff 0.7.3） |
| B6 | 类型检查 | `cd backend && mypy --config-file mypy.ini .` | **可用**（venv 内 mypy 1.13.0） |
| B7 | 打包 sidecar | `backend\build_sidecar.bat` | 仅记录，未实跑。`--collect-all` 列表是该 bat 的唯一配置源 |

### 1.3 CI（`.github/workflows/build-check.yml`，4 个 job）

| job | 跑在哪 | 跑什么 | 与本地命令的对应 |
|---|---|---|---|
| `static`「静态检查（前端 + Rust）」 | `windows-latest` | `npm ci` → `npm run test` → `npm run lint` → `npm run format:check` → `npm run build` → 占位 sidecar → `cargo check` → `cargo test` → `npm audit --omit=dev --audit-level high` | F1/F2/F3/F4 + Rust |
| `backend-check`「后端单测 / 代码质量」 | matrix：`windows-latest` / `ubuntu-latest` / `macos-latest` | `ruff check .` → `mypy --config-file mypy.ini .` → `python -m pytest tests -q` →（仅 Windows）`regression_test.py` + `test_event_stream.py` → `pip-audit -r requirements.txt` → dev 工具链扫描（只记录不阻断） | B5/B6/B1/B2/B3 |
| `e2e`「端到端（mock LLM 真跑 phase0→phase4）」 | `windows-latest` | `python test_e2e_pipeline.py` | B4 |
| `rust-nonwindows`「Rust 编译（非 Windows）」 | `ubuntu-latest` | `cargo check` + `cargo audit` | 仅 Rust |

CI 里 `NODE_VERSION=20`、`PYTHON_VERSION=3.13`；`release.yml` 另存，调用
`backend/build_sidecar.bat`。

---

## 2. 版本号：单一来源 = `package.json`

版本号**只改 `package.json` 的 `version` 这一处**。`prebuild` / `predev` 钩子调用
`scripts/sync-version.mjs`，把它同步进 `src-tauri/Cargo.toml` 与
`src-tauri/tauri.conf.json`；前端由 Vite 注入 `__APP_VERSION__`，后端由 Tauri 外壳
注入 `TEXTFORGE_VERSION`。发布打 `v<version>` 标签，`release.yml` 校验三处版本与
标签一致。

**给代理的硬约束**：不要手改 `Cargo.toml` 或 `tauri.conf.json` 里的版本号 —— 那是
生成物，改了下一次 `npm run build` 会被覆盖，且会让 `release.yml` 的校验看不出来。

---

## 3. `docs/REVIEW-LOG.md` 是唯一追踪表

候选缺陷 → 复核结论 → 方案 → 实施批次 → 验证结果，**只在这一个文件里增行**。
不要在别处另建清单（历史上已经产生过「被遗忘的归档」，这是明令禁止的复发模式）。

配套纪律：文档/代理的结论一律不直接采信，每条给五选一定档；实测过的才标「实测」。
基线绿以六条为准：`pytest tests -q`、`ruff check .`、`mypy --config-file mypy.ini .`、
`regression_test.py`、`test_event_stream.py`、`test_e2e_pipeline.py`。

---

## 4. 闸门标志的 `clear` 必须早于阶段落盘（ADR-005）

**不变式**：先 `clear()` 闸门标志，再把 `current_phase` 落盘到闸门阶段。

反例（ADR-005 记录的真实缺陷）：

```python
progress['current_phase'] = 'phase2_waiting'
atomic_write_json(progress_path, progress)   # 此刻界面已弹框、resume 已就绪
state.blueprint_confirmed.clear()            # 把刚到的确认抹掉
```

存在窗口：progress 已是闸门阶段（界面弹框、`/api/status` 的 resume 就绪），而标志
还没 `clear` —— 此刻到达的确认被紧接着的 `clear()` **静默丢弃**，流水线永远等不到，
界面却显示「已确认」，**无任何报错**。

**给代理的硬约束**：任何改动闸门推送顺序的地方，先 `clear` 后落盘。qc 门本来就是
先 clear 再等，无需改动。

---

## 5. 仓库卫生（给代理的操作边界）

- `.tmp/` 是诊断临时区（沙箱垫片、探测脚本），**不进版本控制**，不要写进
  `README.md` 的命令表。跑测试/构建前把 `TEMP`/`TMP` 指向它。
- **遍历边界**：`eslint .` / `prettier --check .` 会遍历仓库根下所有目录，只要存在
  一个当前用户读不进去的目录（典型是 `mode=0o700` 建出来的），整条命令就以
  `EPERM: scandir` 失败、且 exit 2，看起来像 lint 红。遇到这种红先查「有没有不可读
  目录」，而不是改代码或加 `--ignore-pattern`。
- 不要为「这台机器的沙箱限制」去改构建脚本或 CI：`spawn EPERM`、`WinError 5`
  都是执行环境问题，不是代码缺陷。仓库里已提交的配置以能在干净环境跑通为准。
- 一次改动一提交；任何一批断言失败用 `git revert` 该提交，不使用 `stash`。
