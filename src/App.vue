<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue';
import { open } from '@tauri-apps/plugin-dialog';
import { isTauri } from '@tauri-apps/api/core';
import { waitForPort } from './api/backend';
import { connectSSE, disconnectSSE } from './api/sse';

// ---------- 后端连接 ----------
const port = ref<number | null>(null);
const backendReady = ref(false);
const errorMsg = ref<string | null>(null);
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

// ---------- 运行状态 ----------
const runId = ref<string | null>(null);
const pipelineRunning = ref(false);
const isPaused = ref(false);
const currentPhase = ref('');
const eventLog = ref<string[]>([]);
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
const topError = ref<string | null>(null);
const topTip = ref<string | null>(null);

const phaseLabel: Record<string, string> = {
  phase0: '阶段0：拆书',
  phase1: '阶段1：诊断',
  phase2: '阶段2：蓝图生成',
  phase2_waiting: '阶段2：等待蓝图确认',
  'phase3-1': '阶段3-1：重构',
  phase4: '阶段4：收尾',
};

function addLog(msg: string) {
  const ts = new Date().toLocaleTimeString();
  eventLog.value.push(`[${ts}] ${msg}`);
  if (eventLog.value.length > 200) eventLog.value.shift();
}

onMounted(async () => {
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

onUnmounted(() => stopStatusPolling());

function startStatusPolling() {
  stopStatusPolling();
  statusTimer = window.setInterval(fetchStatus, 5000);
}
function stopStatusPolling() {
  if (statusTimer !== null) { clearInterval(statusTimer); statusTimer = null; }
}

async function fetchStatus() {
  if (!port.value) return;
  try {
    const q = outputPath.value ? `?output_path=${encodeURIComponent(outputPath.value)}` : '';
    const res = await fetch(`http://127.0.0.1:${port.value}/api/status${q}`);
    if (!res.ok) return;
    const s = await res.json();
    isPaused.value = !!s.paused;
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
      break;
    case 'phase_done':
      if (payload.phase === 'phase0') totalChapters.value = payload.total_chapters;
      currentPhase.value = phaseLabel[payload.phase] || payload.phase;
      addLog(`阶段完成：${phaseLabel[payload.phase] || payload.phase}${payload.resumed_skip ? '（恢复跳过）' : ''}`);
      break;
    case 'batch_start':
      streamingText.value = ''; // 新批次清空流式预览
      addLog(`批次 ${payload.batch_id} 开始：章节 ${payload.chapter_ids?.join(',')}`);
      break;
    case 'batch_done':
      addLog(`批次 ${payload.batch_id} 完成`);
      break;
    case 'stream_chunk':
      // #2 节流后的流式增量，保留最近 500 字
      streamingText.value = (streamingText.value + (payload.text || '')).slice(-500);
      break;
    case 'split_ready':
      // P0.3：拆书完成，等待用户确认拆分后进入诊断
      splitChapters.value = payload.chapters || [];
      showSplit.value = true;
      addLog(`拆书完成，共 ${splitChapters.value.length} 章，请确认`);
      break;
    case 'fix_ready':
      // fix_gaps 闭环：诊断断层清单已就绪，等待勾选确认
      fixGaps.value = payload.gaps || [];
      fixSelected.value = fixGaps.value.map((g: any) => g.id).filter(Boolean);
      showFixReview.value = true;
      addLog(`断层诊断完成，共 ${fixGaps.value.length} 项，请勾选需要修复的断层`);
      break;
    case 'blueprint_ready':
      blueprintText.value = payload.blueprint || '';
      showBlueprint.value = true;
      currentPhase.value = 'phase2_waiting';
      addLog(payload.resumed ? '待确认蓝图（恢复）' : '蓝图已生成，请确认后继续');
      break;
    case 'stitch_start':
      addLog(`缝合阶段${payload.stage}开始（${payload.count} 处）`);
      break;
    case 'stitch_done':
      addLog(`缝合阶段${payload.stage}完成`);
      break;
    case 'paused':
      isPaused.value = true;
      addLog('已暂停');
      break;
    case 'resumed':
      isPaused.value = false;
      addLog('已恢复');
      break;
    case 'done':
      pipelineRunning.value = false;
      currentPhase.value = '完成';
      stopStatusPolling(); // C: 结束后停掉轮询，避免空转
      disconnectSSE();     // #4 结束后断开 SSE，避免 keep-alive 空挂
      topTip.value = `全部完成！成品在：${payload.output_dir}\\03_final`;
      addLog(`全部完成！输出目录：${payload.output_dir}`);
      break;
    case 'stopped':
      pipelineRunning.value = false;
      stopStatusPolling();
      disconnectSSE();
      topTip.value = '流水线已停止，进度已保留；再次点击“开始重构”可从断点继续';
      addLog('流水线已停止');
      break;
    case 'error':
      addLog(`错误：${payload.message || payload.code}`);
      if (payload.kind !== 'warning') {
        pipelineRunning.value = false;
        stopStatusPolling();
        disconnectSSE();
        topError.value = payload.message || payload.code || '未知错误';
      }
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
          <span class="stat">Tokens：<b>{{ usageTokens.prompt }}</b>输入 / <b>{{ usageTokens.completion }}</b>输出</span>
        </div>

        <div v-if="streamingText" class="stream-box"><b class="stream-title">流式输出</b>{{ streamingText }}</div>

        <div class="log-box">
          <div v-for="(line, i) in eventLog" :key="i" class="log-line">{{ line }}</div>
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
.row textarea {
  width: 100%;
  padding: 6px 10px;
  border: 1px solid #d1d5db;
  border-radius: 5px;
  font-size: 13px;
  resize: vertical;
}
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
.log-empty { color: #718096; }

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
</style>
