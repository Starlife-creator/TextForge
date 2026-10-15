import { apiFetch } from './client';
/** 后端 /api/system/* 的前端封装（对应 backend/routes/system.py） */

export interface SystemInfo {
  version: string;
  pid: number;
  port: number;
  started_at: number;
  uptime_sec: number;
  python: string;
  executable: string;
  cwd: string;
  platform: string;
  debug: boolean;
  app_dir: string;
  port_file: string;
  port_file_exists: boolean;
  log_path: string;
  log_exists: boolean;
  log_size: number;
  log_mtime: number;
  pipeline_running: boolean;
  paused: boolean;
  run_id: string | null;
  output_path: string | null;
  subscribers: Record<string, number>;
}

export interface LogTail {
  path: string;
  lines: string[];
  total: number;
  offset: number;
  size?: number;
  exists: boolean;
}

export interface ModelListResult {
  ok: boolean;
  endpoint: string | null;
  models: string[];
  tried: string[];
  errors?: string[];
}

export interface LlmCheckResult {
  ok: boolean;
  error?: string;
  elapsed_ms: number;
  reply?: string;
  model?: string;
  /** 后端探测到的服务端真实上下文窗口（token）；探不到为 null */
  context_limit?: number | null;
  /** 探测来源：lmstudio / llama.cpp / ollama / openai / overflow */
  context_source?: string | null;
}

async function call<T>(port: number, path: string, init?: RequestInit): Promise<T> {
  const res = await apiFetch(`http://127.0.0.1:${port}${path}`, init);
  const text = await res.text();
  let body: any = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  if (!res.ok) {
    const detail = body?.detail ?? body ?? res.status;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return body as T;
}

export function getSystemInfo(port: number) {
  return call<SystemInfo>(port, '/api/system/info');
}

/** 增量拉取日志；offset 为已消费行数。 */
export function getLogs(port: number, offset = 0, lines = 300) {
  return call<LogTail>(port, `/api/system/logs?lines=${lines}&offset=${offset}`);
}

export function openPath(port: number, target: 'app_dir' | 'logs_dir' | 'log_file') {
  return call<{ status: string; path: string }>(port, '/api/system/open', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ target }),
  });
}

export function shutdownBackend(port: number, force: boolean) {
  return call<{ status: string }>(port, '/api/system/shutdown', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ force }),
  });
}

export async function fetchModels(port: number, apiUrl: string, apiKey = '') {
  // api_key 走 POST body，不进 URL：query 会落进访问日志/浏览器历史/代理，
  // 等于把密钥暴露在明信片上。
  const res = await apiFetch(`http://127.0.0.1:${port}/api/system/models`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ api_url: apiUrl, api_key: apiKey }),
  });
  const body = await res.json().catch(() => null);
  if (!res.ok || !body) throw new Error(`模型探测失败：HTTP ${res.status}`);
  return body as ModelListResult;
}

export function checkLlm(
  port: number,
  payload: {
    api_url: string;
    model: string;
    api_key?: string;
    proxy?: string;
    ssl_verify?: boolean;
  },
) {
  return call<LlmCheckResult>(port, '/api/system/llm_check', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export interface ContextProbeResult {
  ok: boolean;
  /** 探测到的真实窗口（token）；探不到为 null */
  detected: number | null;
  /** 探测来源：lmstudio / llama.cpp / ollama / openai / overflow */
  source?: string | null;
  configured?: number | null;
  /** 配置值大于实测值时的可读提示，否则 null */
  mismatch?: string | null;
}

// ---------- 任务历史 / 成品版本 ----------

export interface HistoryItem {
  output_path: string;
  novel_name?: string | null;
  input_path?: string | null;
  input_mode?: string | null;
  current_phase?: string | null;
  total_chapters?: number;
  batches_done?: number;
  batches_total?: number;
  finished?: boolean;
  finished_at?: string | null;
  updated_at?: string;
  last_error?: string;
  config_drift?: string[];
  is_current?: boolean;
  running?: boolean;
}

// 与上面的 call 行为一致（文本→JSON、规范化 detail）。保留 req 作为语义别名，
// 避免两处解析逻辑各自漂移 —— 此前两者的错误分支就有细微差异。
function req<T>(port: number, path: string, init?: RequestInit): Promise<T> {
  return call<T>(port, path, init);
}

export function fetchHistory(port: number) {
  return req<{ items: HistoryItem[] }>(port, '/api/history');
}

export function forgetHistory(port: number, outputPath: string) {
  return req<{ status: string }>(port, '/api/history/forget', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ output_path: outputPath }),
  });
}

export interface VersionInfo {
  id: string;
  mtime: number;
  files: number;
  size: number;
  names: string[];
}

export function fetchVersions(port: number, outputPath: string) {
  const q = new URLSearchParams({ output_path: outputPath });
  return req<{ versions: VersionInfo[] }>(port, `/api/versions?${q}`);
}

export function restoreVersion(port: number, outputPath: string, versionId: string) {
  return req<{ restored: string; snapshot_before_restore: string | null }>(
    port,
    '/api/versions/restore',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ output_path: outputPath, version_id: versionId }),
    },
  );
}

export function clearOutput(port: number, outputPath: string) {
  return req<{ status: string; removed: string[] }>(port, '/api/output/clear', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ output_path: outputPath }),
  });
}

export function inspectOutput(port: number, outputPath: string) {
  const q = new URLSearchParams({ output_path: outputPath });
  return req<any>(port, `/api/output/inspect?${q}`);
}

/** 探测服务端真实上下文窗口，并与用户配置值对比（仅自检，不影响能否启动）。 */
export async function probeContext(
  port: number,
  apiUrl: string,
  model: string,
  configured: number,
  apiKey = '',
) {
  // api_key 走 body，理由同 fetchModels。
  const res = await apiFetch(`http://127.0.0.1:${port}/api/system/context_probe`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ api_url: apiUrl, model, api_key: apiKey, configured }),
  });
  const body = await res.json().catch(() => null);
  if (!res.ok || !body) throw new Error(`上下文探测失败：HTTP ${res.status}`);
  return body as ContextProbeResult;
}
