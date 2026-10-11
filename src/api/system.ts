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
}

async function call<T>(port: number, path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`http://127.0.0.1:${port}${path}`, init);
  const text = await res.text();
  let body: any = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = text; }
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
  const q = new URLSearchParams({ api_url: apiUrl, api_key: apiKey });
  const res = await fetch(`http://127.0.0.1:${port}/api/system/models?${q}`);
  const body = await res.json().catch(() => null);
  if (!res.ok || !body) throw new Error(`模型探测失败：HTTP ${res.status}`);
  return body as ModelListResult;
}

export function checkLlm(port: number, payload: {
  api_url: string; model: string; api_key?: string; proxy?: string; ssl_verify?: boolean;
}) {
  return call<LlmCheckResult>(port, '/api/system/llm_check', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}