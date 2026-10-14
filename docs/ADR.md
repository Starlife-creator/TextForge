# 架构决策记录（ADR）

记录**为什么**这样选。这类理由最容易随时间丢失，而它们恰恰是最难重新推导的。
每条都附上当时的实测数据 —— 没有数据的决策在未来很容易被盲目推翻。

---

## ADR-001：sidecar 用 PyInstaller onedir，不用 onefile

**背景**：后端要打包成单个 exe 随应用分发（Tauri 的 `externalBin` 要求单文件）。

**实测对比**：

| | onefile | onedir（采用） |
|---|---|---|
| 启动到接口可用 | 2.5 – 2.8s | **0.65 – 0.69s** |
| `%TEMP%` 临时目录 | 1 个（约 40MB） | **0 个** |
| 进程数 | 2（引导进程 + Python 子进程） | **1** |

**决策**：onedir。

**理由**：
1. onefile 每次运行都要把压缩载荷解压到 `%TEMP%\_MEIxxxxxx`。
2. 它是父子双进程，`child.kill()` 只杀得掉父引导进程，Python 子进程会变成孤儿
   并残留 `_MEI` 目录 —— 这个 bug 真实发生过（见 ADR-003）。
3. 「运行时自解压到临时目录」正是恶意软件 dropper 的典型行为，是 Defender
   把本程序误报为 `Trojan:Win32/Wacatac.B!ml` 的**主要特征**。
4. 长期看，持续写 `%TEMP%` 对 SSD 寿命也是无谓损耗。

**如何绕开 Tauri 的单文件约束**：仍产出那个单文件占位 exe 供 `tauri build`
校验；真正的便携包由 `release.yml` / `build_sidecar.bat` **手工组装** ——
把 `backend.exe` 与 `_internal\` 平铺到同一目录即可（onedir 的 exe 正是从
**自身所在目录**的 `_internal` 找依赖）。

**验证方式**：把 `_internal` 移走后确实启动失败，证明它真在被使用。

---

## ADR-002：应用数据目录由外壳注入，后端只认环境变量

**背景**：历史、日志、端口文件需要在程序目录之外（Program Files 只读），
但便携包又希望「解压到哪、数据就跟到哪」。

**早期做法**：`%APPDATA%\TextForge` 在 4 个 Python 模块里各自拼接，
**Rust 外壳侧还把同一套逻辑又抄了一遍**。

**问题**：两侧一旦不一致，前端就永远读不到 `port.json`，表现为「一直连不上后端」。

**决策**：新增 `backend/utils/appdir.py` 作为唯一数据源，优先级

```
TEXTFORGE_DATA_DIR（外壳注入） > exe 旁的 data/ > %APPDATA%\TextForge
```

外壳解析后经**环境变量**注入 sidecar；后端只认这个变量，认不到才回退。

**要点**：默认仍是 `%APPDATA%`（不靠「猜测 exe 目录是否可写」），否则单元测试
会把数据写进 `venv/Scripts`，彻底搞乱测试隔离。

---

## ADR-003：本地 API 必须鉴权

**背景**：后端绑 `127.0.0.1` + 随机端口。此前**没有任何调用方验证**。

**风险**：任何本地进程读一下 `data/port.json` 就能直接调接口 ——
`/api/start` 可指定任意 `input_path` / `output_path`，以当前用户权限读写磁盘任意位置；
`/api/system/open` 可 `os.startfile` 打开任意路径。
浏览器里任意网页也可能打过来（`text/plain` 这类简单请求不触发 CORS 预检，
请求会照常执行，只是读不到响应）。

**决策**：外壳用 CSPRNG 生成 token，经环境变量注入 sidecar，前端只经 Tauri IPC 取得。

**关键点：token 从不落盘。** 落盘等于公开 —— 任何本地进程都读得到 `port.json`。
存在进程内存 + 子进程环境变量里。

**实现注意**：
- 中间件用**纯 ASGI**，不能用 `BaseHTTPMiddleware` —— 后者会缓冲响应体，
  直接破坏 SSE 长连接（`/api/events`）。
- `/api/health` 豁免（不返回任何数据，供探活）。
- 未配置 token 时放行，否则单元测试与 e2e 会被 401 拦死；生产路径上外壳必定注入。
- **外壳自己发起的 HTTP 调用也必须带 token**。曾漏带导致关窗时的 `/api/stop`
  与 `/api/system/shutdown` 被 401 静默拒绝，优雅退出退化成强杀，
  ADR-001 里修掉的孤儿进程问题原样回归。且这条路径 e2e 覆盖不到
  （e2e 直接起 `python main.py`，不经外壳、不注入 token）。

---

## ADR-004：心跳只写内存，不落盘

**背景**：原先心跳每 15s 把**整个** `progress.json` 读出来、改一个字段、
再全量写回 + fsync。

**问题**：`progress.json` 在缝合阶段可达数十 MB（锚点内嵌全文）。
长时段运行下，每 15s 一次全量重写就是实打实的写放大，
同时还要每 15s 解析 + 序列化几十 MB JSON。

**决策**：心跳时间戳改为只维护 `state.last_heartbeat`（内存），
`/api/status` 优先返回内存值。

**理由**：`last_heartbeat` 的唯一用途是前端判断「是否僵死」
（`pipeline_running` 且心跳超 90s 报警），**不参与任何恢复决策**，因此完全不必落盘。
界面拿到的是内存值，反而更准（没有 fsync 延迟）。

**实测**：e2e 场景写入量 863KB → 406KB；真实长跑场景下这部分归零。

---

## ADR-005：闸门标志的 clear 必须早于阶段落盘

**背景**：蓝图门与验收门原先都是：

```python
progress['current_phase'] = 'phase2_waiting'
atomic_write_json(progress_path, progress)   # 此刻界面已弹框、resume 已就绪
state.blueprint_confirmed.clear()            # 把刚到的确认抹掉
```

**问题**：存在一个窗口 —— progress 已是闸门阶段（界面弹框、`/api/status` 的
resume 已就绪），而标志还没 clear。此刻到达的确认会被紧接着的 `clear()`
**静默丢弃**：流水线随后永远等不到，界面却显示「已确认」，**无任何报错**。
刷新后由 resume 路径弹出的弹窗正好走这个窗口，所以这是真实产品缺陷。

**决策**：先 `clear()` 标志，再落盘阶段。这样「闸门可见」之后到达的任何确认
都不会再被清掉。qc 门本来就是先 clear 再等，无需改动。

---

## ADR-006：免安装便携 zip，不再发 NSIS 安装包

**背景**：v0.4.0 的 NSIS 安装包被 Defender 隔离并删除。

**诊断**：威胁名 `Trojan:Win32/Wacatac.B!ml` —— 末尾 `!ml` 表示**机器学习泛化判定**，
不是病毒特征码命中，属误报。

**定位元凶**：实测把 25MB 的 PyInstaller sidecar 单独扫是**干净**的；
NSIS 把这份高熵载荷再包一层，触发启发式评分。

**决策**：`bundle.active=false`，发布手工组装的便携 zip。
无安装壳、无注册表、无 Program Files 写入。

**未做的**：代码签名。没有数字签名意味着 Defender 仍可能误报，
SmartScreen 也无法消除。根治需要 EV 证书或 Azure Trusted Signing（开源项目免费）。
ADR-001 去掉自解压后误报概率已大幅降低，但不是零。

---

## ADR-007：测试用「单一全局闸门驱动」

**背景**：e2e 要像真实用户一样点掉所有人工闸门。最初每个场景各起一个驱动线程。

**问题**：所有驱动轮询**同一个后端**，彼此看不见对方在干什么，导致两类反复出现的
偶发失败：

1. 旧场景的驱动替新场景确认了闸门 —— 场景七「重拆未生效，拆书预览已被确认」，
   流水线提前冲进 phase1。
2. 去重键跨 run 复用 —— 上一轮记下的 `blueprint_ready` 把下一轮的蓝图闸门
   当成已处理而跳过，流水线无限卡在 `phase2_waiting`。

**决策**：改为**单一全局驱动**，以 `/api/status` 的 `run_id` 划分轮次，
换 run 即清空去重状态（且必须在处理事件流**之前**完成，否则新 run 的闸门事件
会拿上一轮的记录比对而被跳过）。

**另一条纪律**：确认闸门时**不要带 `run_id`**。`_check_run_id` 只在「传了且不匹配」
时拒绝，而 `/api/status` 的 `run_id` 与 `current_phase` 都来自定期落盘的
`progress.json`，在闸门处会滞后 —— 带着滞后的 id 去确认会被 409 静默吞掉。
后端同一时刻只可能有一个 run，交给它用 `state.current_run_id` 判定即可。