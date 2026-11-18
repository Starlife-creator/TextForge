<script setup lang="ts">
import { ref, reactive, computed, watch, onMounted, onUnmounted } from 'vue';
import { open } from '@tauri-apps/plugin-dialog';
import { isTauri } from '@tauri-apps/api/core';
import { waitForPort } from './api/backend';
import { connectSSE, disconnectSSE, type SSEHandlers } from './api/sse';
import SettingsModal from './components/SettingsModal.vue';
import GateModal from './components/GateModal.vue';
import HistoryModal from './components/HistoryModal.vue';
import VersionsModal from './components/VersionsModal.vue';
import RunStats from './components/RunStats.vue';
import QualityModal from './components/QualityModal.vue';
import ChapterReviewWorkbench from './components/ChapterReviewWorkbench.vue';
import { isReviewWorkbenchPhase } from './api/chapters';
import {
  applyThemeToRoot,
  computePipelineSteps,
  persistThemeChoice,
  resolveInitialTheme,
} from './theme';
import { apiFetch, initAuth, postWithBusyRetry } from './api/client';
import {
  DEFAULT_CONTEXT_WINDOW,
  DEFAULT_PRESET_NAME,
  MIN_CONTEXT_WINDOW,
  PRESETS,
} from './presets';
import {
  fetchHistory,
  forgetHistory,
  fetchVersions,
  restoreVersion,
  fetchQualityReport,
  fetchComparisonList,
  fetchComparisonText,
  estimateRun,
  costOf,
  formatCost,
  type HistoryItem,
  type VersionInfo,
  type QualityReport,
  type ComparisonEntry,
  type EstimateResult,
  type UnitPrice,
} from './api/system';

// 版本号单一来源：package.json（经 vite define 注入，见 vite.config.ts）
const appVersion = __APP_VERSION__;

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
// 上下文窗口默认值与预设档位的取值依据在 src/presets.ts（含「为什么不提供 128k 档」）。
// 老用户不受影响：restore() 会用 localStorage 里已存的值覆盖这个默认值。
const contextWindow = ref(DEFAULT_CONTEXT_WINDOW);
const authorStyle = ref('出版级文学重构，文笔凝练，注重画面感');
const novelName = ref('');
const refactorMode = ref<'full_rewrite' | 'fidelity' | 'fix_gaps' | 'reskin'>('full_rewrite');
// 重构保护门（后端 refactor_gates，默认全关；自动重写与缝合最小改动默认开）
const gates = reactive({
  lock_names: false,
  lock_plot: false,
  inject_forbidden: false,
  require_chapter_accept: false,
  skip_stitch_if_smooth: false,
  qc_block_export: false,
  auto_rewrite_bad_batch: true,
  stitch_minimal_change: true,
});
const GATE_KEYS = [
  'lock_names',
  'lock_plot',
  'inject_forbidden',
  'require_chapter_accept',
  'skip_stitch_if_smooth',
  'qc_block_export',
  'auto_rewrite_bad_batch',
  'stitch_minimal_change',
] as const;
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
// 有界并发：默认串行；云端可调到 2-4，本地小模型不建议
const maxConcurrency = ref(1);
// B2 服务商预设（温度/上下文/批次间隔一键套用，纯前端；取值见 src/presets.ts）
const preset = ref(DEFAULT_PRESET_NAME);
function applyPreset() {
  const p = PRESETS[preset.value] || PRESETS[DEFAULT_PRESET_NAME];
  [tDiagnose.value, tBlueprint.value, tRefactor.value, tStitch.value] = p.temps;
  contextWindow.value = p.ctx;
  batchInterval.value = p.interval;
}
function selectPresetPill(name: string) {
  preset.value = name;
  applyPreset();
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
const comparisonItems = ref<ComparisonEntry[]>([]);
const comparisonSel = ref('');
const comparisonText = ref('');

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
// 单价（元 / 百万 token），0 = 本地模型不计费。只在折算时使用，不发给后端、不进日志。
const priceInput = ref(0);
const priceOutput = ref(0);
const estimate = ref<EstimateResult | null>(null);
const estimating = ref(false);
const estimateError = ref('');
const unitPrice = computed<UnitPrice>(() => ({
  input: Number(priceInput.value) || 0,
  output: Number(priceOutput.value) || 0,
}));
const spentText = computed(() =>
  formatCost(costOf(usageTokens.value.prompt, usageTokens.value.completion, unitPrice.value)),
);
const estimateCostText = computed(() =>
  estimate.value
    ? formatCost(
        costOf(estimate.value.tokens.prompt, estimate.value.tokens.completion, unitPrice.value),
      )
    : '',
);
const batchStats = ref({ total: 0, phase1: 0, phase3: 0 });
const chapterProgress = ref({ total: 0, done: 0 });
const chapterRows = ref<{ id: string; label: string; status: string }[]>([]);
const showChapterTree = ref(false);
// 边跑边审三栏工作台状态：在 phase3 / phase3_accept / phase4 及已完成目录自动激活
const rawPhase = ref('');
const reviewRefreshTick = ref(0);
const reviewSummary = ref<{
  total_chapters: number;
  ready_chapters: number;
  approved_chapters: number;
  edited_chapters: number;
} | null>(null);
const showReviewWorkbench = computed(
  () =>
    Boolean(port.value && outputPath.value) &&
    isReviewWorkbenchPhase(
      rawPhase.value || String(outInfo.value?.current_phase || ''),
      Boolean(outInfo.value?.finished || currentPhase.value === '完成'),
      chapterProgress.value.done || (reviewSummary.value?.ready_chapters ?? 0),
    ),
);
const topError = ref<string | null>(null);
// 轮询已点亮的最后一条错误，用于避免同一错误被每个轮询周期重复点亮。
let lastPolledError: string | null = null;
const topTip = ref<string | null>(null);
// B4：僵尸运行检测
const zombieWarn = ref(false);
const darkMode = ref(false);
function toggleTheme() {
  darkMode.value = !darkMode.value;
  const next = darkMode.value ? 'dark' : 'light';
  applyThemeToRoot(next);
  persistThemeChoice(next);
  persist();
}

const pipelineSteps = computed(() =>
  computePipelineSteps({
    rawPhase: rawPhase.value || String(outInfo.value?.current_phase || ''),
    currentPhaseLabel: currentPhase.value,
    finished: Boolean(outInfo.value?.finished || currentPhase.value === '完成'),
    running: pipelineRunning.value,
  }),
);

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
        maxConcurrency: maxConcurrency.value,
        gates: { ...gates },
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
        priceInput: priceInput.value,
        priceOutput: priceOutput.value,
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
    // 优先读取显式主题键 localStorage.getItem('tf-theme')，未设置时兼容旧版 prefs.darkMode
    const initialTheme = resolveInitialTheme(
      typeof localStorage !== 'undefined' ? localStorage : null,
    );
    darkMode.value = initialTheme === 'dark';
    applyThemeToRoot(initialTheme);

    const raw = localStorage.getItem(PERSIST_KEY);
    if (!raw) return;
    const p = JSON.parse(raw);
    if (typeof p.apiUrl === 'string') apiUrl.value = p.apiUrl;
    if (typeof p.model === 'string') model.value = p.model;
    // 下限护栏：老版本可能存过 4096 之类的值，直接回填会让 /api/start 收到
    // 后端 ge=8000 拒绝的 422，而且报错很难懂。
    if (typeof p.contextWindow === 'number' && Number.isFinite(p.contextWindow))
      contextWindow.value = Math.max(MIN_CONTEXT_WINDOW, Math.round(p.contextWindow));
    if (typeof p.authorStyle === 'string') authorStyle.value = p.authorStyle;
    // 白名单校验：localStorage 可能存着旧版/损坏的值，直接赋值会让
    // /api/start 收到非法 refactor_mode 而 422（提示还很难懂）。
    if (['full_rewrite', 'fidelity', 'fix_gaps', 'reskin'].includes(p.refactorMode))
      refactorMode.value = p.refactorMode;
    if (typeof p.batchInterval === 'number') batchInterval.value = p.batchInterval;
    if (typeof p.maxConcurrency === 'number')
      maxConcurrency.value = Math.min(4, Math.max(1, Math.round(p.maxConcurrency)));
    if (p.gates && typeof p.gates === 'object') {
      for (const key of GATE_KEYS) {
        if (typeof p.gates[key] === 'boolean') gates[key] = p.gates[key];
      }
    }
    if (localStorage.getItem('tf-theme') == null && typeof p.darkMode === 'boolean') {
      darkMode.value = p.darkMode;
      applyThemeToRoot(p.darkMode ? 'dark' : 'light');
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
    // 单价：只接受非负数字；损坏/负值一律回落到 0（= 本地不计费），不让它污染折算
    if (typeof p.priceInput === 'number' && p.priceInput >= 0) priceInput.value = p.priceInput;
    if (typeof p.priceOutput === 'number' && p.priceOutput >= 0) priceOutput.value = p.priceOutput;
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

// ---------- 成品质量报告 ----------
const showQuality = ref(false);
const qualityReport = ref<QualityReport | null>(null);
const qualityBusy = ref(false);
const qualityError = ref<string | null>(null);

// 报告状态同时出现在按钮上：不必打开弹窗就知道这次成品有没有硬性问题。
// 数据来自 /api/output/inspect 的轻量 quality 字段，不额外拉整份报告。
const qualityLabel = computed(() => {
  const s = outInfo.value?.quality?.status;
  return s === 'good' ? '通过' : s === 'warn' ? '留意' : s === 'bad' ? '有问题' : '';
});

async function openQuality() {
  if (!port.value || !outputPath.value) return;
  showQuality.value = true;
  qualityBusy.value = true;
  qualityError.value = null;
  try {
    qualityReport.value = await fetchQualityReport(port.value, outputPath.value);
  } catch (e: any) {
    qualityReport.value = null;
    qualityError.value = e?.message || String(e);
  } finally {
    qualityBusy.value = false;
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
    // R10：这两个字段过去被前端整个忽略。progress.json 读坏了 / 闸门超时暂停时，
    // 界面显示的是「一片空白，像什么都没跑」，而真相在响应的这两个字段里。
    if (s.progress_error) topError.value = `进度文件读不了：${s.progress_error}`;
    if (s.gate_timed_out)
      topError.value =
        `闸门超时，任务已安全暂停：${s.gate_timeout_gate || '未知闸门'}` +
        `（已等待 ${Math.round((s.gate_wait_seconds || 0) / 60)} 分钟）`;
    isPaused.value = !!s.paused;
    if (Array.isArray(s.config_drift) && s.config_drift.length) {
      // 续跑时配置变了 → 前几批用旧配置、后几批用新配置，可能风格割裂
      topTip.value = `续跑注意：${s.config_drift.join('；')}。已完成批次不会重做，前后风格可能不一致。`;
    }
    // 必须无条件赋值：原实现只有 `if (s.pipeline_running) pipelineRunning = true`，
    // 没有 else。后端进程一死，fetch 抛异常被空 catch 吞掉，pipelineRunning 就永远
    // 卡在 true → canStart 恒 false → .panel.config.disabled 的 pointer-events:none
    // 把整个配置面板冻住，只能刷新页面。
    const wasRunning = pipelineRunning.value;
    pipelineRunning.value = !!s.pipeline_running;
    // done 事件可能因网络抖动丢失；状态里的 completed 是权威终态。
    // 只在本轮确实认为「还在跑」时收尾，避免页面加载一个已完成目录时反复弹提示。
    if (s.completed && wasRunning) {
      currentPhase.value = '完成';
      closeAllGates();
      stopStatusPolling();
      topTip.value = `全部完成！成品在：${outputPath.value}\\03_final`;
      addLog('全部完成（已从状态轮询确认）', 'ok');
      stopElapsed();
      refreshHistory();
      refreshOutInfo();
    }
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
    reviewSummary.value =
      s.review_summary && typeof s.review_summary === 'object' ? s.review_summary : null;
    if (typeof s.current_phase === 'string') rawPhase.value = s.current_phase;
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
      rawPhase.value = 'phase0';
      currentPhase.value = payload.resumed ? '（断点恢复中）' : '启动';
      topTip.value = payload.resumed
        ? '检测到未完成的进度，将从断点继续（已完成的阶段/批次自动跳过）'
        : null;
      addLog(`流水线启动${payload.resumed ? '（断点恢复）' : ''}`);
      startElapsed();
      break;
    case 'phase_done':
      if (payload.phase === 'phase0') totalChapters.value = payload.total_chapters;
      if (typeof payload.phase === 'string') {
        rawPhase.value = payload.phase === 'phase2' ? 'phase3-1' : payload.phase;
      }
      currentPhase.value = phaseLabel[payload.phase] || payload.phase;
      addLog(
        `阶段完成：${phaseLabel[payload.phase] || payload.phase}${payload.resumed_skip ? '（恢复跳过）' : ''}`,
        'ok',
      );
      break;
    case 'batch_start':
      rawPhase.value = 'phase3-1';
      if (streamRaf) {
        cancelAnimationFrame(streamRaf);
        streamRaf = 0;
      }
      streamBuf = '';
      streamingText.value = ''; // 新批次清空流式预览
      addLog(`批次 ${payload.batch_id} 开始：章节 ${payload.chapter_ids?.join(',')}`, 'accent');
      break;
    case 'batch_done':
      reviewRefreshTick.value++;
      addLog(`批次 ${payload.batch_id} 完成`, 'accent');
      break;
    case 'batch_stream_ready':
      rawPhase.value = rawPhase.value || 'phase3-1';
      reviewRefreshTick.value++;
      addLog(
        `边跑边审：批次 ${payload.batch_id ?? payload.batch_index} 已就绪（${payload.chapters?.length ?? 0} 章可立即审阅精修，进度 ${payload.completed_batches ?? 0}/${payload.total_batches ?? 0} 批）`,
        'ok',
      );
      break;
    case 'batch_split':
      // 批次因「上下文超限 / 分隔符契约失败」被自动对半拆分重跑。
      // 这是本应用在弱模型/小显存下的自愈机制，必须让用户看见：
      // 否则用户只会觉得「怎么跑了这么久」，而不知道程序已在自动降规模。
      addLog(payload.message || `批次 ${payload.from_batch} 自动拆分重跑`, 'warn');
      break;
    case 'qc_auto_rewrite':
      addLog(payload.message || `批次 ${payload.batch_id} 检测到硬伤，已自动重写一次`, 'warn');
      break;
    case 'qc_warning':
      // 后端两类 qc_warning（批次遗留 / 单章重写后仍有硬伤）文案不同，
      // 必须优先用事件自带的 message，否则章节级事件会被显示成「批次遗留」。
      addLog(
        payload.message || `批次质检仍有 ${payload.count ?? 0} 项遗留问题，详见质量报告`,
        'warn',
      );
      break;
    case 'phase_note':
      if (payload.message) addLog(payload.message);
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
      rawPhase.value = 'phase3_accept';
      reviewRefreshTick.value++;
      acceptBatch.value = payload.batch_id;
      acceptChapters.value = payload.chapters || [];
      showAccept.value = true;
      addLog(`批次 ${payload.batch_id} 重构稿已产出，请验收后继续`, 'warn');
      break;
    case 'qc_failed':
      // 质检拦截导出门：展示质检问题，供用户选择强制导出或停止
      rawPhase.value = 'phase3_qc';
      reviewRefreshTick.value++;
      qcIssues.value = payload.issues || [];
      showQcBlock.value = true;
      addLog(`质检未通过（${qcIssues.value.length} 项问题），请决定是否强制导出`, 'err');
      break;
    case 'blueprint_progress':
      // 长书在 phase2 会先把逐批摘要在本地分卷压缩，期间没有流式正文，
      // 不显示进度的话看起来像卡住了。
      currentPhase.value = `阶段2：压缩全书摘要 ${payload.done}/${payload.total}`;
      if (payload.done === payload.total) {
        addLog(`蓝图摘要第 ${payload.round} 轮压缩完成（${payload.total} 段）`);
      }
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
      rawPhase.value = 'phase4';
      currentPhase.value = '完成';
      reviewRefreshTick.value++;
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
    case 'gate_timeout':
      // #2：某个闸门等太久，后端已安全暂停并留痕。不说清「卡在哪个门、等了多久」
      // 的话，界面只是不再滚动，用户会以为程序死了。
      topError.value = `闸门超时，任务已安全暂停：${payload.gate || '未知闸门'}`;
      addLog(
        `闸门超时（${payload.gate || '未知'}，已等待 ${Math.round(payload.waiting_seconds || 0)} 秒），` +
          '请回到对应闸门确认后恢复',
        'err',
      );
      break;
    case 'split_ai_start':
      addLog(
        `识别不到章节边界，正在用 AI 辅助拆分（原因：${payload.reason || '未知'}，` +
          `${payload.candidates || 0} 个候选）`,
        'warn',
      );
      break;
    default:
      // 后端新增事件而前端没接上时，宁可在这里留一条日志：过去这类「前端漏处理」
      // 全部表现为静默失效（对照面板空白、闸门超时不提示都属此类）。
      addLog(`未处理的事件：${event}`, 'info');
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

async function runEstimate() {
  if (estimating.value) return;
  if (!port.value) {
    estimateError.value = '后端未连接，无法预估';
    return;
  }
  if (!inputPath.value || !outputPath.value || !model.value) {
    estimateError.value = '请先填写输入路径、输出路径与模型名';
    return;
  }
  estimating.value = true;
  estimateError.value = '';
  try {
    estimate.value = await estimateRun(port.value, buildStartBody());
  } catch (e: any) {
    estimate.value = null;
    estimateError.value = e?.message || String(e);
  } finally {
    estimating.value = false;
  }
}

/** /api/start 与 /api/estimate 共用同一份请求体：两处各写一遍必然漂移
 *  （预估看到的档位/门/风格与真跑不一致，预估结果就不作数了）。 */
function buildStartBody() {
  return {
    input_mode: inputMode.value,
    input_path: inputPath.value,
    output_path: outputPath.value,
    input_format: inputFormat.value,
    api_url: apiUrl.value,
    api_key: apiKey.value,
    model: model.value,
    // 数字输入框被清空时 v-model.number 得到 ''，序列化后后端 int/float 校验直接 422
    context_window: num(contextWindow.value, MIN_CONTEXT_WINDOW, DEFAULT_CONTEXT_WINDOW),
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
    max_concurrency: Math.round(num(maxConcurrency.value, 1, 1, 4)),
    models: buildModels(),
  };
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
  const body = buildStartBody();
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
    const res = await postWithBusyRetry(
      `${apiBase()}/api/split/confirm`,
      { run_id: runId.value },
      { onBusy: (n) => addLog(`文件正被占用，确认拆分自动重试第 ${n} 次…`, 'warn') },
    );
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
    const res = await postWithBusyRetry(
      `${apiBase()}/api/fix/confirm`,
      { run_id: runId.value, fix_list: descriptions },
      { onBusy: (n) => addLog(`文件正被占用，确认断层自动重试第 ${n} 次…`, 'warn') },
    );
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
    const res = await postWithBusyRetry(
      `${apiBase()}/api/accept`,
      { run_id: runId.value },
      { onBusy: (n) => addLog(`文件正被占用，验收自动重试第 ${n} 次…`, 'warn') },
    );
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
    const res = await postWithBusyRetry(
      `${apiBase()}/api/regen_chapter`,
      { run_id: runId.value, chapter_id: c.id },
      { onBusy: (n) => addLog(`文件正被占用，重写自动重试第 ${n} 次…`, 'warn') },
    );
    const d = await res.json().catch(() => null);
    if (res.ok) {
      c.preview = d?.preview || '';
      reviewRefreshTick.value++;
      addLog(`章节 ${c.id} 已重写，请重新验收`, 'accent');
      // 后端读 progress.json 失败时不写旧快照（R12），代价是这次没记上次数。
      // 保险丝可能少算一次，必须如实说，不能让用户以为「随便重写不花钱」。
      if (d && d.regen_count_recorded === false)
        addLog(`章节 ${c.id} 的重写次数没能登记，重写上限可能比预期宽松一次`, 'warn');
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

async function loadComparisonText() {
  if (!port.value || !outputPath.value || !comparisonSel.value) {
    comparisonText.value = '';
    return;
  }
  try {
    comparisonText.value = await fetchComparisonText(
      port.value,
      outputPath.value,
      comparisonSel.value,
    );
  } catch (e: any) {
    comparisonText.value = '';
    topError.value = `对照正文读取失败：${e.message}`;
  }
}

async function openComparisons() {
  if (!outputPath.value || !port.value) return;
  try {
    // R8：列表不含正文，必须再按选中项取一次；换章节时也要重取。
    comparisonItems.value = await fetchComparisonList(port.value, outputPath.value);
    comparisonSel.value = comparisonItems.value[0]?.name ?? '';
    showComparisons.value = true;
    if (!comparisonItems.value.length) {
      comparisonText.value = '';
      topError.value = '当前输出目录还没有左右对照文件（需开启「左右对照」并跑完 phase3）';
      return;
    }
    await loadComparisonText();
  } catch (e: any) {
    topError.value = `请求失败：${e.message}`;
  }
}

watch(comparisonSel, () => {
  void loadComparisonText();
});

async function confirmBlueprint() {
  if (confirmingBlueprint.value) return; // 防重复点击
  if (blueprintText.value.length < 100) {
    alert('蓝图内容过短（需 >= 100 字）');
    return;
  }
  confirmingBlueprint.value = true;
  try {
    const res = await postWithBusyRetry(
      `${apiBase()}/api/blueprint/confirm`,
      {
        run_id: runId.value,
        blueprint: blueprintText.value,
        user_edited: true,
      },
      { onBusy: (n) => addLog(`文件正被占用，确认蓝图自动重试第 ${n} 次…`, 'warn') },
    );
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
  // 503 是「progress.json 正被别的程序占用」（后端已退避重试一轮，前端又自动重发
  // 过一次）。这时说「失败：文件正被占用」用户还得自己点，直接给下一步动作。
  const text =
    res.status === 503
      ? `${action}：成品文件正被占用，自动重试仍未成功。请关闭正在打开输出目录的程序` +
        '（资源管理器预览、Word/Excel、杀毒扫描）后再点一次'
      : `${action}失败：${msg}`;
  topError.value = text;
  addLog(text, 'err');
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
    const res = await postWithBusyRetry(
      `${apiBase()}/api/${action}`,
      { run_id: runId.value },
      { onBusy: (n) => addLog(`文件正被占用，${label}自动重试第 ${n} 次…`, 'warn') },
    );
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
  if (typeof n === 'number' && n >= MIN_CONTEXT_WINDOW) contextWindow.value = n;
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
      <div class="brand-group">
        <span class="brand-seal" aria-hidden="true">文炼</span>
        <div class="brand-titles">
          <h1>
            文炼工坊 <span class="version">TextForge v{{ appVersion }}</span>
          </h1>
          <span class="brand-subtitle">长篇文学重构与书卷校勘工坊</span>
        </div>
      </div>

      <!-- 五阶书卷工序步进条：拆书 · 诊断 · 蓝图 · 重构缝合 · 成书 -->
      <nav class="pipeline-stepper" aria-label="五阶重构工序进度">
        <template v-for="(st, idx) in pipelineSteps" :key="st.key">
          <span v-if="idx > 0" class="step-sep" aria-hidden="true">·</span>
          <span
            class="step-pill"
            :class="[`is-${st.state}`, { 'is-pulsing': st.state === 'active' && pipelineRunning }]"
          >
            <span class="step-idx mono">
              <template v-if="st.state === 'done'">✓</template>
              <template v-else>{{ idx + 1 }}</template>
            </span>
            <span class="step-label">{{ st.label }}</span>
          </span>
        </template>
      </nav>

      <div class="status">
        <span
          v-if="reviewSummary && reviewSummary.ready_chapters > 0"
          class="header-jade-badge tabular"
          title="来自 .tfproj.db 的实时章节审校汇总"
        >
          已审校 {{ reviewSummary.approved_chapters + reviewSummary.edited_chapters }}/{{
            reviewSummary.ready_chapters
          }}
          章
        </span>
        <button
          type="button"
          class="status-link"
          :class="{ running: pipelineRunning }"
          :title="backendReady ? '查看后端设置' : '后端未连接，点击排查/重启'"
          @click="showSettings = true"
        >
          <span
            :class="[
              'dot',
              backendReady ? 'ok' : 'err',
              { pulse: pipelineRunning && backendReady },
            ]"
          ></span>
          <span>{{ backendReady ? '后端已连接' : errorMsg || '连接中...' }}</span>
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
        <button
          type="button"
          class="theme-toggle"
          aria-label="切换宣纸/墨砚主题"
          :title="darkMode ? '切换至宣纸昼光主题' : '切换至墨砚夜读主题'"
          @click="toggleTheme"
        >
          {{ darkMode ? '☀ 宣纸' : '☾ 墨砚' }}
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
          <span class="panel-title-serif">书卷编修配置</span>
          <button
            type="button"
            class="panel-gear"
            title="系统与后端设置"
            @click="showSettings = true"
          >
            ⚙ 后端设置
          </button>
        </h2>

        <div class="cfg-section-head">01 · 服务与书稿卷宗</div>

        <div class="row backend-row">
          <label>后端服务</label>
          <span class="backend-state tabular">
            <span :class="['dot', backendReady ? 'ok' : 'err']"></span>
            {{
              backendReady
                ? `已连接 :${port ?? '—'}`
                : sseAttempts > 0
                  ? `重连中 (${sseAttempts})`
                  : '未连接'
            }}
          </span>
          <button type="button" :disabled="backendReady" @click="reconnectBackend">重新连接</button>
          <button type="button" class="ghost" @click="showSettings = true">设置</button>
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
          <button type="button" @click="selectInput">选择</button>
        </div>

        <div class="row">
          <label>输出路径</label>
          <input v-model="outputPath" placeholder="可直接粘贴路径，或点击右侧选择" />
          <button type="button" @click="selectOutput">选择</button>
        </div>

        <div class="row">
          <label>输出书名</label>
          <input v-model="novelName" placeholder="可选，默认取输入文件夹名" />
        </div>

        <div class="cfg-section-head">02 · 模型端点与档位预设</div>

        <div class="row">
          <label>API URL</label>
          <input v-model="apiUrl" placeholder="https://api.example.com/v1/chat/completions" />
        </div>

        <div class="row">
          <label>API Key</label>
          <input v-model="apiKey" type="password" placeholder="可选，仅存内存不落盘" />
        </div>

        <div class="row">
          <label>模型名</label>
          <input v-model="model" placeholder="如 gpt-4o / deepseek-chat" />
          <label class="ctx">上下文</label>
          <input
            v-model.number="contextWindow"
            type="number"
            class="tabular"
            :min="MIN_CONTEXT_WINDOW"
            title="它决定单批能装多少字：窗口小→批数多→每批都要重发蓝图/前情这些脚手架。填到「预估本次」里的批数不再下降为止，再大只会多花 token"
          />
        </div>

        <div class="row preset-row">
          <label>预设档位</label>
          <div class="preset-controls">
            <div class="preset-pills" role="group" aria-label="快捷档位切换">
              <button
                v-for="name in Object.keys(PRESETS)"
                :key="name"
                type="button"
                class="preset-pill"
                :class="{ active: preset === name }"
                @click="selectPresetPill(name)"
              >
                <span>{{ name }}</span>
                <small class="mono">{{ Math.round(PRESETS[name].ctx / 1024) }}k</small>
              </button>
            </div>
            <span class="preset-hint">
              一键套用温度 / 上下文 / 批次间隔；本地小模型建议选「快速低保真」
            </span>
          </div>
        </div>

        <div class="cfg-section-head">03 · 重构法则与质量闸门</div>

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
            <summary>重构保护门（自动重写与最小缝合默认开启）</summary>
            <div class="gates-grid">
              <label class="rich"
                ><input v-model="gates.lock_names" type="checkbox" /> 锁人名/设定名</label
              >
              <label class="rich"
                ><input v-model="gates.lock_plot" type="checkbox" /> 锁剧情骨架</label
              >
              <label class="rich"
                ><input v-model="gates.inject_forbidden" type="checkbox" /> 注入禁改清单</label
              >
              <label class="rich"
                ><input v-model="gates.require_chapter_accept" type="checkbox" /> 逐章验收</label
              >
              <label class="rich"
                ><input v-model="gates.skip_stitch_if_smooth" type="checkbox" />
                平滑衔接跳过缝合</label
              >
              <label class="rich"
                ><input v-model="gates.qc_block_export" type="checkbox" /> 质检拦截导出</label
              >
              <label class="rich" title="只处理空章/塌缩/占位/残留等结构硬伤，每批最多重写一次"
                ><input v-model="gates.auto_rewrite_bad_batch" type="checkbox" />
                硬伤批次自动重写</label
              >
              <label
                class="rich"
                title="交界处原文句子有超过 35% 没被原样保留时拒绝该补丁，退回保留原文（默认开；关掉后只受长度比 0.5~3.0 约束）"
                ><input v-model="gates.stitch_minimal_change" type="checkbox" />
                缝合只做最小改动</label
              >
            </div>
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

        <div class="row wrap-checks">
          <label>拆书确认</label>
          <label class="rich"
            ><input v-model="reviewSplit" type="checkbox" /> 拆书后先预览章节再继续</label
          >
          <label
            class="rich"
            title="仅在规则识别不可靠（没认出章节 / 章节长度异常）时才额外调用一次模型，成本很低"
          >
            <input v-model="aiSplit" type="checkbox" /> 识别不到章节时用 AI 辅助定位边界
          </label>
        </div>

        <details class="adv">
          <summary>优化项：左右对照 / 章快照 / 诊断 JSON</summary>
          <div class="row">
            <label class="rich"
              ><input v-model="compareOutput" type="checkbox" />
              左右对照（重构后按章生成原文|重构后表格）</label
            >
          </div>
          <div class="row">
            <label class="rich"
              ><input v-model="chapterSnapshots" type="checkbox" />
              章快照（保留重构初稿与缝合终稿）</label
            >
          </div>
          <div class="row">
            <label>诊断JSON目录</label>
            <input
              v-model="diagnoseJson"
              placeholder="可选，留空关闭；设置后按批次把结构化断层 JSON 写入该目录"
            />
          </div>
        </details>

        <div class="row">
          <label>网络代理</label>
          <input v-model="proxy" placeholder="可选，如 http://127.0.0.1:7890" />
          <label
            class="rich"
            title="仅影响 docx 成品：章标题用 Word 标题样式、章间分页、**粗体**/*斜体* 生效"
            ><input v-model="richText" type="checkbox" /> docx富文本</label
          >
          <label class="rich" title="仅在使用 https 自签证书的服务端时需要关闭"
            ><input v-model="sslVerify" type="checkbox" /> 校验HTTPS证书</label
          >
        </div>

        <details class="adv">
          <summary>高级项：各阶段温度与并发</summary>
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
          <div class="row">
            <label>并发批次数</label>
            <input v-model.number="maxConcurrency" type="number" min="1" max="4" step="1" />
            <span class="ph-note">本地模型建议 1；云端可 2-4</span>
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

        <div class="cfg-section-head">04 · 文学风骨与规模预估</div>

        <div class="row col">
          <label>作者风格</label>
          <textarea v-model="authorStyle" rows="2" class="style-area"></textarea>
        </div>

        <div class="row">
          <label>参考单价</label>
          <input
            v-model.number="priceInput"
            class="num tabular"
            type="number"
            min="0"
            step="0.1"
            aria-label="每百万输入 token 价格"
          />
          <span class="ph-note">¥/百万输入</span>
          <input
            v-model.number="priceOutput"
            class="num tabular"
            type="number"
            min="0"
            step="0.1"
            aria-label="每百万输出 token 价格"
          />
          <span class="ph-note">¥/百万输出（留 0 = 本地不计费）</span>
        </div>

        <div class="row">
          <button type="button" class="ghost" :disabled="estimating" @click="runEstimate">
            {{ estimating ? '预估中…' : '预估本次规模与花费' }}
          </button>
          <span v-if="estimateError" class="ph-note err">{{ estimateError }}</span>
        </div>

        <!-- 铅字票据式预估单（Typographic Receipt） -->
        <div v-if="estimate" class="estimate">
          <div class="estimate-head">
            <span>预估本次</span>
            <span class="estimate-cost mono">{{ estimateCostText }}</span>
          </div>
          <div class="estimate-grid tabular">
            <div class="estimate-cell">
              <span class="estimate-k">书稿切分</span>
              <b class="mono">{{ estimate.chapters }} 章 → {{ estimate.batches }} 批</b>
              <small>每批 {{ estimate.chapters_per_batch.join('/') }} 章</small>
            </div>
            <div class="estimate-cell">
              <span class="estimate-k">模型调用</span>
              <b class="mono">共 {{ estimate.calls.total }} 次</b>
              <small>
                诊 {{ estimate.calls.diagnose }} · 蓝 {{ estimate.calls.blueprint }} · 构
                {{ estimate.calls.refactor }} · 缝 {{ estimate.calls.stitch }}
              </small>
            </div>
            <div class="estimate-cell">
              <span class="estimate-k">预计 Token</span>
              <b class="mono">
                {{ estimate.tokens.prompt.toLocaleString('zh-CN') }} 入 /
                {{ estimate.tokens.completion.toLocaleString('zh-CN') }} 出
              </b>
              <small>
                脚手架占比约 {{ (estimate.scaffold_share * 100).toFixed(0) }}%（每次
                {{ estimate.scaffold_per_request.toLocaleString('zh-CN') }}）
              </small>
            </div>
          </div>
          <p class="hint tabular">
            单批正文上限 {{ estimate.body_token_limit.toLocaleString('zh-CN') }} token · 缝合边界
            {{ estimate.stitch_boundaries }} 处（每侧窗口
            {{ estimate.stitch_window_chars.toLocaleString('zh-CN') }} 字，合计覆盖
            {{ estimate.stitch_exposed_chars.toLocaleString('zh-CN') }} 字）
          </p>
          <ul v-if="estimate.notes.length" class="hint estimate-notes">
            <li v-for="(n, i) in estimate.notes" :key="i">· {{ n }}</li>
          </ul>
        </div>

        <button type="button" class="start-btn" :disabled="!canStart" @click="startPipeline">
          {{ starting ? '正在提交工序…' : '✦ 开始重构书稿' }}
        </button>
      </section>

      <!-- 进度区 -->
      <section class="panel progress">
        <h2>
          <span class="panel-title-serif">工序实况与事件流</span>
          <span v-if="pipelineRunning" class="live-tag">
            <span class="live-dot"></span>流式运行中
          </span>
        </h2>

        <div class="phase-bar">
          <span v-if="currentPhase" class="phase">{{ currentPhase }}</span>
          <span v-if="isPaused" class="badge paused">已暂停</span>
          <span v-if="pipelineRunning" class="badge running">运行中</span>
          <span
            v-if="reviewSummary && reviewSummary.ready_chapters > 0"
            class="badge ok"
            title="来自 .tfproj.db 的实时章节审校汇总"
          >
            已审校 {{ reviewSummary.approved_chapters + reviewSummary.edited_chapters }}/{{
              reviewSummary.ready_chapters
            }}
            章
          </span>
          <button v-if="outputPath && port" type="button" class="open-btn" @click="openOutput">
            打开输出目录
          </button>
          <button v-if="outputPath && port" type="button" class="open-btn ghost" @click="openFinal">
            打开成品
          </button>
          <button
            v-if="outputPath && port"
            type="button"
            class="open-btn ghost"
            @click="openComparisons"
          >
            对照预览
          </button>
          <button
            v-if="outputPath && port"
            type="button"
            class="open-btn ghost"
            @click="openVersions"
          >
            成品版本
          </button>
          <button
            v-if="outputPath && port"
            type="button"
            class="open-btn ghost"
            @click="openQuality"
          >
            质量报告<template v-if="qualityLabel"> · {{ qualityLabel }}</template>
          </button>
          <button
            v-if="port"
            type="button"
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
            type="button"
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

        <div v-if="pipelineRunning" class="controls">
          <button type="button" :disabled="isPaused || controlling" @click="control('pause')">
            暂停
          </button>
          <button type="button" :disabled="!isPaused || controlling" @click="control('resume')">
            恢复
          </button>
          <button type="button" class="danger" :disabled="controlling" @click="control('stop')">
            停止
          </button>
          <span class="controls-kbd-hint"><kbd>Space</kbd> 暂停 / 恢复</span>
        </div>

        <RunStats
          :total-chapters="totalChapters"
          :batch-stats="batchStats"
          :chapter-progress="chapterProgress"
          :usage-tokens="usageTokens"
          :spent-text="spentText"
          :elapsed="elapsed"
          :pipeline-running="pipelineRunning"
          :diag-percent="diagPercent"
          :recon-percent="reconPercent"
          :chapter-percent="chapterPercent"
        />

        <div v-if="chapterRows.length" class="tree-head">
          <button
            type="button"
            class="tree-toggle tabular"
            @click="showChapterTree = !showChapterTree"
          >
            {{ showChapterTree ? '▾' : '▸' }} 章节概览（{{
              chapterRows.filter((c) => c.status === 'reconstructed').length
            }}/{{ chapterRows.length }} 已重构）
          </button>
        </div>
        <div v-if="showChapterTree && chapterRows.length" class="tree-box">
          <div v-for="c in chapterRows" :key="c.id" class="tree-row">
            <span class="split-id">{{ c.label }}</span>
            <span class="badge" :class="c.status === 'reconstructed' ? 'ok' : ''">{{
              c.status === 'reconstructed' ? '已重构' : '待处理'
            }}</span>
          </div>
        </div>

        <div v-if="streamingText" class="stream-box">
          <b class="stream-title">墨迹流式预览</b>{{ streamingText }}
        </div>

        <div class="log-head">
          <b>实时事件暗匣</b>
          <span class="log-count tabular">{{ eventLog.length }} 条</span>
          <button type="button" class="min-btn" @click="exportLog">导出日志</button>
          <button type="button" class="min-btn min-btn-adj" @click="eventLog = []">清空</button>
        </div>
        <div v-if="finalFiles.length" class="bar chapter-row-bar">
          <span class="bar-label">成品文件</span>
          <div class="final-list">
            <button
              v-for="f in finalFiles"
              :key="f.path"
              type="button"
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
          <div v-if="!eventLog.length" class="log-empty">等待工序启动或事件推流…</div>
        </div>
      </section>

      <!-- 三栏「边跑边审」流式章节审阅工作台 -->
      <ChapterReviewWorkbench
        v-if="showReviewWorkbench"
        :port="port"
        :output-path="outputPath"
        :run-id="runId"
        :pipeline-running="pipelineRunning"
        :refresh-tick="reviewRefreshTick"
        :regenerating-ids="regenerating"
        @regen="regenChapter"
        @review-updated="fetchStatus"
        @log="(msg, lvl) => addLog(msg, lvl)"
        @error="(msg) => (topError = msg)"
      />
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
        <button type="button" class="btn-ghost" :disabled="redoingSplit" @click="redoSplit">
          {{ redoingSplit ? '重拆中…' : '按此标记重新拆分' }}
        </button>
      </details>
      <template #actions>
        <span></span>
        <button type="button" :disabled="confirmingSplit" @click="confirmSplit">
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
        <label v-for="g in fixGaps" :key="g.id" class="split-row fix-row">
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
        <button type="button" :disabled="confirmingFix" @click="confirmFix">
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
          <span class="fix-desc serif-preview">{{ c.preview }}</span>
          <button
            type="button"
            class="min-btn"
            :disabled="regenerating.has(c.id)"
            @click="regenChapter(c)"
          >
            {{ regenerating.has(c.id) ? '重写中…' : '重写此章' }}
          </button>
        </div>
      </div>
      <template #actions>
        <span></span>
        <button type="button" :disabled="confirmingAccept" @click="confirmAccept">
          验收通过，继续
        </button>
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
        <button type="button" class="danger" @click="control('stop')">停止</button>
        <button type="button" class="primary" :disabled="forcingExport" @click="forceExport">
          强制导出
        </button>
      </template>
    </GateModal>

    <!-- 左右对照预览弹窗 -->
    <div v-if="showComparisons" class="modal-mask" @click.self="showComparisons = false">
      <div class="modal modal-wide" role="dialog" aria-modal="true" aria-label="左右对照预览">
        <h3>左右对照预览</h3>
        <div v-if="comparisonItems.length" class="row">
          <label>章节</label>
          <select v-model="comparisonSel">
            <option v-for="it in comparisonItems" :key="it.name" :value="it.name">
              {{ it.name }}（{{ Math.round(it.size / 1024) }} KB）
            </option>
          </select>
        </div>
        <pre class="comp-preview">{{ comparisonText }}</pre>
        <div class="modal-actions">
          <span></span>
          <button type="button" @click="showComparisons = false">关闭</button>
        </div>
      </div>
    </div>

    <!-- 蓝图确认弹窗 -->
    <GateModal v-if="showBlueprint" wide title="请检查并确认全书重构蓝图">
      <p class="hint">可直接润色校勘后确认，蓝图将作为后续全部批次重构的核心文学纲领。</p>
      <textarea v-model="blueprintText" rows="16" class="blueprint-area"></textarea>

      <label class="sum-toggle" :class="{ off: !showSummaries }">
        <input v-model="showSummaries" type="checkbox" />
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
        <span class="count tabular">{{ blueprintText.length.toLocaleString('zh-CN') }} 字</span>
        <button type="button" :disabled="confirmingBlueprint" @click="confirmBlueprint">
          {{ confirmingBlueprint ? '确认中...' : '确认蓝图，进入重构' }}
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

    <!-- 成品质量报告（结构性检查，不替代人工通读） -->
    <QualityModal
      v-if="showQuality"
      :report="qualityReport"
      :busy="qualityBusy"
      :error="qualityError"
      @close="showQuality = false"
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
@keyframes tf-pulse {
  0%,
  100% {
    opacity: 1;
    transform: scale(1);
  }
  50% {
    opacity: 0.55;
    transform: scale(1.22);
  }
}

.app {
  display: flex;
  flex-direction: column;
  height: 100vh;
  font-family: var(--tf-font-sans);
  background: var(--tf-bg-gradient);
  color: var(--tf-ink);
}

/* ---------- 工坊顶栏（Atelier Top Bar） ---------- */
.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  padding: 11px 24px;
  background: var(--tf-surface);
  color: var(--tf-ink);
  border-bottom: 1px solid var(--tf-border);
  box-shadow: var(--tf-shadow-sm);
  z-index: 10;
}

.brand-group {
  display: flex;
  align-items: center;
  gap: 11px;
  flex-shrink: 0;
}

.brand-seal {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: var(--tf-radius-sm);
  background: var(--tf-accent);
  color: #fbf8f3;
  font-family: var(--tf-font-serif);
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.04em;
  box-shadow:
    inset 0 0 0 1px rgba(255, 255, 255, 0.22),
    0 2px 6px rgba(181, 73, 58, 0.25);
}

.brand-titles {
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.header h1 {
  font-family: var(--tf-font-serif);
  font-size: 17px;
  font-weight: 700;
  letter-spacing: 0.02em;
  margin: 0;
  color: var(--tf-ink);
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.version {
  font-family: var(--tf-font-mono);
  font-size: 11px;
  color: var(--tf-ink-muted);
  font-weight: 500;
  letter-spacing: 0;
}

.brand-subtitle {
  font-size: 11px;
  color: var(--tf-ink-muted);
  letter-spacing: 0.04em;
}

/* 五阶书卷工序步进条 */
.pipeline-stepper {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 4px 10px;
  background: var(--tf-surface-2);
  border: 1px solid var(--tf-border);
  border-radius: var(--tf-radius-pill);
}

.step-sep {
  color: var(--tf-ink-muted);
  font-size: 12px;
  opacity: 0.6;
}

.step-pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 9px;
  border-radius: var(--tf-radius-pill);
  font-size: 12px;
  color: var(--tf-ink-muted);
  transition:
    background 0.2s var(--tf-ease),
    color 0.2s var(--tf-ease);
}

.step-idx {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 15px;
  height: 15px;
  border-radius: 50%;
  font-size: 10px;
  font-weight: 600;
  background: var(--tf-surface);
  color: var(--tf-ink-muted);
  border: 1px solid var(--tf-border);
}

.step-pill.is-done {
  color: var(--tf-ok);
  font-weight: 500;
}

.step-pill.is-done .step-idx {
  background: var(--tf-ok-bg);
  border-color: var(--tf-ok-border);
  color: var(--tf-ok);
}

.step-pill.is-active {
  background: var(--tf-brand);
  color: var(--tf-ink-on-brand);
  font-weight: 600;
  box-shadow: var(--tf-shadow-sm);
}

.step-pill.is-active .step-idx {
  background: rgba(255, 255, 255, 0.2);
  border-color: rgba(255, 255, 255, 0.35);
  color: var(--tf-ink-on-brand);
}

.step-pill.is-pulsing .step-idx {
  animation: tf-pulse 1.8s ease-in-out infinite;
}

/* 顶栏右侧状态与主题开关 */
.status {
  font-size: 12.5px;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.header-jade-badge {
  padding: 3px 10px;
  border-radius: var(--tf-radius-pill);
  background: var(--tf-ok-bg);
  border: 1px solid var(--tf-ok-border);
  color: var(--tf-ok);
  font-size: 12px;
  font-weight: 600;
}

.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
  flex-shrink: 0;
}

.dot.ok {
  background: var(--tf-ok);
}

.dot.err {
  background: var(--tf-danger);
}

.dot.pulse {
  animation: tf-pulse 1.8s ease-in-out infinite;
}

.status-link {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  background: var(--tf-surface-2);
  border: 1px solid var(--tf-border);
  color: var(--tf-ink-secondary);
  font-size: 12.5px;
  font-weight: 500;
  cursor: pointer;
  padding: 4px 11px;
  border-radius: var(--tf-radius-pill);
  max-width: 300px;
  transition:
    background 0.15s var(--tf-ease),
    border-color 0.15s var(--tf-ease),
    color 0.15s var(--tf-ease);
}

.status-link:hover {
  background: var(--tf-surface-hover);
  border-color: var(--tf-border-strong);
  color: var(--tf-ink);
}

.gear {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: var(--tf-surface-2);
  border: 1px solid var(--tf-border-strong);
  color: var(--tf-ink-secondary);
  border-radius: var(--tf-radius-sm);
  font-size: 14px;
  cursor: pointer;
  width: 30px;
  height: 28px;
  line-height: 1;
  transition:
    background 0.15s var(--tf-ease),
    color 0.15s var(--tf-ease);
}

.gear:hover {
  background: var(--tf-surface-hover);
  color: var(--tf-ink);
}

.theme-toggle {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 11px;
  height: 28px;
  background: var(--tf-surface-2);
  border: 1px solid var(--tf-border-strong);
  color: var(--tf-ink);
  border-radius: var(--tf-radius-pill);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition:
    background 0.15s var(--tf-ease),
    border-color 0.15s var(--tf-ease);
}

.theme-toggle:hover {
  background: var(--tf-surface-hover);
  border-color: var(--tf-brand-border);
}

/* ---------- 主工作台双栏布局 ---------- */
.main {
  flex: 1;
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 18px;
  padding: 18px 24px;
  overflow-y: auto;
}

.panel {
  background: var(--tf-surface);
  border: 1px solid var(--tf-border);
  border-radius: var(--tf-radius-lg);
  padding: 20px 22px;
  box-shadow: var(--tf-shadow-sm);
  overflow-y: auto;
}

.panel h2 {
  font-size: 16px;
  margin: 0 0 14px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--tf-border);
  color: var(--tf-ink);
  display: flex;
  align-items: center;
  gap: 10px;
}

.panel-title-serif {
  font-family: var(--tf-font-serif);
  font-weight: 600;
  letter-spacing: 0.02em;
}

.panel-gear {
  margin-left: auto;
  padding: 4px 12px;
  font-size: 12px;
  font-weight: 500;
  background: var(--tf-surface-2);
  border: 1px solid var(--tf-border-strong);
  color: var(--tf-ink-secondary);
  border-radius: var(--tf-radius-sm);
  cursor: pointer;
  transition:
    background 0.15s var(--tf-ease),
    color 0.15s var(--tf-ease);
}

.panel-gear:hover {
  background: var(--tf-surface-hover);
  color: var(--tf-ink);
}

.panel.config.disabled {
  opacity: 0.56;
  pointer-events: none;
}

.panel.progress {
  display: flex;
  flex-direction: column;
}

.live-tag {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px 10px;
  border-radius: var(--tf-radius-pill);
  background: var(--tf-brand-soft);
  border: 1px solid var(--tf-brand-border);
  color: var(--tf-brand);
  font-size: 11.5px;
  font-weight: 600;
}

.live-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--tf-brand);
  animation: tf-pulse 1.6s ease-in-out infinite;
}

/* 配置分区小标题 */
.cfg-section-head {
  font-family: var(--tf-font-mono);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--tf-ink-muted);
  margin: 14px 0 10px;
  padding-bottom: 4px;
  border-bottom: 1px dashed var(--tf-border);
}

.cfg-section-head:first-of-type {
  margin-top: 2px;
}

/* 后端服务行 */
.backend-row {
  background: var(--tf-surface-2);
  border: 1px solid var(--tf-border);
  border-radius: var(--tf-radius-md);
  padding: 8px 12px;
  margin-bottom: 12px;
}

.backend-state {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 13px;
  font-weight: 500;
  color: var(--tf-ink);
}

.backend-row button {
  padding: 4px 12px;
  border: 1px solid var(--tf-border-strong);
  background: var(--tf-surface);
  color: var(--tf-ink-secondary);
  border-radius: var(--tf-radius-sm);
  cursor: pointer;
  font-size: 12px;
}

.backend-row button:hover:not(:disabled) {
  background: var(--tf-surface-hover);
  color: var(--tf-ink);
}

.backend-row button.ghost {
  border-color: var(--tf-brand-border);
  color: var(--tf-brand);
  background: transparent;
}

/* 全局横幅 */
.banner {
  grid-column: 1 / -1;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  padding: 10px 16px;
  border-radius: var(--tf-radius-md);
  font-size: 13px;
}

.banner.tip {
  background: var(--tf-ok-bg);
  color: var(--tf-ok);
  border: 1px solid var(--tf-ok-border);
}

.banner.err {
  background: var(--tf-danger-bg);
  color: var(--tf-danger);
  border: 1px solid var(--tf-danger-border);
}

.banner.warn {
  background: var(--tf-warn-bg);
  color: var(--tf-warn);
  border: 1px solid var(--tf-warn-border);
}

.banner-act {
  display: flex;
  align-items: center;
  gap: 8px;
}

.banner-btn {
  padding: 3px 10px;
  font-size: 12px;
  cursor: pointer;
  border: 1px solid currentColor;
  background: transparent;
  color: inherit;
  border-radius: var(--tf-radius-xs);
}

.banner-btn.primary {
  background: var(--tf-danger);
  border-color: var(--tf-danger);
  color: #fff;
}

.banner-close {
  background: transparent;
  border: none;
  font-size: 17px;
  cursor: pointer;
  color: inherit;
  padding: 0 4px;
  line-height: 1;
}

/* 表单控件与行排布 */
.row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}

.row.wrap-checks {
  flex-wrap: wrap;
}

.row.col {
  flex-direction: column;
  align-items: stretch;
  gap: 6px;
}

.row label {
  width: 82px;
  font-size: 13px;
  font-weight: 500;
  color: var(--tf-ink-secondary);
  flex-shrink: 0;
}

.row.col > label {
  width: auto;
}

.row label.fmt,
.row label.ctx,
.row label.t {
  width: auto;
  margin-left: 10px;
}

.row label.rich {
  width: auto;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-weight: 400;
  color: var(--tf-ink-secondary);
  cursor: pointer;
}

.row label.rich input {
  width: auto;
  margin: 0;
}

.row select,
.row input:not([type='checkbox']):not([type='radio']),
.row textarea {
  background: var(--tf-surface-elevated);
  color: var(--tf-ink);
  border: 1px solid var(--tf-border-strong);
  border-radius: var(--tf-radius-sm);
  padding: 7px 11px;
  font-size: 13px;
  transition:
    border-color 0.15s var(--tf-ease),
    box-shadow 0.15s var(--tf-ease),
    background 0.15s var(--tf-ease);
}

.row select:hover,
.row input:not([type='checkbox']):not([type='radio']):hover,
.row textarea:hover {
  border-color: var(--tf-ink-muted);
}

.row select:focus,
.row input:not([type='checkbox']):not([type='radio']):focus,
.row textarea:focus {
  border-color: var(--tf-brand);
  box-shadow: 0 0 0 3px var(--tf-brand-soft);
  outline: none;
}

.row select {
  flex: 1;
}

.row input:not([type='checkbox']):not([type='radio']) {
  flex: 1;
  min-width: 0;
}

.row label.t + input,
.row label.rich + input {
  flex: 1;
}

.row label.rich-k {
  width: auto;
  margin-left: 12px;
  font-size: 12px;
  color: var(--tf-ink-muted);
}

.row textarea {
  width: 100%;
  resize: vertical;
  line-height: 1.65;
}

.style-area {
  font-family: var(--tf-font-serif);
  font-size: 14px;
  line-height: 1.75;
}

.lone-area + .lone-area {
  margin-top: 4px;
}

.row button {
  padding: 6px 14px;
  border: 1px solid var(--tf-border-strong);
  background: var(--tf-surface-2);
  color: var(--tf-ink);
  border-radius: var(--tf-radius-sm);
  cursor: pointer;
  font-size: 12.5px;
  font-weight: 500;
  transition:
    background 0.15s var(--tf-ease),
    border-color 0.15s var(--tf-ease);
}

.row button:hover:not(:disabled) {
  background: var(--tf-surface-hover);
}

/* 预设档位胶囊组 */
.preset-row {
  align-items: flex-start;
}

.preset-row > label {
  padding-top: 5px;
}

.preset-controls {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.preset-pills {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.preset-pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 5px 11px !important;
  border-radius: var(--tf-radius-pill) !important;
  background: var(--tf-surface-2) !important;
  border: 1px solid var(--tf-border-strong) !important;
  color: var(--tf-ink-secondary) !important;
  font-size: 12px !important;
}

.preset-pill small {
  opacity: 0.72;
  font-size: 10.5px;
}

.preset-pill.active {
  background: var(--tf-brand) !important;
  border-color: var(--tf-brand) !important;
  color: var(--tf-ink-on-brand) !important;
}

.preset-hint {
  font-size: 11.5px;
  color: var(--tf-ink-muted);
}

/* 折叠高级面板 */
.adv {
  border: 1px solid var(--tf-border);
  background: var(--tf-surface-2);
  border-radius: var(--tf-radius-md);
  padding: 9px 14px;
  margin-bottom: 10px;
}

.adv summary {
  font-size: 12.5px;
  font-weight: 500;
  color: var(--tf-ink-secondary);
  cursor: pointer;
}

.adv[open] summary {
  margin-bottom: 8px;
  color: var(--tf-ink);
}

.adv.gates {
  display: flex;
  flex-direction: column;
  flex: 1;
  margin-bottom: 0;
}

.gates-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 6px 10px;
  margin-top: 6px;
}

.ph-note {
  font-size: 12px;
  color: var(--tf-ink-muted);
  margin: 2px 0;
  line-height: 1.5;
}

.ph-note.err {
  color: var(--tf-danger);
}

/* 铅字票据式预估卡片（Typographic Receipt） */
.estimate {
  border: 1px solid var(--tf-border-strong);
  border-top: 3px solid var(--tf-brand);
  border-radius: var(--tf-radius-md);
  padding: 12px 15px;
  margin: 6px 0 12px;
  background: var(--tf-surface-2);
}

.estimate-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  padding-bottom: 8px;
  margin-bottom: 10px;
  border-bottom: 1px dashed var(--tf-border-strong);
  font-size: 12.5px;
  font-weight: 600;
  color: var(--tf-ink-secondary);
}

.estimate-cost {
  font-size: 15px;
  font-weight: 700;
  color: var(--tf-brand);
}

.estimate-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 10px;
  padding-bottom: 8px;
  margin-bottom: 6px;
  border-bottom: 1px dashed var(--tf-border);
}

.estimate-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.estimate-k {
  font-size: 11px;
  color: var(--tf-ink-muted);
}

.estimate-cell b {
  font-size: 13px;
  color: var(--tf-ink);
}

.estimate-cell small {
  font-size: 11px;
  color: var(--tf-ink-secondary);
}

.estimate-notes {
  margin: 6px 0 0;
  padding-left: 2px;
  list-style: none;
  font-size: 11.5px;
  color: var(--tf-ink-muted);
}

/* 启动主按钮 */
.start-btn {
  width: 100%;
  margin-top: 10px;
  padding: 11px 16px;
  background: var(--tf-brand);
  color: var(--tf-ink-on-brand);
  border: 1px solid var(--tf-brand);
  border-radius: var(--tf-radius-md);
  font-family: var(--tf-font-serif);
  font-size: 15px;
  font-weight: 600;
  letter-spacing: 0.04em;
  cursor: pointer;
  box-shadow: var(--tf-shadow-sm);
  transition:
    background 0.18s var(--tf-ease),
    transform 0.12s var(--tf-ease),
    box-shadow 0.18s var(--tf-ease);
}

.start-btn:hover:not(:disabled) {
  background: var(--tf-brand-hover);
  border-color: var(--tf-brand-hover);
  box-shadow: var(--tf-shadow-md);
}

.start-btn:disabled {
  background: var(--tf-surface-3);
  border-color: var(--tf-border);
  color: var(--tf-ink-muted);
  box-shadow: none;
  cursor: not-allowed;
}

/* ---------- 进度面板 ---------- */
.phase-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}

.phase {
  font-family: var(--tf-font-serif);
  font-size: 14.5px;
  font-weight: 600;
  color: var(--tf-ink);
}

.controls {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}

.controls button {
  padding: 5px 14px;
  border: 1px solid var(--tf-border-strong);
  background: var(--tf-surface-2);
  color: var(--tf-ink);
  border-radius: var(--tf-radius-sm);
  cursor: pointer;
  font-size: 12.5px;
  font-weight: 500;
}

.controls button:hover:not(:disabled) {
  background: var(--tf-surface-hover);
}

.controls button.danger {
  color: var(--tf-danger);
  border-color: var(--tf-danger-border);
  background: var(--tf-danger-bg);
}

.controls button.danger:hover:not(:disabled) {
  background: var(--tf-danger);
  color: #fff;
}

.controls-kbd-hint {
  margin-left: auto;
  font-size: 11.5px;
  color: var(--tf-ink-muted);
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.tree-head {
  margin-bottom: 6px;
}

.tree-toggle {
  background: transparent;
  border: none;
  color: var(--tf-ink-secondary);
  font-size: 12.5px;
  font-weight: 600;
  cursor: pointer;
  padding: 4px 0;
}

.tree-toggle:hover {
  color: var(--tf-ink);
}

.tree-box {
  max-height: 160px;
  overflow-y: auto;
  border: 1px solid var(--tf-border);
  border-radius: var(--tf-radius-md);
  padding: 8px 12px;
  margin-bottom: 12px;
  background: var(--tf-surface-2);
}

.tree-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 3px 0;
  font-size: 12px;
}

.stream-box {
  max-height: 92px;
  overflow-y: auto;
  background: var(--tf-surface-2);
  border: 1px solid var(--tf-border-strong);
  border-left: 3px solid var(--tf-brand);
  border-radius: var(--tf-radius-sm);
  padding: 10px 14px;
  margin-bottom: 12px;
  font-family: var(--tf-font-serif);
  font-size: 13.5px;
  line-height: 1.75;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--tf-ink);
}

.stream-title {
  font-family: var(--tf-font-sans);
  font-size: 11.5px;
  color: var(--tf-brand);
  margin-right: 8px;
  padding: 1px 6px;
  border-radius: var(--tf-radius-xs);
  background: var(--tf-brand-soft);
}

/* 实时事件暗匣（SSE Console） */
.log-head {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--tf-ink-secondary);
  font-size: 12.5px;
  margin-bottom: 8px;
}

.log-count {
  color: var(--tf-ink-muted);
  font-family: var(--tf-font-mono);
  font-size: 11.5px;
}

.min-btn {
  margin-left: auto;
  padding: 3px 10px;
  background: var(--tf-surface-2);
  border: 1px solid var(--tf-border-strong);
  border-radius: var(--tf-radius-xs);
  color: var(--tf-ink-secondary);
  font-size: 11.5px;
  cursor: pointer;
  transition:
    background 0.15s var(--tf-ease),
    color 0.15s var(--tf-ease);
}

.min-btn-adj {
  margin-left: 0;
}

.min-btn:hover:not(:disabled) {
  background: var(--tf-surface-hover);
  color: var(--tf-ink);
}

.log-box {
  flex: 1;
  min-height: 230px;
  overflow-y: auto;
  background: var(--tf-console-bg);
  color: var(--tf-console-ink);
  border: 1px solid var(--tf-border-strong);
  border-radius: var(--tf-radius-md);
  padding: 12px 14px;
  font-family: var(--tf-font-mono);
  font-size: 12px;
  line-height: 1.68;
  font-variant-numeric: tabular-nums;
}

.log-line {
  white-space: pre-wrap;
  word-break: break-all;
}

.log-line .log-ts {
  color: var(--tf-console-muted);
  margin-right: 9px;
}

.log-ok {
  color: var(--tf-console-ok);
}

.log-warn {
  color: var(--tf-console-warn);
}

.log-err {
  color: var(--tf-console-err);
}

.log-accent {
  color: var(--tf-console-accent);
}

/* ---------- 人工闸门与蓝图手稿内部元素 ---------- */
.blueprint-area {
  flex: 1;
  width: 100%;
  padding: 16px 20px;
  border: 1px solid var(--tf-border-strong);
  border-radius: var(--tf-radius-md);
  background: var(--tf-surface-elevated);
  color: var(--tf-ink);
  font-family: var(--tf-font-serif);
  font-size: 15px;
  line-height: 1.85;
  letter-spacing: 0.01em;
  resize: vertical;
  min-height: 320px;
}

.sum-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  color: var(--tf-ink-secondary);
  cursor: pointer;
  margin: 10px 0 4px;
}

.sum-toggle input {
  width: auto;
  margin: 0;
}

.sum-empty {
  color: var(--tf-ink-muted);
}

.split-id {
  flex: 1;
  color: var(--tf-ink);
  font-weight: 500;
  word-break: break-all;
}

.redo-split {
  margin-top: 10px;
  padding: 10px 12px;
  border: 1px solid var(--tf-border);
  border-radius: var(--tf-radius-md);
  background: var(--tf-surface-2);
}

.redo-split summary {
  cursor: pointer;
  color: var(--tf-ink-secondary);
  font-size: 13px;
  font-weight: 500;
}

.redo-split textarea {
  width: 100%;
  margin-top: 8px;
  font-family: var(--tf-font-serif);
}

.redo-split button {
  margin-top: 8px;
}

.fix-row {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
}

.fix-row input {
  width: auto;
  margin: 0;
}

.fix-desc {
  color: var(--tf-ink-secondary);
  word-break: break-all;
  font-size: 13px;
  line-height: 1.6;
}

.serif-preview {
  font-family: var(--tf-font-serif);
}

.accept-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.accept-row .fix-desc {
  flex: 1;
}

.accept-row .min-btn {
  margin-left: auto;
  flex: 0 0 auto;
}

.sum-item {
  border: 1px solid var(--tf-border);
  border-radius: var(--tf-radius-sm);
  padding: 8px 12px;
  background: var(--tf-surface-2);
}

.sum-item summary {
  cursor: pointer;
  color: var(--tf-ink);
  font-weight: 500;
  margin-bottom: 4px;
}

.sum-preview {
  color: var(--tf-ink-secondary);
  font-family: var(--tf-font-serif);
  font-size: 13.5px;
  line-height: 1.75;
  white-space: pre-wrap;
  word-break: break-all;
}

.count {
  font-family: var(--tf-font-mono);
  font-size: 12px;
  color: var(--tf-ink-muted);
}

.comp-preview {
  margin-top: 10px;
  max-height: 60vh;
  overflow: auto;
  background: var(--tf-surface-2);
  color: var(--tf-ink);
  border: 1px solid var(--tf-border-strong);
  border-radius: var(--tf-radius-md);
  padding: 16px 20px;
  font-family: var(--tf-font-serif);
  font-size: 14.5px;
  line-height: 1.85;
  white-space: pre-wrap;
  word-break: break-word;
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

@media (max-width: 1120px) {
  .header {
    flex-wrap: wrap;
  }
  .pipeline-stepper {
    order: 3;
    width: 100%;
    justify-content: center;
  }
  .main {
    grid-template-columns: 1fr;
  }
}
</style>
