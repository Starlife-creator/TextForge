import { apiFetch } from './client';
/**
 * SSE 客户端。
 *
 * 三个必须满足的不变式（缺一个就会退化成「后端连接断开」黑盒）：
 * 1. 心跳保活 —— 静默看门狗必须显著大于后端心跳间隔(15s)，否则误杀正常连接。
 * 2. done 也要重连 —— 服务端正常收尾后不重连，流就永久静默死掉且不报错。
 * 3. 主动断开与故障断开要区分 —— disconnectSSE() 不应触发错误提示，
 *    但看门狗 abort 必须触发重连。
 */

type Status = {
  connected: boolean;
  /** 已连续失败次数；0 表示刚连上 */
  attempt: number;
  detail?: string;
  /** 是否正在等待下一次重连 */
  retrying: boolean;
};

export interface SSEHandlers {
  onEvent: (event: string, payload: any) => void;
  onStatus?: (s: Status) => void;
}

/** 静默看门狗（毫秒）。必须 > 后端 HEARTBEAT_SEC(15s)，留足抖动余量。 */
const IDLE_TIMEOUT_MS = 90_000;
const IDLE_CHECK_MS = 10_000;
/** 指数退避重连序列（秒），用尽后固定间隔重试 —— 后端重启后应自愈，而不是永久放弃。 */
const BACKOFF_SEC = [1, 2, 4, 8, 16];
const BACKOFF_FLOOR_SEC = 5;

let abortController: AbortController | null = null;
let retryTimer: number | null = null;
let lastEventTime = Date.now();
/** 连接代次：每次新建连接 +1，用于让上一代的回调自动失效 */
let generation = 0;

export async function connectSSE(port: number, handlers: SSEHandlers, attempt = 0) {
  clearRetryTimer();
  const gen = ++generation;
  abortController?.abort();
  const ctrl = new AbortController();
  abortController = ctrl;

  let timeoutCheck: number | null = null;
  let watchdogFired = false;

  try {
    const response = await apiFetch(`http://127.0.0.1:${port}/api/events`, {
      signal: ctrl.signal,
      headers: { 'Accept': 'text/event-stream' },
    });
    if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);
    const reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';
    lastEventTime = Date.now();
    handlers.onStatus?.({ connected: true, attempt: 0, retrying: false });

    timeoutCheck = window.setInterval(() => {
      if (Date.now() - lastEventTime > IDLE_TIMEOUT_MS) {
        watchdogFired = true;
        ctrl.abort(); // 走 catch → 重连
      }
    }, IDLE_CHECK_MS);

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      // 后端心跳（`: heartbeat` / `: waiting`）也会走到这里，能刷新 lastEventTime
      lastEventTime = Date.now();
      buffer += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const event = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        parseSSEEvent(event, handlers);
      }
    }
    throw new Error('服务端关闭了事件流');
  } catch (e: any) {
    if (timeoutCheck !== null) { clearInterval(timeoutCheck); timeoutCheck = null; }
    if (gen !== generation) return;               // 已被新连接取代
    const aborted = e?.name === 'AbortError';
    if (aborted && !watchdogFired) return;          // disconnectSSE() 主动断开，不算故障
    scheduleReconnect(port, handlers, attempt + 1, e?.message, gen);
  }
}

function scheduleReconnect(port: number, handlers: SSEHandlers, attempt: number, detail: string | undefined, gen: number) {
  if (gen !== generation) return;
  const delay = (BACKOFF_SEC[attempt - 1] ?? BACKOFF_FLOOR_SEC) * 1000;
  handlers.onStatus?.({ connected: false, attempt, detail, retrying: true });
  clearRetryTimer();
  retryTimer = window.setTimeout(() => {
    retryTimer = null;
    if (gen !== generation) return;
    connectSSE(port, handlers, attempt);
  }, delay);
}

function clearRetryTimer() {
  if (retryTimer !== null) { clearTimeout(retryTimer); retryTimer = null; }
}

/** 主动断开并停止重连（流水线结束、组件卸载时调用）。 */
export function disconnectSSE() {
  generation++;              // 让在途连接的回调失效
  clearRetryTimer();
  abortController?.abort();
  abortController = null;
}

function parseSSEEvent(raw: string, handlers: SSEHandlers) {
  const lines = raw.split('\n');
  let eventType = 'message';
  const dataLines: string[] = [];
  for (const line of lines) {
    if (line.startsWith('event:')) eventType = line.slice(6).trim();
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''));
  }
  if (dataLines.length === 0) return; // 纯心跳注释行
  try { handlers.onEvent(eventType, JSON.parse(dataLines.join('\n'))); }
  catch (e) { console.error('SSE 解析失败:', dataLines); }
}