import { invoke, isTauri } from '@tauri-apps/api/core';

export type PortResult =
  | { status: 'Ready'; data: number }
  | { status: 'NotReady' }
  | { status: 'DevMode'; data: number }
  | { status: 'Error'; data: string };

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
    await new Promise(r => setTimeout(r, 500));
  }
  throw new Error('后端启动超时，请查看 %APPDATA%/TextForge/logs/backend.log');
}
