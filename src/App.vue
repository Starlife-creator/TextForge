<script setup lang="ts">
import { ref, reactive, computed, watch, onMounted, onUnmounted } from 'vue';
import { open } from '@tauri-apps/plugin-dialog';
import { isTauri } from '@tauri-apps/api/core';
import { waitForPort } from './api/backend';
import { connectSSE, disconnectSSE } from './api/sse';

// ---------- 后端连接 ----------
const port = ref<number | null>(null);
const backendReady = ref(false);
const errorMsg = ref('');
// 运行计时
const startedAt = ref<number | null>(null);
const elapsed = ref('00:00');
let elapsedTimer: number | null = null;
const inTauri = isTauri();
let statusTimer: number | null = null;

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
  lock_names: false, lock_plot: false, inject_forbidden: false,
  require_chapter_accept: false, skip_stitch_if_smooth: false, qc_block_export: false,
});
// 逐章验收弹窗
const showAccept = ref(false);
const acceptBatch = ref<number | null>(null);
const acceptChapters = ref<any[]>([]);
const confirmingAccept = ref(false);
// 质检拦截决策弹窗
const showQcBlock = ref(false);
const qcIssues = ref<string[]>([]);
const forcingExport = ref(false);
const proxy = ref('');
const richText = ref(false);
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
// 禁改清单（inject_forbidden 门用，每行一条）+ reskin 人名映射（每行 旧名=新名）
const forbiddenCanon = ref('');
const nameMapText = ref('');
// B1 批次间隔（秒）
const batchInterval = ref(2.0);

// ---------- 运行状态 ----------
const runId = ref<string | null>(null);
const pipelineRunning = ref(false);
const isPaused = ref(false);
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
const compareOutput = ref(false);
const chapterSnapshots = ref(false);
const diagnoseJson = ref('');
const showSplit = ref(false);
const splitChapters = ref<any[]>([]);
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

// 统计与提示
const usageTokens = ref({ prompt: 0, completion: 0 });
const batchStats = ref({ total: 0, phase1: 0, phase3: 0 });
const chapterProgress = ref({ total: 0, done: 0 });
const topError = ref<string | null>(null);
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
    localStorage.setItem(PERSIST_KEY, JSON.stringify({
      apiUrl: apiUrl.value, model: model.value, contextWindow: contextWindow.value,
      authorStyle: authorStyle.value, refactorMode: refactorMode.value,
      batchInterval: batchInterval.value, darkMode: darkMode.value,
      forbiddenCanon: forbiddenCanon.value, nameMapText: nameMapText.value,
      outputPath: outputPath.value, inputPath: inputPath.value,
    }));
  } catch { /* 忽略持久化失败 */ }
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
    if (p.refactorMode) refactorMode.value = p.refactorMode;
    if (typeof p.batchInterval === 'number') batchInterval.value = p.batchInterval;
    if (typeof p.darkMode === 'boolean') { darkMode.value = p.darkMode; document.documentElement.classList.toggle('dark', p.darkMode); }
    if (typeof p.forbiddenCanon === 'string') forbiddenCanon.value = p.forbiddenCanon;
    if (typeof p.nameMapText === 'string') nameMapText.value = p.nameMapText;
    if (typeof p.outputPath === 'string') outputPath.value = p.outputPath;
    if (typeof p.inputPath === 'string') inputPath.value = p.inputPath;
  } catch { /* 忽略损坏数据 */ }
}

// C3：键盘快捷键（Space 暂停/恢复，Enter 确认当前弹窗）；输入组件内不触发
function onKeydown(e: KeyboardEvent) {
  const t = e.target as HTMLElement | null;
  const typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable);
  if (typing) return;
  if (e.code === 'Space' && pipelineRunning.value && !e.repeat) {
    e.preventDefault();
    control(isPaused.value ? 'resume' : 'pause');
  }
  if (e.key === 'Enter' && !e.repeat) {
    if (showSplit.value) { confirmSplit(); return; }
    if (showFixReview.value) { confirmFix(); return; }
    if (showAccept.value) { confirmAccept(); return; }
    if (showQcBlock.value) { forceExport(); return; }
  }
}

const phaseLabel: Record<string, string> = {
  phase0: '阶段0：拆书',
  phase1: '阶段1：诊断',
  phase2: '阶段2：蓝图生成',
  phase2_waiting: '阶段2：等待蓝图确认',
  'phase3-1': '阶段3-1：重构',
  phase4: '阶段4：收尾',
};

function addLog(msg: string, level: LogEntry['level'] = 'info') {
  const ts = new Date().toLocaleTimeString();
  eventLog.value.push({ ts, text: msg, level });
  if (eventLog.value.length > 300) eventLog.value.shift();
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

// 新日志自动滚到底部
watch(eventLog, () => {
  const el = logBoxRef.value;
  if (el) el.scrollTop = el.scrollHeight;
});

onMounted(async () => {
  restore();
  window.addEventListener('keydown', onKeydown);
  try {
    port.value = await waitForPort();
    backendReady.value = true;
    connectSSE(port.value, {
      onEvent: handleSSEEvent,
      onError: (msg) => { errorMsg.value = msg; backendReady.value = false; },
    });
    // 页面刷新/SSE 晚连接时，从后端恢复 UI 状态（暂停态、待确认蓝图、用量等）
    await fetchStatus();
    startStatusPolling();
  } catch (e: any) {
    errorMsg.value = e.message || String(e);
  }
});

onUnmounted(() => {
  stopStatusPolling();
  stopElapsed();
  window.removeEventListener('keydown', onKeydown);
  persist();
});

function startStatusPolling() {
  stopStatusPolling();
  statusTimer = window.setInterval(fetchStatus, 5000);
}
function stopStatusPolling() {
  if (statusTimer !== null) { clearInterval(statusTimer); statusTimer = null; }
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
  if (elapsedTimer !== null) { clearInterval(elapsedTimer); elapsedTimer = null; }
}

async function fetchStatus() {
  if (!port.value) return;
  try {
    const q = outputPath.value ? `?output_path=${encodeURIComponent(outputPath.value)}` : '';
    const res = await fetch(`http://127.0.0.1:${port.value}/api/status${q}`);
    if (!res.ok) return;
    const s = await res.json();
    isPaused.value = !!s.paused;
    // B4：心跳超时判定后端假死（阈值 90s）
    if (s.pipeline_running && s.last_heartbeat) {
      const ageMs = Date.now() - new Date(s.last_heartbeat).getTime();
      zombieWarn.value = ageMs > 90_000;
    } else if (!s.pipeline_running) {
      zombieWarn.value = false;
    }
    usageTokens.value = {
      prompt: s.usage?.prompt_tokens || 0,
      completion: s.usage?.completion_tokens || 0,
    };
    if (s.summaries) summaries.value = s.summaries;
    if (s.total_chapters) totalChapters.value = s.total_chapters;
    if (s.batches) batchStats.value = {
      total: s.batches.total || 0,
      phase1: s.batches.phase1_done || 0,
      phase3: s.batches.phase3_done || 0,
    };
    if (s.total_logic_chapters || s.reconstructed_chapters) chapterProgress.value = {
      total: s.total_logic_chapters || 0,
      done: s.reconstructed_chapters || 0,
    };
    if (s.pipeline_running) {
      pipelineRunning.value = true;
      if (s.current_phase) currentPhase.value = phaseLabel[s.current_phase] || s.current_phase;
    }
    // 恢复时重新弹出蓝图确认（SSE 事件已错过）
    if (s.current_phase === 'phase2_waiting' && s.blueprint && !showBlueprint.value) {
      blueprintText.value = s.blueprint;
      showBlueprint.value = true;
      addLog('检测到待确认蓝图（从后端状态恢复）');
    }
    const lastErr = s.error_logs?.[s.error_logs.length - 1];
    if (lastErr && !pipelineRunning.value) topError.value = lastErr.message;
  } catch {
    // 轮询失败静默，等待下一次
  }
}

function handleSSEEvent(event: string, payload: any) {
  switch (event) {
    case 'run_id':
      runId.value = payload.run_id;
      break;
    case 'run_start':
      pipelineRunning.value = true;
      topError.value = null;
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
      addLog(`阶段完成：${phaseLabel[payload.phase] || payload.phase}${payload.resumed_skip ? '（恢复跳过）' : ''}`, 'ok');
      break;
    case 'batch_start':
      streamingText.value = ''; // 新批次清空流式预览
      addLog(`批次 ${payload.batch_id} 开始：章节 ${payload.chapter_ids?.join(',')}`, 'accent');
      break;
    case 'batch_done':
      addLog(`批次 ${payload.batch_id} 完成`, 'accent');
      break;
    case 'stream_chunk':
      // #2 节流后的流式增量，保留最近 500 字
      streamingText.value = (streamingText.value + (payload.text || '')).slice(-500);
      break;
    case 'split_ready':
      // P0.3：拆书完成，等待用户确认拆分后进入诊断
      splitChapters.value = payload.chapters || [];
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
      stopStatusPolling(); // C: 结束后停掉轮询，避免空转
      disconnectSSE();     // #4 结束后断开 SSE，避免 keep-alive 空挂
      topTip.value = `全部完成！成品在：${payload.output_dir}\\03_final`;
      addLog(`全部完成！输出目录：${payload.output_dir}`, 'ok');
      stopElapsed();
      break;
    case 'stopped':
      pipelineRunning.value = false;
      stopStatusPolling();
      disconnectSSE();
      topTip.value = '流水线已停止，进度已保留；再次点击“开始重构”可从断点继续';
      addLog('流水线已停止', 'warn');
      stopElapsed();
      break;
    case 'error':
      addLog(`错误：${payload.message || payload.code}`, 'err');
      if (payload.kind !== 'warning') {
        pipelineRunning.value = false;
        stopStatusPolling();
        disconnectSSE();
        topError.value = payload.message || payload.code || '未知错误';
      }
      stopElapsed();
      break;
  }
}

// ---------- 文件夹选择 ----------
async function selectInput() {
  if (!inTauri) { topError.value = '浏览器开发模式不支持系统选择器，请直接在输入框中粘贴路径'; return; }
  try {
    if (inputMode.value === 'single_file') {
      const selected = await open({
        multiple: false,
        filters: [{ name: '文档', extensions: ['txt', 'md', 'docx'] }],
      });
      if (typeof selected === 'string') inputPath.value = selected;
    } else {
      const selected = await open({ directory: true });
      if (typeof selected === 'string') inputPath.value = selected;
    }
  } catch (e: any) {
    topError.value = `打开选择器失败：${e?.message || e}`;
  }
}

async function selectOutput() {
  if (!inTauri) { topError.value = '浏览器开发模式不支持系统选择器，请直接在输入框中粘贴路径'; return; }
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
  if (!inputPath.value || !outputPath.value || !apiUrl.value || !model.value) {
    alert('请填写完整：输入路径、输出路径、API URL、模型名');
    return;
  }
  if (refactorMode.value === 'fix_gaps' && !diagnoseJson.value.trim()) {
    alert('「修断层」模式依赖结构化断层清单：请在「优化项」中填写诊断JSON路径，诊断后勾选断层才能闭环修复。');
    return;
  }
  const body = {
    input_mode: inputMode.value,
    input_path: inputPath.value,
    output_path: outputPath.value,
    input_format: inputFormat.value,
    api_url: apiUrl.value,
    api_key: apiKey.value,
    model: model.value,
    context_window: contextWindow.value,
    author_style: authorStyle.value,
    novel_name: novelName.value || null,
    refactor_mode: refactorMode.value,
    review_split: reviewSplit.value,
    refactor_gates: { ...gates },
    forbidden_canon: forbiddenCanon.value.split('\n').map(s => s.trim()).filter(Boolean),
    name_map: Object.fromEntries(
      nameMapText.value.split('\n').map(s => s.trim()).filter(Boolean)
        .map(s => s.split(/[=→：]/).map(p => p.trim()))
        .filter(p => p.length === 2 && p[0]),
    ),
    compare_output: compareOutput.value,
    chapter_snapshots: chapterSnapshots.value,
    diagnose_json: diagnoseJson.value || null,
    proxy: proxy.value || null,
    rich_text: richText.value,
    temperatures: {
      diagnose: tDiagnose.value,
      blueprint: tBlueprint.value,
      refactor: tRefactor.value,
      stitch: tStitch.value,
    },
    batch_interval_sec: batchInterval.value,
    models: Object.fromEntries(
      Object.entries({ diagnose: mDiagnose.value, blueprint: mBlueprint.value, refactor: mRefactor.value, stitch: mStitch.value })
        .filter(([, v]) => v && v.trim()),
    ),
  };
  try {
    const res = await fetch(`${apiBase()}/api/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) {
      alert(`启动失败：${data.detail || res.status}`);
      return;
    }
    runId.value = data.run_id;
    eventLog.value = [];
    addLog(`已提交流水线 run_id=${data.run_id.slice(0, 8)}...`);
    startElapsed();
    persist();
  } catch (e: any) {
    alert(`请求失败：${e.message}`);
  }
}

async function confirmSplit() {
  try {
    const res = await fetch(`${apiBase()}/api/split/confirm`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: runId.value }),
    });
    if (res.ok) {
      showSplit.value = false;
      addLog('拆分已确认，进入诊断');
    } else {
      const d = await res.json();
      alert(`确认失败：${d.detail || res.status}`);
    }
  } catch (e: any) {
    alert(`请求失败：${e.message}`);
  }
}

function toggleFix(gapId: string) {
  const i = fixSelected.value.indexOf(gapId);
  if (i >= 0) fixSelected.value.splice(i, 1);
  else fixSelected.value.push(gapId);
}

async function confirmFix() {
  if (confirmingFix.value) return; // 防重复点击
  confirmingFix.value = true;
  try {
    const res = await fetch(`${apiBase()}/api/fix/confirm`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: runId.value, fix_list: fixSelected.value }),
    });
    if (res.ok) {
      showFixReview.value = false;
      addLog(`已勾选 ${fixSelected.value.length} 处断层，开始修复`);
    } else {
      const d = await res.json();
      alert(`确认失败：${d.detail || res.status}`);
    }
  } catch (e: any) {
    alert(`请求失败：${e.message}`);
  } finally {
    confirmingFix.value = false;
  }
}

async function confirmAccept() {
  if (confirmingAccept.value) return; // 防重复点击
  confirmingAccept.value = true;
  try {
    const res = await fetch(`${apiBase()}/api/accept`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: runId.value }),
    });
    if (res.ok) {
      showAccept.value = false;
      addLog(`批次 ${acceptBatch.value} 已验收，继续重构`);
    } else {
      const d = await res.json();
      alert(`验收失败：${d.detail || res.status}`);
    }
  } catch (e: any) {
    alert(`请求失败：${e.message}`);
  } finally {
    confirmingAccept.value = false;
  }
}

async function openOutput() {
  await callOpenOutput(outputPath.value);
}

async function openFinal() {
  if (!outputPath.value) return;
  const root = outputPath.value.replace(/[\\/]+$/, '');
  const ok = await callOpenOutput(`${root}/03_final`);
  if (!ok) await callOpenOutput(root); // 成品目录尚未生成则打开输出根目录
}

async function callOpenOutput(path: string): Promise<boolean> {
  try {
    const res = await fetch(`${apiBase()}/api/open_output`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ output_path: path }),
    });
    if (!res.ok) {
      const d = await res.json();
      alert(`打开失败：${d.detail || res.status}`);
      return false;
    }
    return true;
  } catch (e: any) {
    alert(`请求失败：${e.message}`);
    return false;
  }
}

async function forceExport() {
  if (forcingExport.value) return; // 防重复点击
  forcingExport.value = true;
  try {
    const res = await fetch(`${apiBase()}/api/export/force`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: runId.value }),
    });
    if (res.ok) {
      showQcBlock.value = false;
      addLog('已忽略质检，强制导出', 'warn');
    } else {
      const d = await res.json();
      alert(`强制导出失败：${d.detail || res.status}`);
    }
  } catch (e: any) {
    alert(`请求失败：${e.message}`);
  } finally {
    forcingExport.value = false;
  }
}

async function exportLog() {
  if (!eventLog.value.length) {
    alert('暂无日志可导出');
    return;
  }
  const text = eventLog.value.map(l => `[${l.ts}] ${l.text}`).join('\n');
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

async function confirmBlueprint() {
  if (confirmingBlueprint.value) return; // 防重复点击
  if (blueprintText.value.length < 100) {
    alert('蓝图内容过短（需 >= 100 字）');
    return;
  }
  confirmingBlueprint.value = true;
  try {
    const res = await fetch(`${apiBase()}/api/blueprint/confirm`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        run_id: runId.value,
        blueprint: blueprintText.value,
        user_edited: true,
      }),
    });
    if (res.ok) {
      showBlueprint.value = false;
      addLog('蓝图已确认，继续重构');
    } else {
      const data = await res.json();
      alert(`确认失败：${data.detail || res.status}`);
    }
  } catch (e: any) {
    alert(`请求失败：${e.message}`);
  } finally {
    confirmingBlueprint.value = false;
  }
}

async function control(action: 'pause' | 'resume' | 'stop') {
  if (controlling.value) return; // 防重复点击
  controlling.value = true;
  try {
    await fetch(`${apiBase()}/api/${action}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: runId.value }),
    });
  } catch (e) {
    console.error(e);
  } finally {
    controlling.value = false;
  }
}

const canStart = computed(() =>
  backendReady.value && !pipelineRunning.value &&
  !!inputPath.value && !!outputPath.value && !!apiUrl.value && !!model.value
);
</script>

<template>
  <div class="app">
    <header class="header">
      <h1>文炼工坊 <span class="version">TextForge v8.8</span></h1>
      <div class="status">
        <span :class="['dot', backendReady ? 'ok' : 'err']"></span>
        {{ backendReady ? '后端已连接' : (errorMsg || '连接中...') }}
        <button class="theme-toggle" @click="toggleTheme">{{ darkMode ? '☀︎' : '☾' }}</button>
      </div>
    </header>

    <main class="main">
      <!-- 全局提示 -->
      <div v-if="topTip" class="banner tip">
        <span>{{ topTip }}</span>
        <button class="banner-close" @click="topTip = null">×</button>
      </div>
      <div v-if="topError" class="banner err">
        <span>⚠ {{ topError }}</span>
        <button class="banner-close" @click="topError = null">×</button>
      </div>
      <div v-if="zombieWarn" class="banner warn">
        <span>⚠ 后端进程超过 90 秒未响应，可能已假死。可尝试「停止」，或检查后端是否仍在运行。</span>
        <button class="banner-close" @click="zombieWarn = false">×</button>
      </div>

      <!-- 配置区 -->
      <section class="panel config" :class="{ disabled: pipelineRunning }">
        <h2>运行配置</h2>

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
            <label class="rich"><input type="checkbox" v-model="gates.lock_names" /> 锁人名/设定名</label>
            <label class="rich"><input type="checkbox" v-model="gates.lock_plot" /> 锁剧情骨架</label>
            <label class="rich"><input type="checkbox" v-model="gates.inject_forbidden" /> 注入禁改清单</label>
            <label class="rich"><input type="checkbox" v-model="gates.require_chapter_accept" /> 逐章验收</label>
            <label class="rich"><input type="checkbox" v-model="gates.skip_stitch_if_smooth" /> 平滑衔接跳过缝合</label>
            <label class="rich"><input type="checkbox" v-model="gates.qc_block_export" /> 质检拦截导出</label>
          </details>
        </div>

        <div v-if="gates.inject_forbidden || refactorMode === 'reskin'" class="row col">
          <label>禁改清单 / 人名映射（配合“保护门·注入禁改清单”与“换皮”模式）</label>
          <textarea v-model="forbiddenCanon" rows="2" placeholder="禁改清单：每行一条，如&#10;主角不能再死亡&#10;不许改设定名" class="lone-area"></textarea>
          <textarea v-model="nameMapText" rows="2" placeholder="换皮映射：每行 旧名=新名，如&#10;林晚=苏晴" class="lone-area"></textarea>
        </div>

        <div class="row">
          <label>输出书名</label>
          <input v-model="novelName" placeholder="可选，默认取输入文件夹名" />
        </div>

        <div class="row">
          <label>拆书确认</label>
          <label class="rich"><input type="checkbox" v-model="reviewSplit" /> 拆书后先预览章节再继续</label>
        </div>

        <details class="adv">
          <summary>优化项：左右对照 / 章快照 / 诊断 JSON</summary>
          <div class="row">
            <label class="rich"><input type="checkbox" v-model="compareOutput" /> 左右对照（重构后按章生成原文|重构后表格）</label>
          </div>
          <div class="row">
            <label class="rich"><input type="checkbox" v-model="chapterSnapshots" /> 章快照（保留重构初稿与缝合终稿）</label>
          </div>
          <div class="row">
            <label>诊断JSON路径</label>
            <input v-model="diagnoseJson" placeholder="可选，留空关闭；设置后按批次输出结构化断层 JSON" />
          </div>
        </details>

        <div class="row">
          <label>代理</label>
          <input v-model="proxy" placeholder="可选，如 http://127.0.0.1:7890" />
          <label class="rich"><input type="checkbox" v-model="richText" /> docx富文本</label>
        </div>

        <details class="adv">
          <summary>高级项：各阶段温度</summary>
          <div class="row">
            <label>诊断</label> <input v-model.number="tDiagnose" type="number" min="0" max="2" step="0.1" />
            <label class="t">蓝图</label> <input v-model.number="tBlueprint" type="number" min="0" max="2" step="0.1" />
          </div>
          <div class="row">
            <label>重构</label> <input v-model.number="tRefactor" type="number" min="0" max="2" step="0.1" />
            <label class="t">缝合</label> <input v-model.number="tStitch" type="number" min="0" max="2" step="0.1" />
          </div>
          <div class="row">
            <label>批次间隔(秒)</label> <input v-model.number="batchInterval" type="number" min="0" step="0.5" />
          </div>
        </details>
        <details class="adv">
          <summary>分阶段模型（可选，留空用全局模型）</summary>
          <div class="row">
            <label>诊断</label> <input v-model="mDiagnose" placeholder="诊断用模型" />
          </div>
          <div class="row">
            <label>蓝图</label> <input v-model="mBlueprint" placeholder="蓝图用模型" />
            <label class="t">重构</label> <input v-model="mRefactor" placeholder="重构用模型" />
          </div>
          <div class="row">
            <label>缝合</label> <input v-model="mStitch" placeholder="缝合用模型" />
          </div>
        </details>

        <div class="row col">
          <label>作者风格</label>
          <textarea v-model="authorStyle" rows="2"></textarea>
        </div>

        <button class="start-btn" :disabled="!canStart" @click="startPipeline">
          开始重构
        </button>
      </section>

      <!-- 进度区 -->
      <section class="panel progress">
        <h2>运行进度</h2>
        <div class="phase-bar">
          <span v-if="currentPhase" class="phase">{{ currentPhase }}</span>
          <span v-if="isPaused" class="badge paused">已暂停</span>
          <span v-if="pipelineRunning" class="badge running">运行中</span>
          <button v-if="outputPath" class="open-btn" @click="openOutput">打开输出目录</button>
          <button v-if="outputPath" class="open-btn ghost" @click="openFinal">打开成品</button>
        </div>

        <div class="controls" v-if="pipelineRunning">
          <button @click="control('pause')" :disabled="isPaused || controlling">暂停</button>
          <button @click="control('resume')" :disabled="!isPaused || controlling">恢复</button>
          <button class="danger" @click="control('stop')" :disabled="controlling">停止</button>
        </div>

        <div class="stats">
          <span class="stat">总章节：<b>{{ totalChapters || '-' }}</b></span>
          <span class="stat">批次：<b>{{ batchStats.phase1 }}/{{ batchStats.total }}</b>（诊断）</span>
          <span class="stat">重构：<b>{{ batchStats.phase3 }}/{{ batchStats.total }}</b></span>
          <span class="stat" v-if="pipelineRunning || elapsed !== '00:00'">已运行：<b>{{ elapsed }}</b></span>
          <span class="stat">Tokens：<b>{{ usageTokens.prompt }}</b>输入 / <b>{{ usageTokens.completion }}</b>输出</span>
        </div>

        <div class="bars">
          <div class="bar">
            <span class="bar-label">诊断批次</span>
            <div class="bar-track"><div class="bar-fill" :style="{ width: diagPercent }"></div></div>
            <span class="bar-num">{{ batchStats.phase1 }}/{{ batchStats.total }} · {{ diagPercent }}</span>
          </div>
          <div class="bar">
            <span class="bar-label">重构批次</span>
            <div class="bar-track"><div class="bar-fill recon" :style="{ width: reconPercent }"></div></div>
            <span class="bar-num">{{ batchStats.phase3 }}/{{ batchStats.total }} · {{ reconPercent }}</span>
          </div>
          <div v-if="chapterProgress.total" class="bar">
            <span class="bar-label">已重构章</span>
            <div class="bar-track"><div class="bar-fill chapter" :style="{ width: chapterPercent }"></div></div>
            <span class="bar-num">{{ chapterProgress.done }}/{{ chapterProgress.total }} · {{ chapterPercent }}</span>
          </div>
        </div>

        <div v-if="streamingText" class="stream-box"><b class="stream-title">流式输出</b>{{ streamingText }}</div>

        <div class="log-head">
          <b>事件日志</b>
          <span class="log-count">{{ eventLog.length }} 条</span>
          <button class="min-btn" @click="exportLog">导出</button>
          <button class="min-btn" @click="eventLog = []">清空</button>
        </div>
        <div ref="logBoxRef" class="log-box">
          <div v-for="(line, i) in eventLog" :key="i" class="log-line" :class="`log-${line.level}`"><span class="log-ts">{{ line.ts }}</span>{{ line.text }}</div>
          <div v-if="!eventLog.length" class="log-empty">暂无事件</div>
        </div>
      </section>
    </main>

    <!-- 拆书确认弹窗 -->
    <div v-if="showSplit" class="modal-mask">
      <div class="modal">
        <h3>请确认拆书结果</h3>
        <p class="hint">共拆分出 {{ splitChapters.length }} 章。确认后进入诊断；如需调整请停止后修改原始文档重跑。</p>
        <div class="sum-list">
          <div v-for="(c, i) in splitChapters" :key="i" class="split-row">
            <span class="split-id">{{ c.filename }}</span>
            <span v-if="c.is_virtual" class="badge paused">虚拟段</span>
            <span v-if="c.is_empty" class="badge err2">空章</span>
          </div>
        </div>
        <div class="modal-actions">
          <span></span>
          <button @click="confirmSplit">确认拆分，继续</button>
        </div>
      </div>
    </div>

    <!-- 断层勾选确认弹窗 -->
    <div v-if="showFixReview" class="modal-mask">
      <div class="modal">
        <h3>修复断层：请勾选需修复项</h3>
        <p class="hint">共诊断出 {{ fixGaps.length }} 处断层，默认全选。确认后将按勾选项执行修复（其余内容保持原样）。</p>
        <label class="sum-toggle"><input type="checkbox"
          :checked="fixGaps.length > 0 && fixSelected.length === fixGaps.length"
          @change="fixGaps.length === fixSelected.length ? fixSelected = [] : fixSelected = fixGaps.map((g:any)=>g.id)" />
          全选 / 全不选</label>
        <div class="sum-list">
          <label v-for="g in fixGaps" :key="g.id" class="fix-row">
            <input type="checkbox" :checked="fixSelected.includes(g.id)" @change="toggleFix(g.id)" />
            <span class="split-id">{{ g.location || g.batch_id }}</span>
            <span class="badge" :class="g.severity === 'high' ? 'badge-err' : (g.severity === 'medium' ? 'badge-warn' : '')">{{ g.severity }}</span>
            <span class="fix-desc">{{ g.description }}</span>
          </label>
        </div>
        <div class="modal-actions">
          <span></span>
          <button :disabled="confirmingFix" @click="confirmFix">确认修复（{{ fixSelected.length }} 处）</button>
        </div>
      </div>
    </div>

    <!-- 逐章验收确认弹窗 -->
    <div v-if="showAccept" class="modal-mask">
      <div class="modal">
        <h3>验收重构稿：批次 {{ acceptBatch }}</h3>
        <p class="hint">以下为本批已产出的章节（预览为开头 80 字）。验收通过后继续下一批；不通过请停止后调整或重跑。</p>
        <div class="sum-list">
          <div v-for="c in acceptChapters" :key="c.id" class="split-row">
            <span class="split-id">{{ c.id }}</span>
            <span class="fix-desc">{{ c.preview }}</span>
          </div>
        </div>
        <div class="modal-actions">
          <span></span>
          <button :disabled="confirmingAccept" @click="confirmAccept">验收通过，继续</button>
        </div>
      </div>
    </div>

    <!-- 质检拦截决策弹窗 -->
    <div v-if="showQcBlock" class="modal-mask">
      <div class="modal">
        <h3>质检未通过（{{ qcIssues.length }} 项）</h3>
        <p class="hint">以下问题按规则检出不满足导出条件。可选择「强制导出」忽略并继续，或「停止」保留进度待修正后重跑。</p>
        <div class="sum-list">
          <div v-for="(it, i) in qcIssues" :key="i" class="split-row"><span class="fix-desc">{{ it }}</span></div>
        </div>
        <div class="modal-actions">
          <button class="danger" @click="control('stop')">停止</button>
          <button class="primary" :disabled="forcingExport" @click="forceExport">强制导出</button>
        </div>
      </div>
    </div>

    <!-- 蓝图确认弹窗 -->
    <div v-if="showBlueprint" class="modal-mask">
      <div class="modal">
        <h3>请确认重构蓝图</h3>
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

        <div class="modal-actions">
          <span class="count">{{ blueprintText.length }} 字</span>
          <button @click="confirmBlueprint" :disabled="confirmingBlueprint">
            {{ confirmingBlueprint ? '确认中...' : '确认并继续' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.app {
  display: flex;
  flex-direction: column;
  height: 100vh;
  font-family: "Microsoft YaHei", system-ui, sans-serif;
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
.header h1 { font-size: 18px; margin: 0; }
.version { font-size: 12px; opacity: 0.7; font-weight: normal; }
.status { font-size: 13px; display: flex; align-items: center; gap: 6px; }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.dot.ok { background: #48bb78; }
.dot.err { background: #fc8181; }

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
  box-shadow: 0 1px 3px rgba(0,0,0,0.08);
  overflow-y: auto;
}
.panel h2 { font-size: 15px; margin: 0 0 14px; color: #2d3748; }
.panel.config.disabled { opacity: 0.55; pointer-events: none; }
.panel.progress { display: flex; flex-direction: column; }

.banner {
  grid-column: 1 / -1;
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 14px;
  border-radius: 6px;
  font-size: 13px;
}
.banner.tip { background: #c6f6d5; color: #22543d; border: 1px solid #9ae6b4; }
.banner.err { background: #fed7d7; color: #822727; border: 1px solid #feb2b2; }
.banner.warn { background: #fefcbf; color: #975a16; border: 1px solid #f6e05e; }
.banner-close {
  background: transparent; border: none; font-size: 16px; cursor: pointer;
  color: inherit; padding: 0 4px;
}

.stats {
  display: flex; flex-wrap: wrap; gap: 14px;
  font-size: 12px; color: #4a5568;
  padding: 8px 10px; margin-bottom: 10px;
  background: #f7fafc; border-radius: 5px;
}
.stat b { color: #2d3748; }

.bars { display: flex; flex-direction: column; gap: 7px; margin-bottom: 10px; }
.bar { display: flex; align-items: center; gap: 8px; font-size: 11px; color: #4a5568; }
.bar-label { flex: 0 0 46px; }
.bar-track { flex: 1; height: 8px; background: #edf2f7; border-radius: 5px; overflow: hidden; }
.bar-fill { height: 100%; width: 0; background: #3182ce; border-radius: 5px; transition: width 0.3s ease; }
.bar-fill.recon { background: #38a169; }
.bar-fill.chapter { background: #805ad5; }
.bar-num { flex: 0 0 60px; text-align: right; }
.open-btn { margin-left: auto; padding: 4px 12px; border: 1px solid #3182ce; color: #3182ce; background: #fff; border-radius: 5px; font-size: 12px; cursor: pointer; }
.open-btn:hover { background: #ebf8ff; }
.open-btn.ghost { margin-left: 6px; border-color: #38a169; color: #38a169; }
.open-btn.ghost:hover { background: #f0fff4; }

.row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}
.row.col { flex-direction: column; align-items: stretch; }
.row label { width: 80px; font-size: 13px; color: #4a5568; flex-shrink: 0; }
.row label.fmt, .row label.ctx, .row label.t { width: auto; margin-left: 12px; }
.row label.rich { width: auto; display: flex; align-items: center; gap: 4px; cursor: pointer; }
.row label.rich input { width: auto; margin: 0; }
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
.row label.t + input, .row label.rich + input { flex: 1; }
.adv { border: 1px solid #e2e8f0; border-radius: 6px; padding: 8px 12px; margin-bottom: 10px; }
.adv summary { font-size: 13px; color: #4a5568; cursor: pointer; }
.adv.gates { display: flex; flex-direction: column; flex: 1; }
.adv.gates .rich { margin: 3px 0; }
.row textarea {
  width: 100%;
  padding: 6px 10px;
  border: 1px solid #d1d5db;
  border-radius: 5px;
  font-size: 13px;
  resize: vertical;
}
.lone-area + .lone-area { margin-top: 6px; }
.row button {
  padding: 6px 14px;
  border: 1px solid #d1d5db;
  background: #fff;
  border-radius: 5px;
  cursor: pointer;
  font-size: 13px;
}
.row button:hover { background: #f0f4f8; }

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
.start-btn:disabled { background: #a0aec0; cursor: not-allowed; }

.phase-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}
.phase { font-size: 14px; font-weight: 600; color: #2d3748; }
.badge { font-size: 12px; padding: 2px 8px; border-radius: 10px; }
.badge.running { background: #bee3f8; color: #2c5282; }
.badge.paused { background: #fefcbf; color: #975a16; }

.controls { display: flex; gap: 8px; margin-bottom: 12px; }
.controls button {
  padding: 5px 14px;
  border: 1px solid #d1d5db;
  background: #fff;
  border-radius: 5px;
  cursor: pointer;
  font-size: 13px;
}
.controls button.danger { color: #c53030; border-color: #feb2b2; }
.controls button:disabled { opacity: 0.4; cursor: not-allowed; }

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
.stream-title { color: #3182ce; margin-right: 6px; }

.log-box {
  flex: 1;
  min-height: 220px;
  overflow-y: auto;
  background: #1a202c;
  color: #e2e8f0;
  border-radius: 6px;
  padding: 10px 12px;
  font-family: "Consolas", monospace;
  font-size: 12px;
  line-height: 1.6;
}
.log-line { white-space: pre-wrap; word-break: break-all; }
.log-line .log-ts { color: #718096; margin-right: 8px; }
.log-ok { color: #9ae6b4; }
.log-warn { color: #fefcbf; }
.log-err { color: #fc8181; }
.log-accent { color: #90cdf4; }
.log-empty { color: #718096; }
.log-head { display: flex; align-items: center; gap: 8px; color: #4a5568; font-size: 12px; margin-bottom: 6px; }
.log-count { color: #718096; }
.min-btn { margin-left: auto; padding: 2px 10px; background: transparent; border: 1px solid #cbd5e0; border-radius: 4px; color: #4a5568; font-size: 12px; cursor: pointer; }
.min-btn:hover { background: #edf2f7; }

.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}
.modal {
  background: #fff;
  border-radius: 8px;
  padding: 20px 24px;
  width: 640px;
  max-height: 85vh;
  display: flex;
  flex-direction: column;
}
.modal h3 { margin: 0 0 6px; }
.hint { font-size: 12px; color: #718096; margin: 0 0 10px; }
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
.modal-actions {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 12px;
}
.sum-toggle {
  display: flex; align-items: center; gap: 6px;
  font-size: 12px; color: #4a5568; cursor: pointer; margin: 10px 0 4px;
}
.sum-toggle input { width: auto; margin: 0; }
.sum-list {
  max-height: 160px; overflow-y: auto;
  border: 1px solid #e2e8f0; border-radius: 5px; padding: 8px 10px;
  font-size: 12px; background: #f7fafc;
}
.sum-empty { color: #718096; }
.split-row { display: flex; align-items: center; gap: 8px; padding: 2px 0; font-size: 12px; }
.split-id { flex: 1; color: #2d3748; word-break: break-all; }
.badge.err2 { background: #fed7d7; color: #822727; }
.badge-err { background: #fed7d7; color: #822727; }
.badge-warn { background: #fefcbf; color: #975a16; }
.fix-row { display: flex; align-items: center; gap: 6px; padding: 3px 0; cursor: pointer; }
.fix-row input { width: auto; margin: 0; }
.fix-desc { color: #4a5568; word-break: break-all; }
.sum-item summary { cursor: pointer; color: #2d3748; margin-bottom: 2px; }
.sum-preview { color: #4a5568; white-space: pre-wrap; word-break: break-all; }
.count { font-size: 12px; color: #718096; }
.modal-actions button {
  padding: 8px 20px;
  background: #3182ce;
  color: #fff;
  border: none;
  border-radius: 5px;
  cursor: pointer;
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
.theme-toggle:hover { background: rgba(255, 255, 255, 0.12); }

/* 深色主题（html.dark）最小覆盖 */
html.dark .app { background: #171923; color: #e2e8f0; }
html.dark .panel { background: #1f2733; box-shadow: 0 1px 3px rgba(0,0,0,0.4); }
html.dark .panel h2 { color: #e2e8f0; }
html.dark .row label,
html.dark .phase, html.dark .phase-bar .open-btn { color: #e2e8f0; }
html.dark input, html.dark select, html.dark textarea {
  background: #1a202c; color: #e2e8f0; border-color: #4a5568;
}
html.dark .stats { background: #232b39; color: #a0aec0; }
html.dark .stat b { color: #e2e8f0; }
html.dark .bar-track { background: #2d3748; }
html.dark .bar, html.dark .bar-num, html.dark .bar-label { color: #a0aec0; }
html.dark .adv { border-color: #4a5568; }
html.dark .adv summary, html.dark .log-count, html.dark .sum-empty { color: #a0aec0; }
html.dark .stream-box { background: #1a202c; border-color: #4a5568; color: #e2e8f0; }
html.dark .sum-list { background: #232b39; border-color: #4a5568; }
html.dark .split-id, html.dark .sum-item summary { color: #e2e8f0; }
html.dark .fix-desc, html.dark .sum-preview { color: #a0aec0; }
html.dark .modal { background: #1f2733; }
html.dark .controls button, html.dark .row button { background: #2d3748; color: #e2e8f0; border-color: #4a5568; }
html.dark .start-btn:disabled { background: #4a5568; }
</style>
