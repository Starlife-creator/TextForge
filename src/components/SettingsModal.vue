<script setup lang="ts">
/**
 * 「系统与后端设置」面板。
 * 把此前完全没有 UI 入口的东西全部可视化：
 *   - 后端连接状态（端口 / PID / TCP 可达性 / 运行时长）
 *   - 后端生命周期操作（重连 / 重启 / 停止 / 打开数据目录与日志）
 *   - backend.log 实时查看
 *   - 模型服务配置（探测模型列表 + 连通性自检 + 端点一键修正）
 *   - 一键诊断：把「后端连接断开」这类黑盒问题变成可读结论
 */
import { ref, computed, watch, onMounted, onUnmounted } from 'vue';
import {
  getSystemInfo, getLogs, openPath, shutdownBackend, fetchModels, checkLlm,
  type SystemInfo, type ModelListResult, type LlmCheckResult,
} from '../api/system';
import { fetchBackendInfo, invokeRestartBackend, type BackendInfo } from '../api/backend';

const props = defineProps<{
  port: number | null;
  connected: boolean;
  model: string;
  apiUrl: string;
  apiKey: string;
  proxy: string;
  sslVerify: boolean;
}>();
const emit = defineEmits<{
  (e: 'close'): void;
  (e: 'apply-model', v: { model: string; apiUrl: string; apiKey: string; proxy: string; sslVerify: boolean }): void;
  (e: 'reconnect'): void;
}>();

type Tab = 'backend' | 'logs' | 'model' | 'diagnose';
const tab = ref<Tab>('backend');

// ---------- 状态 ----------
const info = ref<SystemInfo | null>(null);
const native = ref<BackendInfo | null>(null);
const busy = ref('');
const notice = ref<{ kind: 'ok' | 'err' | 'info'; text: string } | null>(null);
const infoErr = ref('');
let sayTimer: number | null = null;

const tabRef = ref<Tab>('backend');

// ---------- 日志 ----------
const logLines = ref<string[]>([]);
const logPath = ref('');
const logTotal = ref(0);
const logOffset = ref(0);
const logAutoScroll = ref(true);
const logPaused = ref(false);
const logFilter = ref('');
const logBoxRef = ref<HTMLElement | null>(null);
let logTimer: number | null = null;
let infoTimer: number | null = null;

const filteredLogs = computed(() => {
  const f = logFilter.value.trim().toLowerCase();
  if (!f) return logLines.value;
  return logLines.value.filter(l => l.toLowerCase().includes(f));
});

const uptimeText = computed(() => {
  const s = info.value?.uptime_sec ?? 0;
  if (s < 60) return `${Math.round(s)} 秒`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} 分 ${Math.round(s % 60)} 秒`;
  return `${Math.floor(m / 60)} 小时 ${m % 60} 分`;
});

const connectionLabel = computed(() => {
  // 先判 connected：否则首次读取后端信息的那几帧里会显示红点「未连接」，
  // 尽管事件流其实已经在线，容易让人误以为出故障。
  if (props.connected) {
    return { ok: true, text: info.value ? '已连接（事件流在线）' : '已连接（读取后端信息中）' };
  }
  return { ok: false, text: '事件流断开（正在自动重连）' };
});

// ---------- 工具 ----------
function say(kind: 'ok' | 'err' | 'info', text: string) {
  notice.value = { kind, text };
  if (sayTimer !== null) clearTimeout(sayTimer);
  if (kind === 'ok') {
    sayTimer = window.setTimeout(() => {
      if (notice.value?.text === text) notice.value = null;
      sayTimer = null;
    }, 4000);
  }
}
async function withBusy(key: string, fn: () => Promise<void>) {
  busy.value = key;
  try { await fn(); } catch (e: any) { say('err', e?.message || String(e)); }
  finally { busy.value = ''; }
}
function requirePort(): number {
  if (!props.port) throw new Error('后端端口未知，无法请求。请先重启后端。');
  return props.port;
}

// ---------- 后端信息 ----------
async function refreshInfo() {
  try {
    infoErr.value = '';
    native.value = await fetchBackendInfo();
    if (props.port) info.value = await getSystemInfo(props.port);
  } catch (e: any) {
    infoErr.value = e?.message || String(e);
  }
}

// ---------- 操作 ----------
async function doReconnect() {
  await withBusy('reconnect', async () => {
    say('info', '正在重新握手…');
    emit('reconnect');
    await refreshInfo();
    say('ok', '已触发重连，后台会自动重试直到成功');
  });
}

async function doRestart() {
  await withBusy('restart', async () => {
    say('info', '正在重启后端…');
    await invokeRestartBackend();
    say('ok', '重启指令已发出，等待新进程写入端口文件…');
    emit('reconnect');
    // 新进程起来要一点时间，轮询几次让 UI 自动跟上
    for (let i = 0; i < 12; i++) {
      await new Promise(r => setTimeout(r, 1000));
      await refreshInfo();
      if (native.value?.reachable || info.value) break;
    }
    say('ok', '后端已重启');
  });
}

async function doShutdown() {
  await withBusy('stop', async () => {
    if (info.value?.pipeline_running && !window.confirm('流水线正在运行，强制停止后端会中断本次任务。继续？')) return;
    await shutdownBackend(requirePort(), true);
    say('ok', '后端已停止。点击「重新连接」或「重启后端」恢复。');
    await refreshInfo();
  });
}

async function doOpen(target: 'app_dir' | 'logs_dir' | 'log_file') {
  await withBusy(`open_${target}`, async () => {
    await openPath(requirePort(), target);
    say('ok', '已在文件管理器中打开');
  });
}

// ---------- 日志 ----------
async function loadLogs(full = false) {
  if (logPaused.value) return;
  try {
    const r = await getLogs(requirePort(), full ? 0 : logOffset.value, full ? 500 : 200);
    logPath.value = r.path;
    logTotal.value = r.total;
    if (full) {
      logLines.value = r.lines;
      logOffset.value = r.offset;
    } else if (r.lines.length) {
      logLines.value = [...logLines.value, ...r.lines];
      logOffset.value = r.offset;
      if (logLines.value.length > 2000) logLines.value = logLines.value.slice(-1500);
    }
    if (logAutoScroll.value) {
      requestAnimationFrame(() => {
        const el = logBoxRef.value;
        if (el) el.scrollTop = el.scrollHeight;
      });
    }
  } catch (e: any) {
    logLines.value = [...logLines.value, `[读取日志失败] ${e?.message || e}`];
  }
}
function clearLogView() { logLines.value = []; logOffset.value = 0; loadLogs(true); }
async function copyLogs() {
  try {
    await navigator.clipboard.writeText(logLines.value.join('\n'));
    say('ok', `已复制 ${logLines.value.length} 行`);
  } catch (e: any) { say('err', `复制失败：${e?.message || e}`); }
}

// ---------- 模型配置 ----------
const mApiUrl = ref(props.apiUrl);
const mApiKey = ref(props.apiKey);
const mModel = ref(props.model);
const mProxy = ref(props.proxy);
const mSslVerify = ref(props.sslVerify);
// 父组件可能在面板打开期间改模型配置；不 watch 就会显示旧值，
// 再点「应用到运行配置」就把旧值写回去。
watch(() => [props.apiUrl, props.apiKey, props.model, props.proxy, props.sslVerify] as const,
  ([u, k, m, p, s]) => { mApiUrl.value = u; mApiKey.value = k; mModel.value = m; mProxy.value = p; mSslVerify.value = s; });
const models = ref<string[]>([]);
const modelsErr = ref('');
const modelsEndpoint = ref('');
const probing = ref(false);
const llmCheck = ref<LlmCheckResult | null>(null);

/** LM Studio / Ollama / OpenAI 兼容端的常见形态，一键填入省得手敲 */
const ENDPOINT_PRESETS: { label: string; url: string; hint: string }[] = [
  { label: 'LM Studio', url: 'http://127.0.0.1:1234/v1/chat/completions', hint: '本机 LM Studio 默认端口 1234' },
  { label: 'Ollama', url: 'http://127.0.0.1:11434/v1/chat/completions', hint: '本机 Ollama 默认端口 11434' },
  { label: 'OpenAI 兼容', url: 'https://api.example.com/v1/chat/completions', hint: '云端 OpenAI 兼容服务' },
];

async function probeModels() {
  await withBusy('probe', async () => {
    modelsErr.value = '';
    llmCheck.value = null;
    probing.value = true;
    try {
      const r: ModelListResult = await fetchModels(requirePort(), mApiUrl.value, mApiKey.value);
      models.value = r.models;
      modelsEndpoint.value = r.endpoint || '';
      if (r.ok) {
        if (r.models.length && !r.models.includes(mModel.value)) mModel.value = r.models[0];
        say('ok', `探测到 ${r.models.length} 个模型（${r.endpoint}）`);
      } else {
        modelsErr.value = (r.errors || []).join('；') || '未找到模型列表';
        say('err', '模型探测失败');
      }
    } finally { probing.value = false; }
  });
}

async function runLlmCheck() {
  await withBusy('check', async () => {
    llmCheck.value = await checkLlm(requirePort(), {
      api_url: mApiUrl.value, model: mModel.value, api_key: mApiKey.value,
      proxy: mProxy.value, ssl_verify: mSslVerify.value,
    });
  });
}

function applyModel() {
  emit('apply-model', {
    model: mModel.value.trim(), apiUrl: mApiUrl.value.trim(),
    apiKey: mApiKey.value, proxy: mProxy.value.trim(), sslVerify: mSslVerify.value,
  });
  say('ok', '已应用到运行配置');
}

/** 把误填的 /models 列表端点纠正为 /chat/completions 对话端点 */
function fixEndpoint() {
  const u = mApiUrl.value.trim().replace(/\/+$/, '');
  if (!u.endsWith('/models')) { say('info', '当前 URL 已经是对话端点，无需修正'); return; }
  mApiUrl.value = u.replace(/\/models$/, '/chat/completions');
  say('ok', `已修正为对话端点：${mApiUrl.value}`);
}

// ---------- 诊断 ----------
interface DiagItem { name: string; ok: boolean; detail: string }
const diag = ref<DiagItem[]>([]);
const diagnosing = ref(false);

async function runDiagnose() {
  diagnosing.value = true;
  diag.value = [];
  const add = (name: string, ok: boolean, detail: string) => diag.value.push({ name, ok, detail });
  try {
    // 1. 原生侧（端口文件 + TCP 探测）
    const bi = await fetchBackendInfo();
    native.value = bi;
    if (bi?.port) add('端口文件', true, `port=${bi.port} pid=${bi.pid}`);
    else add('端口文件', false, '未找到 port.json（后端可能未启动）');
    if (bi) add('TCP 探测', bi.reachable, bi.reachable ? `127.0.0.1:${bi.port} 可连接` : '端口文件存在但连不上（孤儿进程或进程已死）');

    // 2. HTTP 健康检查
    if (props.port) {
      try {
        const r = await fetch(`http://127.0.0.1:${props.port}/api/health`);
        add('/api/health', r.ok, `HTTP ${r.status}`);
      } catch (e: any) { add('/api/health', false, e?.message || String(e)); }
      // 3. 系统信息接口
      try {
        const si = await getSystemInfo(props.port);
        info.value = si;
        add('/api/system/info', true, `v${si.version} 运行 ${si.uptime_sec}s`);
      } catch (e: any) { add('/api/system/info', false, e?.message || String(e)); }
      // 4. 日志可读
      try {
        const lg = await getLogs(props.port, 0, 50);
        add('backend.log', lg.exists, lg.exists ? `${lg.total} 行，${(lg.size || 0) / 1024 | 0} KB` : '日志文件不存在');
      } catch (e: any) { add('backend.log', false, e?.message || String(e)); }
      // 5. 模型端点
      try {
        const r = await fetchModels(props.port, mApiUrl.value || props.apiUrl, mApiKey.value || props.apiKey);
        add('模型端点', r.ok, r.ok ? `${r.models.length} 个模型 @ ${r.endpoint}` : (r.errors || []).join('；'));
      } catch (e: any) { add('模型端点', false, e?.message || String(e)); }
    }
    add('事件流 SSE', props.connected, props.connected ? '流在线' : '断开中（会自动重连；后端心跳 15s）');
  } catch (e: any) {
    // 没有 catch 时，任何未来新增的未捕获 await 都会变成 unhandled rejection
    say('err', `诊断中断：${e?.message || e}`);
  } finally {
    diagnosing.value = false;
  }
}

// ---------- 生命周期 ----------
watch(tab, (t) => {
  tabRef.value = t;
  if (t === 'logs' && !logLines.value.length) loadLogs(true);
});

onMounted(() => {
  refreshInfo();
  infoTimer = window.setInterval(refreshInfo, 5000);
  logTimer = window.setInterval(() => { if (tabRef.value === 'logs' && !logPaused.value) loadLogs(false); }, 3000);
});

onUnmounted(() => {
  if (infoTimer !== null) clearInterval(infoTimer);
  if (logTimer !== null) clearInterval(logTimer);
  if (sayTimer !== null) clearTimeout(sayTimer);
});
</script>

<template>
  <div class="modal-mask" @click.self="emit('close')">
    <div class="modal modal-wide">
      <div class="head">
        <h3>系统与后端设置</h3>
        <button class="x" @click="emit('close')">×</button>
      </div>

      <nav class="tabs">
        <button :class="{ on: tab === 'backend' }" @click="tab = 'backend'">后端服务</button>
        <button :class="{ on: tab === 'logs' }" @click="tab = 'logs'">运行日志</button>
        <button :class="{ on: tab === 'model' }" @click="tab = 'model'">模型服务</button>
        <button :class="{ on: tab === 'diagnose' }" @click="tab = 'diagnose'">一键诊断</button>
      </nav>

      <div v-if="notice" class="notice" :class="notice.kind">{{ notice.text }}</div>

      <!-- ============ 后端服务 ============ -->
      <div v-show="tab === 'backend'" class="body">
        <div class="status-card" :class="connectionLabel.ok ? 'ok' : 'bad'">
          <span class="big-dot"></span>
          <div>
            <div class="status-title">{{ connectionLabel.text }}</div>
            <div class="status-sub">
              <template v-if="info">
                v{{ info.version }} · 端口 <b>{{ info.port || '?' }}</b> · PID <b>{{ info.pid }}</b> ·
                已运行 {{ uptimeText }} · Python {{ info.python }} · {{ info.platform }}
              </template>
              <template v-else-if="native">
                端口 <b>{{ native.port || '?' }}</b> · PID <b>{{ native.pid || '?' }}</b> ·
                TCP {{ native.reachable ? '可达' : '不可达' }} · {{ native.debug ? '开发模式' : '发布模式' }}
              </template>
              <template v-else>正在读取后端信息…</template>
            </div>
          </div>
        </div>
        <p v-if="infoErr" class="hint err">读取后端信息失败：{{ infoErr }}</p>

        <div class="btn-row">
          <button :disabled="!!busy" @click="doReconnect">重新连接</button>
          <button class="primary" :disabled="!!busy" @click="doRestart">
            {{ busy === 'restart' ? '重启中…' : '重启后端' }}
          </button>
          <button class="danger" :disabled="!!busy" @click="doShutdown">停止后端</button>
          <span class="spacer"></span>
          <button class="ghost" :disabled="!!busy" @click="doOpen('app_dir')">打开数据目录</button>
          <button class="ghost" :disabled="!!busy" @click="doOpen('logs_dir')">打开日志目录</button>
        </div>

        <details class="block">
          <summary>后端文件位置</summary>
          <table class="kv">
            <tbody>
              <tr><th>数据目录</th><td>{{ info?.app_dir || native?.app_dir || '—' }}</td></tr>
              <tr><th>端口文件</th><td>{{ info?.port_file || native?.port_file || '—' }}</td></tr>
              <tr><th>日志文件</th><td>{{ info?.log_path || native?.log_path || '—' }}</td></tr>
              <tr><th>运行模式</th><td>{{ info?.debug ? '开发（TEXTFORGE_DEBUG=1）' : '发布（sidecar）' }}</td></tr>
              <tr><th>解释器</th><td>{{ info?.executable || '—' }}</td></tr>
              <tr><th>当前 run</th><td>{{ info?.run_id || '（无）' }} · 订阅者 {{ Object.keys(info?.subscribers || {}).length }}</td></tr>
            </tbody>
          </table>
        </details>
      </div>

      <!-- ============ 运行日志 ============ -->
      <div v-show="tab === 'logs'" class="body">
        <div class="btn-row">
          <button class="ghost" @click="loadLogs(true)">重新加载</button>
          <button class="ghost" @click="clearLogView">清空视图</button>
          <button class="ghost" @click="copyLogs">复制全部</button>
          <label class="chk"><input type="checkbox" v-model="logAutoScroll" /> 自动滚动</label>
          <label class="chk"><input type="checkbox" v-model="logPaused" /> 暂停</label>
          <input class="filter" v-model="logFilter" placeholder="过滤…" />
        </div>
        <div class="log-path">{{ logPath || 'backend.log' }} · 已显示 {{ filteredLogs.length }}/{{ logTotal }} 行</div>
        <div class="log-view" ref="logBoxRef">
          <div v-if="!filteredLogs.length" class="log-empty">暂无日志</div>
          <div v-for="(l, i) in filteredLogs" :key="i" class="log-row">{{ l }}</div>
        </div>
      </div>

      <!-- ============ 模型服务 ============ -->
      <div v-show="tab === 'model'" class="body">
        <div class="field">
          <label>API URL（对话端点）</label>
          <input v-model="mApiUrl" placeholder="http://127.0.0.1:1234/v1/chat/completions" />
          <p class="hint">必须是 <code>.../chat/completions</code>。若填成了 <code>/models</code> 列表端点，点「修正端点」。</p>
          <div class="btn-row">
            <button class="ghost" v-for="p in ENDPOINT_PRESETS" :key="p.label" :title="p.hint"
              @click="mApiUrl = p.url">{{ p.label }}</button>
            <button class="ghost" @click="fixEndpoint">修正端点</button>
          </div>
        </div>

        <div class="field">
          <label>API Key</label>
          <input v-model="mApiKey" type="password" placeholder="可选，本地服务一般留空；仅存内存不落盘" />
        </div>

        <div class="btn-row">
          <button class="primary" :disabled="!!busy || !mApiUrl" @click="probeModels">
            {{ busy === 'probe' ? '探测中…' : '拉取模型列表' }}
          </button>
          <span v-if="modelsEndpoint" class="hint inline">来自 {{ modelsEndpoint }}</span>
        </div>
        <p v-if="modelsErr" class="hint err">探测失败：{{ modelsErr }}</p>

        <div class="field" v-if="models.length">
          <label>模型名</label>
          <select v-model="mModel">
            <option v-for="mname in models" :key="mname" :value="mname">{{ mname }}</option>
          </select>
        </div>
        <div class="field" v-else>
          <label>模型名</label>
          <input v-model="mModel" placeholder="如 qwen3-8b / gpt-4o" />
          <p class="hint">拉取列表后可下拉选择，避免手打模型名出错。</p>
        </div>

        <div class="field">
          <label>代理</label>
          <input v-model="mProxy" placeholder="可选，如 http://127.0.0.1:7890" />
          <label class="chk"><input type="checkbox" v-model="mSslVerify" /> 校验 HTTPS 证书</label>
        </div>

        <div class="btn-row">
          <button :disabled="!!busy || !mModel" @click="runLlmCheck">
            {{ busy === 'check' ? '测试中…' : '测试连通性' }}
          </button>
          <button class="primary" @click="applyModel">应用到运行配置</button>
        </div>
        <p v-if="llmCheck" class="hint" :class="llmCheck.ok ? 'ok' : 'err'">
          <template v-if="llmCheck.ok">连通正常 · {{ llmCheck.elapsed_ms }}ms · 回包「{{ llmCheck.reply }}」</template>
          <template v-else>失败 · {{ llmCheck.error }}</template>
        </p>
      </div>

      <!-- ============ 一键诊断 ============ -->
      <div v-show="tab === 'diagnose'" class="body">
        <div class="btn-row">
          <button class="primary" :disabled="diagnosing" @click="runDiagnose">
            {{ diagnosing ? '诊断中…' : '开始诊断' }}
          </button>
          <span class="hint inline">逐项检查端口、SSE、日志与模型端点</span>
        </div>
        <div v-if="!diag.length && !diagnosing" class="log-empty">点「开始诊断」查看逐项结果</div>
        <table v-else class="kv diag">
          <tbody>
            <tr v-for="(d, i) in diag" :key="i">
              <th><span class="mini-dot" :class="d.ok ? 'ok' : 'err'"></span>{{ d.name }}</th>
              <td :class="d.ok ? 'ok-txt' : 'err-txt'">{{ d.detail }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<style scoped>
.modal-mask {
  position: fixed; inset: 0; background: rgba(0,0,0,0.5);
  display: flex; align-items: center; justify-content: center; z-index: 200;
}
.modal {
  background: #fff; border-radius: 8px; padding: 0;
  width: 760px; max-width: 94vw; max-height: 88vh;
  display: flex; flex-direction: column; overflow: hidden;
}
.head {
  display: flex; justify-content: space-between; align-items: center;
  padding: 14px 20px; border-bottom: 1px solid #e2e8f0;
}
.head h3 { margin: 0; font-size: 15px; }
.x { background: transparent; border: none; font-size: 20px; cursor: pointer; color: #718096; line-height: 1; }
.tabs { display: flex; gap: 2px; padding: 8px 20px 0; border-bottom: 1px solid #e2e8f0; }
.tabs button {
  padding: 7px 16px; border: none; background: transparent; cursor: pointer;
  font-size: 13px; color: #718096; border-bottom: 2px solid transparent;
}
.tabs button.on { color: #3182ce; border-bottom-color: #3182ce; font-weight: 600; }
.body { padding: 16px 20px 20px; overflow-y: auto; flex: 1; }

.notice { margin: 12px 20px 0; padding: 7px 12px; border-radius: 5px; font-size: 12px; }
.notice.ok { background: #c6f6d5; color: #22543d; }
.notice.err { background: #fed7d7; color: #822727; }
.notice.info { background: #bee3f8; color: #2c5282; }

.status-card {
  display: flex; align-items: center; gap: 12px;
  padding: 12px 14px; border-radius: 7px; margin-bottom: 14px; border: 1px solid;
}
.status-card.ok { background: #f0fff4; border-color: #9ae6b4; }
.status-card.bad { background: #fff5f5; border-color: #feb2b2; }
.big-dot { width: 12px; height: 12px; border-radius: 50%; flex: 0 0 auto; }
.status-card.ok .big-dot { background: #38a169; }
.status-card.bad .big-dot { background: #e53e3e; }
.status-title { font-size: 14px; font-weight: 600; color: #2d3748; }
.status-sub { font-size: 12px; color: #4a5568; margin-top: 2px; }

.btn-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 12px; }
.btn-row .spacer { flex: 1; }
.btn-row button {
  padding: 6px 14px; border: 1px solid #d1d5db; background: #fff;
  border-radius: 5px; cursor: pointer; font-size: 13px;
}
.btn-row button:hover:not(:disabled) { background: #f0f4f8; }
.btn-row button:disabled { opacity: 0.45; cursor: not-allowed; }
.btn-row button.primary { background: #3182ce; border-color: #3182ce; color: #fff; }
.btn-row button.primary:hover:not(:disabled) { background: #2b6cb0; }
.btn-row button.danger { color: #c53030; border-color: #feb2b2; }
.btn-row button.ghost { border-color: #cbd5e0; color: #4a5568; }

.block { border: 1px solid #e2e8f0; border-radius: 6px; padding: 8px 12px; }
.block summary { cursor: pointer; font-size: 13px; color: #4a5568; }

.kv { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 12px; }
.kv th {
  text-align: left; font-weight: 500; color: #718096; width: 96px;
  vertical-align: top; padding: 3px 8px 3px 0; white-space: nowrap;
}
.kv td { color: #2d3748; word-break: break-all; font-family: Consolas, monospace; padding: 3px 0; }
.kv .ok-txt { color: #22543d; }
.kv .err-txt { color: #822727; }
.diag th { width: 120px; }
.mini-dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 6px; }
.mini-dot.ok { background: #38a169; }
.mini-dot.err { background: #e53e3e; }

.field { margin-bottom: 12px; }
.field label { display: block; font-size: 13px; color: #4a5568; margin-bottom: 4px; }
.field input, .field select {
  width: 100%; padding: 6px 10px; border: 1px solid #d1d5db;
  border-radius: 5px; font-size: 13px;
}
.field .chk { display: inline-flex; align-items: center; gap: 5px; margin-top: 6px; font-size: 12px; }
.field .chk input { width: auto; }
.filter { flex: 1; min-width: 120px; padding: 5px 10px; border: 1px solid #d1d5db; border-radius: 5px; font-size: 12px; }
.chk { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; color: #4a5568; }
.chk input { width: auto; }

.hint { font-size: 12px; color: #718096; margin: 4px 0 8px; }
.hint.inline { margin: 0; }
.hint.err { color: #c53030; }
.hint.ok { color: #2f855a; }

.log-path { font-size: 11px; color: #718096; margin-bottom: 4px; font-family: Consolas, monospace; }
.log-view {
  height: 380px; overflow-y: auto; background: #1a202c; color: #e2e8f0;
  border-radius: 6px; padding: 10px 12px; font-family: Consolas, monospace; font-size: 12px; line-height: 1.6;
}
.log-row { white-space: pre-wrap; word-break: break-all; }
.log-empty { color: #718096; font-size: 12px; padding: 8px 0; }

html.dark .modal { background: #1f2733; }
html.dark .head, html.dark .tabs { border-color: #4a5568; }
html.dark .tabs button { color: #a0aec0; }
html.dark .tabs button.on { color: #63b3ed; border-bottom-color: #63b3ed; }
html.dark .status-card.ok { background: #1d2b21; border-color: #2f855a; }
html.dark .status-card.bad { background: #2d1d1d; border-color: #9b2c2c; }
html.dark .status-title, html.dark .kv td, html.dark .block summary { color: #e2e8f0; }
html.dark .status-sub, html.dark .kv th, html.dark .hint, html.dark .field label, html.dark .chk { color: #a0aec0; }
html.dark .btn-row button, html.dark .field input, html.dark .field select, html.dark .filter {
  background: #2d3748; color: #e2e8f0; border-color: #4a5568;
}
html.dark .block { border-color: #4a5568; }
html.dark .x { color: #a0aec0; }
</style>