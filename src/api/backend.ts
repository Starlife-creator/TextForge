import { invoke, isTauri } from '@tauri-apps/api/core';

export type PortResult =
  | { status: 'Ready'; data: number }
  | { status: 'NotReady' }
  | { status: 'DevMode'; data: number }
  | { status: 'Error'; data: string };

/** Rust 侧 backend_info 命令的返回值，镜像 src-tauri/src/backend.rs:BackendInfo */
export interface BackendInfo {
  port: number;
  pid: number;
  /** 对端口做 TCP 探测的结果：port.json 存在不等于进程还活着 */
  reachable: boolean;
  /** sidecar 子进程句柄是否还在（release 模式才有意义） */
  child_alive: boolean;
  debug: boolean;
  port_file: string;
  app_dir: string;
  log_path: string;
  log_exists: boolean;
}

export async function waitForPort(): Promise<number> {
  if (!isTauri()) {
    const p = parseInt(import.meta.env.VITE_BACKEND_PORT || '8000');
    if (isNaN(p)) throw new Error('VITE_BACKEND_PORT 未设置');
    return p;
  }
  for (let i = 0; i < 60; i++) {
    const result = await invoke<PortResult>('get_backend_port');
    if (result.status === 'Ready' || result.status === 'DevMode') return result.data;
    if (result.status === 'Error') throw new Error(result.data);
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error('后端启动超时，请打开「⚙ 系统与后端设置」查看日志');
}

/** 拉取后端运行时快照；非 Tauri 环境（浏览器 dev）返回 null。 */
export async function fetchBackendInfo(): Promise<BackendInfo | null> {
  if (!isTauri()) return null;
  try {
    return await invoke<BackendInfo>('backend_info');
  } catch {
    return null;
  }
}

/** 重启后端（release）。开发模式由 Rust 侧返回明确的操作提示。 */
export async function invokeRestartBackend(): Promise<string> {
  if (!isTauri())
    throw new Error('浏览器开发模式无法重启后端，请在终端手动执行 python backend/main.py');
  return invoke<string>('restart_backend');
}
