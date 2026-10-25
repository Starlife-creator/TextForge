/**
 * #11 闸门状态 composable（骨架，尚未接入 App.vue）。
 *
 * 迁移口径：`src/App.vue` 的 `confirmedGates` / `isGateSettled` / `gateKey` 与
 * `s.resume` 恢复弹窗仍是现行实现；本文件把「当前闸门、等待时长、恢复负载、
 * 已确认闩锁」抽成可测单元，待 npm 环境下分批替换。
 *
 * 本轮缺 npm 构建/组件测试环境，未执行 `npm run build` / `vitest`（#11 ⬜）。
 */

import { computed, ref, type Ref } from 'vue';
import type { StatusPayload } from './useStatusPolling';

export interface ResumePayload {
  key: string;
  chapters?: unknown[];
  [key: string]: unknown;
}

export function useGates(status: Ref<StatusPayload | null>) {
  const activeGate = computed(() => status.value?.active_gate ?? null);
  const waitingSeconds = computed(() => status.value?.waiting_seconds ?? 0);
  const resume = computed<ResumePayload | null>(
    () => (status.value?.resume as ResumePayload | null) ?? null,
  );

  /** 已确认闸门闩锁：确认过的闸门在恢复轮询中不再重复弹窗。 */
  const settled = ref<Set<string>>(new Set());

  function settle(key: string): void {
    settled.value.add(key);
  }

  function isSettled(key: string): boolean {
    return settled.value.has(key);
  }

  function reset(): void {
    settled.value.clear();
  }

  return { activeGate, waitingSeconds, resume, settle, isSettled, reset };
}
