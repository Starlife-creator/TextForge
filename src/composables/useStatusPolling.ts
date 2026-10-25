/**
 * #11 状态轮询 composable（骨架，尚未接入 App.vue）。
 *
 * 迁移口径：`src/App.vue` 的 `fetchStatus` / `statusTimer` 仍是现行实现；本文件先把
 * 「定时拉取 `/api/status`、暴露最新状态」的纯逻辑抽成可测单元，待 npm 环境下分批
 * 替换 App.vue 内联逻辑。未接入前不要在组件里调用本文件。
 *
 * 本轮缺 npm 构建/组件测试环境，未执行 `npm run build` / `vitest`（#11 ⬜）。
 */

import { ref, onUnmounted, type Ref } from 'vue';
import { apiFetch } from '../api/client';

/** `/api/status` 响应中前端实际消费的字段（与 App.vue 现行读取口径对齐）。 */
export interface StatusPayload {
  paused?: boolean;
  pipeline_running?: boolean;
  completed?: boolean;
  last_heartbeat?: string;
  usage?: { prompt_tokens?: number; completion_tokens?: number };
  summaries?: unknown;
  total_chapters?: number;
  batches?: { total?: number; phase1_done?: number; phase3_done?: number };
  total_logic_chapters?: number;
  reconstructed_chapters?: number;
  chapters_summary?: unknown[];
  current_phase?: string;
  blueprint?: string;
  error_logs?: { message: string }[];
  resume?: Record<string, unknown> | null;
  active_gate?: string | null;
  waiting_seconds?: number;
  config_drift?: string[];
  [key: string]: unknown;
}

export function useStatusPolling(port: Ref<number | null>) {
  const status = ref<StatusPayload | null>(null);
  const lastError = ref<string | null>(null);
  let timer: number | null = null;

  async function fetchStatus(): Promise<boolean> {
    if (!port.value) return false;
    try {
      const res = await apiFetch(`http://127.0.0.1:${port.value}/api/status`);
      if (!res.ok) return false;
      status.value = (await res.json()) as StatusPayload;
      lastError.value = null;
      return true;
    } catch (e) {
      lastError.value = e instanceof Error ? e.message : String(e);
      return false;
    }
  }

  function start(intervalMs = 5000): void {
    stop();
    void fetchStatus();
    timer = window.setInterval(fetchStatus, intervalMs);
  }

  function stop(): void {
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  }

  onUnmounted(stop);

  return { status, lastError, fetchStatus, start, stop };
}
