<script setup lang="ts">
import { ref, reactive, computed, watch, onMounted, onUnmounted } from 'vue';
import { open } from '@tauri-apps/plugin-dialog';
import { isTauri } from '@tauri-apps/api/core';
import { waitForPort } from './api/backend';
import { connectSSE, disconnectSSE, type SSEHandlers } from './api/sse';
import SettingsModal from './components/SettingsModal.vue';
import GateModal from './components/GateModal.vue';
import RunStats from './components/RunStats.vue';
import { apiFetch, initAuth } from './api/client';
import {
  fetchHistory,
  forgetHistory,
  fetchVersions,
  restoreVersion,
  type HistoryItem,
  type VersionInfo,
} from './api/system';

// ---------- 后端连接 ----------
const port = ref<number | null>(null);
const backendReady = ref(false);
/** SSE 已连续失败次数（>0 表示正在退避重连）；用于把「瞬时抖动」和「真断了」区分开 */
const sseAttempts = ref(0);
const errorMsg = ref('');
// 运行计时
const startedAt = ref<number | null>(null);
const elapsed = ref('00:00');
let elapsedTimer: number | null = null;
const inTauri = isTauri();
let statusTimer: number | null = null;
// 后端设置面板
const showSettings = ref(false);
const sseHandlers: SSEHandlers = {
  onEvent: (e, p) => handleSSEEvent(e, p),
  onStatus: (s) => {
    backendReady.value = s.connected;
    sseAttempts.value = s.attempt;
    // 只有连续失败到第 3 次才提示，避免一次网络抖动就弹红字
    errorMsg.value = s.connected
      ? ''
      : s.attempt >= 3
        ? `后端连接断开，正在自动重连（第 ${s.attempt} 次）`
        : '';
  },
};

// ---------- 输入配置 ----------
const inputMode = ref<'single_file' | 'multi_file'>('single_file');
const inputPath = ref('');
const outputPath = ref('');
const inputFormat = ref<'docx' | 'txt' | 'md'>('txt');
const apiUrl = ref('');
const apiKey = ref('');
const model = ref('');
const contextWindow = ref(8000);
const authorStyle = ref('出版级文学重构，文笔凝练，注重画面感');
const novelName = ref('');
const refactorMode = ref<'full_rewrite' | 'fidelity' | 'fix_gaps' | 'reskin'>('full_rewrite');
// 重构保护门（后端 refactor_gates，默认全关）
const gates = reactive({
  lock_names: false,
  lock_plot: false,
  inject_forbidden: false,
  require_chapter_accept: false,
  skip_stitch_if_smooth: false,
  qc_block_export: false,
});
// 逐章验收弹窗
const showAccept = ref(false);
const acceptBatch = ref<number | null>(null);
const acceptChapters = ref<any[]>([]);
const confirmingAccept = ref(false);
const regenerating = ref<Set<string>>(new Set());
// 质检拦截决策弹窗
const showQcBlock = ref(false);
const qcIssues = ref<string[]>([]);
const forcingExport = ref(false);
const proxy = ref('');
const richText = ref(false);
// HTTPS 证书校验：后端 StartRequest.ssl_verify 必须真正收到这个值，
// 否则设置面板里那个开关只影响自检、实际运行恒为 True
const sslVerify = ref(true);
// 各阶段温度（高级项，对应后端 Temperatures）
const tDiagnose = ref(0.3);
const tBlueprint = ref(0.5);
const tRefactor = ref(0.8);
const tStitch = ref(0.6);
// P4.1 分阶段模型（可选，留空则用全局模型名）
const mDiagnose = ref('');
const mBlueprint = ref('');
const mRefactor = ref('');
const mStitch = ref('');
// A2：各阶段可选独立 API 地址与密钥（留空则用全局端点）
const uDiagnose = ref('');
const uBlueprint = ref('');
const uRefactor = ref('');
const uStitch = ref('');
// A2 逃生口：分阶段端点换了主机时后端会丢弃全局 Key（防泄露），
// 此时必须有一个能单独填密钥的地方，否则该配置无解、直接 401。
const kDiagnose = ref('');
const kBlueprint = ref('');
const kRefactor = ref('');
const kStitch = ref('');
// 禁改清单（inject_forbidden 门用，每行一条）+ reskin 人名映射（每行 旧名=新名）
const forbiddenCanon = ref('');
const nameMapText = ref('');
// B1 批次间隔（秒）
const batchInterval = ref(2.0);
// B2 服务商预设（温度/上下文/批次间隔一键套用，纯前端）
const preset = ref('通用');
const PRESETS: Record<string, { temps: number[]; ctx: number; interval: number }> = {
  通用: { temps: [0.3, 0.5, 0.8, 0.6], ctx: 8000, interval: 2.0 },
  快速低保真: { temps: [0.2, 0.3, 0.7, 0.5], ctx: 8000, interval: 1.0 },
  '创作·宽松': { temps: [0.5, 0.7, 1.0, 0.8], ctx: 8000, interval: 2.5 },
};
function applyPreset() {
  const p = PRESETS[preset.value] || PRESETS['通用'];
  [tDiagnose.value, tBlueprint.value, tRefactor.value, tStitch.value] = p.temps;
  contextWindow.value = p.ctx;
  batchInterval.value = p.interval;
}
// A2：构建 models 字典——仅模型则为字符串；配了独立端点则为 {model, api_url[, api_key]}
function buildModels(): Record<
  string,
  string | { model: string; api_url: string; api_key?: string }
> {
  const out: Record<string, string | { model: string; api_url: string; api_key?: string }> = {};
  const put = (ph: string, m: string, u: string, k: string) => {
    if (!m || !m.trim()) return;
    if (!u || !u.trim()) {
      out[ph] = m.trim();
      return;
    }
    const entry: { model: string; api_url: string; api_key?: string } = {
      model: m.trim(),
      api_url: u.trim(),
    };
    if (k && k.trim()) entry.api_key = k.trim();
    out[ph] = entry;
  };
  put('diagnose', mDiagnose.value, uDiagnose.value, kDiagnose.value);
  put('blueprint', mBlueprint.value, uBlueprint.value, kBlueprint.value);
  put('refactor', mRefactor.value, uRefactor.value, kRefactor.value);
  put('stitch', mStitch.value, uStitch.value, kStitch.value);
  return out;
}
// C4 成品文档直开
const finalFiles = ref<{ name: string; path: string }[]>([]);
// C1 左右对照预览
const showComparisons = ref(false);
const comparisonItems = ref<{ name: string; text: string }[]>([]);
const comparisonSel = ref('');

// ---------- 运行状态 ----------
const runId = ref<string | null>(null);
const pipelineRunning = ref(false);
const isPaused = ref(false);
// 「已提交、等后端确认」窗口：/api/start 返回前 pipelineRunning 仍是 false，
// 连点两次「开始重构」会先后发出两个 run。用它把按钮立刻锁住。
const starting = ref(false);
const currentPhase = ref('');
type LogEntry = { ts: string; text: string; level: 'info' | 'ok' | 'warn' | 'err' | 'accent' };
const eventLog = ref<LogEntry[]>([]);
const logBoxRef = ref<HTMLElement | null>(null);
const totalChapters = ref(0);
const blueprintText = ref('');
const showBlueprint = ref(false);
// P0.5：诊断摘要预览（可开关查看本步蓝图摘要）
const summaries = ref<any[]>([]);
const showSummaries = ref(false);
// P0.3：拆书预览确认门
const reviewSplit = ref(false);
const aiSplit = ref(true);
const compareOutput = ref(false);
const chapterSnapshots = ref(false);
const diagnoseJson = ref('');
const showSplit = ref(false);
const splitChapters = ref<any[]>([]);
const splitDetection = ref<any | null>(null);
const confirmingSplit = ref(false);
// fix_gaps 闭环：断层勾选弹窗
const showFixReview = ref(false);
const fixGaps = ref<any[]>([]);
const fixSelected = ref<string[]>([]);
const confirmingFix = ref(false);
// A: 操作防重复标志
const confirmingBlueprint = ref(false);
const controlling = ref(false);
// #2 流式增量预览（最近 500 字）
const streamingText = ref('');
// 流式增量按帧合并。每个 SSE chunk 都直接写 streamingText 会让整个 App
// 重渲染（模板在 App 作用域内），高频输出下明显卡顿；攒到下一帧一次性刷新。
let streamBuf = '';
let streamRaf = 0;
function flushStream() {
  streamRaf = 0;
  if (!streamBuf) return;
  streamingText.value = (streamingText.value + streamBuf).slice(-500);
  streamBuf = '';
}

// 统计与提示
const usageTokens = ref({ prompt: 0, completion: 0 });
const batchStats = ref({ total: 0, phase1: 0, phase3: 0 });
const chapterProgress = ref({ total: 0, done: 0 });
const chapterRows = ref<{ id: string; label: string; status: string }[]>([]);
const showChapterTree = ref(false);
const topError = ref<string | null>(null);
// 轮询已点亮的最后一条错误，用于避免同一错误被每个轮询周期重复点亮。
let lastPolledError: string | null = null;
const topTip = ref<string | null>(null);
// B4：僵尸运行检测
const zombieWarn = ref(false);
const darkMode = ref(false);
function toggleTheme() {
  darkMode.value = !darkMode.value;
  document.documentElement.classList.toggle('dark', darkMode.value);
  persist();
}

// C2：本地持久化记忆设置项
const PERSIST_KEY = 'textforge_prefs_v1';
function persist() {
  try {
    localStorage.setItem(
      PERSIST_KEY,
      JSON.stringify({
        apiUrl: apiUrl.value,
        model: model.value,
        contextWindow: contextWindow.value,
        authorStyle: authorStyle.value,
        refactorMode: refactorMode.value,
        batchInterval: batchInterval.value,
        darkMode: darkMode.value,
        forbiddenCanon: forbiddenCanon.value,
        nameMapText: nameMapText.value,
        outputPath: outputPath.value,
        inputPath: inputPath.value,
        aiSplit: aiSplit.value,
        // inputMode / inputFormat / novelName 必须一起存：后端 paths.py 会硬校验
        // 「声明格式 == 文件扩展名」，只存 inputPath 会让重开后的默认 txt + .docx 直接 422
        inputMode: inputMode.value,
        inputFormat: inputFormat.value,
        novelName: novelName.value,
        sslVerify: sslVerify.value,
        tDiagnose: tDiagnose.value,
        tBlueprint: tBlueprint.value,
        tRefactor: tRefactor.value,
        tStitch: tStitch.value,
        proxy: proxy.value,
        richText: richText.value,
        // 刻意不持久化 apiKey：后端全链路约定「Key 仅存内存、不落盘、不进日志」
        // （api_client.make_httpx_client / routes/state.current_req），明文写进
        // WebView2 localStorage 会打破这个不变量，也会让设置面板上
        // 「仅存内存不落盘」的提示变成假话。
      }),
    );
  } catch {
    /* 忽略持久化失败 */
  }
}
function restore() {
  try {
    const raw = localStorage.getItem(PERSIST_KEY);
    if (!raw) return;
    const p = JSON.parse(raw);
    if (typeof p.apiUrl === 'string') apiUrl.value = p.apiUrl;
    if (typeof p.model === 'string') model.value = p.model;
    if (typeof p.contextWindow === 'number') contextWindow.value = p.contextWindow;
    if (typeof p.authorStyle === 'string') authorStyle.value = p.authorStyle;
    // 白名单校验：localStorage 可能存着旧版/损坏的值，直接赋值会让
    // /api/start 收到非法 refactor_mode 而 422（提示还很难懂）。
    if (['full_rewrite', 'fidelity', 'fix_gaps', 'reskin'].includes(p.refactorMode))
      refactorMode.value = p.refactorMode;
    if (typeof p.batchInterval === 'number') batchInterval.value = p.batchInterval;
    if (typeof p.darkMode === 'boolean') {
      darkMode.value = p.darkMode;
      document.documentElement.classList.toggle('dark', p.darkMode);
    }
    if (typeof p.forbiddenCanon === 'string') forbiddenCanon.value = p.forbiddenCanon;
    if (typeof p.nameMapText === 'string') nameMapText.value = p.nameMapText;
    if (typeof p.outputPath === 'string') outputPath.value = p.outputPath;
    if (typeof p.inputPath === 'string') inputPath.value = p.inputPath;
    if (p.inputMode === 'single_file' || p.inputMode === 'multi_file')
      inputMode.value = p.inputMode;
    if (['txt', 'md', 'docx'].includes(p.inputFormat)) inputFormat.value = p.inputFormat;
    if (typeof p.aiSplit === 'boolean') aiSplit.value = p.aiSplit;
    if (typeof p.novelName === 'string') novelName.value = p.novelName;
    if (typeof p.sslVerify === 'boolean') sslVerify.value = p.sslVerify;
    if (typeof p.proxy === 'string') proxy.value = p.proxy;
    if (typeof p.richText === 'boolean') richText.value = p.richText;
    // 不恢复 apiKey：见 persist() 处的说明（凭据不落盘）
    for (const [k, ref] of [
      ['tDiagnose', tDiagnose],
      ['tBlueprint', tBlueprint],
      ['tRefactor', tRefactor],
      ['tStitch', tStitch],
    ] as const) {
      if (typeof p[k] === 'number') ref.value = p[k];
    }
  } catch {
    /* 忽略损坏数据 */
  }
}

// C3：键盘快捷键（Space 暂停/恢复，Enter 确认当前弹窗）；输入组件内不触发
function onKeydown(e: KeyboardEvent) {
  const t = e.target as HTMLElement | null;
  const typing =
    t &&
    (t.tagName === 'INPUT' ||
      t.tagName === 'TEXTAREA' ||
      t.tagName === 'SELECT' ||
      t.isContentEditable);
  if (typing) return;
  if (e.code === 'Space' && pipelineRunning.value && !e.repeat) {
    e.preventDefault();
    control(isPaused.value ? 'resume' : 'pause');
  }
  if (e.key === 'Enter' && !e.repeat) {
    if (showSplit.value) {
      confirmSplit();
      return;
    }
    if (showFixReview.value) {
      confirmFix();
      return;
    }
    if (showAccept.value) {
      confirmAccept();
      return;
    }
    if (showQcBlock.value) {
      forceExport();
      return;
    }
  }
}

const phaseLabel: Record<string, string> = {
  phase0: '阶段0：拆书',
  phase0_confirm: '阶段0：等待拆书确认',
  phase1: '阶段1：诊断',
  phase1_fixreview: '阶段1：等待断层勾选',
  phase2: '阶段2：蓝图生成',
  phase2_waiting: '阶段2：等待蓝图确认',
  phase3_accept: '阶段3：等待逐章验收',
  phase3_qc: '阶段3：等待质检决策',
  'phase3-1': '阶段3-1：重构',
  phase4: '阶段4：收尾',
};

const logRev = ref(0);
function addLog(msg: string, level: LogEntry['level'] = 'info') {
  const ts = new Date().toLocaleTimeString();
  eventLog.value.push({ ts, text: msg, level });
  if (eventLog.value.length > 300) eventLog.value.shift();
  logRev.value++; // 通知自动滚动 watch，免去对 300 条对象的 deep 遍历
}

const diagPercent = computed(() => {
  const t = batchStats.value.total;
  return t ? `${Math.round((batchStats.value.phase1 / t) * 100)}%` : '0%';
});
const reconPercent = computed(() => {
  const t = batchStats.value.total;
  return t ? `${Math.round((batchStats.value.phase3 / t) * 100)}%` : '0%';
});
const chapterPercent = computed(() => {
  const t = chapterProgress.value.total;
  return t ? `${Math.round((chapterProgress.value.done / t) * 100)}%` : '0%';
});

// 新日志自动滚到底部。用递增的 logRev 作触发源，而非 deep watch：
// deep 每次 addLog 都要遍历整份日志数组；改用 length 也不行 —— 封顶后
// shift 掉一条长度不变，watch 便不再触发。flush:'post' 等 DOM 更新后再滚。
watch(
  logRev,
  () => {
    const el = logBoxRef.value;
    if (el) el.scrollTop = el.scrollHeight;
  },
  { flush: 'post' },
);

onMounted(async () => {
  restore();
  window.addEventListener('keydown', onKeydown);
  void connectBackend();
});

/** 建立后端连接：取令牌 → 拿端口 → 接 SSE → 拉状态 → 起轮询。失败也让用户能进设置面板自救。 */
async function connectBackend() {
  try {
    // 必须最先取：后端 release 模式要求每个请求带 X-TextForge-Token，
    // 晚于第一个 fetch 获取就会全部 401（表现为界面一片空白）。
    await initAuth();
    port.value = await waitForPort();
    connectSSE(port.value, sseHandlers);
    // 页面刷新/SSE 晚连接时，从后端恢复 UI 状态（暂停态、待确认蓝图、用量等）
    await fetchStatus();
    startStatusPolling();
    refreshHistory(); // 任务历史（跨会话）
  } catch (e: any) {
    errorMsg.value = e.message || String(e);
    backendReady.value = false;
  }
}

/** 设置面板里的「重新连接」：重置退避计数，重新走一遍握手。 */
function reconnectBackend() {
  disconnectSSE();
  backendReady.value = false;
  sseAttempts.value = 0;
  errorMsg.value = '正在重新连接后端…';
  void connectBackend();
}

onUnmounted(() => {
  disconnectSSE();
  stopStatusPolling();
  stopElapsed();
  window.removeEventListener('keydown', onKeydown);
  if (outputPathTimer) clearTimeout(outputPathTimer);
  if (persistTimer) clearTimeout(persistTimer);
  if (streamRaf) cancelAnimationFrame(streamRaf);
  persist();
});

function startStatusPolling() {
  stopStatusPolling();
  statusTimer = window.setInterval(fetchStatus, 5000);
}
function stopStatusPolling() {
  if (statusTimer !== null) {
    clearInterval(statusTimer);
    statusTimer = null;
  }
}

function startElapsed() {
  startedAt.value = Date.now();
  elapsed.value = '00:00';
  if (elapsedTimer !== null) clearInterval(elapsedTimer);
  elapsedTimer = window.setInterval(() => {
    if (startedAt.value == null) return;
    const s = Math.max(0, Math.floor((Date.now() - startedAt.value) / 1000));
    const m = Math.floor(s / 60);
    const ss = s % 60;
    elapsed.value = `${String(m).padStart(2, '0')}:${String(ss).padStart(2, '0')}`;
  }, 1000);
}
function stopElapsed() {
  if (elapsedTimer !== null) {
    clearInterval(elapsedTimer);
    elapsedTimer = null;
  }
}

// ---------- 闸门闩锁 ----------
// 控制端点不再写 current_phase，流水线最多 1s 后才推进阶段；这 1s 窗口内
// 5s 一次的 fetchStatus 仍会看到旧阶段，把刚确认完的弹窗重新弹出来。
// 用户再点一次会拿到 409，而该弹窗没有关闭按钮 → 全屏锁死。
// 用 run_id + 闸门名做闩锁，确认过后不再响应 _build_resume。
const confirmedGates = ref<Set<string>>(new Set());
function gateKey(k: string) {
  return `${runId.value || '-'}:${k}`;
}
function isGateSettled(k: string) {
  return confirmedGates.value.has(gateKey(k));
}
function markGateSettled(k: string) {
  confirmedGates.value.add(gateKey(k));
}

// 输出目录状态：是否可续跑 / 已完成 / 失败原因。用于「重跑为什么没保存」的困惑：
// 数据其实一直在盘上，缺的是让用户看见、并能一键重来。
const outInfo = ref<any | null>(null);
const clearingOutput = ref(false);

// ---------- 任务历史（跨会话）----------
const historyItems = ref<HistoryItem[]>([]);
const showHistory = ref(false);
const histQuery = ref('');
const histSort = ref<'recent' | 'name' | 'progress'>('recent');
const histBusy = ref(false);

/** 未完成且可续跑的数量，用于「任务历史」按钮上的角标提醒。
 *  刻意放在 App.vue 而非 HistoryModal：这里统计的是**全部**未完成条目，
 *  与弹窗内按搜索词过滤后的列表无关。 */
const historyPendingCount = computed(
  () => historyItems.value.filter((h) => !h.finished && !h.running && h.current_phase).length,
);

async function refreshHistory() {
  if (!port.value || histBusy.value) return;
  histBusy.value = true;
  try {
    historyItems.value = (await fetchHistory(port.value)).items || [];
  } catch {
    /* 后端未起/无历史：静默 */
  } finally {
    histBusy.value = false;
  }
}

async function clearAllHistory() {
  if (!port.value) return;
  if (!window.confirm('清空任务历史列表吗？\n（只清除这份记录，不动任何磁盘文件）')) return;
  try {
    await req0('/api/history/clear');
    await refreshHistory();
    addLog('已清空任务历史记录（磁盘文件未动）');
  } catch (e: any) {
    topError.value = `清空历史失败：${e.message}`;
  }
}
/** 裸请求（供不需要强类型的清空操作使用） */
async function req0(path: string) {
  const res = await apiFetch(`${apiBase()}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json().catch(() => ({}));
}
/** 把历史条目填回配置（不自动开跑，用户确认后再点开始） */
function loadFromHistory(h: HistoryItem) {
  if (h.input_path) inputPath.value = h.input_path;
  if (h.input_mode === 'single_file' || h.input_mode === 'multi_file')
    inputMode.value = h.input_mode;
  if (h.input_path) {
    const dot = h.input_path.lastIndexOf('.');
    if (dot > 0) {
      const ext = h.input_path.slice(dot + 1).toLowerCase();
      if (ext === 'txt' || ext === 'md' || ext === 'docx') inputFormat.value = ext;
    }
  }
  outputPath.value = h.output_path;
  if (h.novel_name) novelName.value = h.novel_name;
  refreshOutInfo();
  addLog(`已载入历史任务「${h.novel_name || h.output_path}」，确认无误后点开始即可续跑`);
}
async function forgetHistoryItem(h: HistoryItem) {
  if (!port.value) return;
  try {
    await forgetHistory(port.value, h.output_path);
    await refreshHistory();
  } catch (e: any) {
    topError.value = `移除失败：${e.message}`;
  }
}

// ---------- 成品版本（重跑前的整版快照，可回滚）----------
const showVersions = ref(false);
const versionList = ref<VersionInfo[]>([]);
const versionBusy = ref(false);
async function openVersions() {
  if (!port.value || !outputPath.value) return;
  showVersions.value = true;
  versionBusy.value = true;
  try {
    versionList.value = (await fetchVersions(port.value, outputPath.value)).versions || [];
  } catch (e: any) {
    versionList.value = [];
    topError.value = `读取版本失败：${e.message}`;
  } finally {
    versionBusy.value = false;
  }
}
async function doRestore(v: VersionInfo) {
  if (!port.value || !outputPath.value) return;
  if (
    !window.confirm(`把成品恢复到版本 ${v.id} 吗？\n（恢复前会先给当前版本留一份快照，仍可再回退）`)
  )
    return;
  versionBusy.value = true;
  try {
    await restoreVersion(port.value, outputPath.value, v.id);
    addLog(`已恢复成品版本 ${v.id}`);
    await openVersions();
    await fetchStatus();
  } catch (e: any) {
    topError.value = `恢复失败：${e.message}`;
  } finally {
    versionBusy.value = false;
  }
}

async function refreshOutInfo() {
  if (!port.value || !outputPath.value) {
    outInfo.value = null;
    return;
  }
  try {
    const res = await apiFetch(
      `${apiBase()}/api/output/inspect?output_path=${encodeURIComponent(outputPath.value)}`,
    );
    outInfo.value = res.ok ? await res.json() : null;
  } catch {
    outInfo.value = null;
  }
}
async function clearOutputDir() {
  if (!outputPath.value) return;
  if (
    !window.confirm(
      '确定清空输出目录里的 TextForge 产物吗？\n（只删 01_summaries / 02_workspace / 03_final / _diagnostics / Story_Bible.md / progress.json，不影响你的其它文件）',
    )
  )
    return;
  clearingOutput.value = true;
  try {
    const res = await apiFetch(`${apiBase()}/api/output/clear`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ output_path: outputPath.value }),
    });
    if (await checkRes(res, '清空输出目录')) {
      const body = await res.json().catch(() => ({}));
      addLog(`已清空输出目录（删除 ${(body.removed || []).length} 项）`);
      await refreshOutInfo();
    }
  } catch (e: any) {
    topError.value = `清空失败：${e.message}`;
  } finally {
    clearingOutput.value = false;
  }
}
// 路径是逐字符敲进去的：persist（全量 JSON→localStorage）与 refreshOutInfo
//（一次 /api/output/inspect HTTP）都不该每个按键都跑，统一防抖 400ms。
let outputPathTimer: ReturnType<typeof setTimeout> | undefined;
let persistTimer: ReturnType<typeof setTimeout> | undefined;
watch(outputPath, () => {
  if (persistTimer) clearTimeout(persistTimer);
  persistTimer = setTimeout(persist, 400);
  if (outputPathTimer) clearTimeout(outputPathTimer);
  outputPathTimer = setTimeout(refreshOutInfo, 400);
});
watch([port, () => pipelineRunning.value], () => {
  refreshOutInfo();
});

async function fetchStatus(): Promise<boolean> {
  if (!port.value) return false;
  try {
    const q = outputPath.value ? `?output_path=${encodeURIComponent(outputPath.value)}` : '';
    const res = await apiFetch(`http://127.0.0.1:${port.value}/api/status${q}`);
    if (!res.ok) return false;
    const s = await res.json();
    isPaused.value = !!s.paused;
    if (Array.isArray(s.config_drift) && s.config_drift.length) {
      // 续跑时配置变了 → 前几批用旧配置、后几批用新配置，可能风格割裂
      topTip.value = `续跑注意：${s.config_drift.join('；')}。已完成批次不会重做，前后风格可能不一致。`;
    }
    // 必须无条件赋值：原实现只有 `if (s.pipeline_running) pipelineRunning = true`，
    // 没有 else。后端进程一死，fetch 抛异常被空 catch 吞掉，pipelineRunning 就永远
    // 卡在 true → canStart 恒 false → .panel.config.disabled 的 pointer-events:none
    // 把整个配置面板冻住，只能刷新页面。
    pipelineRunning.value = !!s.pipeline_running;
    // B4：僵尸检测。后端已改为后台任务每 10s 刷新心跳（覆盖 LLM 调用期间），
    // 所以 90s 阈值现在是真的「进程卡死」，不会再在跑大批次时误报。
    zombieWarn.value = !!(
      s.pipeline_running &&
      s.last_heartbeat &&
      Date.now() - new Date(s.last_heartbeat).getTime() > 90_000
    );
    usageTokens.value = {
      prompt: s.usage?.prompt_tokens || 0,
      completion: s.usage?.completion_tokens || 0,
    };
    if (s.summaries) summaries.value = s.summaries;
    // 用 typeof 判断而非真值：0 也是合法状态（新一轮更短、或尚未开始），
    // 真值判断会让进度条/总数残留上一轮 run 的旧数字。
    if (typeof s.total_chapters === 'number') totalChapters.value = s.total_chapters;
    if (s.batches)
      batchStats.value = {
        total: s.batches.total || 0,
        phase1: s.batches.phase1_done || 0,
        phase3: s.batches.phase3_done || 0,
      };
    if (typeof s.total_logic_chapters === 'number' || typeof s.reconstructed_chapters === 'number')
      chapterProgress.value = {
        total: s.total_logic_chapters || 0,
        done: s.reconstructed_chapters || 0,
      };
    if (Array.isArray(s.chapters_summary)) chapterRows.value = s.chapters_summary;
    if (pipelineRunning.value && s.current_phase)
      currentPhase.value = phaseLabel[s.current_phase] || s.current_phase;
    // 恢复时重新弹出蓝图确认（SSE 事件已错过）。已确认过就不要再弹：
    // 确认后后端推进到下一阶段前，仍有 1~5s 的轮询窗口会命中这个分支，
    // 反复重开会让用户卡在「确认→又被弹回」的循环里（再点必得 409）。
    if (
      s.current_phase === 'phase2_waiting' &&
      s.blueprint &&
      !showBlueprint.value &&
      !isGateSettled('blueprint')
    ) {
      blueprintText.value = s.blueprint;
      showBlueprint.value = true;
      addLog('检测到待确认蓝图（从后端状态恢复）');
    }
    const lastErr = s.error_logs?.[s.error_logs.length - 1];
    // 只在错误「变化」时点亮横幅：否则空闲期每 5s 轮询都会把用户刚关掉的
    // 红条重新拉回来，永远关不掉。新一轮 run 会在别处把它重置为 null。
    if (lastErr && !pipelineRunning.value && lastErr.message !== lastPolledError) {
      lastPolledError = lastErr.message;
      topError.value = lastErr.message;
    }
    // B3：刷新后恢复等待态弹窗（必须覆盖后端 _build_resume 的全部分支）
    if (s.resume) {
      const r = s.resume;
      if (r.key === 'split' && !showSplit.value && !isGateSettled('split')) {
        splitChapters.value = r.chapters || [];
        showSplit.value = true;
        addLog('已恢复拆书确认（刷新后重开）', 'warn');
      } else if (
        r.key === 'accept' &&
        !showAccept.value &&
        !isGateSettled(`accept:${r.batch_id}`)
      ) {
        acceptBatch.value = r.batch_id;
        acceptChapters.value = r.chapters || [];
        showAccept.value = true;
        addLog('已恢复逐章验收（刷新后重开）', 'warn');
      } else if (r.key === 'fix' && !showFixReview.value && !isGateSettled('fix')) {
        fixGaps.value = r.gaps || [];
        fixSelected.value = fixGaps.value.map((g: any) => g.id).filter(Boolean);
        showFixReview.value = true;
        addLog('已恢复断层勾选（刷新后重开）', 'warn');
      } else if (r.key === 'qc' && !showQcBlock.value && !isGateSettled('qc')) {
        qcIssues.value = r.issues || [];
        showQcBlock.value = true;
        addLog('已恢复质检决策（刷新后重开）', 'err');
      }
    }
    return true;
  } catch {
    // 轮询失败静默，等待下一次。返回 false 表示「本轮没有拿到权威状态」，
    // 调用方不能据此判定流水线已停。
    return false;
  }
}

function handleSSEEvent(event: string, payload: any) {
  switch (event) {
    case 'run_id':
      runId.value = payload.run_id;
      break;
    case 'run_start':
      // 「重放」由服务端用 _replay 标记（backend/routes/events.py）。
      // **不能**用 `payload.run_id === runId.value` 判断：后端在重放 run_start
      // 之前必定先发 run_id 事件并把它写进 runId.value，那样判据 100% 命中，
      // 会把整个 handler 变成死代码 —— 断点恢复提示、运行计时器、阶段标签全部失效。
      if (payload._replay) {
        addLog('已重新连接事件流', 'warn');
        break;
      }
      pipelineRunning.value = true;
      topError.value = null;
      lastPolledError = null;
      runId.value = payload.run_id || runId.value;
      confirmedGates.value = new Set(); // 新一轮 run：清掉上一轮的闸门闩锁
      finalFiles.value = [];
      currentPhase.value = payload.resumed ? '（断点恢复中）' : '启动';
      topTip.value = payload.resumed
        ? '检测到未完成的进度，将从断点继续（已完成的阶段/批次自动跳过）'
        : null;
      addLog(`流水线启动${payload.resumed ? '（断点恢复）' : ''}`);
      startElapsed();
      break;
    case 'phase_done':
      if (payload.phase === 'phase0') totalChapters.value = payload.total_chapters;
      currentPhase.value = phaseLabel[payload.phase] || payload.phase;
      addLog(
        `阶段完成：${phaseLabel[payload.phase] || payload.phase}${payload.resumed_skip ? '（恢复跳过）' : ''}`,
        'ok',
      );
      break;
    case 'batch_start':
      if (streamRaf) {
        cancelAnimationFrame(streamRaf);
        streamRaf = 0;
      }
      streamBuf = '';
      streamingText.value = ''; // 新批次清空流式预览
      addLog(`批次 ${payload.batch_id} 开始：章节 ${payload.chapter_ids?.join(',')}`, 'accent');
      break;
    case 'batch_done':
      addLog(`批次 ${payload.batch_id} 完成`, 'accent');
      break;
    case 'batch_split':
      // 批次因「上下文超限 / 分隔符契约失败」被自动对半拆分重跑。
      // 这是本应用在弱模型/小显存下的自愈机制，必须让用户看见：
      // 否则用户只会觉得「怎么跑了这么久」，而不知道程序已在自动降规模。
      addLog(payload.message || `批次 ${payload.from_batch} 自动拆分重跑`, 'warn');
      break;
    case 'stream_chunk':
      // #2 节流后的流式增量：再按帧合并，避免逐 chunk 触发全量重渲染。
      streamBuf += payload.text || '';
      if (!streamRaf) streamRaf = requestAnimationFrame(flushStream);
      break;
    case 'split_ready':
      // P0.3：拆书完成，等待用户确认拆分后进入诊断
      splitChapters.value = payload.chapters || [];
      splitDetection.value = payload.detection || null;
      showSplit.value = true;
      addLog(`拆书完成，共 ${splitChapters.value.length} 章，请确认`, 'warn');
      break;
    case 'fix_ready':
      // fix_gaps 闭环：诊断断层清单已就绪，等待勾选确认
      fixGaps.value = payload.gaps || [];
      fixSelected.value = fixGaps.value.map((g: any) => g.id).filter(Boolean);
      showFixReview.value = true;
      addLog(`断层诊断完成，共 ${fixGaps.value.length} 项，请勾选需要修复的断层`, 'warn');
      break;
    case 'accept_ready':
      // 逐章验收门：本批重构稿已产出，等待验收
      acceptBatch.value = payload.batch_id;
      acceptChapters.value = payload.chapters || [];
      showAccept.value = true;
      addLog(`批次 ${payload.batch_id} 重构稿已产出，请验收后继续`, 'warn');
      break;
    case 'qc_failed':
      // 质检拦截导出门：展示质检问题，供用户选择强制导出或停止
      qcIssues.value = payload.issues || [];
      showQcBlock.value = true;
      addLog(`质检未通过（${qcIssues.value.length} 项问题），请决定是否强制导出`, 'err');
      break;
    case 'blueprint_ready':
      // 已确认过就不再弹（重连重放或轮询竞态会把已关闭的弹窗重新锁上）。
      if (isGateSettled('blueprint')) break;
      blueprintText.value = payload.blueprint || '';
      showBlueprint.value = true;
      currentPhase.value = 'phase2_waiting';
      addLog(payload.resumed ? '待确认蓝图（恢复）' : '蓝图已生成，请确认后继续');
      break;
    case 'stitch_start':
      addLog(`缝合阶段${payload.stage}开始（${payload.count} 处）`, 'accent');
      break;
    case 'stitch_done':
      addLog(`缝合阶段${payload.stage}完成`, 'accent');
      break;
    case 'paused':
      isPaused.value = true;
      addLog('已暂停', 'warn');
      break;
    case 'resumed':
      isPaused.value = false;
      addLog('已恢复', 'warn');
      break;
    case 'done':
      pipelineRunning.value = false;
      currentPhase.value = '完成';
      closeAllGates();
      stopStatusPolling(); // 空闲期不空转轮询；下次 startPipeline 会重新拉起
      // 不再断开 SSE：后端 /api/events 会「跟随」current_run_id，
      // 保持连接才能让下一次 run 自动接上；断开等于把流打死且不再重连。
      topTip.value = `全部完成！成品在：${payload.output_dir}\\03_final`;
      addLog(`全部完成！输出目录：${payload.output_dir}`, 'ok');
      stopElapsed();
      refreshHistory();
      refreshOutInfo(); // 历史里立刻显示「已完成」
      break;
    case 'stopped':
      pipelineRunning.value = false;
      // 必须关掉所有闸门弹窗：.modal-mask 是 fixed + z-index:100，
      // 残留一个「停止/强制导出」弹窗会全屏锁死界面，用户连关闭按钮都点不到。
      closeAllGates();
      stopStatusPolling();
      topTip.value = '流水线已停止，进度已保留；再次点击"开始重构"可从断点继续';
      addLog('流水线已停止', 'warn');
      stopElapsed();
      refreshHistory();
      refreshOutInfo();
      break;
    case 'error':
      // kind='warning' 是非致命提示（如摘要超长被截断），不应停表、也不该记成错误
      if (payload.kind === 'warning') {
        addLog(`提示：${payload.message || payload.code}`, 'warn');
        break;
      }
      addLog(`错误：${payload.message || payload.code}`, 'err');
      pipelineRunning.value = false;
      closeAllGates();
      stopStatusPolling();
      topError.value = payload.message || payload.code || '未知错误';
      stopElapsed();
      refreshHistory();
      refreshOutInfo(); // 历史里立刻显示「出错中断 + 原因」
      break;
    case 'comparison_done':
      addLog(`左右对照已生成：写入 ${payload.written} 章 / 跳过 ${payload.skipped} 章`, 'ok');
      break;
  }
}

/** 关闭所有人工闸门弹窗（停止 / 出错 / 完成时统一调用，避免全屏锁死）。 */
function closeAllGates() {
  showSplit.value = false;
  showFixReview.value = false;
  showAccept.value = false;
  showQcBlock.value = false;
  showBlueprint.value = false;
}

// ---------- 文件夹选择 ----------
async function selectInput() {
  if (!inTauri) {
    topError.value = '浏览器开发模式不支持系统选择器，请直接在输入框中粘贴路径';
    return;
  }
  try {
    if (inputMode.value === 'single_file') {
      const selected = await open({
        multiple: false,
        filters: [{ name: '文档', extensions: ['txt', 'md', 'docx'] }],
      });
      if (typeof selected === 'string') {
        inputPath.value = selected;
        // 选了 .docx 却仍声明 txt，后端 paths.py 会直接 422：
        // 「输入格式选择(txt)与文件扩展名(.docx)不一致」
        const ext = selected.split('.').pop()?.toLowerCase();
        if (ext === 'txt' || ext === 'md' || ext === 'docx') inputFormat.value = ext;
      }
    } else {
      const selected = await open({ directory: true });
      if (typeof selected === 'string') inputPath.value = selected;
    }
  } catch (e: any) {
    topError.value = `打开选择器失败：${e?.message || e}`;
  }
}

async function selectOutput() {
  if (!inTauri) {
    topError.value = '浏览器开发模式不支持系统选择器，请直接在输入框中粘贴路径';
    return;
  }
  try {
    const selected = await open({ directory: true });
    if (typeof selected === 'string') outputPath.value = selected;
  } catch (e: any) {
    topError.value = `打开选择器失败：${e?.message || e}`;
  }
}

// ---------- API 调用 ----------
function apiBase() {
  return `http://127.0.0.1:${port.value}`;
}

async function startPipeline() {
  if (starting.value) return; // 防重复提交：双击会并发起两个 run
  if (!inputPath.value || !outputPath.value || !apiUrl.value || !model.value) {
    alert('请填写完整：输入路径、输出路径、API URL、模型名');
    return;
  }
  if (refactorMode.value === 'fix_gaps' && !diagnoseJson.value.trim()) {
    alert(
      '「修断层」模式依赖结构化断层清单：请在「优化项」中填写诊断JSON输出目录，诊断后勾选断层才能闭环修复。',
    );
    return;
  }
  starting.value = true;
  const body = {
    input_mode: inputMode.value,
    input_path: inputPath.value,
    output_path: outputPath.value,
    input_format: inputFormat.value,
    api_url: apiUrl.value,
    api_key: apiKey.value,
    model: model.value,
    // 数字输入框被清空时 v-model.number 得到 ''，序列化后后端 int/float 校验直接 422
    context_window: num(contextWindow.value, 8000, 8000),
    author_style: authorStyle.value,
    novel_name: novelName.value || null,
    refactor_mode: refactorMode.value,
    review_split: reviewSplit.value,
    ai_split: aiSplit.value,
    refactor_gates: { ...gates },
    forbidden_canon: forbiddenCanon.value
      .split('\n')
      .map((s) => s.trim())
      .filter(Boolean),
    name_map: Object.fromEntries(
      nameMapText.value
        .split('\n')
        .map((s) => s.trim())
        .filter(Boolean)
        .map((s) => s.split(/[=→：]/).map((p) => p.trim()))
        // 旧实现只校验 key 非空，「林晚=」会产出 {"林晚": ""}，
        // 渲染进提示词后等于让模型把人名映射成空串
        .filter((p) => p.length === 2 && p[0] && p[1]),
    ),
    compare_output: compareOutput.value,
    chapter_snapshots: chapterSnapshots.value,
    diagnose_json: diagnoseJson.value || null,
    proxy: proxy.value || null,
    ssl_verify: sslVerify.value,
    rich_text: richText.value,
    temperatures: {
      diagnose: num(tDiagnose.value, 0, 0.3, 2),
      blueprint: num(tBlueprint.value, 0, 0.5, 2),
      refactor: num(tRefactor.value, 0, 0.8, 2),
      stitch: num(tStitch.value, 0, 0.6, 2),
    },
    batch_interval_sec: num(batchInterval.value, 0, 2.0, 300),
    models: buildModels(),
  };
  try {
    const res = await apiFetch(`${apiBase()}/api/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      await checkRes(res, '启动', data);
      return;
    }
    runId.value = data.run_id;
    // 新一轮 run：这里是最权威的重置时机（前端已经拿到新 run_id）。
    // 不能只依赖 SSE 的 run_start —— 那个事件 emit 得比 SSE 订阅更早，
    // 没有后端重放缓冲时必然丢失（已靠 events.py 的 _recent 兜住，双保险）。
    confirmedGates.value = new Set();
    finalFiles.value = [];
    topError.value = null;
    lastPolledError = null;
    eventLog.value = [];
    addLog(`已提交流水线 run_id=${data.run_id.slice(0, 8)}...`);
    startElapsed();
    startStatusPolling();
    // /api/start 立即返回，快速失败的任务可能已经把 error 事件发出去并停掉了轮询。
    // 主动拉一次权威状态再决定要不要继续 —— 注意不能写成
    // `if (pipelineRunning.value) startStatusPolling()`：此刻 pipelineRunning 几乎必然
    // 还是 false（run_start 走 SSE、轮询要 5s 后才跑），那样就永远起不来轮询。
    if ((await fetchStatus()) && !pipelineRunning.value) stopStatusPolling();
    // SSE 若已断（例如手动停过后端），这里补一次握手
    if (!backendReady.value) reconnectBackend();
    persist();
  } catch (e: any) {
    topError.value = `请求失败：${e.message}`;
  } finally {
    starting.value = false;
  }
}

async function confirmSplit() {
  if (confirmingSplit.value) return; // 防重复点击：Enter 键和鼠标都会触发
  confirmingSplit.value = true;
  try {
    const res = await apiFetch(`${apiBase()}/api/split/confirm`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: runId.value }),
    });
    if (await checkRes(res, '确认拆分')) {
      markGateSettled('split');
      showSplit.value = false;
      addLog('拆分已确认，进入诊断');
    }
  } catch (e: any) {
    topError.value = `请求失败：${e.message}`;
  } finally {
    confirmingSplit.value = false;
  }
}

// 拆书预览里「按自定义标记重新拆分」：规则与 AI 都搞不定的创新排版，由用户拍板。
const redoMarkers = ref('');
const redoingSplit = ref(false);
async function redoSplit() {
  const markers = redoMarkers.value
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean);
  if (!markers.length) {
    topError.value = '请至少填写一个章节标题行（每行一个，需与原文完全一致）';
    return;
  }
  redoingSplit.value = true;
  try {
    const res = await apiFetch(`${apiBase()}/api/split/redo`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: runId.value, markers }),
    });
    if (await checkRes(res, '重新拆分'))
      addLog(`已提交 ${markers.length} 个章节标记，正在重新拆分…`);
  } catch (e: any) {
    topError.value = `请求失败：${e.message}`;
  } finally {
    redoingSplit.value = false;
  }
}

function toggleFix(gapId: string) {
  const i = fixSelected.value.indexOf(gapId);
  if (i >= 0) fixSelected.value.splice(i, 1);
  else fixSelected.value.push(gapId);
}

/** 把勾选的断层 id 转成可读文本再提交。
 *  旧实现直接发 id（如 "B3G2"），后端 refactor.py 原样拼进提示词，
 *  模型收到的是无意义 token，于是无视勾选、全书自由发挥，且没有任何报错。 */
function selectedGapDescriptions(): string[] {
  return (
    fixGaps.value
      .filter((g: any) => fixSelected.value.includes(g.id))
      // 三级兜底：模型产出的 gap 不保证有 description/location，
      // 一律为空的项若被 filter(Boolean) 丢掉，fix_list 会变空 → 退化成「自由改写」，
      // 而界面上仍显示「已勾选 N 处」。
      .map((g: any) => {
        const text = [g.location, g.description].filter(Boolean).join('：');
        return (
          text ||
          `${g.type || '断层'} @ ${g.batch_id || '?'}${g.severity ? ` [${g.severity}]` : ''} (${g.id})`
        );
      })
  );
}

async function confirmFix() {
  if (confirmingFix.value) return; // 防重复点击
  confirmingFix.value = true;
  try {
    const descriptions = selectedGapDescriptions();
    const res = await apiFetch(`${apiBase()}/api/fix/confirm`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: runId.value, fix_list: descriptions }),
    });
    if (await checkRes(res, '确认断层')) {
      markGateSettled('fix');
      showFixReview.value = false;
      addLog(`已勾选 ${descriptions.length} 处断层，开始修复`);
    }
  } catch (e: any) {
    topError.value = `请求失败：${e.message}`;
  } finally {
    confirmingFix.value = false;
  }
}

async function confirmAccept() {
  if (confirmingAccept.value) return; // 防重复点击
  confirmingAccept.value = true;
  try {
    const res = await apiFetch(`${apiBase()}/api/accept`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: runId.value }),
    });
    if (await checkRes(res, '验收')) {
      markGateSettled('accept:' + acceptBatch.value);
      showAccept.value = false;
      addLog(`批次 ${acceptBatch.value} 已验收，继续重构`);
    }
  } catch (e: any) {
    topError.value = `请求失败：${e.message}`;
  } finally {
    confirmingAccept.value = false;
  }
}

async function regenChapter(c: any) {
  if (regenerating.value.has(c.id)) return; // 防重复
  regenerating.value.add(c.id);
  try {
    const res = await apiFetch(`${apiBase()}/api/regen_chapter`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: runId.value, chapter_id: c.id }),
    });
    const d = await res.json().catch(() => null);
    if (res.ok) {
      c.preview = d?.preview || '';
      addLog(`章节 ${c.id} 已重写，请重新验收`, 'accent');
    } else {
      await checkRes(res, '重写', d);
    }
  } catch (e: any) {
    topError.value = `请求失败：${e.message}`;
  } finally {
    regenerating.value.delete(c.id);
  }
}

async function openOutput() {
  await callOpenOutput(outputPath.value);
}

async function openFinal() {
  if (!outputPath.value) return;
  const root = outputPath.value.replace(/[\\/]+$/, '');
  // C4：先列出成品文件，优先直开文档；无可开文件则打开根目录
  try {
    const res = await apiFetch(
      `${apiBase()}/api/final_files?output_path=${encodeURIComponent(root)}`,
    );
    if (res.ok) {
      const d = await res.json();
      const files = d.files || [];
      if (files.length) {
        finalFiles.value = files;
        // 单个成品直开；多个则打开成品目录，并在界面上列出清单供逐个点开
        if (files.length === 1) {
          await callOpenOutput(files[0].path);
          return;
        }
        await callOpenOutput(`${root}/03_final`);
        addLog(`成品目录含 ${files.length} 个文件，可在上方「成品文件」逐个打开`);
        return;
      }
    }
  } catch (e: any) {
    // 静默会让人以为「成品不存在」，实际是接口失败；记一条日志便于排查。
    addLog(`读取成品文件失败：${e?.message || e}（改为直接打开输出目录）`, 'warn');
  }
  await callOpenOutput(root);
}

async function callOpenOutput(path: string): Promise<boolean> {
  try {
    const res = await apiFetch(`${apiBase()}/api/open_output`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ output_path: path }),
    });
    if (!res.ok) {
      await checkRes(res, '打开');
      return false;
    }
    return true;
  } catch (e: any) {
    topError.value = `请求失败：${e.message}`;
    return false;
  }
}

async function forceExport() {
  if (forcingExport.value) return; // 防重复点击
  forcingExport.value = true;
  try {
    const res = await apiFetch(`${apiBase()}/api/export/force`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: runId.value }),
    });
    if (await checkRes(res, '强制导出')) {
      markGateSettled('qc');
      showQcBlock.value = false;
      addLog('已忽略质检，强制导出', 'warn');
    }
  } catch (e: any) {
    topError.value = `请求失败：${e.message}`;
  } finally {
    forcingExport.value = false;
  }
}

async function exportLog() {
  if (!eventLog.value.length) {
    alert('暂无日志可导出');
    return;
  }
  const text = eventLog.value.map((l) => `[${l.ts}] ${l.text}`).join('\n');
  const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `textforge-log-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.txt`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
  addLog(`已导出日志（${eventLog.value.length} 条）`);
}

async function openComparisons() {
  if (!outputPath.value) return;
  try {
    const res = await apiFetch(
      `${apiBase()}/api/comparisons?output_path=${encodeURIComponent(outputPath.value)}`,
    );
    if (!res.ok) {
      alert('获取对照失败');
      return;
    }
    const d = await res.json();
    comparisonItems.value = d.items || [];
    comparisonSel.value = comparisonItems.value[0]?.name ?? '';
    showComparisons.value = true;
    if (!comparisonItems.value.length)
      topError.value = '当前输出目录还没有左右对照文件（需开启「左右对照」并跑完 phase3）';
  } catch (e: any) {
    topError.value = `请求失败：${e.message}`;
  }
}

async function confirmBlueprint() {
  if (confirmingBlueprint.value) return; // 防重复点击
  if (blueprintText.value.length < 100) {
    alert('蓝图内容过短（需 >= 100 字）');
    return;
  }
  confirmingBlueprint.value = true;
  try {
    const res = await apiFetch(`${apiBase()}/api/blueprint/confirm`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        run_id: runId.value,
        blueprint: blueprintText.value,
        user_edited: true,
      }),
    });
    if (await checkRes(res, '确认蓝图')) {
      // 与 split/accept/fix/qc 一致：确认后落闩锁，防止轮询/重放再次弹窗。
      markGateSettled('blueprint');
      showBlueprint.value = false;
      addLog('蓝图已确认，继续重构');
    }
  } catch (e: any) {
    topError.value = `请求失败：${e.message}`;
  } finally {
    confirmingBlueprint.value = false;
  }
}

/** fetch 不会因 4xx/5xx reject，必须自己判 res.ok，否则 409/422 全被静默吞掉。
 *  parsed 传入已解析的响应体：body 只能读一次，重复 res.json() 会抛
 *  "Body is unusable"，错误信息会退化成裸状态码。 */
async function checkRes(res: Response, action: string, parsed?: any): Promise<boolean> {
  if (res.ok) return true;
  let detail: any = parsed?.detail;
  if (detail === undefined) {
    try {
      detail = (await res.json())?.detail;
    } catch {
      /* 非 JSON 或已读完 */
    }
  }
  // Pydantic 校验错误的 detail 是数组，直接插值会渲染成 [object Object]
  if (Array.isArray(detail)) {
    detail = detail.map((e: any) => `${(e.loc || []).slice(1).join('.')}: ${e.msg}`).join('; ');
  }
  const msg = detail || `HTTP ${res.status}`;
  topError.value = `${action}失败：${msg}`;
  addLog(`${action}失败：${msg}`, 'err');
  return false;
}

/** 数字输入框被清空时 v-model.number 得到的是 ''，必须夹回合法值，否则后端 422。 */
function num(v: any, min: number, fallback: number, max = Number.MAX_SAFE_INTEGER) {
  const n = Number(v);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, n));
}

async function control(action: 'pause' | 'resume' | 'stop') {
  if (controlling.value) return; // 防重复点击
  controlling.value = true;
  try {
    const label = { pause: '暂停', resume: '恢复', stop: '停止' }[action];
    const res = await apiFetch(`${apiBase()}/api/${action}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: runId.value }),
    });
    await checkRes(res, label);
  } catch (e: any) {
    topError.value = `请求失败：${e.message}`;
  } finally {
    controlling.value = false;
  }
}

/** 设置面板里改完模型配置后写回主表单并持久化。 */
function applyModelFromSettings(v: {
  model: string;
  apiUrl: string;
  apiKey: string;
  proxy: string;
  sslVerify: boolean;
}) {
  model.value = v.model;
  apiUrl.value = v.apiUrl;
  apiKey.value = v.apiKey;
  proxy.value = v.proxy;
  sslVerify.value = v.sslVerify;
  persist();
}

function applyContextFromSettings(n: number) {
  if (typeof n === 'number' && n >= 8000) contextWindow.value = n;
  persist();
}

const canStart = computed(
  () =>
    backendReady.value &&
    !pipelineRunning.value &&
    !starting.value &&
    !!inputPath.value &&
    !!outputPath.value &&
    !!apiUrl.value &&
    !!model.value,
);
</script>

<template>
  <div class="app">
    <header class="header">
      <h1>文炼工坊 <span class="version">TextForge v0.9.0</span></h1>
      <div class="status">
        <button
          class="status-link"
          :title="backendReady ? '查看后端设置' : '后端未连接，点击排查/重启'"
          @click="showSettings = true"
        >
          <span :class="['dot', backendReady ? 'ok' : 'err']"></span>
          {{ backendReady ? '后端已连接' : errorMsg || '连接中...' }}
        </button>
        <button
          type="button"
          class="gear"
          title="系统与后端设置"
          aria-label="系统与后端设置"
          @click="showSettings = true"
        >
          ⚙
        </button>
        <button type="button" class="theme-toggle" aria-label="切换深浅色主题" @click="toggleTheme">
          {{ darkMode ? '☀︎' : '☾' }}
        </button>
      </div>
    </header>

    <main class="main">
      <!-- 全局提示 -->
      <div v-if="topTip" class="banner tip">
        <span>{{ topTip }}</span>
        <button type="button" class="banner-close" aria-label="关闭提示" @click="topTip = null">
          ×
        </button>
      </div>
      <div v-if="errorMsg" class="banner err">
        <span>⚠ {{ errorMsg }}</span>
        <span class="banner-act">
          <button type="button" class="banner-btn" @click="reconnectBackend">重新连接</button>
          <button type="button" class="banner-btn primary" @click="showSettings = true">
            打开后端设置
          </button>
          <button type="button" class="banner-close" aria-label="关闭提示" @click="errorMsg = ''">
            ×
          </button>
        </span>
      </div>
      <div v-if="topError" class="banner err">
        <span>⚠ {{ topError }}</span>
        <button
          type="button"
          class="banner-close"
          aria-label="关闭错误提示"
          @click="topError = null"
        >
          ×
        </button>
      </div>
      <div v-if="zombieWarn" class="banner warn">
        <span
          >⚠ 后端进程超过 90 秒未响应，可能已假死。可尝试「停止」，或检查后端是否仍在运行。</span
        >
        <button
          type="button"
          class="banner-close"
          aria-label="关闭警告"
          @click="zombieWarn = false"
        >
          ×
        </button>
      </div>

      <!-- 配置区 -->
      <section class="panel config" :class="{ disabled: pipelineRunning }">
        <h2>
          运行配置
          <button class="panel-gear" title="系统与后端设置" @click="showSettings = true">
            ⚙ 后端设置
          </button>
        </h2>

        <div class="row backend-row">
          <label>后端服务</label>
          <span class="backend-state">
            <span :class="['dot', backendReady ? 'ok' : 'err']"></span>
            {{
              backendReady
                ? `已连接 :${port ?? '—'}`
                : sseAttempts > 0
                  ? `重连中 (${sseAttempts})`
                  : '未连接'
            }}
          </span>
          <button @click="reconnectBackend" :disabled="backendReady">重新连接</button>
          <button class="ghost" @click="showSettings = true">设置</button>
        </div>

        <div class="row">
          <label>输入模式</label>
          <select v-model="inputMode">
            <option value="single_file">单文件</option>
            <option value="multi_file">多文件（文件夹）</option>
          </select>
          <label class="fmt">格式</label>
          <select v-model="inputFormat">
            <option value="txt">txt</option>
            <option value="md">md</option>
            <option value="docx">docx</option>
          </select>
        </div>

        <div class="row">
          <label>输入路径</label>
          <input v-model="inputPath" placeholder="可直接粘贴路径，或点击右侧选择" />
          <button @click="selectInput">选择</button>
        </div>

        <div class="row">
          <label>输出路径</label>
          <input v-model="outputPath" placeholder="可直接粘贴路径，或点击右侧选择" />
          <button @click="selectOutput">选择</button>
        </div>

        <div class="row">
          <label>API URL</label>
          <input v-model="apiUrl" placeholder="https://api.example.com/v1/chat/completions" />
        </div>

        <div class="row">
          <label>API Key</label>
          <input v-model="apiKey" type="password" placeholder="可选，仅存内存" />
        </div>

        <div class="row">
          <label>模型名</label>
          <input v-model="model" placeholder="如 gpt-4o" />
          <label class="ctx">上下文</label>
          <input v-model.number="contextWindow" type="number" min="8000" />
        </div>

        <div class="row">
          <label>预设</label>
          <select v-model="preset" @change="applyPreset">
            <option v-for="name in Object.keys(PRESETS)" :key="name" :value="name">
              {{ name }}
            </option>
          </select>
          <label class="rich">选后自动套用温度/上下文/批次间隔</label>
        </div>

        <div class="row">
          <label>重构模式</label>
          <select v-model="refactorMode">
            <option value="full_rewrite">全部重构（默认）</option>
            <option value="fidelity">保真润色</option>
            <option value="fix_gaps">修断层</option>
            <option value="reskin">换皮</option>
          </select>
        </div>

        <div class="row">
          <label>保护门</label>
          <details class="adv gates">
            <summary>重构保护门（默认全关）</summary>
            <label class="rich"
              ><input type="checkbox" v-model="gates.lock_names" /> 锁人名/设定名</label
            >
            <label class="rich"
              ><input type="checkbox" v-model="gates.lock_plot" /> 锁剧情骨架</label
            >
            <label class="rich"
              ><input type="checkbox" v-model="gates.inject_forbidden" /> 注入禁改清单</label
            >
            <label class="rich"
              ><input type="checkbox" v-model="gates.require_chapter_accept" /> 逐章验收</label
            >
            <label class="rich"
              ><input type="checkbox" v-model="gates.skip_stitch_if_smooth" />
              平滑衔接跳过缝合</label
            >
            <label class="rich"
              ><input type="checkbox" v-model="gates.qc_block_export" /> 质检拦截导出</label
            >
          </details>
        </div>

        <div v-if="gates.inject_forbidden || refactorMode === 'reskin'" class="row col">
          <label>禁改清单 / 人名映射（配合“保护门·注入禁改清单”与“换皮”模式）</label>
          <textarea
            v-model="forbiddenCanon"
            rows="2"
            placeholder="禁改清单：每行一条，如&#10;主角不能再死亡&#10;不许改设定名"
            class="lone-area"
          ></textarea>
          <textarea
            v-model="nameMapText"
            rows="2"
            placeholder="换皮映射：每行 旧名=新名，如&#10;林晚=苏晴"
            class="lone-area"
          ></textarea>
        </div>

        <div class="row">
          <label>输出书名</label>
          <input v-model="novelName" placeholder="可选，默认取输入文件夹名" />
        </div>

        <div class="row">
          <label>拆书确认</label>
          <label class="rich"
            ><input type="checkbox" v-model="reviewSplit" /> 拆书后先预览章节再继续</label
          >
          <label
            class="rich"
            title="仅在规则识别不可靠（没认出章节 / 章节长度异常）时才额外调用一次模型，成本很低"
          >
            <input type="checkbox" v-model="aiSplit" /> 识别不到章节时用 AI
            辅助定位边界（多一次模型调用）
          </label>
        </div>

        <details class="adv">
          <summary>优化项：左右对照 / 章快照 / 诊断 JSON</summary>
          <div class="row">
            <label class="rich"
              ><input type="checkbox" v-model="compareOutput" />
              左右对照（重构后按章生成原文|重构后表格）</label
            >
          </div>
          <div class="row">
            <label class="rich"
              ><input type="checkbox" v-model="chapterSnapshots" />
              章快照（保留重构初稿与缝合终稿）</label
            >
          </div>
          <div class="row">
            <label>诊断JSON输出目录</label>
            <input
              v-model="diagnoseJson"
              placeholder="可选，留空关闭；设置后按批次把结构化断层 JSON 写入该目录"
            />
          </div>
        </details>

        <div class="row">
          <label>代理</label>
          <input v-model="proxy" placeholder="可选，如 http://127.0.0.1:7890" />
          <label class="rich"><input type="checkbox" v-model="richText" /> docx富文本</label>
          <label class="rich" title="仅在使用 https 自签证书的服务端时需要关闭"
            ><input type="checkbox" v-model="sslVerify" /> 校验HTTPS证书</label
          >
        </div>

        <details class="adv">
          <summary>高级项：各阶段温度</summary>
          <div class="row">
            <label>诊断</label>
            <input v-model.number="tDiagnose" type="number" min="0" max="2" step="0.1" />
            <label class="t">蓝图</label>
            <input v-model.number="tBlueprint" type="number" min="0" max="2" step="0.1" />
          </div>
          <div class="row">
            <label>重构</label>
            <input v-model.number="tRefactor" type="number" min="0" max="2" step="0.1" />
            <label class="t">缝合</label>
            <input v-model.number="tStitch" type="number" min="0" max="2" step="0.1" />
          </div>
          <div class="row">
            <label>批次间隔(秒)</label>
            <input v-model.number="batchInterval" type="number" min="0" max="300" step="0.5" />
          </div>
        </details>
        <details class="adv">
          <summary>分阶段模型（可选，留空用全局模型）</summary>
          <p class="ph-note">
            地址与全局<strong>不同主机</strong>时，全局 API Key
            不会带过去（防泄露）；需要鉴权请在对应「密钥」里单独填。
          </p>
          <div class="row">
            <label>诊断</label> <input v-model="mDiagnose" placeholder="诊断用模型" />
            <label class="t">地址</label> <input v-model="uDiagnose" placeholder="(留空=全局)" />
          </div>
          <div class="row">
            <label class="rich-k">密钥</label>
            <input
              v-model="kDiagnose"
              type="password"
              placeholder="(留空=不同主机时不带全局 Key)"
            />
          </div>
          <div class="row">
            <label>蓝图</label> <input v-model="mBlueprint" placeholder="蓝图用模型" />
            <label class="t">地址</label> <input v-model="uBlueprint" placeholder="(留空=全局)" />
          </div>
          <div class="row">
            <label class="rich-k">密钥</label>
            <input
              v-model="kBlueprint"
              type="password"
              placeholder="(留空=不同主机时不带全局 Key)"
            />
          </div>
          <div class="row">
            <label>重构</label> <input v-model="mRefactor" placeholder="重构用模型" />
            <label class="t">地址</label> <input v-model="uRefactor" placeholder="(留空=全局)" />
          </div>
          <div class="row">
            <label class="rich-k">密钥</label>
            <input
              v-model="kRefactor"
              type="password"
              placeholder="(留空=不同主机时不带全局 Key)"
            />
          </div>
          <div class="row">
            <label>缝合</label> <input v-model="mStitch" placeholder="缝合用模型" />
            <label class="t">地址</label> <input v-model="uStitch" placeholder="(留空=全局)" />
          </div>
          <div class="row">
            <label class="rich-k">密钥</label>
            <input v-model="kStitch" type="password" placeholder="(留空=不同主机时不带全局 Key)" />
          </div>
        </details>

        <div class="row col">
          <label>作者风格</label>
          <textarea v-model="authorStyle" rows="2"></textarea>
        </div>

        <button class="start-btn" :disabled="!canStart" @click="startPipeline">开始重构</button>
      </section>

      <!-- 进度区 -->
      <section class="panel progress">
        <h2>运行进度</h2>
        <div class="phase-bar">
          <span v-if="currentPhase" class="phase">{{ currentPhase }}</span>
          <span v-if="isPaused" class="badge paused">已暂停</span>
          <span v-if="pipelineRunning" class="badge running">运行中</span>
          <button v-if="outputPath && port" class="open-btn" @click="openOutput">
            打开输出目录
          </button>
          <button v-if="outputPath && port" class="open-btn ghost" @click="openFinal">
            打开成品
          </button>
          <button v-if="outputPath && port" class="open-btn ghost" @click="openComparisons">
            对照预览
          </button>
          <button v-if="outputPath && port" class="open-btn ghost" @click="openVersions">
            成品版本
          </button>
          <button
            v-if="port"
            class="open-btn ghost"
            @click="
              showHistory = true;
              refreshHistory();
            "
          >
            任务历史<template v-if="historyPendingCount"> · {{ historyPendingCount }}</template>
          </button>
          <button
            v-if="outputPath && port"
            class="open-btn ghost"
            :disabled="pipelineRunning || clearingOutput"
            @click="clearOutputDir"
          >
            {{ clearingOutput ? '清空中…' : '清空输出目录' }}
          </button>
        </div>
        <p
          v-if="outInfo && outInfo.exists && outInfo.artifacts && outInfo.artifacts.length"
          class="out-status"
        >
          <template v-if="outInfo.resumable">
            检测到<strong>未完成</strong>的进度：{{ outInfo.novel_name || '本书' }} · 共
            {{ outInfo.total_chapters }} 章 · 已完成批次 {{ outInfo.batches_done }}/{{
              outInfo.batches_total
            }}
            （阶段 {{ outInfo.current_phase }}）——
            点开始将<strong>断点续跑</strong>，已完成的批次不会重做。
          </template>
          <template v-else-if="outInfo.finished"
            >该目录含已完成的成品，点开始会重新生成同名文件（旧文件自动备份）。</template
          >
          <template v-else>该目录已含 TextForge 产物。</template>
          <br />
          如要彻底重来，请点「清空输出目录」（只删 TextForge 自己生成的文件，不影响你的其它文件）。
        </p>
        <p v-if="outInfo && outInfo.error_logs && outInfo.error_logs.length" class="out-status err">
          上次中断原因：{{ outInfo.error_logs[outInfo.error_logs.length - 1].message }}
        </p>

        <div class="controls" v-if="pipelineRunning">
          <button @click="control('pause')" :disabled="isPaused || controlling">暂停</button>
          <button @click="control('resume')" :disabled="!isPaused || controlling">恢复</button>
          <button class="danger" @click="control('stop')" :disabled="controlling">停止</button>
        </div>

        <RunStats
          :total-chapters="totalChapters"
          :batch-stats="batchStats"
          :chapter-progress="chapterProgress"
          :usage-tokens="usageTokens"
          :elapsed="elapsed"
          :pipeline-running="pipelineRunning"
          :diag-percent="diagPercent"
          :recon-percent="reconPercent"
          :chapter-percent="chapterPercent"
        />

        <div class="tree-head" v-if="chapterRows.length">
          <button class="tree-toggle" @click="showChapterTree = !showChapterTree">
            {{ showChapterTree ? '▾' : '▸' }} 章节概览（{{
              chapterRows.filter((c) => c.status === 'reconstructed').length
            }}/{{ chapterRows.length }} 已重构）
          </button>
        </div>
        <div v-if="showChapterTree && chapterRows.length" class="tree-box">
          <div v-for="c in chapterRows" :key="c.id" class="tree-row">
            <span class="split-id">{{ c.label }}</span>
            <span class="badge" :class="c.status === 'reconstructed' ? 'running' : ''">{{
              c.status === 'reconstructed' ? '已重构' : '待处理'
            }}</span>
          </div>
        </div>

        <div v-if="streamingText" class="stream-box">
          <b class="stream-title">流式输出</b>{{ streamingText }}
        </div>

        <div class="log-head">
          <b>事件日志</b>
          <span class="log-count">{{ eventLog.length }} 条</span>
          <button class="min-btn" @click="exportLog">导出</button>
          <button class="min-btn" @click="eventLog = []">清空</button>
        </div>
        <div class="bar chapter-row-bar" v-if="finalFiles.length">
          <span class="bar-label">成品文件</span>
          <div class="final-list">
            <button
              v-for="f in finalFiles"
              :key="f.path"
              class="min-btn"
              @click="callOpenOutput(f.path)"
            >
              {{ f.name }}
            </button>
          </div>
        </div>
        <div ref="logBoxRef" class="log-box">
          <div v-for="(line, i) in eventLog" :key="i" class="log-line" :class="`log-${line.level}`">
            <span class="log-ts">{{ line.ts }}</span
            >{{ line.text }}
          </div>
          <div v-if="!eventLog.length" class="log-empty">暂无事件</div>
        </div>
      </section>
    </main>

    <!-- 拆书确认弹窗 -->
    <GateModal v-if="showSplit" title="请确认拆书结果">
      <p class="hint">共拆分出 {{ splitChapters.length }} 章。确认后进入诊断。</p>
      <p v-if="splitDetection" class="hint" :class="splitDetection.needs_review ? 'err' : 'ok'">
        识别方式：{{ splitDetection.mode }}
        <template v-if="splitDetection.confidence"
          >· 置信度 {{ splitDetection.confidence }}</template
        >
        <template v-if="splitDetection.reason">· {{ splitDetection.reason }}</template>
      </p>
      <div class="sum-list">
        <div v-for="(c, i) in splitChapters" :key="i" class="split-row">
          <span class="split-id">{{ c.title || c.filename }}</span>
          <span v-if="c.is_virtual" class="badge paused">虚拟段</span>
          <span v-if="c.is_empty" class="badge err2">空章</span>
        </div>
      </div>
      <!-- 兜底：规则与 AI 都搞不定的创新排版，让用户直接给出章节标题行 -->
      <details class="redo-split">
        <summary>拆分不对？按自定义章节标记重新拆分</summary>
        <p class="hint">
          每行填一个章节标题，需与原文该行<strong>完全一致</strong>（含标点空格）。程序按这些行切分，正文不会改写。
        </p>
        <textarea
          v-model="redoMarkers"
          rows="4"
          placeholder="楔子&#10;风起于青萍之末&#10;尾声"
        ></textarea>
        <button :disabled="redoingSplit" @click="redoSplit">
          {{ redoingSplit ? '重拆中…' : '按此标记重新拆分' }}
        </button>
      </details>
      <template #actions>
        <span></span>
        <button :disabled="confirmingSplit" @click="confirmSplit">
          {{ confirmingSplit ? '确认中…' : '确认拆分，继续' }}
        </button>
      </template>
    </GateModal>

    <!-- 断层勾选确认弹窗 -->
    <GateModal v-if="showFixReview" title="修复断层：请勾选需修复项">
      <p class="hint">
        共诊断出
        {{ fixGaps.length }} 处断层，默认全选。确认后将按勾选项执行修复（其余内容保持原样）。
      </p>
      <label class="sum-toggle"
        ><input
          type="checkbox"
          :checked="fixGaps.length > 0 && fixSelected.length === fixGaps.length"
          @change="
            fixGaps.length === fixSelected.length
              ? (fixSelected = [])
              : (fixSelected = fixGaps.map((g: any) => g.id))
          "
        />
        全选 / 全不选</label
      >
      <div class="sum-list">
        <label v-for="g in fixGaps" :key="g.id" class="fix-row">
          <input type="checkbox" :checked="fixSelected.includes(g.id)" @change="toggleFix(g.id)" />
          <span class="split-id">{{ g.location || g.batch_id }}</span>
          <span
            class="badge"
            :class="
              g.severity === 'high' ? 'badge-err' : g.severity === 'medium' ? 'badge-warn' : ''
            "
            >{{ g.severity }}</span
          >
          <span class="fix-desc">{{ g.description }}</span>
        </label>
      </div>
      <template #actions>
        <span></span>
        <button :disabled="confirmingFix" @click="confirmFix">
          确认修复（{{ fixSelected.length }} 处）
        </button>
      </template>
    </GateModal>

    <!-- 逐章验收确认弹窗 -->
    <GateModal v-if="showAccept" :title="`验收重构稿：批次 ${acceptBatch}`">
      <p class="hint">
        以下为本批已产出的章节（预览为开头 80 字）。验收通过后继续下一批；不通过请停止后调整或重跑。
      </p>
      <div class="sum-list">
        <div v-for="c in acceptChapters" :key="c.id" class="split-row accept-row">
          <span class="split-id">{{ c.id }}</span>
          <span class="fix-desc">{{ c.preview }}</span>
          <button class="min-btn" :disabled="regenerating.has(c.id)" @click="regenChapter(c)">
            {{ regenerating.has(c.id) ? '重写中…' : '重写此章' }}
          </button>
        </div>
      </div>
      <template #actions>
        <span></span>
        <button :disabled="confirmingAccept" @click="confirmAccept">验收通过，继续</button>
      </template>
    </GateModal>

    <!-- 质检拦截决策弹窗 -->
    <GateModal v-if="showQcBlock" :title="`质检未通过（${qcIssues.length} 项）`">
      <p class="hint">
        以下问题按规则检出不满足导出条件。可选择「强制导出」忽略并继续，或「停止」保留进度待修正后重跑。
      </p>
      <div class="sum-list">
        <div v-for="(it, i) in qcIssues" :key="i" class="split-row">
          <span class="fix-desc">{{ it }}</span>
        </div>
      </div>
      <template #actions>
        <span></span>
        <button class="danger" @click="control('stop')">停止</button>
        <button class="primary" :disabled="forcingExport" @click="forceExport">强制导出</button>
      </template>
    </GateModal>

    <!-- 左右对照预览弹窗 -->
    <div v-if="showComparisons" class="modal-mask">
      <div class="modal modal-wide">
        <h3>左右对照预览</h3>
        <div class="row" v-if="comparisonItems.length">
          <label>章节</label>
          <select v-model="comparisonSel">
            <option v-for="it in comparisonItems" :key="it.name" :value="it.name">
              {{ it.name }}
            </option>
          </select>
        </div>
        <pre class="comp-preview">{{
          (comparisonItems.find((i) => i.name === comparisonSel) || {}).text
        }}</pre>
        <div class="modal-actions">
          <span></span>
          <button @click="showComparisons = false">关闭</button>
        </div>
      </div>
    </div>

    <!-- 蓝图确认弹窗 -->
    <GateModal v-if="showBlueprint" title="请检查并确认蓝图">
      <p class="hint">可直接编辑后确认，蓝图将作为后续重构的系统提示词。</p>
      <textarea v-model="blueprintText" rows="16" class="blueprint-area"></textarea>

      <label class="sum-toggle" :class="{ off: !showSummaries }">
        <input type="checkbox" v-model="showSummaries" />
        查看本步诊断摘要（生成蓝图的输入）
      </label>
      <div v-if="showSummaries" class="sum-list">
        <div v-if="!summaries.length" class="sum-empty">暂无诊断摘要</div>
        <details v-for="(s, i) in summaries" :key="i" class="sum-item">
          <summary>{{ s.name }}</summary>
          <div class="sum-preview">{{ s.preview }}</div>
        </details>
      </div>

      <template #actions>
        <span class="count">{{ blueprintText.length }} 字</span>
        <button @click="confirmBlueprint" :disabled="confirmingBlueprint">
          {{ confirmingBlueprint ? '确认中...' : '确认蓝图' }}
        </button>
      </template>
    </GateModal>

    <!-- 任务历史（跨会话） -->
    <HistoryModal
      v-if="showHistory"
      :items="historyItems"
      :query="histQuery"
      :sort="histSort"
      :busy="histBusy"
      @update:query="histQuery = $event"
      @update:sort="histSort = $event"
      @refresh="refreshHistory()"
      @load="loadFromHistory"
      @forget="forgetHistoryItem"
      @clear="clearAllHistory"
      @close="showHistory = false"
    />

    <!-- 成品版本（重跑前的整版快照，可回滚） -->
    <VersionsModal
      v-if="showVersions"
      :versions="versionList"
      :busy="versionBusy"
      @restore="doRestore"
      @close="showVersions = false"
    />

    <!-- 系统与后端设置 -->
    <SettingsModal
      v-if="showSettings"
      :port="port"
      :connected="backendReady"
      :model="model"
      :api-url="apiUrl"
      :api-key="apiKey"
      :proxy="proxy"
      :ssl-verify="sslVerify"
      :context-window="contextWindow"
      @close="showSettings = false"
      @reconnect="reconnectBackend"
      @apply-model="applyModelFromSettings"
      @apply-context="applyContextFromSettings"
    />
  </div>
</template>

<style scoped>
.app {
  display: flex;
  flex-direction: column;
  height: 100vh;
  font-family: 'Microsoft YaHei', system-ui, sans-serif;
  background: #f5f6f8;
  color: #1f2328;
}
.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 24px;
  background: #2d3748;
  color: #fff;
}
.header h1 {
  font-size: 18px;
  margin: 0;
}
.version {
  font-size: 12px;
  opacity: 0.7;
  font-weight: normal;
}
.status {
  font-size: 13px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
}
.dot.ok {
  background: #48bb78;
}
.dot.err {
  background: #fc8181;
}

.main {
  flex: 1;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  padding: 16px;
  overflow: hidden;
}
.panel {
  background: #fff;
  border-radius: 8px;
  padding: 16px 20px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
  overflow-y: auto;
}
.panel h2 {
  font-size: 15px;
  margin: 0 0 14px;
  color: #2d3748;
  display: flex;
  align-items: center;
}
.panel-gear {
  margin-left: auto;
  padding: 3px 12px;
  font-size: 12px;
  background: #fff;
  border: 1px solid #cbd5e0;
  color: #4a5568;
  border-radius: 5px;
  cursor: pointer;
}
.panel-gear:hover {
  background: #edf2f7;
}
.panel.config.disabled {
  opacity: 0.55;
  pointer-events: none;
}
.panel.progress {
  display: flex;
  flex-direction: column;
}

/* 后端服务行 */
.backend-row {
  background: #f7fafc;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  padding: 8px 10px;
  margin-bottom: 12px;
}
.backend-state {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: #2d3748;
}
.backend-row button {
  padding: 4px 12px;
  border: 1px solid #d1d5db;
  background: #fff;
  border-radius: 5px;
  cursor: pointer;
  font-size: 12px;
}
.backend-row button:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.backend-row button.ghost {
  border-color: #3182ce;
  color: #3182ce;
}

/* 顶栏：状态可点击 + 齿轮入口 */
.status-link {
  display: flex;
  align-items: center;
  gap: 6px;
  background: transparent;
  border: none;
  color: #fff;
  font-size: 13px;
  cursor: pointer;
  padding: 2px 4px;
  border-radius: 4px;
  max-width: 340px;
}
.status-link:hover {
  background: rgba(255, 255, 255, 0.12);
}
.gear {
  background: transparent;
  border: 1px solid rgba(255, 255, 255, 0.4);
  color: #fff;
  border-radius: 5px;
  font-size: 15px;
  cursor: pointer;
  width: 28px;
  height: 24px;
  line-height: 1;
}
.gear:hover {
  background: rgba(255, 255, 255, 0.12);
}

/* 断连横幅里的操作按钮 */
.banner-act {
  display: flex;
  align-items: center;
  gap: 6px;
}
.banner-btn {
  padding: 3px 10px;
  font-size: 12px;
  cursor: pointer;
  border: 1px solid currentColor;
  background: transparent;
  color: inherit;
  border-radius: 4px;
}
.banner-btn.primary {
  background: #822727;
  border-color: #822727;
  color: #fff;
}

.banner {
  grid-column: 1 / -1;
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 14px;
  border-radius: 6px;
  font-size: 13px;
}
.banner.tip {
  background: #c6f6d5;
  color: #22543d;
  border: 1px solid #9ae6b4;
}
.banner.err {
  background: #fed7d7;
  color: #822727;
  border: 1px solid #feb2b2;
}
.banner.warn {
  background: #fefcbf;
  color: #975a16;
  border: 1px solid #f6e05e;
}
.banner-close {
  background: transparent;
  border: none;
  font-size: 16px;
  cursor: pointer;
  color: inherit;
  padding: 0 4px;
}

.bar-fill.recon {
  background: #38a169;
}
.bar-fill.chapter {
  background: #805ad5;
}
.tree-head {
  margin-bottom: 6px;
}
.tree-toggle {
  background: transparent;
  border: none;
  color: #4a5568;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  padding: 4px 0;
}
.tree-box {
  max-height: 150px;
  overflow-y: auto;
  border: 1px solid #e2e8f0;
  border-radius: 5px;
  padding: 6px 10px;
  margin-bottom: 10px;
  background: #f7fafc;
}
.tree-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 2px 0;
  font-size: 12px;
}
html.dark .tree-box {
  background: #232b39;
  border-color: #4a5568;
}
html.dark .tree-toggle {
  color: #a0aec0;
}

.row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}
.row.col {
  flex-direction: column;
  align-items: stretch;
}
.row label {
  width: 80px;
  font-size: 13px;
  color: #4a5568;
  flex-shrink: 0;
}
.row label.fmt,
.row label.ctx,
.row label.t {
  width: auto;
  margin-left: 12px;
}
.row label.rich {
  width: auto;
  display: flex;
  align-items: center;
  gap: 4px;
  cursor: pointer;
}
.row label.rich input {
  width: auto;
  margin: 0;
}
.row select {
  flex: 1;
  padding: 6px 10px;
  border: 1px solid #d1d5db;
  border-radius: 5px;
  font-size: 13px;
}
.row input {
  flex: 1;
  min-width: 0;
  padding: 6px 10px;
  border: 1px solid #d1d5db;
  border-radius: 5px;
  font-size: 13px;
}
.row label.t + input,
.row label.rich + input {
  flex: 1;
}
.row label.rich-k {
  width: auto;
  margin-left: 12px;
  font-size: 12px;
  color: #718096;
}
.ph-note {
  font-size: 12px;
  color: #718096;
  margin: 4px 0 8px;
  line-height: 1.5;
}
.adv {
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  padding: 8px 12px;
  margin-bottom: 10px;
}
.adv summary {
  font-size: 13px;
  color: #4a5568;
  cursor: pointer;
}
.adv.gates {
  display: flex;
  flex-direction: column;
  flex: 1;
}
.adv.gates .rich {
  margin: 3px 0;
}
.row textarea {
  width: 100%;
  padding: 6px 10px;
  border: 1px solid #d1d5db;
  border-radius: 5px;
  font-size: 13px;
  resize: vertical;
}
.lone-area + .lone-area {
  margin-top: 6px;
}
.row button {
  padding: 6px 14px;
  border: 1px solid #d1d5db;
  background: #fff;
  border-radius: 5px;
  cursor: pointer;
  font-size: 13px;
}
.row button:hover {
  background: #f0f4f8;
}

.start-btn {
  width: 100%;
  margin-top: 8px;
  padding: 10px;
  background: #3182ce;
  color: #fff;
  border: none;
  border-radius: 6px;
  font-size: 14px;
  cursor: pointer;
}
.start-btn:disabled {
  background: #a0aec0;
  cursor: not-allowed;
}

.phase-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}
.phase {
  font-size: 14px;
  font-weight: 600;
  color: #2d3748;
}

.controls {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}
.controls button {
  padding: 5px 14px;
  border: 1px solid #d1d5db;
  background: #fff;
  border-radius: 5px;
  cursor: pointer;
  font-size: 13px;
}
.controls button.danger {
  color: #c53030;
  border-color: #feb2b2;
}
.controls button:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.stream-box {
  max-height: 72px;
  overflow-y: auto;
  background: #edf2f7;
  border: 1px solid #e2e8f0;
  border-radius: 5px;
  padding: 8px 10px;
  margin-bottom: 10px;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
  color: #2d3748;
}
.stream-title {
  color: #3182ce;
  margin-right: 6px;
}

.log-box {
  flex: 1;
  min-height: 220px;
  overflow-y: auto;
  background: #1a202c;
  color: #e2e8f0;
  border-radius: 6px;
  padding: 10px 12px;
  font-family: 'Consolas', monospace;
  font-size: 12px;
  line-height: 1.6;
}
.log-line {
  white-space: pre-wrap;
  word-break: break-all;
}
.log-line .log-ts {
  color: #718096;
  margin-right: 8px;
}
.log-ok {
  color: #9ae6b4;
}
.log-warn {
  color: #fefcbf;
}
.log-err {
  color: #fc8181;
}
.log-accent {
  color: #90cdf4;
}
.log-head {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #4a5568;
  font-size: 12px;
  margin-bottom: 6px;
}
.log-count {
  color: #718096;
}
.min-btn {
  margin-left: auto;
  padding: 2px 10px;
  background: transparent;
  border: 1px solid #cbd5e0;
  border-radius: 4px;
  color: #4a5568;
  font-size: 12px;
  cursor: pointer;
}
.min-btn:hover {
  background: #edf2f7;
}

.blueprint-area {
  flex: 1;
  width: 100%;
  padding: 10px;
  border: 1px solid #d1d5db;
  border-radius: 5px;
  font-size: 13px;
  resize: vertical;
  min-height: 300px;
}
.sum-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: #4a5568;
  cursor: pointer;
  margin: 10px 0 4px;
}
.sum-toggle input {
  width: auto;
  margin: 0;
}
.sum-empty {
  color: #718096;
}
.split-id {
  flex: 1;
  color: #2d3748;
  word-break: break-all;
}
.redo-split {
  margin-top: 10px;
}
.redo-split summary {
  cursor: pointer;
  color: #4a5568;
  font-size: 13px;
}
.redo-split textarea {
  width: 100%;
  margin-top: 8px;
  padding: 6px 8px;
  font-family: inherit;
  font-size: 13px;
  border: 1px solid #cbd5e0;
  border-radius: 4px;
  box-sizing: border-box;
}
.redo-split button {
  margin-top: 8px;
}
.fix-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 3px 0;
  cursor: pointer;
}
.fix-row input {
  width: auto;
  margin: 0;
}
.fix-desc {
  color: #4a5568;
  word-break: break-all;
}
.accept-row {
  display: flex;
  align-items: center;
  gap: 6px;
}
.accept-row .fix-desc {
  flex: 1;
}
.accept-row .min-btn {
  margin-left: auto;
  flex: 0 0 auto;
}
.sum-item summary {
  cursor: pointer;
  color: #2d3748;
  margin-bottom: 2px;
}
.sum-preview {
  color: #4a5568;
  white-space: pre-wrap;
  word-break: break-all;
}
.count {
  font-size: 12px;
  color: #718096;
}
.comp-preview {
  margin-top: 10px;
  max-height: 60vh;
  overflow: auto;
  background: #1a202c;
  color: #e2e8f0;
  border-radius: 6px;
  padding: 10px 12px;
  font-family: 'Consolas', monospace;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
}

.theme-toggle {
  margin-left: 8px;
  padding: 2px 10px;
  background: transparent;
  border: 1px solid rgba(255, 255, 255, 0.4);
  color: #fff;
  border-radius: 5px;
  font-size: 14px;
  cursor: pointer;
}
.theme-toggle:hover {
  background: rgba(255, 255, 255, 0.12);
}

.final-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
/* .min-btn 默认 margin-left:auto（为「导出/清空」右对齐设计），在列表里要归零 */
.final-list .min-btn {
  margin-left: 0;
}
.chapter-row-bar {
  align-items: center;
}

/* 深色主题（html.dark）最小覆盖 */
html.dark .app {
  background: #171923;
  color: #e2e8f0;
}
html.dark .panel {
  background: #1f2733;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.4);
}
html.dark .panel h2 {
  color: #e2e8f0;
}
html.dark .panel-gear {
  background: #2d3748;
  border-color: #4a5568;
  color: #e2e8f0;
}
html.dark .backend-row {
  background: #232b39;
  border-color: #4a5568;
}
html.dark .backend-state {
  color: #e2e8f0;
}
html.dark .backend-row button {
  background: #2d3748;
  color: #e2e8f0;
  border-color: #4a5568;
}
html.dark .row label,
html.dark .phase,
html.dark .phase-bar .open-btn {
  color: #e2e8f0;
}
html.dark input,
html.dark select,
html.dark textarea {
  background: #1a202c;
  color: #e2e8f0;
  border-color: #4a5568;
}
html.dark .stats {
  background: #232b39;
  color: #a0aec0;
}
html.dark .stat b {
  color: #e2e8f0;
}
html.dark .bar-track {
  background: #2d3748;
}
html.dark .bar,
html.dark .bar-num,
html.dark .bar-label {
  color: #a0aec0;
}
html.dark .adv {
  border-color: #4a5568;
}
html.dark .adv summary,
html.dark .log-count,
html.dark .sum-empty {
  color: #a0aec0;
}
html.dark .stream-box {
  background: #1a202c;
  border-color: #4a5568;
  color: #e2e8f0;
}
html.dark .sum-list {
  background: #232b39;
  border-color: #4a5568;
}
html.dark .split-id,
html.dark .sum-item summary {
  color: #e2e8f0;
}
html.dark .fix-desc,
html.dark .sum-preview {
  color: #a0aec0;
}
html.dark .modal {
  background: #1f2733;
}
html.dark .controls button,
html.dark .row button {
  background: #2d3748;
  color: #e2e8f0;
  border-color: #4a5568;
}
html.dark .start-btn:disabled {
  background: #4a5568;
}
</style>
