# 审查与优化追踪表（REVIEW-LOG）

本文是**唯一的追踪表**：候选缺陷 → 复核结论 → 方案 → 实施批次 → 验证结果。
新增候选请直接加行，不要在别处另建清单（避免产生下一个"被遗忘的归档"）。

- 当前轮次：R9（PR #1 合并后的自查），基线提交 `b325bf3`
- 找法：5 路只读并行审计（IO/Windows、并发、密钥链路、测试可达性、前端+外壳）+ 本地复现实验与量纲测量
- 纪律：**代理/文档的结论一律不直接采信**；每条给五选一定档；实测过的才标「实测」

## 0. 本轮流程（自己设计，逐步执行）

| 阶段 | 产出 | 出口判据 |
|---|---|---|
| A 定流程 | 本节 + 本文件成为唯一追踪表 | README 指向本文，不会成孤儿文档 |
| B 复核定档 | §1 表：五选一 + 「该抓它的门禁抓得住吗」 | 每条要么有复现证据，要么写明未复现 |
| C 出方案 | §2：批次、DoD、反向断言、改动清单、§3 明确不做 | 每条改动自带「缺陷装回必红」的用例设计 |
| D 对抗自审 | §1/§2 的修正记录（含我驳回的代理结论） | 每条修正有代码行号或实测支撑 |
| E 分批实施 | 一改动一提交，分支 `fix/review-r9-gates` | 每批后六条闸门绿 + 行尾核对 |
| F 收口 | §4「还有什么没查」+ 记忆归档 | 未复现项标注重测条件，不写成已修 |

回滚方式：一改动一提交，任何一批断言失败 ⇒ `git revert` 该提交（不使用 stash）；
基线绿以 `pytest tests -q` / `ruff check .` / `mypy --config-file mypy.ini .` /
`regression_test.py` / `test_event_stream.py` / `test_e2e_pipeline.py` 六条为准。

## 1. 复核定档表

| ID | 结论 | 证据 | 该抓它的门禁，现在抓得住吗 |
|---|---|---|---|
| R1 闸门处「停止」被当成「确认」 | **仍存在 · 高** | 机制链：[control.py:53-59](../backend/routes/control.py) 把 5 个闸门事件全 set → [state.py:127](../backend/routes/state.py) `while not event.is_set()` 因此不查 `stop_requested` 直接返回 → phase2 之后没有停止检查，[pipeline_runner.py:455-457](../backend/routes/pipeline_runner.py) 的 `check_pause` **只在暂停态才拦** → 整个 phase3（几十次模型调用）跑完才在 :61 抛。实验：事件已置位 + `stop_requested=True` 时 `wait_gate` 实测正常返回 | **抓不住**。`tests/test_gate_timeout.py` 只测超时路径；e2e 的停止场景不在闸门处按停止 |
| R2 `versions.restore` 中途失败删掉唯一副本 | **仍存在 · 高** | [versions.py:126-134](../backend/core/versions.py)：第二次 `shutil.move` 抛错后 `finally` 删掉装着现成品的 `backup_dir`。实测注入一次 PermissionError ⇒ `03_final 还在吗: False`；仅当恢复前那次 `snapshot()` 侥幸成功才可回生，而它在失败时只 warning 返回 None（:51-53） | **抓不住**。版本恢复用例只走成功路径 |
| R3 验收门把 `regen_count` 清零 | **仍存在 · 中高** | [refactor.py:47](../backend/core/refactor.py) 的 `ACCEPT_ABSORB_KEYS` 无 `regen_count`，而 [control.py:231-233](../backend/routes/control.py) 写的是磁盘。实测：磁盘 `{'5': 4}` → absorb+写回后 `None`；`REGEN_MAX_PER_CHAPTER = 3` ⇒ 每过一次门保险丝复位 | **抓不住**。`tests/test_regen_qc.py` 只断言被吸收的那 3 个键 |
| R4 单章重写用 LLM 调用前的旧快照整份盖盘 | **仍存在 · 高** | [refactor.py:300](../backend/core/refactor.py) 无锁读 → `await call_with_retry(...)`（几十秒）→ :345-346 带锁写回**同一个对象**。期间流水线/另一章/验收端点写的任何键都被抹掉。R12 是它的同族 | 抓不住。regen 用例是单发调用，没有并发 |
| R5a 复合键名不脱敏 | **仍存在 · 中** | 实测 `_is_sensitive_key`：`client_secret`/`refresh_token`/`api_secret`/`my_token`/`auth_token` 全 False；`_mask_text` 对 `?key=sk-…`、裸 `key=`、单独出现的 `sk-…`、`https://user:pw@host` 全不遮 | 抓不住。现有用例只覆盖 `api_key`/`token`/`access_token` 拼写 |
| R5b `error_logs`/`last_error` 明文落盘且读侧不遮 | **仍存在 · 中高** | [status.py:131](../backend/routes/status.py) 原样返回；[history.py:92-94](../backend/routes/history.py) 只截断不遮。实测：`401 api_key=sk-live-ABCDEFGHIJ` 进 runs.json 后读侧原样出来，而**同一个字符串**在诊断包里是 `401 api_key=***`（口径不对称） | 抓不住，且 `test_pipeline_recovery.py` 断的是"原样透传"（方向相反） |
| R5c root logger 上的兜底脱敏无效 | **仍存在 · 中高** | [logging_setup.py:22](../backend/utils/logging_setup.py)。实测第三方 logger 的 warning 经 root handler 输出时**明文**——CPython `callHandlers` 只跑各 handler 自己的 filter，不跑祖先 logger 的 filter | 抓不住。#12 的回归只测 `textforge` logger 自己 |
| R5d 历史残留磁盘文件没人扫 | **⏸ 需拍板** | `scrub_models` 只覆盖 progress.json 的 `models`；`runs.json.bak`、轮转 `backend.log.1-5`、`progress.json.bak` 无入口 | 不适用（属"要不要做"，见 §3） |
| R6 占用退避把事件循环睡住 | **仍存在 · 中** | [atomic.py:15-26](../backend/utils/atomic.py) 用 `time.sleep`，而这些写盘就在循环线程上同步执行并持 `progress_lock`。实测注入两次 PermissionError ⇒ 单次写 62.7 ms、同循环 10 ms 心跳最大漂移 52.8 ms；五次全失败上限 ≈620 ms | 半抓住：`test_atomic_winerror5` 断"不抛错"，不断"不卡循环" |
| R7 大书稿下轮询与落盘的成本 | **仍存在 · 中（架构项）** | 实测 27.6 MB：命中缓存的 `deepcopy` 18.8 ms（不复制下限 0.04 ms）、`_summaries_preview` 重扫 313 个文件 49 ms（缓存已命中仍要 stat 313 次）；并发 6 路尾部 119 ms。30.8 MB 全量落盘 96–122 ms，其中 indent 只值 0.4 ms。注释「deepcopy 成本约 1ms」已不真 | 没有这类门禁（无性能守卫用例） |
| R8 左右对照面板永远空白 | **仍存在 · 中** | [App.vue:1443](../src/App.vue) 不带 `name` 请求 → 列表分支只回 `{name,size}`（[status.py:316](../backend/routes/status.py)），而 :2252 渲染 `.text`。全仓 grep 只有一次调用、从不带 `name` ⇒ 我们前三轮加固的 `_read_comparison`（白名单+符号链接）没有任何 GUI 入口 | 后端分支有单测；**前端零组件测试**，所以缺陷无声 |
| R9 外壳与后端数据目录不同源 + 端口 latch 到死端口 | **仍存在 · 中** | [backend.rs:63-79/189](../src-tauri/src/backend.rs) 有 exe-dir/data 分支且 `let _ = create_dir_all` 忽略失败；Python 在同情况下**退回 %APPDATA%**（[appdir.py:44-53](../backend/utils/appdir.py)）→ 两侧算出不同路径，`backend.rs:63-66` 的注释断言是假的。`get_backend_port`(:240-254) 不看 pid/存活，`reap_orphan_backend`(:141-143) 在 pid 已死时直接 return 不删陈旧文件；前端 [backend.ts:31-32](../src/api/backend.ts) 拿到第一个 Ready 就冻结端口 | 抓不住：路径解析依赖 `current_exe`，不可测；Rust 单测只在 Windows 腿跑（且此前被 npm 红吃掉，现已恢复） |
| R10 前端对新字段/503/闸门超时无反应 | **仍存在 · 中** | [App.vue:852-1024](../src/App.vue) 的 switch 无 `gate_timeout` 分支、无 `default`；grep `gate_timeout`/`active_gate`/`waiting_seconds`/`gate_timed_out`/`chapter_detection`/`progress_error`/`503` 在 src 全部 0 命中 ⇒ progress 读不了时接口照样回 200，界面继续显示旧进度 | 抓不住（前端零测试） |
| R11 `_push_preview` 抹掉重拆期间的确认 | **仍存在 · 中（未复现）** | [pipeline_runner.py:374-380](../backend/routes/pipeline_runner.py)：`while True` 每轮开头 `split_confirm.clear()`；重拆（一次 LLM 调用）期间用户点的确认会被抹掉 → 卡到 24 h 门超时。与 ADR-005 的「先 clear 再推送」不变式相互纠缠，改动需谨慎 | 抓不住。闸门驱动只测正常确认路径 |
| R12 control 读盘失败退回旧快照 | **并入 R4** | [control.py:228-230](../backend/routes/control.py) `updated = progress` 后整份写回，同族缺陷，同一次修 | 同 R4 |
| R13 `_ESTIMATE_CACHE` 无锁却经 to_thread 使用 | **仍存在 · 低中（未复现异常）** | 实测两点：`estimate.py` 全文 0 处锁；`routes/start.py` 确实以 `asyncio.to_thread` 调估算。GIL 下多数单步原子，异常未复现 ⇒ 定为「与同仓 `_SUMMARY_CACHE`/`_progress_cache` 口径不一致」的治理项 | 抓不住 |
| X1 缓存按大小写/分隔符两种拼写占两份并喂旧数据 | **误报** | 实测反驳：Windows 的 `Path` 哈希走 casefold，两种拼写命中同一条目（缓存条目数 = 1，`_cache_bytes` = 35 = 一份） | 不适用 |
| X2 `test_atomic_winerror5` 是空守卫 | **误报** | 我用等价变异（`delays = ()`）两轮均在 Windows 上 0.15 s 内变红；它只是 POSIX 腿碰不到该竞争，属平台限定 | 它在 Windows 腿有效 |
| X3 `regression_test.py` 的 home 回退分支任何腿都跑不到 | **误报** | 我在这台 Windows 上抽掉 `APPDATA` 就跑到了：现状 exit 0；去掉 `pop` ⇒ FAIL；退回 `Path(_REAL_APPDATA)` ⇒ `Path(None)` TypeError | 该用例有判别力 |
| X4 「把 deepcopy 挪出锁」是优化 | **测量后判定不做** | 并发 3 路：现状中位 44.2 ms，锁外复制 60.5 ms（GIL 已串行化纯 Python 复制，**更差**）；并发 6 才略好。真正的收益在"不复制"，而下限 0.04 ms 说明要改的是契约不是锁位置 | 已用数字关闭，重测条件见 §3 |

## 1.5 D 阶段对抗审查的处置记录（每条都自己复验过）

| 审查意见 | 我的复验 | 处置 |
|---|---|---|
| 【成立】在 `wait_gate` 出口直接抛 `CancelledError` 会**跳过 `absorb_gate_result`**，而 `run_pipeline` 的 `finally`（[pipeline_runner.py:139-146](../backend/routes/pipeline_runner.py)）总要把内存 progress 整份写回 ⇒ 用户刚拿到 HTTP 200 的确认被静默回退，正是 `absorb_gate_result` docstring 禁止的那个坑 | 读码确认链：端点「先写盘再 set」→ 返回 → absorb → 才轮到下一个停止检查。今天不丢，改早了必丢 | **B1.1 改为「先 absorb 再抛」**，DoD 增加「磁盘必须保留刚做的选择」 |
| 【成立】停止复查若放在 `finally`，会把 `GateTimeoutError` 变成 `CancelledError`（超时分支[:131](../backend/routes/state.py)先置 `stop_requested` 再抛） | 实测：`timeout=0.05` 的 `wait_gate` 抛 `GateTimeoutError`，且此时 `stop_requested=True`；既有 `test_gate_timeout.py:73/:130` 断超时、`:97` 断外部停止 | **B1.1 钉死位置**：只放在 `while` 循环之后，`finally` 只留 `gate_exit` |
| 【成立】`shutil.move` 在目标目录已存在时**不抛错，而是把源目录嵌进去** | 实测：返回 `<dst>/tmpdir`，src 消失，dst 顶层 `['old.txt','tmpdir']` | **B1.3 的成功判据改为「返回值/目录内容等于新内容」**，禁止用 `backup_dir.exists()` 当证据 |
| 【成立】`snapshot()` 失败只 warning 返回 None ⇒ `backup_dir` 成了唯一副本；且 `finally` 的 `rmtree(tmp)`（[:121-122](../backend/core/versions.py)）没带 `ignore_errors`，一个删不掉的 `._restore_tmp` 会让以后每次恢复都进不去 | 读码确认 + `_prune`(:60-74) 会把 `_versions/` 下**所有**目录纳入「保留 N 个」⇒ 不能把救急副本停进 `_versions/` | **B1.3 补三层**：`cur_name is None` 时永不删 backup；回滚本身 try/except 并报出存活绝对路径；临时名带时间戳且清理 `ignore_errors` |
| 【部分成立→已修正】「诊断包 `redact(summary)` 会把 `prompt_tokens` 遮成 `***`」 | 全仓 grep `redact` 只有 6 处调用（history×3、`_cfg_brief`、`models`、status 的 config_drift），**没有任何路径把 `usage` 交给 redact** | 结论不成立，但**陷阱是真的**：`redact()` 是「键命中即 `***`」（[redact.py:46](../backend/utils/redact.py)）⇒ 泛词加宽必须走「键名泛词 + 值形状」联合判定，且不改变 `_is_sensitive_key(k)` 的**单参契约**（[_fingerprint 在 pipeline_runner.py:238 就是单参调用](../backend/routes/pipeline_runner.py)） |
| 【成立】反向用例里的 `token_count` 键在本仓不存在，是我编的 | grep 无此键 | 反向夹具改用真实键：`prompt_tokens`(int)、`completion_tokens`(int)、`tokens`(dict) |
| 【成立】「紧凑 + 保留 fsync」不能靠现有 `pretty=False`（它把 fsync 一起去掉，[atomic.py:49-55](../backend/utils/atomic.py)） | 读码确认 | **B2.3 明确：先解耦 `sync` 参数，再谈序列化口径** |
| 【成立】root 上根本没有 handler（logging_setup.py 只 `addFilter`），而 [main.py:60-63](../backend/main.py) 是「先 import app（setup_logging 已跑完）→ 再 `Config/Server` 触发 dictConfig」⇒ 导入期挂不到 uvicorn 的 handler | 读码确认顺序 | **B1.5 挂点改到 FastAPI lifespan 启动段** |
| 【采纳】e2e 的单一闸门驱动只确认、从不在闸门处按停止（`/api/stop` 只在 done 之后出现一次）⇒ 这条改动没有任何端到端覆盖 | 读码确认 | 批 3 顺带给 e2e 加一个「停在验收门」的场景：断 `stopped` 事件 + `current_phase` 仍为 `phase3_accept` + 刷新后 `_build_resume` 仍能弹回来（这正是今天的用户可见损失：过了门才停 ⇒ 恢复对话框再也回不来） |

## 2. 方案（批次 / DoD / 反向断言）

> 每条都要求：**新用例在改动前必红、改动后必绿**；单文件改动 ≤ 原字节 ×150%；CRLF 文件保持 CRLF。

### 批 1 —— 安全语义，一行到十几行（先做）

| # | 改动 | DoD | 反向断言 |
|---|---|---|---|
| B1.1 | `wait_gate` 增加 `absorb_keys: tuple = ()`：**在 `while` 之后**（不在 `finally`）复查 `stop_requested`；若已置位则先 `await absorb_gate_result(progress, progress_path, list(absorb_keys))` 再抛 `CancelledError`。6 个调用点各自把原来写在下一行的 keys 传进参数（split 门无 absorb，传空）。`finally` 仍只做 `gate_exit` | ①「事件已 set + stop」⇒ 抛 CancelledError 且**磁盘上刚做的选择仍在**；②超时路径仍抛 `GateTimeoutError`（`test_gate_timeout.py:73/:130` 不得变红）；③外部停止路径 `:97` 仍绿 | 三面包围：把出口复查删掉 ⇒ 断言①红；把复查挪进 `finally` ⇒ 断言②红；把 absorb 挪到抛错之后 ⇒ 断言①的"磁盘仍保留"红 |
| B1.2 | `ACCEPT_ABSORB_KEYS` 增加 `regen_count` | 磁盘 `regen_count` 在验收门之后仍在；原 3 键行为不变 | 去掉该键 ⇒ 用例红（复刻 R3 实测：`{'5': 4}` → `None`） |
| B1.3 | `versions.restore` 三层改：①成功判据 = `shutil.move` 返回值且 `03_final` 内容等于 tmp（**不能用 exists 当证据**）；②只有替换成功才删 `backup_dir`，`cur_name is None`（快照失败）时**永不删**、把它停下并在返回值/异常里报出绝对路径；③回滚本身 try/except，失败时抛「成品停在 <path>」；临时名改 `._restore_tmp_<ts>` / `._restore_old_<ts>`，预清理带 `ignore_errors`；救急副本**不停进 `_versions/`**（`_prune` 会把它当旧版本删掉），而是把 `._restore_old*` 前缀加进 `routes/output.py` 的 `ARTIFACTS`，让「清空输出目录」能收口 | 注入「第二次 move 抛 PermissionError」⇒ `03_final` 内容 == 改动前；注入「`03_final` 被别的写者重建」⇒ 断言内容是新版而不是嵌套目录；快照失败路径 ⇒ 救急副本仍在且路径被报出 | 还原现有 `finally` ⇒ 第一条红；把判据改回 `exists()` ⇒ 嵌套那条红 |
| B1.4 | `history.upsert` 写 `last_error` 前 `_mask_text`；`/api/status` 的 `error_logs` 走 `redact()`（读侧兜底覆盖历史残留） | 明文 `api_key=sk-live-…` 进 runs.json / status 响应后为 `***`；既有断言（`test_pipeline_recovery.py:165`「最后一条错」、`regression_test.py:792`「炸了」、`:618`「boom-injected」）全部**不受影响**——我已实测这些文本不含凭据形状 | 去掉任一处 redact ⇒ 用例红（写侧 + 读侧两面都钉）；顺带写明：读侧遮罩后若再 upsert 会把遮好的值写回，属可接受自愈 |
| B1.5 | 把 `SensitiveFilter` 挂到**handler** 上：挂点从 `setup_logging()` 移到 FastAPI **lifespan 启动段**（`main.py:60-63` 是「先 import app、后 `Config/Server` 触发 dictConfig」，导入期挂不到 uvicorn 的 handler），对 `uvicorn.error`/`uvicorn.access`/`httpx` 的每个 handler 各挂一份；root logger 那份保留 | `logging.getLogger("uvicorn.error").warning("api_key=sk-live…")` 经捕获 handler 后为 `***`；`textforge` 自身日志不双份 | 只在 import 期挂 ⇒ 新用例红（正是现状） |
| B1.6 | `_ESTIMATE_CACHE` 加 `threading.Lock`（取/放/淘汰同锁），并把 `move_to_end` 对「已被别的线程淘汰的键」做成 try/except KeyError | 并发预估不再可能抛 KeyError（真实失败模式：缓存满 16 时两线程互踩 `move_to_end`）。**无「回退即红」断言**：GIL 下缺锁只是概率性失败，概率断言我不写进门禁 | 不适用（治理项，实现自证 + 锁存在性断言） |

### 批 2 —— 合并写与脱敏判定（需要两面断言，设计先行）

| # | 改动 | DoD | 反向断言 |
|---|---|---|---|
| B2.1 | 新增 `merge_progress_write(progress_path, changes)`：写盘前重读磁盘、只并入本次真正改动的键；`regen_single_chapter` 与 `control.py` 的 regen 计数改走它（R4/R12 一并收口） | 两个 regen 并发（stub 掉 LLM 调用）后两章的 QC 结论都在；写盘失败时不再回写旧快照 | 退回「读一次 + 整份写」⇒ 并发用例红 |
| B2.2 | 脱敏判定拆成两层：**精确键名**（`apikey/token/password/authorization/secret/accesstoken/secretkey`）无条件遮；**泛词子串**（新增 `token`/`secret`/`credential`）只在「值是字符串、`strip()` 后长度 ≥16、无空白、匹配 `[A-Za-z0-9_:.\-]+` 或以 `sk-/ghp-/AIza/xox` 开头」时遮。`_is_sensitive_key(k)` **保持单参语义**（`_fingerprint` 就是单参调用，改签名会让它退化成不哈希 ⇒ 明文进指纹）；值形状判定放在 `redact()` 的键分支里 | 正向：`refresh_token`/`client_secret`/`?key=sk-live-…` 被遮；**带尾随换行的全局 api_key 仍被遮**（精确键无条件，App.vue:1113 未 trim 这条真实路径我查过）。反向不得误伤（用**本仓真实存在的键**）：`usage.prompt_tokens=1200`、`completion_tokens`、estimate 的 `tokens` dict、散文 `"每章 token 数量上限 1200"`、`"禁止出现 secret identity 设定"` 原样 | 只放宽键名不管值形状 ⇒ int/dict 反向用例红；把 `_is_sensitive_key` 改成双参 ⇒ `_fingerprint` 用例红（复刻 `test_config_drift_fingerprint.py`） |
| B2.3 | **先解耦**：`atomic_write_json(path, data, *, pretty=True, sync=True)`，`pretty` 只管缩进、`sync` 只管 fsync（现状是 `pretty=False` 把 fsync 一起去掉）；随后 progress 写盘走 `pretty=False, sync=True` | 30 MB 上单次写耗时下降 ≥15%（实测 indent 本身只值 0.4 ms，收益来自更小字符串与写盘量），且 fsync 仍在 ⇒ 掉电语义不变。已确认 `progress.json` 无任何人类可读依赖（文档把它当机器状态，测试一律 `json.loads`，ADR-004 只断「心跳不落盘」） | 把 `sync` 默认改成 False ⇒ 新增的 fsync 断言红 |

### 批 3 —— 前端可达性（必须先补最小 vitest，否则同类缺陷还会无声存活）

| # | 改动 | DoD |
|---|---|---|
| B3.1 | 对照面板：选中项后带 `name` 再取正文（两次请求），列表项类型改 `{name,size}` | vitest：mock fetch 断言两次调用且 `text` 渲染出内容；删掉第二次调用 ⇒ 红 |
| B3.2 | switch 补 `gate_timeout`/`split_ai_start` 分支 + `default`；`fetchStatus` 消费 `progress_error`；`checkRes` 对 503 给「文件被占用，稍后自动重试」并做一次退避重试 | vitest：闸门超时事件渲染出横幅；503 走重试路径；删 case ⇒ 红 |

### 批 4 —— 外壳（`R9`，含可测性重构）

B4.1 Rust 侧 `create_dir_all` 失败 ⇒ **不注入** `TEXTFORGE_DATA_DIR`，并把 exe-dir 分支改为「仅当可创建」；B4.2 `get_backend_port` 复用同文件已有的 `probe_port` 做存活探测，`reap_orphan_backend` 在 pid 已死时删除陈旧 `port.json`；B4.3 把路径解析抽成不依赖 `current_exe` 的纯函数以便 `cargo test`（三端腿都能跑）。

## 3. 本轮明确不做

- **状态拆分 / 单写者队列**（R6+R7 的正解）：需要改 `progress_lock` 的所有权模型，属架构项，需拍板后单开一轮；本轮只落 B2.3 这种不动模型的半步。**重测条件**：若 `progress.json` 实际尺寸分布中位数 > 30 MB，或 `/api/status` p99 > 200 ms，必须启动。
- **"deepcopy 移出锁"**：X4 实测更差，禁止做。
- **历史残留文件清扫**（R5d）：会删用户 `logs/backend.log.*`、`runs.json.bak`，属数据处置，需产品拍板；若做，必须先给出「将删除哪些文件、可否撤销」的界面确认。
- **对照列表返回正文**：退回载荷膨胀问题，与既有按需加载设计冲突（改前端，见 B3.1）。

## 4. 还有什么没查（收口时接着查）

1. 五路审计只覆盖了后端 IO/并发/密钥/测试/外壳契约；**流水线算法层**（`stitch` 面积、`budget` 预算、`quality` 阈值来源）未审。
2. `dist/backend/_internal` 里 vendored 的 fastapi/h11 源码含 TODO/FIXME，属第三方代码，本轮未判定版本是否落后上游。
3. release.yml 只出 Windows：macOS `.dmg` 的 5 条手工验收长期无人代跑。
4. 前端只有 4 个 vitest 文件（`src/*.test.ts`），界面态（弹窗、闸门、进度条）无任何自动断言——本轮 B3 是最小补齐，不是补齐完毕。
5. e2e 走本地 HTTP mock LLM，**TLS 路径与真实模型错误文本**（正是 R5b 的来源）从未被真实覆盖。
