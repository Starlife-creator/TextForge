/**
 * 后端 API 统一入口：负责附带调用方 token。
 *
 * 后端（release 模式）要求每个请求都带 `X-TextForge-Token`，token 由 Rust 外壳
 * 用 CSPRNG 生成、只存在进程内存里，经 Tauri IPC 交给前端，从不落盘。
 * 目的：挡住「任意本地进程读一下 data/port.json 就能调 /api/start 以你的权限
 * 读写任意路径」这一攻击面。
 *
 * 为什么统一走 apiFetch 而不是各处手写 headers：调用点有 20 多处，漏一处就是
 * 一个静默失效的功能（而且 401 的表现往往是界面空数据，很难联想到鉴权）。
 *
 * 开发/测试模式下后端不启用鉴权（未注入 token 即放行），此时这里不发该头，
 * 请求依然通过。
 */

import { invoke, isTauri } from '@tauri-apps/api/core';

export const TOKEN_HEADER = 'X-TextForge-Token';

let authToken: string | null = null;

export function setAuthToken(token: string | null) {
  authToken = token && token.length > 0 ? token : null;
}

export function getAuthToken(): string | null {
  return authToken;
}

export function authHeaders(): Record<string, string> {
  return authToken ? { [TOKEN_HEADER]: authToken } : {};
}

/** 启动时向外壳索取 token。必须在任何 API 调用之前完成。 */
export async function initAuth(): Promise<void> {
  if (!isTauri()) return; // 浏览器 dev：后端未启用鉴权
  try {
    setAuthToken(await invoke<string>('get_backend_token'));
  } catch (e) {
    // 老版本外壳没有该命令：降级为无 token，后端未启用鉴权时仍可工作。
    console.warn('未能取得后端访问令牌，降级为无鉴权模式', e);
  }
}

/** 带 token 的 fetch。所有对后端的请求都应走这个。 */
export function apiFetch(input: string, init: RequestInit = {}): Promise<Response> {
  const headers: Record<string, string> = {
    ...(init.headers as Record<string, string> | undefined),
    ...authHeaders(),
  };
  return fetch(input, { ...init, headers });
}

export interface BusyRetryOptions {
  /** 总共允许几次尝试（含第一次），默认 2。 */
  tries?: number;
  /** 两次尝试之间的等待毫秒数，默认 800。 */
  delayMs?: number;
  /** 每次因 503 重发前回调，界面用它把「正在自动重试」说给用户。 */
  onBusy?: (attempt: number) => void;
}

/**
 * POST 一个控制端点，并替用户吃掉 503「文件正被占用」。
 *
 * 后端写 progress.json 时，Windows 上文件可能正被杀软/Word 占着，退避重试仍失败
 * 就返回 503（backend/routes/control.py 的 _save_progress_guarded）。过去界面只说
 * 「验收失败：文件正被占用，请稍后重试」，用户必须自己再点一次——而占用通常几百
 * 毫秒就过去。这里把那一次点击补上：默认再多试一次、间隔 800ms。
 * 其他状态码原样返回，仍由调用方的 checkRes 处理（不能把 4xx 当占用无限重发）。
 */
export async function postWithBusyRetry(
  url: string,
  body: unknown,
  opts: BusyRetryOptions = {},
): Promise<Response> {
  const tries = opts.tries ?? 2;
  const delayMs = opts.delayMs ?? 800;
  const init: RequestInit = {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  };
  let res = await apiFetch(url, init);
  for (let attempt = 1; res.status === 503 && attempt < tries; attempt++) {
    opts.onBusy?.(attempt + 1);
    await new Promise((r) => setTimeout(r, delayMs));
    res = await apiFetch(url, init);
  }
  return res;
}
