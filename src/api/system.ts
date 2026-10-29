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

// ---------- 成品质量报告（_diagnostics/quality_report.json） ----------

export type QualityStatus = 'good' | 'warn' | 'bad';
export type QualitySeverity = 'bad' | 'warn';

export interface QualityIssue {
  severity: QualitySeverity;
  code: string;
  chapter_id: string | null;
  message: string;
  evidence: string;
}

export interface QualityChapterRow {
  id: string;
  title: string;
  original_chars: number;
  reconstructed_chars: number;
  ratio: number | null;
  status: 'ok' | 'warn' | 'bad' | 'skipped';
  issue_codes: string[];
}

export interface QualityDuplicate {
  a: string;
  b: string;
  shared_shingles: number;
  containment: number;
}

export interface StitchAnchorMetric {
  file: string | null;
  source: string | null;
  chars: number | null;
  change_rate: number;
}

/** 缝合观测：补丁覆盖了多少字、原文句子被改掉了多少。口径见 config.stitch_metric */
export interface StitchMetrics {
  applied_patches: number;
  measured: number;
  exposed_chars: number;
  mean: number | null;
  p90: number | null;
  max: number | null;
  worst: StitchAnchorMetric[];
}

export interface QualityReport {
  schema: number;
  generated_at: string;
  status: QualityStatus;
  headline: string;
  summary: {
    chapters: number;
    chapters_reconstructed: number;
    chapters_skipped_empty: number;
    chapters_with_issues: number;
    total_original_chars: number;
    total_reconstructed_chars: number;
    length_ratio: number | null;
    duplicate_pairs: number;
    empty_chapters: number;
    residual_markers: number;
    placeholder_hits: number;
    forbidden_hits: number;
    old_name_hits: number;
    auto_rewrites?: number;
    pre_stitch_issues?: number;
    stitch_failures?: number;
    stitch_patches?: number;
    stitch_measured?: number;
    stitch_exposed_chars?: number;
    stitch_exposed_ratio?: number | null;
    stitch_mean_rate?: number | null;
    stitch_p90_rate?: number | null;
    stitch_max_rate?: number | null;
    issues_by_severity: Record<QualitySeverity, number>;
  };
  issues: QualityIssue[];
  issues_truncated: boolean;
  chapters: QualityChapterRow[];
  duplicates: QualityDuplicate[];
  stitch?: StitchMetrics;
  config: {
    novel_name?: string | null;
    model?: string | null;
    context_window?: number | null;
    input_mode?: string | null;
    input_format?: string | null;
    refactor_mode?: string | null;
    refactor_gates?: Record<string, boolean>;
    prompt_version?: string | null;
    forbidden_canon_count?: number;
    name_map_count?: number;
    forbidden_check?: string;
    stitch_metric?: string;
    /** 当时生效的缝合护栏（窗口封顶值 + 最小改动闸门开/关），由后端拼好整句 */
    stitch_gate?: string;
  };
  usage: { prompt_tokens?: number | string; completion_tokens?: number | string };
  caveat: string;
}

export function fetchQualityReport(port: number, outputPath: string) {
  const q = new URLSearchParams({ output_path: outputPath });
  return req<QualityReport>(port, `/api/output/quality?${q}`);
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

export interface ComparisonEntry {
  name: string;
  size: number;
}

/** R8：对照面板要发**两次**请求。
 *
 * `/api/comparisons` 不带 `name` 时只返回文件名与大小（整目录正文一次返回会让弹窗
 * 卡住，按需加载是既有设计）；正文必须带 `name` 再取。此前前端把列表返回值当成
 * 带正文的列表直接用，`it.text` 永远是 undefined ⇒ 面板永远空白。
 */
export async function fetchComparisonList(
  port: number,
  outputPath: string,
): Promise<ComparisonEntry[]> {
  const q = new URLSearchParams({ output_path: outputPath });
  const d = await req<{ items: ComparisonEntry[] }>(port, `/api/comparisons?${q}`);
  return d.items ?? [];
}

export async function fetchComparisonText(
  port: number,
  outputPath: string,
  name: string,
): Promise<string> {
  const q = new URLSearchParams({ output_path: outputPath, name });
  const d = await req<{ items: { name: string; text: string }[] }>(port, `/api/comparisons?${q}`);
  return d.items?.[0]?.text ?? '';
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

// ---------- 开跑前预估与费用换算 ----------

export interface EstimateResult {
  chapters: number;
  batches: number;
  chapters_per_batch: number[];
  body_token_limit: number;
  context_window: number;
  max_chapters_per_batch: number;
  safety: number;
  calls: { diagnose: number; blueprint: number; refactor: number; stitch: number; total: number };
  tokens: {
    prompt: number;
    completion: number;
    total: number;
    by_stage: Record<string, { prompt: number; completion: number }>;
  };
  scaffold_per_request: number;
  scaffold_share: number;
  stitch_boundaries: number;
  stitch_window_chars: number;
  stitch_exposed_chars: number;
  stitch_exposed_ratio: number;
  notes: string[];
}

/** 用与 /api/start 完全相同的请求体预估（纯本地读文件，不调模型）。 */
export function estimateRun(port: number, body: unknown) {
  return req<EstimateResult>(port, '/api/estimate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export interface UnitPrice {
  /** 每百万输入 token 的价格（元） */
  input: number;
  /** 每百万输出 token 的价格（元） */
  output: number;
}

/**
 * 按单价折算费用。两个单价都是 0（或非法）时返回 null，表示「本地模型，不计费」——
 * 界面据此显示说明而不是 ¥0.00，避免把「没填」渲染成「免费」。
 */
export function costOf(
  promptTokens: number | string,
  completionTokens: number | string,
  price: UnitPrice,
): number | null {
  const p = Number(price.input);
  const o = Number(price.output);
  if (!(p > 0) && !(o > 0)) return null;
  const tin = Math.max(0, Number(promptTokens) || 0) / 1e6;
  const tout = Math.max(0, Number(completionTokens) || 0) / 1e6;
  return tin * (p > 0 ? p : 0) + tout * (o > 0 ? o : 0);
}

export function formatCost(value: number | null): string {
  if (value === null) return '本地模型（未计单价）';
  if (value < 0.01) return `¥${value.toFixed(4)}`;
  return `¥${value.toFixed(2)}`;
}
